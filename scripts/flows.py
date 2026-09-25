"""Whole-system flows beyond the Banani story: sign-up to payment, races, failed payments, expiry, cancellations,
errors crossing several services, logout, catch-up. Everything goes through the real gateway, with real RabbitMQ and
Redis, exactly as phones would. Each flow registers its OWN brand-new people, so flows don't disturb each other (or
the Banani story), and the whole thing can be run again.

    docker compose -f docker-compose.yml -f docker-compose.flows.yml up --build -d    # stale rides expire in 20 s
    .venv\\Scripts\\python scripts\\flows.py

Moved ports: the same E2E_API / E2E_WS / E2E_RABBIT_UI variables as scripts/e2e.py.
"""
import asyncio
import json
import random
import sys
import time
import uuid

import httpx
import websockets

from e2e import API, GATEWAY, RABBIT_UI, WS, Story, env_file, until

RUN = f"{random.randint(0, 99_999_999):08d}"  # makes this run's phones, licences and plates unique
_counter = iter(range(10, 99))


def phone() -> str:
    return f"019{RUN[:6]}{next(_counter)}"


class Person:
    def __init__(self, http: httpx.AsyncClient, name: str, token: str, user_id: str):
        self.http, self.name, self.token, self.id = http, name, token, user_id
        self.phone_messages: list[dict] = []
        self._listener: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.close_code: int | None = None

    def h(self, key: str | None = None) -> dict:
        return {"Authorization": f"Bearer {self.token}"} | ({"Idempotency-Key": key} if key else {})

    async def get(self, path, **kw):
        return await self.http.get(f"{API}{path}", headers=self.h(), **kw)

    async def post(self, path, json=None, key=None):
        return await self.http.post(f"{API}{path}", headers=self.h(key), json=json)

    async def open_phone(self):
        """A WebSocket like the app's; collects everything pushed to this person."""
        async def run():
            try:
                async with websockets.connect(f"{WS}?token={self.token}") as ws:
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        except asyncio.TimeoutError:
                            continue
                        if raw != "pong":
                            self.phone_messages.append(json.loads(raw))
            except websockets.ConnectionClosed as e:
                self.close_code = e.rcvd.code if e.rcvd else None
        self._listener = asyncio.create_task(run())
        await asyncio.sleep(0.5)

    async def close_phone(self):
        self._stop.set()
        if self._listener:
            await asyncio.gather(self._listener, return_exceptions=True)

    def got(self, kind: str) -> list[dict]:
        return [m for m in self.phone_messages if m["type"] == kind]


async def _politely(http, url: str, body: dict) -> httpx.Response:
    """Sign-up and login are limited to 10/min per address at the gateway (Part 2), and these flows create ~15
    people from one machine. Like a real app: on 429, wait for the next minute's window and try again."""
    for _ in range(3):
        r = await http.post(url, json=body)
        if r.status_code != 429:
            return r
        await asyncio.sleep(61 - time.time() % 60)
    return r


async def register(http, name: str, role="PASSENGER", topup: int = 0) -> Person:
    ph = phone()
    body = {"full_name": name, "phone": ph, "password": "Flow@12345", "role": role}
    if role == "DRIVER":
        body["license_number"] = f"FLOW-{RUN}-{name}"
    r = await _politely(http, f"{API}/auth/register", body)
    assert r.status_code == 201, r.text
    login = (await _politely(http, f"{API}/auth/login", {"phone": ph, "password": "Flow@12345"})).json()
    p = Person(http, name, login["access_token"], login["user"]["id"])
    if topup:
        await p.post("/wallet/topup", {"amount_poysha": topup})
    return p


async def new_driver(http, name: str, car: str, seats: int, zone_point: tuple[float, float]) -> Person:
    d = await register(http, name, "DRIVER")
    r = await d.http.put(f"{API}/drivers/me/vehicle", headers=d.h(),
                         json={"nickname": car, "make": "Tesla", "model": "Model Y", "plate": f"DHK-{RUN}-{name}",
                               "seat_capacity": seats})
    assert r.status_code == 200, r.text
    assert (await d.post("/drivers/me/online")).json()["status"] == "ONLINE"
    await d.post("/driver/location", {"lat": zone_point[0], "lng": zone_point[1]})
    await asyncio.sleep(1.5)  # Matching and Trip hear "online" through RabbitMQ
    return d


