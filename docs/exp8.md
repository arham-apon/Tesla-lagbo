# Step 8 Explained: Running the Whole System

## Progress

| Section | What it is | Status |
|---|---|---|
| 8.1 | Environment: `.env`, the Dockerfile, `docker-compose.yml` | **Done** (explained below) |
| 8.2 | Boot sequence (who starts when, and why) | **Done** (explained below) |
| 8.3 | Local run (with and without Docker) | **Done** (explained below) |
| 8.4 | End-to-end verification: the Banani story | **Done**: **52 of 52 checks pass on the real system**, three runs in a row |
| 8.5 | Test plan (maps to the PRD) | **Done** (explained below) |
| 8.6 | Observability minimum | **Done** (explained below) |
| 8.7 | Known limitations & scale path | **Done** (explained below) |

**Part 8 is complete, and with it the whole plan.** Two commands check everything:

```
.venv\Scripts\python scripts\test_all.py     # every automated test (no Docker): 1,038 passed
.venv\Scripts\python scripts\e2e.py          # the Banani story on the running system: 52 of 52
```

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

## 8.5: the test plan

The plan maps each **PRD requirement** to the test that proves it. All seven files it names were written in Parts 5 and 6. Here's each row, with the test that actually does it:

| PRD requirement | Plan's test file | Plan's assertion | Where it is | Status |
|---|---|---|---|---|
| Bullet's capacity can never be exceeded | `trip/tests/test_capacity.py` | a direct `UPDATE … occupied_seats = 4` raises `IntegrityError`; `try_join` with 2 seats on 2/3 → `STALE`, seats stay 2 | `test_direct_overbooking_is_refused_by_the_database`, `test_two_seats_do_not_fit_in_one` | ✅ as written, plus "seats always equal the riders booked" after every step of a whole evening |
| Two concurrent requests can't corrupt capacity | `trip/tests/test_concurrency.py` | the plan's code: Nusrat and Shirin race for the last seat → one `joined`, one `stale`, 3 of 3 | `test_last_seat_goes_to_exactly_one_rider` | ✅ the plan's test, plus 10 riders racing for 1 seat, and 2 drivers accepting the same ride |
| Invalid transitions rejected | `trip/tests/test_state_machine.py` | all 36 pairs × 3 actors | `test_transition` (108 cases), `test_the_grid_is_complete` | ✅ |
| Pooled fares correct | `fare/tests/test_pricing.py` | 7200 / 5400 / 8250 | `test_nusrat_pooled_banani_to_mohakhali`, `test_rafiq_pooled_banani_to_gulshan_1`, `test_nusrat_alone` | ✅ and the real system gives the same numbers (8.4, step 10) |
| Users can't modify another's ride | `trip/tests/test_ownership.py` | Rafiq cancelling Nusrat's ride → 404; Jashim acting on a ride not in his pool → 404 | `test_rafiq_cannot_cancel_nusrats_ride`, `test_another_driver_cannot_touch_jashims_riders`, and through HTTP: `test_http_rafiq_cannot_see_or_cancel_nusrats_ride`, `test_http_other_driver_gets_404_on_every_move` | ✅ |
| Cancellation rules | `trip/tests/test_lifecycle.py` | passenger cancel in `DRIVER_ARRIVED` → 409; cancel in `MATCHED` frees the seat and removes the stops | `test_passenger_cannot_cancel_after_driver_arrived`, `test_cancel_in_matched_frees_the_seat_and_the_stops` | ✅ |
| Idempotent settlement | `fare/tests/test_settlement.py` | same event twice → one fare, one debit | `test_same_event_twice_one_fare_one_debit` | ✅ plus two **different** events for one ride, and 5 racing copies |

**The race tests, 50 times** (the plan: "run `--count 50` to shake out flakiness"): `150 passed` (3 race tests × 50), again today, with no flakes.

**One difference from the plan's note:** it says the concurrency test's database is made with `Base.metadata.create_all`. Ours is made by **running the real migrations** (every test in every service does that). So the race is tested against exactly the tables production has, including the partial unique indexes and CHECK rules that `create_all` and the migrations could, in principle, disagree on.

