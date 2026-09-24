# Step 5 Explained: the Trip & Pooling Service

## Progress

| Section | What it is | Status |
|---|---|---|
| 5.1 | Overview & domain scope | **Done** (explained below) |
| 5.2 | Directory structure | **Done** (explained below) |
| 5.3 | Data layer (tables, constraints, schemas) | **Done** (explained below) |
| 5.4 | State machine (which status can follow which) | **Done** (explained below) |
| 5.5 | Core logic (request, join, accept, transitions, sweeper) | **Done** (explained below) |
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

**2. Fare doesn't exist yet (5.5).** *Handled in 5.5: Fare is faked in every test.* Trip calls Fare on every request, but Fare is Part 6. That's why the plan says to test the clients with `respx`: Fare's answers are faked in the tests, and a real Fare is only needed when running the whole system.

**3. `datetime.utcnow()` in the plan's `FareClient` (5.5).** It's deprecated in Python 3.12 and gives a "naive" time. *Fixed in 5.5, and it turned out to be a real crash, not just a deprecation. See 5.5.*

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

## 5.4: the state machine (`state_machine.py`)

### The ride's life

```
                 ┌──────────── CANCELLED ◀───────────┬──────────────────┐
                 │  passenger / system        passenger / driver      driver (no-show)
                 │                                   │                  │
 (new) ──▶ REQUESTED ──────────▶ MATCHED ──────────▶ DRIVER_ARRIVED ──▶ STARTED ──▶ COMPLETED
     passenger      system (joined a pool)    driver               driver       driver
                    or driver (accepted)
```

The whole rule is one small table in code: **7 allowed (from, to) pairs**, each with the actors allowed to make that move. Anything else gets **409 `INVALID_TRANSITION`**, with a message like `DRIVER_ARRIVED → CANCELLED is not allowed for PASSENGER`.