async def request(p: Person, pickup: str, dropoff: str, seats=1, method="CASH", quote_id=None) -> httpx.Response:
    body = {"pickup_zone": pickup, "dropoff_zone": dropoff, "seats": seats, "payment_method": method}
    if quote_id:
        body["quote_id"] = quote_id
    return await p.post("/rides", body, key=str(uuid.uuid4()))


async def offer_for(driver: Person, ride_id: str, timeout=8.0) -> bool:
    offers = await until(lambda: _json(driver.get("/driver/offers")),
                         lambda o: any(x["ride_id"] == ride_id for x in o), timeout=timeout)
    return any(x["ride_id"] == ride_id for x in offers)


async def _json(coro):
    return (await coro).json()


async def ride_of(p: Person, ride_id: str) -> dict:
    return (await p.get(f"/rides/{ride_id}")).json()


# Zone centres (Matching's seed, 4.3); each flow works in its own part of Dhaka.
MIRPUR, UTTARA, DHANMONDI = (23.8069, 90.3687), (23.8759, 90.3795), (23.7465, 90.3760)
BASHUNDHARA, GULSHAN_2, FARMGATE = (23.8193, 90.4526), (23.7925, 90.4144), (23.7580, 90.3897)


# ---------------------------------------------------------------------------------------------------------------------

async def flow_signup_to_paid_ride(s: Story, http):
    s.step("A. Brand-new people: sign up -> car -> online -> quote -> ride -> pay by wallet -> everyone told")
    karim = await new_driver(http, "Karim", "Toofan", 2, MIRPUR)
    mitu = await register(http, "Mitu")
    me = (await mitu.get("/users/me")).json()
    s.check((me["full_name"], me["role"]) == ("Mitu", "PASSENGER"), "Mitu signed up and logged in", me)
    s.check((await mitu.get("/wallet")).json()["balance_poysha"] == 0, "her new wallet is empty")
    s.check((await mitu.post("/wallet/topup", {"amount_poysha": 50000})).json() == {"balance_poysha": 50000},
            "tops up 500 taka")
    await mitu.open_phone()
    await karim.open_phone()

    q = (await mitu.post("/fares/estimate", {"pickup_zone": "MIRPUR", "dropoff_zone": "FARMGATE", "seats": 1})).json()
    d = q["distance_m"]
    s.check(q["solo_total_poysha"] == 3000 + d * 1500 // 1000 and
            q["pooled_total_poysha"] == 3000 + d * 1500 // 1000 - (d * 1500 // 1000) * 20 // 100,
            f"quote follows the tariff for Matching's {d} m", q)
    r = await request(mitu, "MIRPUR", "FARMGATE", method="WALLET", quote_id=q["quote_id"])
    ride = r.json()
    s.check(ride.get("status") == "REQUESTED", "Mitu requests: REQUESTED", r.text)
    s.check(await offer_for(karim, ride["id"]), "Karim (the only driver near Mirpur) is offered it")
    pool = (await karim.post(f"/driver/offers/{ride['id']}/accept")).json()
    s.check((pool["vehicle_nickname"], pool["occupied_seats"], pool["max_capacity"]) == ("Toofan", 1, 2),
            "Karim accepts: Toofan 1/2", pool)
    await asyncio.sleep(1)
    await karim.post("/driver/location", {"lat": 23.80, "lng": 90.37})  # driving to her
    for action in ("arrive", "start", "complete"):
        s.check((await karim.post(f"/driver/rides/{ride['id']}/{action}")).status_code == 200, f"Karim: {action}")

    fare = await until(lambda: mitu.get(f"/fares/rides/{ride['id']}"), lambda x: x.status_code == 200)
    f = fare.json()
    s.check((f["total_poysha"], f["pooled"], f["payment_status"]) == (q["solo_total_poysha"], False, "PAID"),
            "she rode alone: pays the SOLO price she was quoted, PAID", f)
    s.check((await mitu.get("/wallet")).json()["balance_poysha"] == 50000 - q["solo_total_poysha"],
            "taken from her wallet")
    s.check((await karim.get("/wallet")).json()["balance_poysha"] == q["solo_total_poysha"],
            "credited to Karim's wallet")
    s.check((await karim.get("/driver/earnings")).json() ==
            {"rides": 1, "total_poysha": q["solo_total_poysha"], "cash_poysha": 0,
             "wallet_poysha": q["solo_total_poysha"]}, "Karim's earnings")
    final = await until(lambda: ride_of(mitu, ride["id"]), lambda x: x.get("final_fare_poysha") is not None)
    s.check(final["final_fare_poysha"] == q["solo_total_poysha"], "Trip got the final fare back from Fare")

    await asyncio.sleep(1)
    await mitu.close_phone()
    await karim.close_phone()
    matched = mitu.got("ride.matched")
    s.check(bool(matched) and (matched[0]["data"]["driver_name"], matched[0]["data"]["vehicle_nickname"]) ==
            ("Karim", "Toofan"), "Mitu's phone: matched with Karim in Toofan", matched)
    s.check([m["data"]["to_status"] for m in mitu.got("ride.status")] == ["DRIVER_ARRIVED", "STARTED"],
            "Mitu's phone: arrived, started")
    s.check(bool(mitu.got("vehicle.location")), "Mitu's phone: the car moving")
    s.check(bool(mitu.got("fare.settled")) and mitu.got("fare.settled")[0]["data"]["total_poysha"] ==
            q["solo_total_poysha"], "Mitu's phone: what she paid")
    s.check(bool(karim.got("ride.offer")) and bool(karim.got("pool.updated")) and bool(karim.got("fare.settled")),
            "Karim's phone: the offer, his pool, the payment")

    inbox = (await mitu.get("/notifications")).json()
    live = {m["event_id"] for m in mitu.phone_messages if m["type"] != "vehicle.location"}
    s.check({n["payload"]["event_id"] for n in inbox} == live, "her inbox = everything she got live")
    r = await mitu.post(f"/notifications/{inbox[0]['id']}/read")
    s.check(r.status_code == 204, "she marks one read")
    other = await register(http, "Nosy")
    s.check((await other.post(f"/notifications/{inbox[0]['id']}/read")).status_code == 404,
            "someone else can't touch her inbox")
    s.check((await karim.post("/drivers/me/offline")).json()["status"] == "OFFLINE", "Karim goes offline")


