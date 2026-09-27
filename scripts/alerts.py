"""The plan's alert-worthy signals (8.6), checked against the running system. Exit code 1 = something to look at.

    .venv\\Scripts\\python scripts\\alerts.py            (Windows)  |  .venv/bin/python scripts/alerts.py

1. a dead-letter queue has messages   -> an event failed 3 retries and is parked: someone must look at it
2. an outbox row unpublished > 30 s   -> a service can't reach RabbitMQ: its news isn't going out
3. a RATE_LIMITED spike at the gateway -> someone (or something) is hammering the API

Run it from a scheduler (every minute) or after a deploy. Moved ports: E2E_RABBIT_UI=http://localhost:35672.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
RABBIT_UI = os.environ.get("E2E_RABBIT_UI", "http://localhost:15672")
OUTBOX_MAX_AGE_S = 30
RATE_LIMITED_MAX = int(os.environ.get("ALERT_RATE_LIMITED_MAX", "20"))  # 429s at the gateway per window
WINDOW = os.environ.get("ALERT_WINDOW", "5m")
OUTBOXES = [("trip", "/data/trip.db"), ("fare", "/data/fare.db"), ("identity", "/data/identity.db")]


def env_file() -> dict:
    out = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def compose(*args: str) -> str:
    out = subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return out.stdout


def dead_letters() -> list[str]:
    e = env_file()
    queues = httpx.get(f"{RABBIT_UI}/api/queues", auth=(e["RABBITMQ_DEFAULT_USER"], e["RABBITMQ_DEFAULT_PASS"]),
                       timeout=10).json()
    return [f"{q['name']}: {q.get('messages', 0)} parked event(s)" for q in queues
            if q["name"].endswith(".dlq") and q.get("messages", 0)]


def stale_outboxes() -> list[str]:
    alerts = []
    for svc, db in OUTBOXES:
        # created_at is naive UTC (tesla_common.timeutil.utcnow); SQLite's datetime('now') is UTC too.
        q = ("SELECT COUNT(*), MIN(created_at) FROM outbox WHERE published_at IS NULL "
             f"AND created_at < datetime('now', '-{OUTBOX_MAX_AGE_S} seconds')")
        code = f"import sqlite3,json;print(json.dumps(sqlite3.connect('{db}').execute({q!r}).fetchone()))"
        count, oldest = json.loads(compose("exec", "-T", svc, "python", "-c", code))
        if count:
            alerts.append(f"{svc}: {count} event(s) unpublished for over {OUTBOX_MAX_AGE_S} s (oldest {oldest})")
    return alerts


def rate_limited() -> list[str]:
    hits = 0
    for line in compose("logs", "--no-log-prefix", "--since", WINDOW, "gateway").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        hits += d.get("logger") == "access" and d.get("status") == 429
    return [f"gateway: {hits} RATE_LIMITED (429) in the last {WINDOW} (limit {RATE_LIMITED_MAX})"] \
        if hits > RATE_LIMITED_MAX else []


def main() -> int:
    alerts = 0
    for name, check in [("dead-letter queues", dead_letters), (f"outboxes (> {OUTBOX_MAX_AGE_S} s)", stale_outboxes),
                        (f"rate limiting (last {WINDOW})", rate_limited)]:
        try:
            found = check()
        except Exception as exc:  # can't even check: that's worth an alert too
            found = [f"could not check: {exc}"]
        print(f"{'ALERT' if found else 'ok':5}  {name}")
        for f in found:
            print(f"       {f}")
        alerts += len(found)
    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
