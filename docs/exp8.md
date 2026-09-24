# Step 8 Explained: Running the Whole System

## Progress

| Section | What it is | Status |
|---|---|---|
| 8.1 | Environment: `.env`, the Dockerfile, `docker-compose.yml` | **Done** (explained below) |
| 8.2 | Boot sequence (who starts when, and why) | **Done** (explained below) |
| 8.3 | Local run (with and without Docker) | **Done** (explained below) |
| 8.4 | End-to-end verification: the Banani story | **Done**: **52 of 52 checks pass on the real system**, three runs in a row |
| 8.5 | Test plan (maps to the PRD) | Not started |
| 8.6 | Observability minimum | Not started |
| 8.7 | Known limitations & scale path | Not started |

This file grows as each section gets done.

---

## The big picture in 30 seconds

Parts 1–7 built six services and tested each one **on its own**, with the others faked: a fake bus instead of RabbitMQ, fakeredis instead of Redis, a fake Fare inside Trip's tests, and so on. That proves each piece. It doesn't prove they **fit**.

Part 8 is where everything runs **together, for real**: real RabbitMQ, real Redis, six real containers talking to each other over a real network. Then the whole Banani story is played through the front door, exactly as the phones would:

```
                     your computer
  ┌───────────────────────────────────────────────────────────────────────────┐
  │  scripts/e2e.py  (plays Jashim, Nusrat, Rafiq and Shirin)                  │
  └──────┬────────────────────────────────┬──────────────────────┬────────────┘
         │ HTTP :8000                      │ WebSocket :8005      │ RabbitMQ UI :15672
  ┌──────▼──────────────── docker compose ─▼──────────────────────▼────────────┐
  │  gateway ──▶ identity ──▶ trip ──▶ fare ──▶ matching         notification  │
  │                  └───────── RabbitMQ (events) ─────────────────┘  ▲       │
  │                             Redis (logins, rate limits, GPS) ─────┘       │
  │  each service: its own SQLite file in its own volume                       │
  └────────────────────────────────────────────────────────────────────────────┘
```

**Result:** the whole story passes, **52 checks out of 52**, on the real system, three runs in a row. Getting there also found **two real problems** that no unit test could have found (a port clash with another project on this machine, and a RabbitMQ healthcheck that says "ready" too early); both are fixed below.

---

## 8.1: the environment

### `.env`: one file of settings for every service

The plan's `.env.example` was already in the repo from Part 1 (RabbitMQ user and password, the internal token, the key paths, the pooling numbers). **`.env` is a copy of it** (`cp .env.example .env`, the plan's step), and it's gitignored, because in a real deployment it holds real passwords. Every service reads the settings it needs from it and ignores the rest.

> Side note: `.env.example` is **also** listed in `.gitignore` (last lines). It's committed anyway (git keeps tracking a file it already has), so nothing is broken, but the line is misleading: the example **should** be committed. Worth deleting that line one day.

### The Dockerfile: one recipe for six services

Every service's `Dockerfile` is the plan's generic one: Python 3.12 → install Part 1's shared library → install the service's requirements → copy the service's code. It was written service by service in Parts 2–7; nothing new here. The build context is the **whole repo** (so `libs/common` can be copied in), and `.dockerignore` (Part 1) keeps `.venv`, the keys, tests and database files **out** of the images.

### `docker-compose.yml`: the whole system in one file

It had only RabbitMQ and Redis (Part 1). Now it has all eight containers, as in the plan:

| Container | Starts with | Why it's shaped this way |
|---|---|---|
| `rabbitmq`, `redis` | — | everyone connects to them at startup |
| `matching` | `alembic upgrade head` (zones), then the app | |
| `fare` | migrations, **`python -m app.seed`** (the cast's wallets, 6.7), the app | |
| `trip` | migrations, the app | |
| `identity` | migrations, **seed** (the four people + Bullet), the app | gets **both** keys (it **signs** logins) |
| `notification` | migrations, the app | gets the **public** key (it **checks** logins on the socket, 7.5); port **8005** open to phones |
| `gateway` | the app | gets the public key; port **8000** open to phones |

- **Each service has its own named volume** (`trip-data`, `fare-data`, …) for its SQLite file, so data survives restarts, and no service can see another's database (database-per-service, Part 0).
- **Only three doors face the outside** (the plan): the gateway (8000), Notification's WebSocket (8005) and the RabbitMQ UI (15672). RabbitMQ (5672) and Redis (6379) are published too, **only** so a service can be run outside Docker during development (8.3).
- **Every service has a healthcheck**: its own `/health` (database + RabbitMQ + Redis where used, Parts 3–7).

### What I changed from the plan's compose file, and why

| Plan says | What I did | Why |
|---|---|---|
| RabbitMQ healthcheck `rabbitmq-diagnostics -q ping` | **`rabbitmq-diagnostics -q check_port_connectivity`** | **found by running it** (see below): `ping` says "healthy" about **5 seconds before** RabbitMQ accepts connections. Matching and Notification started in that gap, crashed, and were restarted twice each |
| fixed host ports `8000:8000`, `8005:8005`, … | **`${GATEWAY_PORT:-8000}:8000`** etc., the plan's numbers as defaults | **found by running it** (see below): another project on this machine already uses 8000, 8005, 5672, 15672 and 6379. Now the outside door can be moved without editing the file; inside the system nothing changes |
| gateway: no healthcheck | added one (its `/health` checks every other service, Part 2) | `docker compose ps` then says "healthy" only when the **whole** system is reachable through the front door |
| matching: `PORT: "8002"` | removed | nothing reads it (the port is in the start command) |

### Found by running it (1): "healthy" before it's ready

The first real start **worked**, but the logs showed Matching and Notification each failing twice with `AMQPConnectionError: Connection refused`, then succeeding. The timeline from the logs:

```
19:12:48   Compose: RabbitMQ is "healthy" (ping OK)  →  starts Matching and Notification
19:12:48   Matching: connection refused → startup failed → restarted
19:12:53   Matching: connection refused → startup failed → restarted
19:12:53.6 RabbitMQ: "started TCP listener on [::]:5672"        ← only NOW can anyone connect
           Matching: startup complete  (restarts: 2)
```

`rabbitmq-diagnostics ping` checks that the RabbitMQ **program** is alive, not that its **door** (port 5672) is open. The system survived only because of `restart: unless-stopped`: each crash was retried until it worked. With a slower machine or more services, that could be a long crash loop, and "the logs are full of errors on every start" hides real errors. **`check_port_connectivity`** passes only once the listener accepts clients.

**Proof:** a full restart afterwards: every service **0 restarts**, **0** connection errors in the logs, then the whole story again: 52 of 52.

### Found by running it (2): someone else's containers on our ports

The first `docker compose up` stopped at Redis: *"Bind for 127.0.0.1:6379 failed: port is already allocated"*. Starting Docker Desktop had also started **another project on this machine** (containers named `mse-*`), which already uses ports **8000, 8005, 5672, 15672 and 6379**. I didn't stop it (it's not this project's to stop). Instead, the host ports became settings, with the plan's numbers as defaults:

```
docker compose up -d                                   # the plan's ports: 8000, 8005, 15672, 5672, 6379
GATEWAY_PORT=18000 NOTIFICATION_PORT=18005 RABBITMQ_PORT=25672 RABBITMQ_UI_PORT=35672 REDIS_PORT=16379 docker compose up -d
```

(PowerShell: `$env:GATEWAY_PORT=18000; …` before `docker compose up -d`.) Only the outside door moves; inside, the services still talk to each other on 8001–8005, 5672 and 6379, so nothing else changes. All the runs below used the second line.

### How 8.1 was checked

| Check | Result |
|---|---|
| `docker compose config` | parses; 8 services; every mount and setting resolves (e.g. Identity gets `/run/keys`, Notification and the gateway get `jwt_public.pem`) |
| `docker compose up --build -d` | **all six images build** from the repo, all eight containers start, **every one "healthy"** |
| `GET /health` on the gateway | `{"status":"ok","checks":{"redis":"ok","identity":"ok","matching":"ok","trip":"ok","fare":"ok","notification":"ok"}}` |

---

## 8.2: the boot sequence

| Order | Who | Waits for | Why |
|---|---|---|---|
| 1 | RabbitMQ, Redis | — | everyone connects to them at startup |
| 2 | **Matching** | infra | calls nobody; others call it |
| 2 | **Notification** | infra | only listens |
| 3 | **Fare** | Matching | quotes need distances |
| 4 | **Trip** | Fare, Matching | booking calls both |
| 5 | **Identity** | Trip | "can Jashim go offline?" asks Trip; it also seeds the cast |
| 6 | **Gateway** | everyone | the front door opens last |

**Why the order matters beyond "the service it calls is up":** a RabbitMQ **queue is created by its consumer** when the consumer starts. An event published to the exchange **before** its queue exists goes **nowhere**: it's silently dropped. So every consumer must be running before anything can produce an event:

| Queue | Consumer | Starts at | The events it needs come from | …which start at |
|---|---|---|---|---|
| `matching.fleet-state` | Matching | 2 | Identity, Trip | 5, 4 |
| `notification.inbox` | Notification | 2 | Trip, Fare | 4, 3 (and only after a user acts) |
| `fare.ride-lifecycle` | Fare | 3 | Trip | 4 |
| `trip.driver-shift`, `trip.fare-settled` | Trip | 4 | Identity, Fare | 5, 3 (Fare only settles **after** Trip's events) |

And no user can act until the gateway (6) is up, which waits for everyone.

### How 8.2 was checked

| Check | Result |
|---|---|
| the order **Compose will actually use** (worked out from the file's `depends_on`) | `1 rabbitmq, redis → 2 matching, notification → 3 fare → 4 trip → 5 identity → 6 gateway`: exactly the table |
| the real start (container uptimes right after `up`) | RabbitMQ/Redis 50 s, Matching/Notification 34 s, Fare 28 s, Trip 17 s, Identity 11 s, gateway 0 s |
| **every consumer's queue exists before anything is produced** (the plan: "in CI, assert with `rabbitmqctl list_queues`") | checked through the RabbitMQ API right after start: the **5 durable queues**, each with **1 consumer**, plus their `.retry` and `.dlq`, and Notification's per-copy broadcast queue (`amq_…`). The e2e script checks it again at the end of every run |

---

## 8.3: running it

### With Docker (the normal way)

```
./scripts/gen_keys.sh          # once: the login key pair (Git Bash / Linux / macOS; needs openssl)
cp .env.example .env           # once
docker compose up --build -d   # build and start; ~1 minute to all healthy after the first build
docker compose ps              # all "healthy"
```
RabbitMQ UI: `http://localhost:15672` (user `tesla`, password `change-me`). Stop with `docker compose down` (keeps the data) or `docker compose down -v` (also deletes every database: a clean slate).

### Without Docker for one service (fast iteration)

Infra in Docker (`docker compose up -d rabbitmq redis`), and the service you're working on straight from its folder, with `.env` pointing at `localhost`:
```
cd services/matching
alembic upgrade head
uvicorn app.main:app --reload --port 8002
```

### How 8.3 was checked

- The Docker way: as in 8.1 and 8.4.
- **The non-Docker way, for real**: Matching run from source with `uvicorn`, against the RabbitMQ and Redis inside Docker. It migrated its own database (`0001 init`, `0002 seed zones`), started, and its `/health` answered `{"db":"ok","redis":"ok","rabbitmq":"ok"}`. Then it was stopped.

---

## 8.4: the Banani story, end to end

### `scripts/e2e.py` instead of `e2e.sh`

The plan's `scripts/e2e.sh` is a `curl` script that **prints** results for a person to compare by eye, and it needs `jq`, `uuidgen` and `websocat`. None of those exist on this Windows machine. So I wrote the same story as **`scripts/e2e.py`**, using `httpx` and `websockets`, which the project's `.venv` already has:

- **the same 12 steps, in the same order**, with the plan's expected values;
- it **checks** each one and prints `ok` / `FAIL`, ending with "N passed, M failed" and exit code 0 only if everything passed (so it can run in CI);
- **the live channel** (the plan's second terminal with `websocat`) is a background listener for Nusrat's and Jashim's phones;
- **the post-run checks** (the plan's "RabbitMQ UI → queues", and `sqlite3` inside the containers) are done by the script too, through the RabbitMQ API and `docker compose exec`;
- it **refuses to start** if an earlier run left someone mid-ride (and says how to reset), and it **tidies up** after itself (Jashim offline, Shirin's leftover request cancelled), so it can be run again straight away.

```
.venv\Scripts\python scripts\e2e.py
```
(With moved ports: set `E2E_API`, `E2E_WS`, `E2E_RABBIT_UI`, e.g. `E2E_API=http://localhost:18000/api/v1`.)

### Two small additions to the plan's story

| Addition | Why |
|---|---|
| **5b. Jashim pings again after Rafiq joins** | the plan expects Nusrat's phone to receive `vehicle.location`, but its script only pings **before** there's a pool, and a position is only broadcast to a pool's passengers (4.5, 7.6). Without a ping during the ride, the plan's own expectation can't be met |
| **13. Money moved as it should** + **15. Shirin cancels** | the wallet and earnings **deltas** (not just the fares), and a tidy end state |

### The result: 52 checks, 0 failed

```
 0. system up: gateway /health 200; all four log in; nobody mid-ride, Bullet has no live pool
 1. Jashim online in Bullet; ping from Banani → zone BANANI
 2. Nusrat's quote: 8250 solo / 7200 pooled
 3. Nusrat requests → REQUESTED;   3b. same Idempotency-Key → Idempotent-Replay: true, same ride
 4. Jashim sees Nusrat's offer, accepts → pool 1/3
 5. Rafiq auto-joins → MATCHED in Bullet, pool 2/3, stops: Banani, Banani, Gulshan 1, Mohakhali
 6. Shirin wants 2 seats, only 1 left → REQUESTED, not in Bullet
 7. Rafiq reading Nusrat's ride → 404
 8. arrive + start, both riders
 9. Nusrat cancelling after start → 409 INVALID_TRANSITION; Rafiq dropped at Gulshan 1; Nusrat dropped → pool COMPLETED
10. fares: Nusrat 7200 (discount 1050) PAID by wallet; Rafiq 5400 (discount 600) PAID cash; Rafiq reading Nusrat's fare → 404
11. Nusrat's ride: COMPLETED, final fare 7200 PAID; audit: REQUESTED by PASSENGER → MATCHED by DRIVER → DRIVER_ARRIVED → STARTED → COMPLETED
12. Jashim goes offline → OFFLINE (Trip confirms his pool is finished)
13. Nusrat's wallet −7200 exactly; Jashim earned +7200 by wallet and +5400 in cash
14. Nusrat's phone got ride.matched, arrived, started, vehicle.location, fare.settled, and nothing about Rafiq;
    Jashim's phone got the offer; Nusrat's inbox has everything she got live
15. Shirin's leftover request cancelled
post-run: all 5 consumer queues exist; every .dlq and .retry empty; no unpublished outbox rows in Trip, Fare, Identity;
          Bullet's pool COMPLETED; no pool ever above its seats (Bullet: 3)

52 checks passed, 0 failed
```

**Three runs, all 52 of 52**: the first, a second one straight after (the tidy-up works), and a third after a full cold restart with the fixed RabbitMQ healthcheck.

### What this proves that the unit tests couldn't

Every arrow in the system was crossed **for real** at least once:

| Across | Proven by |
|---|---|
| phone → gateway → each service (JWT, internal token, idempotency) | every step; 3b |
| Trip → Fare (quotes), Trip → Matching (pool advice), Fare → Matching (distances) | 2, 3, 5 |
| Identity → Trip (may Jashim go offline?) | 12 |
| Identity's event → Matching and Trip (Jashim online) | 1 → 4 (the offer only exists because both heard it) |
| Trip's event → Matching (Bullet busy) | 6 (Shirin gets **no** driver: Jashim is no longer "available") |
| Trip's event → Fare → Fare's event → Trip | 10 → 11 (the final fare on Nusrat's ride came back from Fare) |
| every event → Notification → phone; Matching's GPS → Redis → Notification → phone | 14 |
| the outbox relay in Trip, Fare and Identity | post-run: 0 unpublished rows |
| nothing silently failed | post-run: every dead-letter and retry queue empty |

### Also checked on the running system

| Check | Result |
|---|---|
| **login tokens in Notification's log** (the 7.5 fix) | every WebSocket line reads `"WebSocket /ws?token=***" [accepted]`; **0** real tokens in the log |
| errors in any service's log during the runs | none, apart from the RabbitMQ startup race (fixed above) |
| every service's own test suite, after all of Part 8 | gateway 65, identity 94, matching 127, trip 341, fare 151, notification 99: **877 passed** |

---

## Things to know before the next sections

- **The system is left running** on this machine, on the moved ports (gateway `http://localhost:18000`, WebSocket `ws://localhost:18005`, RabbitMQ UI `http://localhost:35672`). Stop it with `docker compose down` (add `-v` to wipe the data). Remember the other project (`mse-*`) holds the default ports while it's running.
- **Reset to a clean slate:** `docker compose down -v && docker compose up -d`. The e2e script can also just be run again: it tidies up after itself.
- **Wallets go down with every run** (Nusrat pays ৳72 each time; she starts with ৳500), so after ~6 runs on the same data her wallet payment would come back **FAILED** (step 10 would fail, correctly). `down -v` resets them.
- **8.5 (the test plan)** maps the PRD's requirements to test files. All seven files it names already exist (written in Parts 5 and 6); 8.5 will check that each one really asserts what the plan's table says.