async def flow_last_seat_race(s: Story, http):
    s.step("B. Two strangers race for the last seat in a 2-seat car, through the real system")
    rana = await new_driver(http, "Rana", "Chhoto", 2, UTTARA)
    first = await register(http, "Ayesha")
    r = (await request(first, "UTTARA", "MIRPUR")).json()
    await offer_for(rana, r["id"])
    pool = (await rana.post(f"/driver/offers/{r['id']}/accept")).json()
    s.check(pool["occupied_seats"] == 1, "Ayesha in Chhoto: 1/2")
    await asyncio.sleep(1)  # Matching hears Rana is busy
    b, c = await register(http, "Bappi"), await register(http, "Chaity")
    rb, rc = await asyncio.gather(request(b, "UTTARA", "MIRPUR"), request(c, "UTTARA", "MIRPUR"))
    statuses = sorted([rb.json()["status"], rc.json()["status"]])
    s.check(statuses == ["MATCHED", "REQUESTED"], "exactly one gets the seat, the other waits", statuses)
    live = (await rana.get("/driver/pool")).json()
    s.check((live["occupied_seats"], live["max_capacity"], len(live["riders"])) == (2, 2, 2),
            "the car is full: 2/2, never 3", live)
    loser = b if rb.json()["status"] == "REQUESTED" else c
    loser_ride = (rb if loser is b else rc).json()
    s.check(not await offer_for(rana, loser_ride["id"], timeout=2), "the one who lost isn't offered to busy Rana")
    # tidy: everyone cancels (still MATCHED / REQUESTED), the pool dissolves, Rana goes offline
    for p, rid in [(loser, loser_ride["id"]), (first, r["id"]),
                   ((c if loser is b else b), (rc if loser is b else rb).json()["id"])]:
        await p.post(f"/rides/{rid}/cancel", {})
    s.check((await rana.get("/driver/pool")).status_code == 204, "all cancelled: Rana's pool is gone")
    s.check((await rana.post("/drivers/me/offline")).json()["status"] == "OFFLINE", "Rana goes offline")


