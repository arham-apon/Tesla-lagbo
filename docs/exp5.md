# Step 5 Explained: the Trip & Pooling Service

## Progress

| Section | What it is | Status |
|---|---|---|
| 5.1 | Overview & domain scope | **Done** (explained below) |
| 5.2 | Directory structure | **Done** (explained below) |
| 5.3 | Data layer (tables, constraints, schemas) | **Done** (explained below) |
| 5.4 | State machine (which status can follow which) | Not started |
| 5.5 | Core logic (request, join, accept, transitions, sweeper) | Not started |
| 5.6 | API endpoints | Not started |
| 5.7 | Messaging integration | Not started |
| 5.8 | Lifespan (startup and shutdown) | Not started |
| 5.9 | Step-by-step build + tests | Not started |

This file grows as each section gets done.

---

## The big picture in 30 seconds

So far:
- **Gateway** (Part 2): the front door.
- **Identity** (Part 3): who everyone is, and whether Jashim is working.
- **Matching** (Part 4): the map, where the drivers are, and *advice* on which pool a rider fits in.

Part 5 builds the service that **actually books rides**. When Nusrat taps "Request", Trip:

1. asks **Fare** for a price,
2. asks **Matching** "is there a pool she could join?",
3. **puts her in the car**: one atomic database write that also checks the seat is still free,
4. records every status change (`REQUESTED → MATCHED → DRIVER_ARRIVED → STARTED → COMPLETED`) so anyone can later see *exactly what happened*,
5. tells everyone else through events (`trip.ride.*`, `trip.pool.updated`).

Matching *advises*, Trip *decides*. Trip is the service the whole system trusts about seats.

```
                          Trip & Pooling :8003
                 ┌──────────────────────────────────────────────────┐
Nusrat's phone ──┼▶ POST /rides, GET /rides/{id}, cancel             │──▶ Fare     "price Banani → Mohakhali"
 (via Gateway)   │                                                   │──▶ Matching "which pool fits?"
Jashim's phone ──┼▶ /driver/offers, accept, arrive/start/complete    │
                 │                                                   │
Identity ────────┼▶ GET /internal/drivers/{id}/live-pool             │    (may Jashim go offline?)
                 │   pools, rides, waypoints, history, offers        │
                 │                (trip.db)                          │──▶ outbox ──▶ RabbitMQ: trip.ride.*, trip.pool.updated
RabbitMQ ────────┼▶ identity.driver.online/offline, fare.ride.settled│
                 └──────────────────────────────────────────────────┘
```

---

## 5.1: what Trip owns and what it delegates

The plan says:

> **Owns:** ride requests, pools, pool membership (via `ride_requests.pool_id`), waypoints, status history (audit), driver offers, and a local projection of driver shifts. **This is the only service allowed to change seat counts.**
> **Delegates:** pricing (Fare), compatibility + driver search (Matching), user identity (JWT via gateway).

What each part means:

### Owns (1): ride requests

One row per booking: Nusrat, Banani → Mohakhali, 1 seat, cash. It holds the **status**, the chosen **quote**, the **estimated** fare (solo and pooled), and later the **final** fare once Fare settles it.

Two rules are enforced by the **database itself**, not just by code:
- **One active ride per passenger.** Nusrat can't have two rides `REQUESTED`/`MATCHED`/... at once (a partial unique index). A double-tap on "Request" can't create two bookings, even if the gateway's idempotency check (Part 2) were bypassed.
- **Pickup ≠ drop-off**, and **1–6 seats**. Same rule Matching checks at its door (4.4), so both services agree.

### Owns (2): pools

A **pool** is one shared trip in one car: Bullet, driven by Jashim, starting in Banani, `max_capacity` 4, `occupied_seats` 1.

- Status: `FORMING` (still accepting riders) → `IN_PROGRESS` (first rider picked up, closed to new riders) → `COMPLETED` or `CANCELLED`.
- **"Full" is not a status.** It's simply `occupied_seats = max_capacity`. Storing it would be one more thing that could disagree with the seat count.
- **One live pool per driver** (partial unique index): Jashim can't accidentally be driving two pools.
- The seat count can never go below 0 or above the car's capacity: a **CHECK rule in the database**. Even buggy code can't overbook Bullet.

