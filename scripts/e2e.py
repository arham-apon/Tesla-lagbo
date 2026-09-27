"""End-to-end check of the Banani story against the running system (plan 8.4).

    docker compose up --build -d          # then:
    .venv\\Scripts\\python scripts\\e2e.py   (Windows)   |   .venv/bin/python scripts/e2e.py

The plan's scripts/e2e.sh needs curl + jq + uuidgen, and websocat for the live channel, and only PRINTS results for
a person to compare. This does the same story, in the same order, with the project's own libraries (httpx,
websockets), and CHECKS every expected value: exit code 0 = the story worked. It also runs the plan's "live channel"
and "post-run" checks (RabbitMQ dead-letter queues, unpublished outbox rows, Bullet's seats).

Run it on a fresh system (`docker compose down -v && docker compose up --build -d`). It tidies up after itself
(Jashim offline, Shirin's leftover request cancelled), so it can also be run again straight away.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
API = os.environ.get("E2E_API", "http://localhost:8000/api/v1")
GATEWAY = API.rsplit("/api/", 1)[0]
WS = os.environ.get("E2E_WS", "ws://localhost:8005/ws")
RABBIT_UI = os.environ.get("E2E_RABBIT_UI", "http://localhost:15672")
PASSWORD = "Pool@1234"
PHONES = {"jashim": "01711000001", "nusrat": "01711000002", "rafiq": "01711000003", "shirin": "01711000004"}
ACTIVE = {"REQUESTED", "MATCHED", "DRIVER_ARRIVED", "STARTED"}
DURABLE_QUEUES = {"matching.fleet-state", "trip.driver-shift", "trip.fare-settled", "fare.ride-lifecycle",
                  "notification.inbox"}  # plan 0.4's queue table


class Story:
    def __init__(self):
        self.passed = self.failed = 0

    def step(self, title: str) -> None:
        print(f"\n{title}")

    def check(self, ok: bool, label: str, got=None) -> bool:
        if ok:
            self.passed += 1
            print(f"   ok    {label}")
        else:
            self.failed += 1
            print(f"   FAIL  {label}" + ("" if got is None else f"   (got: {got})"))
        return ok


def env_file() -> dict:
    out = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def sql(service: str, db_file: str, query: str) -> list:
    """Run a read-only query inside a service's container (the image has Python's sqlite3, not the CLI)."""
    code = f"import sqlite3,json;print(json.dumps(sqlite3.connect('{db_file}').execute({query!r}).fetchall()))"
    out = subprocess.run(["docker", "compose", "exec", "-T", service, "python", "-c", code], cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return json.loads(out.stdout)


async def until(fetch, ok, timeout=10.0, every=0.25):
    """Poll `fetch` until `ok(result)`; events travel through RabbitMQ, so some effects take a moment."""
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        last = await fetch()
        if ok(last):
            return last
        await asyncio.sleep(every)
    return last


async def listen(token: str, inbox: list, stop: asyncio.Event) -> None:
    """The plan's second terminal (websocat), as a background task: collect what a phone would receive."""
    async with websockets.connect(f"{WS}?token={token}") as ws:
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if raw != "pong":
                inbox.append(json.loads(raw))