async def flow_short_wallet(s: Story, http):
    s.step("C. Wallet too short: the ride still completes, payment FAILED, the driver is told to collect cash")
    dipu = await new_driver(http, "Dipu", "Lal", 4, DHANMONDI)
    shila = await register(http, "Shila", topup=1000)  # 10 taka
    await dipu.open_phone()
    r = (await request(shila, "DHANMONDI", "FARMGATE", method="WALLET")).json()
    await offer_for(dipu, r["id"])
    await dipu.post(f"/driver/offers/{r['id']}/accept")
    for action in ("arrive", "start", "complete"):
        await dipu.post(f"/driver/rides/{r['id']}/{action}")
    f = (await until(lambda: shila.get(f"/fares/rides/{r['id']}"), lambda x: x.status_code == 200)).json()
    s.check((f["payment_method"], f["payment_status"]) == ("WALLET", "FAILED"), "fare: WALLET, FAILED", f)
    s.check((await shila.get("/wallet")).json()["balance_poysha"] == 1000, "nothing taken from her 10 taka")
    s.check((await dipu.get("/wallet")).json()["balance_poysha"] == 0, "nothing credited to Dipu's wallet")
    s.check((await dipu.get("/driver/earnings")).json()["cash_poysha"] == f["total_poysha"],
            "Dipu's earnings count it as cash (he collects it)")
    await asyncio.sleep(1)
    await dipu.close_phone()
    told = dipu.got("fare.settled")
    s.check(bool(told) and told[0]["data"]["payment_status"] == "FAILED", "Dipu's phone says FAILED: collect cash")
    await dipu.post("/drivers/me/offline")


async def flow_nobody_accepts(s: Story, http):
    s.step("D. Nobody accepts: the driver declines, the request expires by itself (TTL 20 s in this run)")
    babu = await new_driver(http, "Babu", "Neel", 4, BASHUNDHARA)
    tania = await register(http, "Tania")
    await tania.open_phone()
    r = (await request(tania, "BASHUNDHARA", "GULSHAN_2")).json()
    s.check(await offer_for(babu, r["id"]), "Babu is offered Tania's ride")
    s.check((await babu.post(f"/driver/offers/{r['id']}/decline")).status_code == 204, "Babu declines")
    s.check((await babu.get("/driver/offers")).json() == [], "the offer is gone from his list")
    s.check((await ride_of(tania, r["id"]))["status"] == "REQUESTED", "Tania's ride still waits")
    t0 = time.monotonic()
    gone = await until(lambda: ride_of(tania, r["id"]), lambda x: x["status"] == "CANCELLED", timeout=60, every=2)
    s.check((gone["status"], gone["cancel_reason"]) == ("CANCELLED", "NO_DRIVER_FOUND"),
            f"expired by the sweeper after ~{time.monotonic() - t0:.0f} s more: NO_DRIVER_FOUND", gone)
    s.check(gone["history"][-1]["actor_role"] == "SYSTEM", "the audit says SYSTEM did it")
    await asyncio.sleep(1)
    await tania.close_phone()
    s.check(bool(tania.got("ride.cancelled")), "Tania's phone was told")
    s.step("   ...and a driver who is offline is never offered anything")
    await babu.post("/drivers/me/offline")
    await asyncio.sleep(1.5)
    r2 = (await request(tania, "BASHUNDHARA", "GULSHAN_2")).json()
    s.check(r2["status"] == "REQUESTED", "Tania can ask again (the expired ride doesn't block her)")
    await babu.post("/drivers/me/online")
    s.check(not await offer_for(babu, r2["id"], timeout=2), "Babu (offline when she asked) wasn't offered it")
    await tania.post(f"/rides/{r2['id']}/cancel", {})
    await babu.post("/drivers/me/offline")