### Owns (3): pool membership

There's no separate "members" table. A ride **belongs** to a pool by having `ride_requests.pool_id` set. One fact stored once, so "who's in Bullet?" and "which pool is Nusrat in?" can never disagree.

### Owns (4): waypoints

The ordered list of stops: `1. PICKUP Banani (Nusrat)`, `2. PICKUP Banani (Rafiq)`, `3. DROPOFF Gulshan 1 (Rafiq)`, `4. DROPOFF Mohakhali (Nusrat)`. **Matching** works out the order (4.4); **Trip** stores it and marks each stop done. Jashim's app shows this list.

### Owns (5): status history (the audit)

An **append-only** log: every change, who made it (passenger, driver or system), when, and why:

```
REQUESTED → MATCHED         SYSTEM     08:41:05  (joined Bullet)
MATCHED   → DRIVER_ARRIVED  DRIVER     08:47:12
DRIVER_ARRIVED → CANCELLED  DRIVER     08:52:40  PASSENGER_NO_SHOW
```

This answers the requirement "explain exactly what happened" when a rider complains. Rows are only ever **added**, never changed.

### Owns (6): driver offers

When no pool fits, Trip creates a new ride in `REQUESTED` and **offers** it to the nearby free drivers Matching found. `ride_offers` records who was offered what, and whether they accepted or declined. The first driver to accept gets it (atomic again). Nobody accepts within **180 s** (`RIDE_REQUEST_TTL_SECONDS`)? The **sweeper** cancels the request so Nusrat isn't left waiting forever.

### Owns (7): a local copy of driver shifts

To create a pool, Trip needs Jashim's name, Bullet's nickname and **seat capacity**. It doesn't call Identity for that. It keeps its own small table, `driver_shifts`, filled from Identity's `identity.driver.online` / `.offline` events.

**Why a copy:** if Trip called Identity on every accept, Identity being slow or down would block bookings. Copies can be slightly behind, which is fine: Jashim going online a second earlier doesn't change what capacity Bullet has.

### "The only service allowed to change seat counts"

This is **the** rule of Part 5. The race it prevents:

> Bullet has **1** seat left. Rafiq and Shirin both request at the same moment. Matching tells *both* "Bullet fits" (4.1: it's advisory and doesn't know about the other request).

Trip then does, for each:

```sql
UPDATE pools SET occupied_seats = occupied_seats + :seats, version = version + 1
WHERE id = :bullet AND status = 'FORMING' AND version = :version_matching_saw
  AND occupied_seats + :seats <= max_capacity
```