| From → To | Who may do it | In the story |
|---|---|---|
| (new) → REQUESTED | PASSENGER | Nusrat taps "Request" (a new row, so it's not in the table) |
| REQUESTED → MATCHED | SYSTEM or DRIVER | Nusrat auto-joins Bullet (SYSTEM), or Jashim accepts her offer (DRIVER) |
| REQUESTED → CANCELLED | PASSENGER or SYSTEM | Nusrat gives up, or the sweeper gives up for her after 180 s (`NO_DRIVER_FOUND`) |
| MATCHED → DRIVER_ARRIVED | DRIVER | Jashim is at the Banani pickup |
| MATCHED → CANCELLED | PASSENGER or DRIVER | either side backs out before pickup; the seat is freed |
| DRIVER_ARRIVED → STARTED | DRIVER | Nusrat is in the car |
| DRIVER_ARRIVED → CANCELLED | DRIVER only | Nusrat never came out (`PASSENGER_NO_SHOW`) |
| STARTED → COMPLETED | DRIVER | dropped at Mohakhali |

`COMPLETED` and `CANCELLED` are **final**: nothing leaves them.

### Rules worth noticing

- **Nusrat can't cancel once Jashim has arrived.** He drove there and is waiting, so walking away now counts as a no-show, and only Jashim can record that. (Plan 5.9 step 5 asks for exactly this test.)
- **Nobody can cancel a started ride.** Once Nusrat is in the car, the only way out is `COMPLETED`. A ride can't be "cancelled" halfway through Dhaka traffic, which would make the fare and the audit meaningless.
- **Only the driver moves a ride forward** (arrive, start, complete). A passenger can't mark her own ride complete.
- **No skipping steps**: `MATCHED → STARTED` without "arrived" is refused, so the history always shows the full story.
- **Repeating a move is refused**: a second "cancel" on a cancelled ride is 409, not silently "OK". (The gateway's idempotency key, Part 2, is what makes a *retried* request safe.)

### Where it's used

Everything after booking goes through **one** function, `lifecycle.transition()` (5.5), which calls `assert_transition()` **before** changing anything. The two ways to become `MATCHED` (auto-join, driver accept) are written as a guarded update instead (`... WHERE status = 'REQUESTED'`), which enforces the same `REQUESTED → MATCHED` row atomically. So the table is the single source of truth for what's allowed, and the code follows it.

The **actor** comes from the gateway's headers: a `DRIVER` role → DRIVER, anything else → PASSENGER, and no user at all (the sweeper) → SYSTEM. The same word is written to the audit (`ride_status_history.actor_role`), whose CHECK rule from 5.3 accepts exactly these three.

### What I did

**`state_machine.py` is the plan's code, unchanged.** It's already minimal and correct, so all the work went into proving it.

### How 5.4 was checked: 123 new tests (197 in total), all passing

`test_state_machine.py`:
- **The full grid (plan 5.9 step 2):** every status × every status × every actor = **108 cases**. The 10 allowed (from, to, actor) moves pass; the other 98 are refused with `INVALID_TRANSITION` / 409. The allowed list in the test is **written out by hand from the plan's table**, not copied from the code, so a change to either one gets noticed.
- The story rules above, each as its own named test (no cancelling after arrival, nothing after `COMPLETED`/`CANCELLED`, only the driver moves forward, no skipping, no repeats, unknown status or actor refused).
- **Every status can be reached** from `REQUESTED` (no dead rows in the table).
- **It agrees with 5.3:**
  - `ACTIVE_RIDE_STATUSES` (used by the "one active ride per passenger" index) is **exactly** the statuses that can still move.
  - The statuses match the database's `ck_ride_status` rule.
  - The actors match `ck_history_actor`.

  If someone adds a status in one place only, a test fails.
- **What the phone sees:** through Part 1's real error handler, a refusal comes back as HTTP **409** with `{"error": {"code": "INVALID_TRANSITION", "message": "DRIVER_ARRIVED → CANCELLED is not allowed for PASSENGER"}}` (the `→` arrives intact).

**Break-it checks:**

| Deliberately broke... | Result |
|---|---|
| passenger may cancel after the driver arrived | 3 failed |
| a started ride can be cancelled | 3 failed |
| passenger can complete a ride | 2 failed |
| the sweeper can't expire requests | 1 failed |
| added a shortcut `MATCHED → STARTED` | 3 failed |
| refusal returns 400 instead of 409 | 99 failed |

---

## 5.5: the core logic

Five files do the actual work. Here's Rafiq's evening, and which file handles each step:

```
Rafiq taps "Request" (Banani → Gulshan 1)
 │
 ├─ clients.py    FareClient: "price this"                     → a quote: distance, solo and pooled price
 ├─ pooling.py    save the ride as REQUESTED                   (a 2nd active ride → 409 ACTIVE_RIDE_EXISTS)
 ├─ pooling.py    snapshot of Banani pools that have room      → [Bullet: 3 seats left, version 1, stops]
 ├─ clients.py    MatchingClient: "does he fit?"               → "Bullet, version 1, plan B·B·G1·M"
 ├─ pooling.py    try_join: take the seat IF Bullet is still version 1 and has room
 │                   ├─ yes → MATCHED, stops rewritten, events       (done)
 │                   └─ no (someone got there first) → ask Matching again, up to 3 times
 └─ pooling.py    nothing fits → offers to nearby drivers, stays REQUESTED
                     └─ Jashim accepts → accept_offer: a new pool

Later: arrive / start / complete / cancel  → lifecycle.py   (checks the 5.4 state machine first)
Nobody accepted in 180 s                  → workers.py     (the sweeper cancels it: NO_DRIVER_FOUND)
What Matching and the phones are told     → snapshots.py   (pool view + trip.pool.updated)
```

### `pooling.py`: getting a rider into a car

**`try_join` is the heart of Part 5.** For each pool Matching suggested, in Matching's order, it runs **one** statement:

```sql
UPDATE pools SET occupied_seats = occupied_seats + :seats, version = version + 1
WHERE id = :pool AND status = 'FORMING' AND version = :version AND occupied_seats + :seats <= max_capacity
```

- **1 row changed** → the seat is Rafiq's. In the **same transaction**: his ride becomes `MATCHED`, the stops are rewritten to Matching's plan (with the `__new__` placeholder replaced by his real ride id), the audit gets `REQUESTED → MATCHED` by `SYSTEM`, and two events go into the outbox (`trip.ride.matched`, `trip.pool.updated`).
- **0 rows** → someone else changed Bullet since Matching looked (a seat went, or the stop order changed). Try the next suggestion. If none work, the answer is **STALE**, and `request_ride` asks Matching again with a fresh snapshot, **at most 3 times**. After that it falls back to offers.

Why it can't overbook (the plan's reasoning, now proven by tests):
1. `BEGIN IMMEDIATE` (Part 1): only one write transaction runs at a time; the second waits.
2. The seat check is **inside** the `UPDATE`, so it reads the latest committed number, not a copy from earlier.
3. `version` also protects the **stop order**: Matching's plan was built for version 1. If Shirin joined in between, the plan is out of date even if a seat is free.
4. The database CHECK from 5.3 is the last line of defence.

**`accept_offer`**: Jashim accepts Nusrat's offer. Checks, in order: he has an open offer (404), he's online (409), Bullet has enough seats for her party (409), and he has no live pool already (409, the unique index from 5.3). Then it makes a pool **sized from his shift** (4 seats), marks her `MATCHED` **only if she's still `REQUESTED`**, marks the offer `ACCEPTED`, and writes the two stops. If two drivers accept at the same moment, the second gets 409 `RIDE_NO_LONGER_AVAILABLE`, and **his half-made pool is rolled back** too.

### `lifecycle.py`: everything after booking

One function, `transition(ride, target, who)`, used by every arrive / start / complete / cancel, and by the sweeper:
1. **Load and check ownership.** Nusrat can only touch her own ride. Jashim can only touch rides in **his** pool. Anyone else gets **404, not 403**, so Rafiq can't even find out that Nusrat's ride id exists.
2. **Check the 5.4 state machine** (409 otherwise).
3. Change the status (guarded by the ride's `version`), and write the audit row.
4. **What it does to the pool:**
   - **STARTED**: pickup marked done. The pool goes `FORMING → IN_PROGRESS`, so it's **closed to new riders** once the first person is in the car.
   - **CANCELLED**: seats freed, the rider's stops removed, the rest renumbered. **Stops already done stay done.**
   - **COMPLETED**: seats freed, drop-off marked done.
   - **No active riders left**: the pool is `COMPLETED` (if anyone finished) or `CANCELLED` (if everyone backed out). Either way **Jashim is free**, and the `trip.pool.updated` event tells Matching so (4.7).
5. **Events**: `trip.ride.cancelled` / `completed` / `status_changed`, plus `trip.pool.updated` whenever there's a pool.

**`pooled` (what Fare prices on):** a completed ride is "pooled" if its pool had **2 or more rides that weren't cancelled**. Rafiq and Nusrat both finish → both pooled. Rafiq cancels → Nusrat rode alone → **solo fare**.

### `workers.py`: the sweeper

Every 15 s, requests still `REQUESTED` after `RIDE_REQUEST_TTL_SECONDS` (180) are cancelled **through `transition()`** as `SYSTEM` with reason `NO_DRIVER_FOUND`. So they get the same audit row and event as any other cancel, and Nusrat's phone is told. A ride matched at the last moment is skipped (the state machine refuses `MATCHED → CANCELLED` for SYSTEM). If the database hiccups, the error is logged and the loop carries on.

### `snapshots.py` and `clients.py`

- **`snapshots.py`**: builds the pool view (riders, stops with done flags) and the `trip.pool.updated` payload from it, so the phones and Matching see the **same** picture. `replace_waypoints` rewrites the stop list while **keeping done stops done**.
- **`clients.py`**: `FareClient` gets a new quote, or checks the one the app already has (it must belong to **this** passenger, **this** route and seat count, and not be expired). `MatchingClient` sends the evaluate request. `deps.py` now provides both (as promised in 5.2).

### Two fixes in `clients.py`

| Plan's code | Problem | Fix |
|---|---|---|
| only 5xx from Fare/Matching become an error | a **401** (wrong internal token) or **404** is parsed as a quote → **crash, 500**. The same trap Identity hit in Part 3 | anything other than 200/201 (after the 404/422 the code expects) → **503 `UPSTREAM_ERROR`** |
| `if q.expires_at < datetime.utcnow()` | if Fare ever sends `expires_at` with a timezone (`...Z` or `+06:00`), Python **refuses to compare** a time-with-zone and a time-without → `TypeError`, **500** on every ride request | convert Fare's time to naive UTC first, and use Part 1's `utcnow()` (one clock everywhere) |

### One fix outside Trip's app code: the sweeper's log was silenced in tests

A test that breaks the database on purpose found that the sweeper's "iteration failed" message **never appeared**. The cause: `migrations/env.py` calls Python's `fileConfig(...)`, which by default **switches off every logger that already exists**. Tests run migrations in the same process as the app, so `trip.sweeper` went silent. **Fix:** `fileConfig(..., disable_existing_loggers=False)` in Trip's `env.py`. In Docker, migrations run as a separate command, so production wasn't affected. But a silently-failing sweeper is exactly the kind of thing you want to see in test logs.

(Matching and Identity use the same `env.py` line. Their loggers aren't tested this way, so I left them alone. It's the same one-word change if it's ever needed.)

### What the plan's code does as-is (known limitations, not changed)

- **No candidates → the ride waits 180 s for nothing.** Offers are only sent at request time, so a driver who comes online a minute later is never offered Nusrat's ride, and a pool formed later can't pick her up. The sweeper then cancels her. The plan lists this in 8.7 ("stale `REQUESTED` rides are cancelled, not re-matched"). It's a product decision, so I left it.
- **"Pooled" is decided when each ride completes.** If Nusrat completes while Rafiq is still `MATCHED` and waiting, she's counted as pooled, even if Rafiq later becomes a no-show. That's rare with same-zone pickups (both are picked up at Banani before anyone is dropped), but possible.

### How 5.5 was checked: 79 new tests (276 in total), all passing

| File | Tests | What |
|---|---|---|
| `test_pooling.py` | 21 | the snapshot is exactly what Matching's `OpenPool` expects; **Rafiq joins Bullet** (seats 1→2, version 1→2, stops B·B·G1·M with his real id, audit, 2 events); a stale version changes **nothing**; **no joining once the car has left** (`IN_PROGRESS`), even with the right version; the next suggestion is tried; a ride cancelled mid-matching rolls the seat back; `request_ride`: auto-join, own quote, offers + `trip.ride.requested`, 2nd active ride 409, stale → re-ask, **gives up after exactly 3**; `accept_offer`: all 5 refusals, and a late second driver leaves no pool behind |
| `test_lifecycle.py` | 13 | **the whole story** (arrive, start closes the pool, Rafiq drops at G1 first, Nusrat last, pool `COMPLETED`, both `pooled`); the audit tells it; **passenger cancel after arrival → 409, nothing changed**; cancel in `MATCHED` frees the seat and the stops (plan 8.5); no-show; last rider out dissolves the pool; Jashim can take a new pool afterwards; `pooled` false when alone or when the co-rider cancelled |
| `test_ownership.py` | 5 | Rafiq can't cancel Nusrat's ride; Karim can't touch Jashim's riders; a driver can't act on a ride before it's in his pool; **"not yours" looks exactly like "doesn't exist"** |
| `test_capacity.py` | 5 | plan 8.5's two checks; then **a whole evening**: after **every** step, `occupied_seats` equals the seats of the riders actually booked (fill to 4, disappear from the snapshot, cancel, reopen, start, complete, 0) |
| `test_concurrency.py` | 3 | **the plan's last-seat race** on a real file (Nusrat vs Shirin: exactly one `joined`, one `stale`, 3 of 3 seats); **10 riders, 1 seat → exactly 1 wins**; two drivers accept the same ride at once → one pool |
| `test_workers.py` | 5 | old request → `CANCELLED` / `NO_DRIVER_FOUND` / by `SYSTEM`, with event; a young one is kept; matched rides never expire; the whole batch is processed; a broken database is logged and survived, and the loop stops when asked |
| `test_clients.py` | 24 | with `respx`: quote sent with the internal token and request id; unknown zone; the app's quote (not found, **someone else's / wrong route / wrong seats**, expired); **expiry with `Z` and `+06:00`** (the fix); **400/401/403/404 → 503** (the fix); Fare down / 500; Matching ok / 422 / 401 / down |
| `test_events_contract.py` | 3 | an evening that produces **all 6 event types**; each has **exactly** the fields in the plan's registry (0.4), since Matching, Fare and Notification depend on them; fixed-width `occurred_at`; done stops stay done when the route changes |

**Break-it checks** (broke the code on purpose, ran all tests, restored):

| Deliberately broke... | Result |
|---|---|
| `try_join` without the seat check in its `UPDATE` | 1 failed (the database CHECK would still stop it, but as a crash instead of "try the next pool") |
| `try_join` without the version check | 3 failed |
| `try_join` may join an `IN_PROGRESS` pool | **0 failed at first** → added "no joining once the car has left" → now 1 fails |
| cancel/complete doesn't free seats | 5 failed |
| no ownership check | 4 failed |
| 1 attempt instead of 3 | 2 failed |
| "pooled" counts cancelled co-riders | 1 failed |
| sweeper expires young requests | 1 failed |
| client fix reverted: 4xx parsed as a quote | 6 failed |
| client fix reverted: timezone expiry | 4 failed |
| accepting doesn't mark the offer `ACCEPTED` | 1 failed |

---

## Things to know before the next sections

- **Build order:** the plan's recommended order (0.7) builds **Fare's quotes (Part 6) before Trip**, because Trip calls Fare on every request. This project follows the part numbers instead, so Fare doesn't exist yet. That's fine for building and testing Trip (Fare is faked with `respx`), but running Trip for real needs Fare's quote endpoints.
- **Fare and Matching errors reach the phone as 503** (`UPSTREAM_ERROR` / `UPSTREAM_UNAVAILABLE`), or 422 for zone/quote problems. The ride is **not** created when Fare fails, because the quote comes first.
- **Every ride status change after booking must go through `lifecycle.transition()`** (5.5), which checks the state machine first. Writing `ride.status = ...` anywhere else would skip the rules.
- **One data store:** `trip.db` (SQLite). No Redis. Run `alembic upgrade head` before starting (the Dockerfile does).
- **Changing a CHECK rule needs a hand-written migration** (as in Matching). `test_migrations.py` lists every rule by name, so a missing one fails a test.
- **Trip is the only writer of seat counts.** Every seat change is a compare-and-set on `pools.version` inside `BEGIN IMMEDIATE`. Matching's answers are only advice.
- **Must send `trip.pool.updated` with `driver_id`, `pool_id`, `status`** (and the rest of the registry fields). Matching's availability set depends on those three (4.7).
- **Must answer `GET /internal/drivers/{id}/live-pool` with `{"pool_id": ...}`.** Identity already calls it and blocks going offline on anything but a 200.
- **Event times are fixed-width now** (`...05.000000Z`), so Trip's consumer can compare `occurred_at` as text safely, as the plan does.
- **`respx` is installed** into `.venv` (`uv pip install respx`). On another machine: `uv pip install -r services/trip/requirements-dev.txt`.