async def main() -> int:
    s = Story()
    async with httpx.AsyncClient(timeout=15) as http:
        def auth(token: str, key: str | None = None) -> dict:
            h = {"Authorization": f"Bearer {token}"}
            return h | ({"Idempotency-Key": key} if key else {})

        s.step("0. The system is up (gateway /health checks every service)")
        health = await until(lambda: _get_json(http, f"{GATEWAY}/health"), lambda r: r and r[0] == 200, timeout=180,
                             every=2)
        if not s.check(bool(health) and health[0] == 200, "gateway /health is 200", health):
            return 1

        tok = {}
        for name, phone in PHONES.items():
            r = await http.post(f"{API}/auth/login", json={"phone": phone, "password": PASSWORD})
            s.check(r.status_code == 200, f"{name} logs in", r.status_code)
            tok[name] = r.json()["access_token"]
        J, N, R, S = tok["jashim"], tok["nusrat"], tok["rafiq"], tok["shirin"]

        s.step("   Starting state: nobody has an active ride, Bullet has no live pool")
        for name in ("nusrat", "rafiq", "shirin"):
            rides = (await http.get(f"{API}/rides?limit=100", headers=auth(tok[name]))).json()
            active = [x["status"] for x in rides if x["status"] in ACTIVE]
            if not s.check(not active, f"{name} has no active ride", active):
                print("\n   Left over from an interrupted run. Start fresh: docker compose down -v && "
                      "docker compose up --build -d")
                return 1
        s.check((await http.get(f"{API}/driver/pool", headers=auth(J))).status_code == 204, "Jashim has no live pool")
        wallet_before = (await http.get(f"{API}/wallet", headers=auth(N))).json()["balance_poysha"]
        earned_before = (await http.get(f"{API}/driver/earnings", headers=auth(J))).json()

        stop, nusrat_phone, jashim_phone = asyncio.Event(), [], []
        listeners = [asyncio.create_task(listen(N, nusrat_phone, stop)),
                     asyncio.create_task(listen(J, jashim_phone, stop))]
        await asyncio.sleep(0.5)

        s.step("1. Jashim goes online and pings from Banani")
        r = (await http.post(f"{API}/drivers/me/online", headers=auth(J))).json()
        s.check((r["status"], r["vehicle"]["nickname"]) == ("ONLINE", "Bullet"), "ONLINE in Bullet", r)
        r = await http.post(f"{API}/driver/location", headers=auth(J), json={"lat": 23.7937, "lng": 90.4066})
        s.check((r.status_code, r.json()) == (202, {"zone": "BANANI"}), "ping accepted, zone BANANI", r.text)
        await asyncio.sleep(1.5)  # Matching and Trip learn he's online from Identity's event

        s.step("2. Nusrat gets a quote (expect solo 8250, pooled 7200)")
        q = (await http.post(f"{API}/fares/estimate", headers=auth(N),
                             json={"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 1})).json()
        s.check((q["solo_total_poysha"], q["pooled_total_poysha"]) == (8250, 7200), "8250 / 7200", q)

        s.step("3. Nusrat requests (expect REQUESTED)")
        key = str(uuid.uuid4())
        body = {"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 1, "payment_method": "WALLET",
                "quote_id": q["quote_id"]}
        r = await http.post(f"{API}/rides", headers=auth(N, key), json=body)
        nr = r.json()
        s.check((r.status_code, nr["status"]) == (201, "REQUESTED"), "201 REQUESTED", r.text)
        s.step("3b. Same Idempotency-Key replays the same ride (no duplicate)")
        r = await http.post(f"{API}/rides", headers=auth(N, key), json=body)
        s.check(r.headers.get("idempotent-replay") == "true" and r.json()["id"] == nr["id"],
                "Idempotent-Replay: true, same ride id", dict(r.headers))

        s.step("4. Jashim sees the offer and accepts (pool 1/3)")
        offers = await until(lambda: _json(http.get(f"{API}/driver/offers", headers=auth(J))),
                             lambda o: any(x["ride_id"] == nr["id"] for x in o))
        s.check(any(x["ride_id"] == nr["id"] and x["passenger_name"] == "Nusrat" for x in offers),
                "Nusrat's offer is listed", offers)
        p = (await http.post(f"{API}/driver/offers/{nr['id']}/accept", headers=auth(J))).json()
        s.check((p["occupied_seats"], p["max_capacity"]) == (1, 3), "pool 1/3", p)

        s.step("5. Rafiq auto-joins (expect MATCHED, pool 2/3, drop Gulshan 1 before Mohakhali)")
        r = await http.post(f"{API}/rides", headers=auth(R, str(uuid.uuid4())),
                            json={"pickup_zone": "BANANI", "dropoff_zone": "GULSHAN_1", "seats": 1})
        rr = r.json()
        s.check(rr.get("status") == "MATCHED" and (rr.get("driver") or {}).get("vehicle_nickname") == "Bullet",
                "MATCHED, in Bullet", r.text)
        pool = (await http.get(f"{API}/driver/pool", headers=auth(J))).json()
        stops = [f"{w['seq']} {w['kind']} {w['zone']} {w['passenger_name']}" for w in pool["waypoints"]]
        s.check(pool["occupied_seats"] == 2, "pool 2/3", pool["occupied_seats"])
        s.check(stops == ["1 PICKUP BANANI Nusrat", "2 PICKUP BANANI Rafiq", "3 DROPOFF GULSHAN_1 Rafiq",
                          "4 DROPOFF MOHAKHALI Nusrat"], "stops: B, B, Gulshan 1, Mohakhali", stops)

        s.step("5b. Bullet moves: Jashim pings again (Nusrat's map should show it)")
        await asyncio.sleep(1)  # Matching learns Bullet has a pool from trip.pool.updated
        await http.post(f"{API}/driver/location", headers=auth(J), json={"lat": 23.7900, "lng": 90.4080})

        s.step("6. Shirin wants 2 seats; only 1 left (expect REQUESTED, not in Bullet)")
        r = await http.post(f"{API}/rides", headers=auth(S, str(uuid.uuid4())),
                            json={"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 2})
        sr = r.json()
        s.check((sr.get("status"), sr.get("pool_id")) == ("REQUESTED", None), "REQUESTED, no pool", r.text)

        s.step("7. Rafiq cannot read Nusrat's ride (expect 404)")
        r = await http.get(f"{API}/rides/{nr['id']}", headers=auth(R))
        s.check(r.status_code == 404, "404", r.status_code)

        s.step("8. Run the pool")
        for rid in (nr["id"], rr["id"]):
            for action in ("arrive", "start"):
                r = await http.post(f"{API}/driver/rides/{rid}/{action}", headers=auth(J))
                s.check(r.status_code == 200, f"{action} {'Nusrat' if rid == nr['id'] else 'Rafiq'}", r.text)

        s.step("9. Nusrat tries to cancel after start (expect 409 INVALID_TRANSITION)")
        r = await http.post(f"{API}/rides/{nr['id']}/cancel", headers=auth(N), json={})
        s.check((r.status_code, r.json()["error"]["code"]) == (409, "INVALID_TRANSITION"), "409", r.text)

        r = await http.post(f"{API}/driver/rides/{rr['id']}/complete", headers=auth(J))
        s.check(r.status_code == 200, "Rafiq dropped at Gulshan 1", r.text)
        r = await http.post(f"{API}/driver/rides/{nr['id']}/complete", headers=auth(J))
        s.check(r.status_code == 200 and r.json()["status"] == "COMPLETED", "Nusrat dropped; pool COMPLETED", r.text)

        s.step("10. Fares (expect Nusrat 7200 PAID, Rafiq 5400 PAID)")
        settled = lambda x: bool(x) and x[0] == 200  # noqa: E731  (the fare appears once Fare has settled it)
        fn = await until(lambda: _get_json(http, f"{API}/fares/rides/{nr['id']}", auth(N)), settled) or (None, {})
        fr = await until(lambda: _get_json(http, f"{API}/fares/rides/{rr['id']}", auth(R)), settled) or (None, {})
        s.check((fn[1].get("total_poysha"), fn[1].get("pool_discount_poysha"), fn[1].get("payment_status")) ==
                (7200, 1050, "PAID"), "Nusrat 7200, discount 1050, PAID (wallet)", fn)
        s.check((fr[1].get("total_poysha"), fr[1].get("pool_discount_poysha"), fr[1].get("payment_status")) ==
                (5400, 600, "PAID"), "Rafiq 5400, discount 600, PAID (cash)", fr)
        r = await http.get(f"{API}/fares/rides/{nr['id']}", headers=auth(R))
        s.check(r.status_code == 404, "Rafiq reading Nusrat's fare: 404", r.status_code)

        s.step("11. History + audit trail")
        d = await until(lambda: _json(http.get(f"{API}/rides/{nr['id']}", headers=auth(N))),
                        lambda x: x.get("final_fare_poysha") is not None)
        trail = [f"{h['from_status']} -> {h['to_status']} by {h['actor_role']}" for h in d["history"]]
        s.check((d["status"], d["final_fare_poysha"], d["payment_status"]) == ("COMPLETED", 7200, "PAID"),
                "COMPLETED, final fare 7200 PAID (copied from Fare's event)", d)
        s.check(trail == ["None -> REQUESTED by PASSENGER", "REQUESTED -> MATCHED by DRIVER",
                          "MATCHED -> DRIVER_ARRIVED by DRIVER", "DRIVER_ARRIVED -> STARTED by DRIVER",
                          "STARTED -> COMPLETED by DRIVER"], "the full audit trail", trail)

        s.step("12. Jashim can now go offline")
        r = await http.post(f"{API}/drivers/me/offline", headers=auth(J))
        s.check(r.status_code == 200 and r.json()["status"] == "OFFLINE", "OFFLINE", r.text)

        s.step("13. Money moved as it should")
        after = (await http.get(f"{API}/wallet", headers=auth(N))).json()["balance_poysha"]
        s.check(after == wallet_before - 7200, "Nusrat's wallet went down by exactly 7200", (wallet_before, after))
        earned = (await http.get(f"{API}/driver/earnings", headers=auth(J))).json()
        s.check((earned["wallet_poysha"] - earned_before["wallet_poysha"],
                 earned["cash_poysha"] - earned_before["cash_poysha"]) == (7200, 5400),
                "Jashim earned 7200 by wallet + 5400 in cash", earned)

        s.step("14. Live channel (the plan's websocat check)")
        await asyncio.sleep(1)
        stop.set()
        await asyncio.gather(*listeners, return_exceptions=True)
        mine = [(m["type"], m["data"].get("to_status")) for m in nusrat_phone]
        for want in [("ride.matched", None), ("ride.status", "DRIVER_ARRIVED"), ("ride.status", "STARTED"),
                     ("vehicle.location", None), ("fare.settled", None)]:
            s.check(want in mine, f"Nusrat's phone got {want[0]}{' ' + want[1] if want[1] else ''}", mine)
        s.check(rr["id"] not in json.dumps(nusrat_phone) and "5400" not in json.dumps(nusrat_phone),
                "nothing about Rafiq's ride or fare reached Nusrat")
        s.check(any(m["type"] == "ride.offer" and m["data"]["ride_id"] == nr["id"] for m in jashim_phone),
                "Jashim's phone got the ride offer", [m["type"] for m in jashim_phone])
        caught = (await http.get(f"{API}/notifications", headers=auth(N))).json()
        s.check({n["payload"]["event_id"] for n in caught} >=
                {m["event_id"] for m in nusrat_phone if m["type"] != "vehicle.location"},
                "Nusrat's inbox has everything she got live (for catch-up)")

        s.step("15. Tidy up: Shirin gives up on her request (so the story can run again)")
        r = await http.post(f"{API}/rides/{sr['id']}/cancel", headers=auth(S), json={})
        s.check(r.status_code == 200 and r.json()["status"] == "CANCELLED", "CANCELLED", r.text)

        s.step("Post-run checks")
        await asyncio.sleep(2)  # let the last events drain
        e = env_file()
        r = await http.get(f"{RABBIT_UI}/api/queues", auth=(e["RABBITMQ_DEFAULT_USER"], e["RABBITMQ_DEFAULT_PASS"]))
        queues = {q["name"]: q.get("messages", 0) for q in r.json()}
        s.check(DURABLE_QUEUES <= set(queues), "every consumer's queue exists (8.2)", sorted(queues))
        stuck = {n: c for n, c in queues.items() if n.endswith((".dlq", ".retry")) and c}
        s.check(not stuck, "every *.dlq and *.retry queue is empty", stuck)
        for svc, db in [("trip", "/data/trip.db"), ("fare", "/data/fare.db"), ("identity", "/data/identity.db")]:
            rows = sql(svc, db, "SELECT COUNT(*) FROM outbox WHERE published_at IS NULL")
            s.check(rows == [[0]], f"{svc}: no unpublished outbox rows", rows)
        pools = sql("trip", "/data/trip.db", "SELECT id, occupied_seats, max_capacity, status FROM pools")
        bullet = [p for p in pools if p[0] == pool["id"]]
        s.check(bullet and bullet[0][3] == "COMPLETED", "Bullet's pool is COMPLETED", bullet)
        s.check(all(p[1] <= p[2] <= 3 for p in pools), "no pool ever above its seats (Bullet: 3)", pools)

    print(f"\n{s.passed} checks passed, {s.failed} failed")
    return 0 if s.failed == 0 else 1


async def _json(coro):
    return (await coro).json()


async def _get_json(http: httpx.AsyncClient, url: str, headers: dict | None = None):
    try:
        r = await http.get(url, headers=headers)
        return r.status_code, r.json()
    except (httpx.HTTPError, ValueError):
        return None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