inside `BEGIN IMMEDIATE` (Part 1: SQLite's write lock). Only **one** of them can change the row; the other sees "0 rows updated", which means "someone got there first". That request is re-evaluated and goes to another pool or to driver offers. **The car is never overbooked**, and nobody else in the system writes seat numbers, so there's nothing to keep in sync.

### Delegates

| Question | Who answers it | How Trip uses it |
|---|---|---|
| What does Nusrat pay? | **Fare** (Part 6) | Trip asks for a **quote** (or checks the one the app already has) and stores the numbers on the ride |
| Which pool fits Rafiq, and in what stop order? Which drivers are near? | **Matching** (Part 4) | Trip sends a **snapshot** of its forming pools (with `version`) and gets ranked options back |
| Who is this user? Passenger or driver? | **Gateway + Identity** | The gateway checks the JWT and passes `X-User-Id` / `X-User-Role` (Part 2). Trip only reads those headers |
| Charging the wallet, driver earnings | **Fare** | Trip emits `trip.ride.completed`; Fare settles and replies with `fare.ride.settled` |
| Pushing "your driver has arrived" | **Notification** | Trip emits `trip.ride.status_changed`; Notification does the rest |

### Where Trip sits in the call chain

```
Identity ──▶ Trip ──▶ Fare ──▶ Matching
               └────────────────▶ Matching
```

Trip calls **Fare** and **Matching** over HTTP, and only **Identity** calls Trip (the live-pool check before Jashim goes offline). Nothing Trip calls ever calls back into Trip, so there's no circle of services waiting on each other (Part 0.2). Everything else Trip says to the world goes out as **events**, through the **outbox** (Part 1): the event is written in the **same transaction** as the change, so "Nusrat was matched" can never be saved without its event, or the other way round.

### No Redis

Unlike Identity and Matching, Trip uses **no Redis** (plan 5.7). All its correctness comes from SQLite's transactions. A Redis lock on top would be a second source of truth that could disagree with the database.

---

## What I did for 5.1

Like 2.1, 3.1 and 4.1, this section is a **scope definition** with no code of its own. I checked what Trip will depend on:

| Check | Result |
|---|---|
| Part 1 pieces Trip uses: `OutboxMixin`, `ProcessedEventMixin`, `emit`, `first_time`, `run_outbox_relay`, `Bus`, `InternalAuth`/`Principal`, `ServiceClient`, `DomainError`, `install_error_handlers`, `health_router`, `configure_logging`, `new_id`/`utcnow` | all import OK |
| `.env.example` has Trip's setting | `RIDE_REQUEST_TTL_SECONDS=180` |
| Identity's `identity.driver.online` carries what `driver_shifts` needs | yes: `driver_id`, `driver_name`, `vehicle_id`, `vehicle_nickname`, `seat_capacity` (`services/identity/app/routers/drivers.py`) |
| Identity already calls Trip | `GET /internal/drivers/{id}/live-pool`, expects `{"pool_id": ...}`, and treats anything but 200 as an error (so a Trip outage can't let Jashim go offline mid-trip) |
| Matching's `POST /internal/match/evaluate` accepts what Trip will send | `EvaluateIn`: `pickup_zone`, `dropoff_zone`, `seats`, `open_pools` (with `version`), `max_candidates` |
| Gateway routes Trip's URLs | `/api/v1/rides` and `/api/v1/driver` → `TRIP_URL` (`http://trip:8003`); `/api/v1/driver/location` and `/driver/earnings` still go to Matching and Fare, because the gateway tries the longest prefix first (Part 2) |
| **`respx`** (plan 5.9 step 3: mock Fare/Matching in tests) | wasn't installed; **installed `respx 0.23.1`** into `.venv` and listed it in `requirements-dev.txt` |

---

## Found while reading ahead (to fix in the right section)

**1. The whole-second timestamp bug (fixed).** Raised in 4.5: Part 1's `emit()` wrote `...08:41:05Z` when the time landed exactly on a second, and `...08:41:05.120000Z` otherwise, and **text comparison put the later one first**. Trip's consumer compares exactly like that (`if shift.state_ts >= ts: return`), so Jashim's "online" could have been ignored, and Trip would refuse to make him a pool. **Fixed** in `libs/common/tesla_common/events.py`: see *Fix: fixed-width event timestamps* below.

**2. Fare doesn't exist yet (5.5).** Trip calls Fare on every request, but Fare is Part 6. That's why the plan says to test the clients with `respx`: Fare's answers are faked in the tests, and a real Fare is only needed when running the whole system.

**3. `datetime.utcnow()` in the plan's `FareClient` (5.5).** It's deprecated in Python 3.12 and gives a "naive" time. It works as long as Fare's `expires_at` is also naive UTC; I'll use Part 1's `utcnow()` instead so there's one clock everywhere.

**4. Partial unique indexes and autogenerate (5.3).** The plan warns (5.9 step 1) that Alembic's autogenerate can miss `sqlite_where`. Both key rules of Part 5 (one live pool per driver, one active ride per passenger) are partial indexes, so the migration will be **hand-checked** and **tested** (insert a second active ride → rejected). *Done in 5.3: autogenerate kept both here, and a test now reads the real index SQL.*

---

## 5.2: the directory structure

### What I created

```
services/trip/
├── Dockerfile                  generic service Dockerfile, port 8003
├── requirements.txt            uvicorn + alembic (same as Matching)
├── requirements-dev.txt        pytest, pytest-asyncio, respx (no fakeredis: Trip has no Redis)
├── pytest.ini
├── alembic.ini
├── migrations/
│   ├── env.py                  same as Identity's and Matching's
│   └── versions/0001_init.py   all 8 tables (added in 5.3)
├── app/
│   ├── __init__.py
│   ├── config.py               settings
│   ├── deps.py                 db, auth, bus, fare_http, matching_http, settings
│   ├── models.py               placeholder: the 8 tables, code in 5.3
│   ├── schemas.py              placeholder: request/response shapes, code in 5.3
│   ├── state_machine.py        placeholder: allowed transitions, code in 5.4
│   ├── snapshots.py            placeholder: pool view + event payloads, code in 5.5
│   ├── pooling.py              placeholder: request / join / accept, code in 5.5
│   ├── lifecycle.py            placeholder: transitions + seat release, code in 5.5
│   ├── clients.py              placeholder: FareClient, MatchingClient, code in 5.5
│   ├── workers.py              placeholder: stale-request sweeper, code in 5.5
│   ├── consumers.py            placeholder: RabbitMQ consumers, code in 5.7
│   ├── main.py                 placeholder: app + lifespan, code in 5.8
│   └── routers/
│       ├── __init__.py
│       ├── passenger.py        placeholder: /rides...
│       ├── driver.py           placeholder: /driver/offers, /driver/pool, /driver/rides/{id}/...
│       └── internal.py         placeholder: /internal/drivers/{id}/live-pool
└── tests/
    ├── conftest.py             sets the environment before app.config is imported
    ├── test_state_machine.py   placeholder (5.4)
    ├── test_capacity.py        placeholder (5.5)
    ├── test_concurrency.py     placeholder (5.9)
    └── test_ownership.py       placeholder (5.6)
```

Each placeholder is a one-line docstring saying what goes there and in which section, as in 4.2.

### How the files map to 5.1's jobs

| 5.1 job | File |
|---|---|
| Rides, pools, waypoints, history, offers, driver shifts (the tables) | `models.py` |
| Which status can follow which, and who may do it | `state_machine.py` |
| Putting a rider in a car (the atomic seat write) | `pooling.py` |
| Everything after booking (arrive, start, complete, cancel, freeing seats, finishing the pool) | `lifecycle.py` |
| What Matching and Notification are told about a pool | `snapshots.py` |
| Talking to Fare and Matching | `clients.py` |
| Expiring requests nobody accepted | `workers.py` |
| The local copy of driver shifts, final fares | `consumers.py` |
| What the outside world can call | `routers/passenger.py` (Nusrat), `routers/driver.py` (Jashim), `routers/internal.py` (Identity only) |

As in Matching, routers are split **by who calls them**, so each file has one security rule: passenger routes need `PASSENGER`, driver routes need `DRIVER`, internal needs only the internal token.

The split between `pooling.py` and `lifecycle.py` follows the ride's life: **pooling** gets a rider *into* a pool (the tricky concurrent part), **lifecycle** moves them *through* it. Every write in both goes through the state machine, so there's one place that decides what's allowed.

### Decisions

| Plan says | What I did | Why |
|---|---|---|
| no `__init__.py` | added `app/` and `app/routers/` ones | relative imports need them (as in Parts 2–4) |
| migration env | **copied Matching's `env.py`, `alembic.ini` and `script.py.mako` unchanged** | they only refer to `app.config.settings` and `app.models.Base`. Same proven setup: batch mode for SQLite, and the URL override the tests use |
| `migrations/versions/0001_init.py` listed | **not created yet** | a migration without a revision id makes Alembic fail. It'll be written in 5.3 with the models (and hand-checked, see finding 4). `alembic heads` runs fine on the empty folder |
| `deps.py` (5.6 imports `fare_client`, `matching_client`; 5.8 imports `fare_http`, `matching_http`) | **`fare_http` and `matching_http` now**; `fare_client` / `matching_client` added in 5.5 | the typed clients live in `clients.py`, which is written in 5.5. Importing them now would break the import. The raw clients are what the lifespan closes on shutdown |
| no `requirements-dev.txt` for Trip | added, with **`respx`** instead of `fakeredis` | plan 5.9 step 3 tests the clients with `respx`; Trip has no Redis |

`config.py`: `DB_PATH` (default `trip.db`), `RABBITMQ_URL`, `INTERNAL_TOKEN` (required), `FARE_URL` (`http://fare:8004`), `MATCHING_URL` (`http://matching:8002`), **`RIDE_REQUEST_TTL_SECONDS=180`** (same default as `.env.example`), `LOG_LEVEL`. **No `REDIS_URL`**: Trip doesn't use Redis (5.1), and asking for a setting it never uses would only make it fail to start for no reason. It reads a local `.env` like the others.

The Dockerfile starts with `alembic upgrade head && uvicorn ... --port 8003`. Like Matching, there's no seed step: Trip starts empty and fills up from real requests and Identity's events.

### How 5.2 was checked

| Check | Result |
|---|---|
| `app.config` with only the required env vars | defaults as above (`trip.db`, Fare on 8004, Matching on 8002, TTL 180) |
| `app.deps` | builds `db`, `auth`, `bus`, and two HTTP clients pointing at Fare and Matching, each sending the internal token |
| every placeholder module imports | OK (13 modules) |
| `alembic heads` with no migrations yet | runs, no error |
| building `deps` doesn't create a database file | confirmed (`Database` connects lazily) |

No tests in 5.2; the first real ones came with the tables in 5.3.

---

## Fix: fixed-width event timestamps (the bug from 4.5)

**The change:** one line in Part 1's `emit()` (`libs/common/tesla_common/events.py`):

```python
"occurred_at": utcnow().isoformat(timespec="microseconds") + "Z",   # was: utcnow().isoformat() + "Z"
```

| Moment | Before | After |
|---|---|---|
| exactly 08:41:05 | `2026-09-24T08:41:05Z` | `2026-09-24T08:41:05.000000Z` |
| 120 ms later | `2026-09-24T08:41:05.120000Z` | `2026-09-24T08:41:05.120000Z` |
| earlier sorts first as text? | **no** | **yes** |

Every service's events now have the same width, so **text order = time order**. Trip's consumer (5.7) can use the plan's `state_ts >= ts` check as written.

**What else changed:**
- `services/matching/tests/test_consumers.py`: the test that **confirmed the bug** (`emit()` writes `...05Z`) now confirms the fix (`...05.000000Z`, and it sorts before `...05.120000Z`).
- `services/matching/tests/test_fleet.py`: comment only. Its helper deliberately builds **old-format** times, because Matching still has to handle them.
- **Matching's microsecond comparison (4.5) stays.** Events written before the fix may still be sitting in an outbox or a RabbitMQ queue, and the extra check costs nothing.
- Nothing parses `occurred_at` in a way the extra `.000000` could break: Identity's contract test reads it with `datetime.fromisoformat`, which accepts both.

**How it was checked:**

| Check | Result |
|---|---|
| `emit()` on a whole second, then 120 ms later | `...05.000000Z` < `...05.120000Z` as text: **True** |
| Matching tests | 127 passed |
| Identity tests | 94 passed |
| Gateway tests | 65 passed |
| Break-it: put the old line back | Matching: **1 failed** (the updated `test_consumers.py` test), so the fix is guarded |

---

## 5.3: the data layer

### Eight tables

| Table | One row is... | Rules the **database** enforces |
|---|---|---|
| `driver_shifts` | Trip's copy of a driver who's been online: name, Bullet's nickname, **seat capacity**, online or not | capacity 1–6 |
| `pools` | one shared trip in one car | capacity 1–6; **0 ≤ `occupied_seats` ≤ `max_capacity`**; status is one of the 4; **one `FORMING`/`IN_PROGRESS` pool per driver**; the driver must be in `driver_shifts` |
| `ride_requests` | one booking | 1–6 seats; pickup ≠ drop-off; status is one of the 6; `CASH`/`WALLET`; fare ≥ 0; **`MATCHED` or later must have a pool**; **one active ride per passenger**; the pool must exist |
| `pool_waypoints` | one stop (pickup or drop-off) | `seq` ≥ 1; `PICKUP`/`DROPOFF`; **no two stops share a place in line** (`pool_id`, `seq` unique) |
| `ride_status_history` | one status change (the audit) | must belong to a real ride; actor is `PASSENGER`, `DRIVER` or `SYSTEM` (**added**, see below) |
| `ride_offers` | "ride X was offered to driver Y" | one offer per ride per driver; `OFFERED`/`ACCEPTED`/`DECLINED` |
| `outbox`, `processed_events` | outgoing events / events already handled | from Part 1's mixins, unchanged |

All of it is the plan's code as written, plus one CHECK rule.

**Why so many rules in the database, not just in Python?** Because Part 5's promise is "**never overbooked, never in an impossible state**", and code has bugs. With `occupied_seats <= max_capacity` as a CHECK rule, even a buggy `UPDATE` that tries to squeeze a 5th rider into Bullet is **refused by SQLite** and rolled back. A test proves this with an `UPDATE` that has no version check at all.

### Two rules that are "partial" (and why that matters)

"One active ride per passenger" can't be a plain UNIQUE on `passenger_id`: Nusrat would then be allowed **one ride in her whole life**. It's a **partial** unique index, so only rows in an active status count:

```sql
CREATE UNIQUE INDEX uq_ride_one_active_per_passenger ON ride_requests (passenger_id)
WHERE status IN ('REQUESTED','MATCHED','DRIVER_ARRIVED','STARTED')
```

Same idea for pools: one **live** pool per driver, any number of finished ones.

The plan warns (5.9 step 1) that Alembic's autogenerate can **drop the `WHERE`**. That would turn both into plain UNIQUE indexes, and the second ride Nusrat ever booked would fail. It's a silent disaster, because the first ride in every quick test would still work. I **hand-checked** the migration (autogenerate kept both `WHERE`s this time) and added a test that reads the **real SQL** SQLite stored for each index.

### "Full" and "who's in the pool" are not stored

- **Full** = `occupied_seats = max_capacity`. It's not a status, so it can't disagree with the seat count.
- **Membership** = `ride_requests.pool_id`. There's no members table.

One fact, one place.

### The one change: `actor_role` has a CHECK rule

| Plan says | What I did | Why |
|---|---|---|
| `actor_role: String(10)`, any text | `CHECK (actor_role IN ('PASSENGER','DRIVER','SYSTEM'))` | the history is the audit ("explain exactly what happened"). A typo like `"DIRVER"` or an empty role would make an audit row useless, and nobody would notice until a complaint. The plan's code only ever writes these three (`state_machine.Actor`) |

### The migration: `0001_init.py`

Autogenerated from the models, then **hand-checked** (plan 5.9 step 1): all 8 tables, both partial indexes with their `WHERE`, every CHECK rule. `ck_history_actor` was added to the migration by hand, because Alembic's `check` can't see CHECK rules (found in 4.3). The `.gitkeep` is gone now that the folder has a file.

### `schemas.py`, as in the plan

| Shape | Used for | Rules |
|---|---|---|
| `RideCreate` | Nusrat's "Request" | zone codes shaped like `BANANI` (capital letters, digits and `_`, 2–30 characters); pickup ≠ drop-off; 1–6 seats; `CASH` (default) or `WALLET`; optional `quote_id` |
| `CancelIn` | cancel reason | default `changed_plans`, max 200 characters (the column's size) |
| `RideOut` / `RideDetailOut` | Nusrat's view of **her own** ride, + its history | read straight from database rows |
| `PoolOut`, `PoolRider`, `WaypointOut` | Jashim's view of his pool | names and stops, **no fares** |
| `OfferOut` | "new ride near you" | includes the estimated fare, so Jashim can decide |

`RideCreate` only checks that a zone code **looks** right. Whether `MOTIJHEEL` **exists** is Matching's and Fare's answer (422 `UNKNOWN_ZONE`), so the list of zones lives in one place.

**Privacy, as the PRD asks:** the driver's pool view has no fare fields at all, and the history a passenger sees says **that** the driver cancelled (`actor_role`), not the driver's user id. Both are guarded by tests, so adding a fare field to `PoolOut` later would fail a test.

### How 5.3 was checked: 74 tests, all passing

| File | Tests | What |
|---|---|---|
| `test_migrations.py` | 5 | exactly the 8 tables; **both partial indexes keep their `WHERE`** (read from SQLite's stored SQL); every CHECK rule is in the database; models and migration agree (`alembic check`); downgrade to nothing and back |
| `test_models.py` | 43 | every rule in the table above, with the demo cast. Among them: **Bullet with 3 of 4 seats taken refuses +2 even with no version check**; a double-tap "Request" is refused, but a new ride after a cancelled one is fine; a `MATCHED` ride without a pool is refused; a pool with riders can't be deleted; two stops can't share `seq` 1 |
| `test_schemas.py` | 26 | seats, zone shape (both fields), pickup ≠ drop-off, payment method, cancel reason length; **`RideOut` and `RideDetailOut` built from real database rows, exactly as 5.6 will do it**; no fares in the driver's view; no actor id in the passenger's history |

`alembic upgrade head` on a fresh file (what the Dockerfile runs) → `-> 0001, init`; `alembic current` → `0001 (head)`.

**Break-it checks** (broke the migration or model on purpose, ran the tests, restored):

| Deliberately broke... | Result |
|---|---|
| pool index loses its `WHERE` (the autogenerate risk) | 2 failed |
| ride index loses its `WHERE` | 2 failed |
| capacity CHECK removed | 4 failed |
| "matched needs a pool" CHECK removed | 5 failed |
| actor CHECK removed | 2 failed |
| model gains an index the migration lacks | 1 failed (`alembic check`) |

Run them with:

```
cd services\trip
..\..\.venv\Scripts\python -m pytest
```

### Found while doing 5.3 (for later sections)

- **The driver's cancel "`reason` required" (5.6 table) isn't enforced by `CancelIn`.** It has a default (`changed_plans`), which is right for passengers. For Jashim's `PASSENGER_NO_SHOW` cancel, 5.6 will need a separate shape with no default, or a check in the router.
- **`ride_offers.driver_id` has no link to `driver_shifts`**, on purpose (as in the plan): offers go to the candidates Matching found, and a missing shift row mustn't make creating the ride fail. The plan's `accept_offer` (5.5) already handles a missing shift: 409 `DRIVER_OFFLINE`.

---

## Things to know before the next sections

- **Build order:** the plan's recommended order (0.7) builds **Fare's quotes (Part 6) before Trip**, because Trip calls Fare on every request. This project follows the part numbers instead, so Fare doesn't exist yet. That's fine for building and testing Trip (Fare is faked with `respx`), but running Trip for real needs Fare's quote endpoints.
- **One data store:** `trip.db` (SQLite). No Redis. Run `alembic upgrade head` before starting (the Dockerfile does).
- **Changing a CHECK rule needs a hand-written migration** (as in Matching). `test_migrations.py` lists every rule by name, so a missing one fails a test.
- **Trip is the only writer of seat counts.** Every seat change is a compare-and-set on `pools.version` inside `BEGIN IMMEDIATE`. Matching's answers are only advice.
- **Must send `trip.pool.updated` with `driver_id`, `pool_id`, `status`** (and the rest of the registry fields). Matching's availability set depends on those three (4.7).
- **Must answer `GET /internal/drivers/{id}/live-pool` with `{"pool_id": ...}`.** Identity already calls it and blocks going offline on anything but a 200.
- **Event times are fixed-width now** (`...05.000000Z`), so Trip's consumer can compare `occurred_at` as text safely, as the plan does.
- **`respx` is installed** into `.venv` (`uv pip install respx`). On another machine: `uv pip install -r services/trip/requirements-dev.txt`.