### One command for everything: `scripts/test_all.py`

Every service's tests, plus the shared library's (new in 8.6), plus optionally the race repeat:

```
.venv\Scripts\python scripts\test_all.py --race 50
```
```
ok    libs/common                                11 passed
ok    services/gateway                           65 passed
ok    services/identity                          94 passed
ok    services/matching                          127 passed
ok    services/trip                              341 passed
ok    services/fare                              151 passed
ok    services/notification                      99 passed
ok    services/trip (race x50)                   150 passed

1038 tests passed
```

No Docker needed: each suite fakes what it doesn't own. The **real** system is checked by `scripts/e2e.py` (8.4). Between them, every PRD requirement is tested both **in isolation** (fast, every edge case) and **for real** (slow, the whole path).

---

## 8.6: observability

The plan asks for three things. The first was only half there; the other two are now checked for real.

### 1. "JSON logs with `request_id`": now true for every line

**What existed:** the gateway created a request id and passed it on in the `X-Request-Id` header, services passed it on to their own outgoing calls, and error answers included it. **What was missing:** no log line ever **carried** it. Part 1's JSON formatter could print a `request_id`, but only if each log call passed one, and none did. Uvicorn's own lines weren't even JSON. So you couldn't follow a request through the logs, which is the whole point.

**What I added**, all in Part 1's shared library, so every service got it by adding one line to its `main.py`:

| Piece | What it does |
|---|---|
| a **request context** (`RequestContextMiddleware`) | keeps the request's id for the whole request (from `X-Request-Id`, or new), and returns it in the response |
| `ContextFilter` | stamps that id onto **every** log line written during the request, from any code, without passing it around |
| **one access line** per request | `{"logger": "access", "msg": "POST /rides 201 121.7ms", "method", "path", "status", "duration_ms", "request_id"}`. The path is logged **without** its query string (it can hold secrets). `/health` lines go to DEBUG, because Docker asks every 5 s |
| uvicorn's lines | through the same JSON formatter; its plain-text access line is switched off (the new one replaces it) |
| outgoing calls | `ServiceClient` sends the current id even if a caller forgot to pass it |
| error answers | `error.request_id` is filled even when the id was made by this service |
| the bus | while an **event** is handled, every log line carries its **`event_id`** (both the durable and the per-copy queues) |

The gateway now uses the middleware's id instead of making its own, so the id in the gateway's logs, the response header, and every downstream service is the same one.

**Checked on the real system.** Rafiq requested a ride with `X-Request-Id: trace-8-6-demo`; then every log line of every service carrying that id:

```
trip-service  httpx   HTTP Request: POST http://fare:8004/internal/quotes "HTTP/1.1 201 Created"
trip-service  httpx   HTTP Request: POST http://matching:8002/internal/match/evaluate "HTTP/1.1 200 OK"
trip-service  access  POST /rides 201 121.7ms
matching      access  POST /internal/match/evaluate 200 7.1ms
fare          access  POST /internal/quotes 201 13.4ms
gateway       httpx   HTTP Request: POST http://trip:8003/rides "HTTP/1.1 201 Created"
gateway       access  POST /api/v1/rides 201 127.5ms
```