async def flow_cancellations(s: Story, http):
    s.step("E. Cancellations and the rules that span services")
    sumon = await new_driver(http, "Sumon", "Sabuj", 4, GULSHAN_2)
    p1, p2 = await register(http, "Farhan"), await register(http, "Nabila")
    r1 = (await request(p1, "GULSHAN_2", "BANANI")).json()
    await offer_for(sumon, r1["id"])
    await sumon.post(f"/driver/offers/{r1['id']}/accept")
    r = await sumon.post("/drivers/me/offline")
    s.check((r.status_code, r.json()["error"]["code"]) == (409, "DRIVER_HAS_LIVE_POOL"),
            "Sumon can't go offline with a passenger waiting (Identity asks Trip)", r.text)
    s.check((await p1.post(f"/rides/{r1['id']}/cancel", {})).json()["status"] == "CANCELLED",
            "Farhan cancels while MATCHED")
    s.check((await sumon.get("/driver/pool")).status_code == 204, "the pool dissolves: Sumon is free")
    await asyncio.sleep(1)
    q = (await p2.post("/fares/estimate", {"pickup_zone": "GULSHAN_2", "dropoff_zone": "BANANI", "seats": 1})).json()
    r2 = (await request(p2, "GULSHAN_2", "BANANI", quote_id=q["quote_id"])).json()
    s.check(await offer_for(sumon, r2["id"]), "free again, Sumon is offered Nabila's ride (Matching heard it)")
    await sumon.post(f"/driver/offers/{r2['id']}/accept")
    await sumon.post(f"/driver/rides/{r2['id']}/arrive")
    r = await p2.post(f"/rides/{r2['id']}/cancel", {})
    s.check((r.status_code, r.json()["error"]["code"]) == (409, "INVALID_TRANSITION"),
            "Nabila can't cancel once Sumon has arrived")
    r = await sumon.post(f"/driver/rides/{r2['id']}/cancel", {})
    s.check(r.status_code == 422, "Sumon must give a reason")
    r = await sumon.post(f"/driver/rides/{r2['id']}/cancel", {"reason": "PASSENGER_NO_SHOW"})
    s.check(r.status_code == 200, "Sumon cancels: PASSENGER_NO_SHOW")
    d = await ride_of(p2, r2["id"])
    s.check((d["status"], d["cancel_reason"], d["history"][-1]["actor_role"]) ==
            ("CANCELLED", "PASSENGER_NO_SHOW", "DRIVER"), "her history says the driver cancelled, and why", d)
    s.check((await p2.get(f"/fares/rides/{r2['id']}")).status_code == 404, "no fare for a cancelled ride")
    await asyncio.sleep(1.5)  # Fare hears the cancel and voids the quote
    r = await request(p2, "GULSHAN_2", "BANANI", quote_id=q["quote_id"])
    s.check((r.status_code, r.json()["error"]["code"]) == (422, "QUOTE_NOT_FOUND"),
            "her cancelled ride's quote can't book another (Trip -> Fare)", r.text)
    s.check((await sumon.post("/drivers/me/offline")).json()["status"] == "OFFLINE", "now Sumon can go offline")


async def flow_errors_through_the_chain(s: Story, http):
    s.step("F. Errors travel back through every service in the chain, unchanged")
    p, q_owner = await register(http, "Imran"), await register(http, "Joya")
    r = await request(p, "MIRPUR", "MOTIJHEEL")
    s.check((r.status_code, r.json()["error"]["code"], r.json()["error"]["message"]) ==
            (422, "UNKNOWN_ZONE", "Unknown zone MOTIJHEEL"),
            "unknown zone: Matching -> Fare -> Trip -> gateway -> phone", r.text)
    r = await p.post("/fares/estimate", {"pickup_zone": "MIRPUR", "dropoff_zone": "MIRPUR", "seats": 1})
    s.check(r.status_code == 422, "same pickup and drop-off: refused")
    q = (await q_owner.post("/fares/estimate", {"pickup_zone": "MIRPUR", "dropoff_zone": "UTTARA",
                                                "seats": 1})).json()
    r = await request(p, "MIRPUR", "UTTARA", quote_id=q["quote_id"])
    s.check((r.status_code, r.json()["error"]["code"]) == (422, "QUOTE_MISMATCH"), "someone else's quote: refused")
    body = {"pickup_zone": "MIRPUR", "dropoff_zone": "UTTARA", "seats": 1}
    r = await http.post(f"{API}/rides", headers={"Authorization": f"Bearer {p.token}"}, json=body)
    s.check((r.status_code, r.json()["error"]["code"]) == (400, "IDEMPOTENCY_KEY_REQUIRED"),
            "booking without an Idempotency-Key: refused at the gateway")
    key = str(uuid.uuid4())
    first = await p.post("/rides", body, key=key)
    r = await p.post("/rides", body | {"seats": 2}, key=key)
    s.check((r.status_code, r.json()["error"]["code"]) == (422, "IDEMPOTENCY_KEY_REUSED"),
            "the same key for a different booking: refused")
    await p.post(f"/rides/{first.json()['id']}/cancel", {})
    s.check((await http.get(f"{API}/rides")).status_code == 401, "no login: 401")
    s.check((await http.get(f"{API}/rides", headers={"Authorization": "Bearer nonsense"})).status_code == 401,
            "a forged token: 401")
    s.check((await p.get("/driver/offers")).status_code == 403, "a passenger on a driver route: 403")
    r = await p.get("/rides/does-not-exist")
    s.check(r.status_code == 404 and r.headers.get("x-request-id") == r.json()["error"]["request_id"],
            "404, with the same request id in the header and the body")


async def flow_logout(s: Story, http):
    s.step("G. Logging out ends it everywhere: the gateway and the live socket")
    p = await register(http, "Rumi")
    await p.open_phone()
    s.check((await p.get("/users/me")).status_code == 200, "logged in: works")
    s.check((await p.post("/auth/logout")).status_code == 204, "logs out")
    s.check((await p.get("/users/me")).status_code == 401, "the same token is now refused at the gateway")
    t0 = time.monotonic()
    for _ in range(80):
        if p._listener.done():
            break
        await asyncio.sleep(0.5)
    s.check(p.close_code == 4401, f"her open socket was closed with 4401 after ~{time.monotonic() - t0:.0f} s",
            p.close_code)
    try:
        async with websockets.connect(f"{WS}?token={p.token}") as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
        code = None
    except websockets.ConnectionClosed as e:
        code = e.rcvd.code if e.rcvd else None
    s.check(code == 4401, "and it can't open a new one", code)


async def main() -> int:
    s = Story()
    async with httpx.AsyncClient(timeout=20) as http:
        r = await until(lambda: _health(http), lambda x: x == 200, timeout=120, every=2)
        if not s.check(r == 200, "the system is up"):
            return 1
        for flow in (flow_signup_to_paid_ride, flow_last_seat_race, flow_short_wallet, flow_nobody_accepts,
                     flow_cancellations, flow_errors_through_the_chain, flow_logout):
            try:
                await flow(s, http)
            except Exception as exc:  # a flow that crashes is a failure, not the end of the run
                s.check(False, f"{flow.__name__} crashed", repr(exc))

        s.step("After all flows: nothing got stuck anywhere")
        await asyncio.sleep(3)
        e = env_file()
        queues = {q["name"]: q.get("messages", 0) for q in (await http.get(
            f"{RABBIT_UI}/api/queues", auth=(e["RABBITMQ_DEFAULT_USER"], e["RABBITMQ_DEFAULT_PASS"]))).json()}
        stuck = {n: c for n, c in queues.items() if n.endswith((".dlq", ".retry")) and c}
        s.check(not stuck, "every dead-letter and retry queue is empty", stuck)
    print(f"\n{s.passed} checks passed, {s.failed} failed")
    return 0 if s.failed == 0 else 1


async def _health(http):
    try:
        return (await http.get(f"{GATEWAY}/health")).status_code
    except httpx.HTTPError:
        return None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