**One tap, four services, one id, with timings.** (No Fare → Matching line: that distance was still in Fare's 24-hour cache, which is correct.)

**Tested** in `libs/common/tests/test_observability.py` (11 tests, the shared library's first pytest suite):
- the gateway's id is kept, returned, and on a log line written deep inside the endpoint;
- a new id is made when none came in, the same one inside and outside;
- one JSON access line per request; **no query string in it**;
- errors carry the id in the body, the header and the log;
- health checks are quiet;
- WebSockets pass through untouched;
- **the id doesn't leak into the next request**;
- **outgoing calls carry it** without being told;
- event handlers log with the `event_id`;
- uvicorn's lines are JSON and its duplicate access line is gone.

All 877 service tests still pass with the change.

### 2. `/health` on every service

Done in Parts 2–7: database + RabbitMQ (+ Redis where used), 503 naming what's down. In Part 8 they drive Compose's boot order, and the gateway's `/health` checks all the others (8.1).

### 3. The alert-worthy signals: `scripts/alerts.py`

The plan names three. The script checks all three against the running system, prints `ok` / `ALERT`, and exits 1 on any alert, so a scheduler can run it every minute:

| Signal | Means | How it's checked |
|---|---|---|
| a **dead-letter queue** has messages | an event failed 3 retries and is parked; someone must look | RabbitMQ's API |
| an **outbox row unpublished > 30 s** | a service can't reach RabbitMQ; its news isn't going out | the outbox tables in Trip, Fare and Identity |
| a **`RATE_LIMITED` spike** | someone is hammering the API | the gateway's new access lines with status 429, last 5 minutes (threshold 20, adjustable) |

If a check **can't run** (RabbitMQ unreachable), that's reported as an alert too: silence must never look like "all fine".

**Each alert was made to fire for real:**

| Alert | How it was triggered | Result |
|---|---|---|
| dead-letter queue | **by accident, which was the best test**: I'd run Part 1's `check_events.py` against this stack's RabbitMQ. It publishes a fake `trip.ride.completed` (no quote) on the **real** exchange; Fare couldn't settle it, retried 3 times, and parked it | `ALERT fare.ride-lifecycle.dlq: 2 parked event(s)`. Read the parked messages (`error: 'quote_id'`, `attempt 4`), traced them to the check script, cleared them. That script now warns at the top not to run it against a live stack |
| rate limiting | 70 location pings from Jashim in a row (the limit is 60/min) | **exactly 60 accepted, 10 refused with 429**; `ALERT gateway: 10 RATE_LIMITED (429) in the last 5m` (with the threshold lowered to 5 for the demo) |
| stuck outbox | **stopped RabbitMQ**, had Jashim go online, waited 35 s | going online **still worked** (the change and its event were saved together); `ALERT identity: 1 event(s) unpublished for over 30 s`, and the dead-letter check reported that it couldn't check |

**Then RabbitMQ was started again**, which also tests one of 8.7's claims: the waiting event was published **14 s later on its own**, Trip received it with its **original** time (it says Jashim went online at 19:34:57, during the outage), **no service restarted** (they all reconnected by themselves), and the alerts went back to `ok`.

---

## 8.7: known limitations, and the way to scale

### The plan's table, checked against what was built

| Limitation now | Why it's fine for now | At 1M riders / 100k drivers | Checked here |
|---|---|---|---|
| **SQLite**: one container per service, one writer per database | correctness is simple and provable | Trip/Fare → PostgreSQL; the same `UPDATE … WHERE` compare-and-set works under row locks | the race tests (1 seat, 10 riders; 2 drivers, 1 ride) pass 50 times in a row (8.5) |
| **same-pickup-zone** pooling | a rule you can check by hand | nearby-cell pickups (H3), insertion over real travel times | the 140 % rule and the stop order are hand-checked in 4.4; Rafiq joins with G1 before M on the real system (8.4) |
| a driver accepts only the **first** rider | fewer round trips | a confirm window per rider, if drivers want it | Rafiq joins Bullet with no tap from Jashim (8.4, step 5) |
| a `REQUESTED` ride nobody accepts is **cancelled, not re-matched** | simple, visible | the sweeper asks Matching again before giving up | Shirin's 2-seat request stays `REQUESTED` with **no** driver offered (8.4, step 6) |
| the WebSocket has **its own port** | no WebSocket proxying in the gateway | one entry point (Traefik / Nginx) for TLS and upgrades | 8005 published beside 8000 (8.1) |
| **one Redis** | losing it only loses short-lived state | Redis Cluster, geo keys by region | see below: one more thing Redis holds |
| **one RabbitMQ** | the outbox means no event is lost if the broker restarts | quorum queues, 3 nodes | **proven on the real system** (8.6): RabbitMQ stopped, an event made meanwhile, published 14 s after restart, no restarts |

### Two more, found while running it

| Limitation | What happens | What to do |
|---|---|---|
| **RabbitMQ has no volume** in the compose file (the plan's, too) | `docker compose stop` keeps its data, but `docker compose down` **deletes** it. An event already **published** (so no longer in any outbox) but not yet **consumed** would be lost. The outbox only protects events that haven't left yet | give RabbitMQ a named volume (`/var/lib/rabbitmq`), or at least use `stop`, not `down`, while there's traffic |
| **Redis holds the "logged out" list** (Parts 2, 3, 7) | if Redis's data is lost (`docker compose down` deletes it, since Redis has no volume either; or a crash between its periodic snapshots), **logged-out tokens become valid again** until they expire (up to 1 hour). The plan's "loss only affects ephemeral state" is true for positions and rate counters, but not for this | acceptable with 1-hour tokens for an MVP. At scale: Redis persistence (AOF) for that key, or short tokens + refresh |

### Limitations found in earlier parts (all documented where they were found)

| Where | Limitation | Why it's fine for now |
|---|---|---|
| Trip (5.5) | a rider is offered only to drivers near **at request time**; a driver who comes online a minute later never sees her | the plan's "cancelled, not re-matched" row above |
| Trip (5.5) | "pooled" is decided when **each** ride completes, so a rider can pay the pooled price if her co-rider no-shows after she's dropped | rare with same-zone pickups (both are picked up before anyone is dropped) |
| Trip (5.7) | if a driver's very first "online" is delayed and his "offline" arrives first, Trip thinks he's online | only affects accepting offers, and offers come from Matching, which handles it |
| Fare (6.5) | a double-tapped top-up adds money twice unless the app sends an `Idempotency-Key` | simulated money; the gateway supports the key on every `POST` |
| Notification (7.3) | inbox ids come from SQLite's rowid; deleting the **newest** rows could make a catch-up skip a message | nothing deletes inbox rows; a clean-up job should delete old ones only |
| Notification (7.4) | one person's sockets are sent to one after another; a very slow phone delays their others | at scale: a small send queue per socket |
| Matching (4.x) | Fare caches distances for 24 h; changing a distance override needs `fare:dist:*` cleared | overrides are set by migration, rarely |

---

## Everything changed from the plan in Part 8, in one place

| Where | Change | Section |
|---|---|---|
| `docker-compose.yml` | RabbitMQ healthcheck `check_port_connectivity` (not `ping`); host ports as settings; a gateway healthcheck; no unused `PORT` | 8.1 |
| `scripts/e2e.py` | the plan's `e2e.sh` story in Python, **checking** every step, plus the live-channel and post-run checks; a GPS ping during the ride; tidy-up | 8.4 |
| Part 1 `logging.py`, `errors.py`, `http.py`, `events.py` + each service's `main.py` | request/event id on every log line, JSON access lines, JSON uvicorn lines, the id passed on automatically | 8.6 |
| `scripts/alerts.py` | the plan's three alert signals, checked | 8.6 |
| `scripts/test_all.py` | every suite in one command | 8.5 |
| `libs/common/tests/`, `pytest.ini` | the shared library's first pytest suite | 8.6 |
| `libs/common/checks/check_events.py` | a warning not to run it against the live stack | 8.6 |

---

## Things to know

- **The system is left running** on this machine, on the moved ports (gateway `http://localhost:18000`, WebSocket `ws://localhost:18005`, RabbitMQ UI `http://localhost:35672`). Stop it with `docker compose down` (add `-v` to wipe the data). Remember the other project (`mse-*`) holds the default ports while it's running.
- **Reset to a clean slate:** `docker compose down -v && docker compose up -d`. The e2e script can also just be run again: it tidies up after itself.
- **Wallets go down with every run** (Nusrat pays ৳72 each time; she starts with ৳500), so after ~6 runs on the same data her wallet payment would come back **FAILED** (step 10 would fail, correctly). `down -v` resets them.
- **To check the system after any change:** `scripts/test_all.py` (fast, no Docker), then `docker compose up --build -d` and `scripts/e2e.py`, then `scripts/alerts.py`.
- **To follow one request through the logs:** send it with `X-Request-Id: <something>` (or read the id from the response header), then `docker compose logs --no-log-prefix | grep '"request_id": "<something>"'`.
