# Step 4 Explained: the Location & Matching Service

## Progress

| Section | What it is | Status |
|---|---|---|
| 4.1 | Overview & domain scope | **Done** (explained below) |
| 4.2 | Directory structure | **Done** (explained below) |
| 4.3 | Data layer (zones, distances, schemas) | **Done** (explained below) |
| 4.4 | Matching algorithm (the pool planner) | **Done** (explained below) |
| 4.5 | Fleet state in Redis (where drivers are, who's free) | **Done** (explained below) |
| 4.6 | API endpoints | **Done** (explained below) |
| 4.7 | Messaging integration | **Done** (explained below) |
| 4.8 | Step-by-step build + tests | **Done** (explained below) |

**Part 4 is complete.** Location & Matching is written and covered by 127 automated tests, all passing. Run them with:

```
cd services\matching
..\..\.venv\Scripts\python -m pytest
```

---

## The big picture in 30 seconds

So far:
- **Gateway** (Part 2): the front door.
- **Identity** (Part 3): who everyone is, and whether Jashim is working.

Part 4 builds the service that knows **the map**, and uses it to answer two questions whenever someone asks for a ride:

1. **"Is there a pool she could join?"** Nusrat is already in Bullet going Banani → Mohakhali. Can Rafiq (Banani → Gulshan 1) squeeze in without making Nusrat's trip much longer?
2. **"If not, which drivers are nearby and free?"**

It **answers** these questions but **doesn't act** on the answer. Trip does the actual booking.

```
                       Location & Matching :8002
                ┌──────────────────────────────────────────────┐
Jashim's phone ─┼▶ POST /driver/location  (GPS ping)           │──▶ Redis GEO "where is everyone"
  (via Gateway) │                                              │──▶ Redis Pub/Sub loc:pool:{id} ──▶ Notification ──▶ Nusrat's map
                │  9 zones + distance table   (matching.db)    │
Trip ───────────┼▶ POST /internal/match/evaluate               │──▶ "join pool X in this order" / "ask Jashim"
Fare ───────────┼▶ GET  /internal/zones/distance?from=&to=     │──▶ 3500 m
                │                                              │
RabbitMQ ───────┼▶ identity.driver.online/offline, trip.pool.updated ──▶ keeps "who is free" up to date
                └──────────────────────────────────────────────┘
```

---

## 4.1: what Matching owns and what it delegates

The plan says:

> **Owns:** the zone list, zone-to-zone distances, live driver positions (Redis GEO), driver availability set, and the **matching algorithm** (pool compatibility + candidate driver search).
> **Delegates:** seat reservation (Trip). Matching is **advisory and stateless per call**: it ranks options; Trip performs the atomic write. Matching never reads `trip.db`; Trip sends the open-pool snapshot in the request.

What each part means:

### Owns (1): the zone list, "the map"

- Dhaka is simplified to **9 zones**: Banani, Gulshan 1, Gulshan 2, Mohakhali, Farmgate, Dhanmondi, Mirpur, Uttara and Bashundhara. Each has a name and a centre point (lat/lng).
- Riders pick a **pickup zone** and a **drop-off zone**, not an exact address. That's decision A3 in the plan: *no map API*. So no Google Maps bill, no network call, and every distance can be checked by hand.
- `GET /zones` is **public** (the gateway lets it through without a login, Part 2), so the app can show the zone list on the sign-up screen.

### Owns (2): zone-to-zone distances

- Every other service that needs "how far is Banani → Mohakhali?" asks Matching. **Fare** needs it to price a ride (Part 6) and caches the answer in Redis for 24 h. **Trip** gets it in every evaluate answer (`solo_distance_m`).
- Most distances are calculated: straight-line distance × **1.3** (roads wind), rounded to 100 m.
- Three distances are **fixed by hand** so the demo story has round numbers you can check in your head:

  | From | To | Distance |
  |---|---|---|
  | Banani | Mohakhali | 3,500 m |
  | Banani | Gulshan 1 | 2,000 m |
  | Gulshan 1 | Mohakhali | 2,000 m |

**Why one owner:** if Fare and Trip each calculated distances, a rounding difference would mean Nusrat is *priced* for one distance and *routed* for another. One table, one answer.

### Owns (3): live driver positions

- Jashim's phone sends a **GPS ping** every few seconds: `POST /driver/location {lat, lng}`.
- Matching stores it in **Redis GEO** (`geo:drivers`), a Redis structure made for "find everything within 3 km of this point, nearest first".
- It also works out **which zone** he's in (the nearest zone centre), and refreshes a **30-second heartbeat** key. If his phone goes quiet (tunnel, dead battery, app killed), the heartbeat expires, and he's no longer offered rides even though he's still "online" in Identity.
- If Jashim is carrying passengers, each ping is also **broadcast** on Redis Pub/Sub channel `loc:pool:{pool_id}`. Notification forwards it to Nusrat's and Rafiq's phones, so they see Bullet moving.

**Why Redis and not the database:** a ping every few seconds from every driver is a lot of writes, and only the **latest** position matters. Redis keeps it in memory. If a ping is lost, the next one arrives seconds later. SQLite (one writer at a time, Part 1) would be the wrong tool.

**Why Pub/Sub and not RabbitMQ for positions:** RabbitMQ is for events that **must not be lost** ("fare settled"). A position is out of date after 5 seconds anyway. The plan's rule (Part 0.3): *high-frequency, loss-tolerant → Redis Pub/Sub.*

### Owns (4): the "available drivers" set

`drivers:available` is the set of drivers who are **online *and* not in a pool**. Matching can't know that by itself, so it builds the set from **events**:

| Event | From | Effect on Jashim |
|---|---|---|
| `identity.driver.online` | Identity (Part 3.5) | add to available (unless he's in a pool) |
| `identity.driver.offline` | Identity | remove from available, remove from the map |
| `trip.pool.updated`, status `FORMING` / `IN_PROGRESS` | Trip | he's busy: remove from available |
| `trip.pool.updated`, pool finished | Trip | free again: add back (if still online) |

This set is a **copy** built from other services' events. It can be a fraction of a second behind the truth. That's fine because of the next point.

### Owns (5): the matching algorithm

When Rafiq requests Banani → Gulshan 1, Trip sends Matching:
- Rafiq's request (pickup, drop-off, seats),
- a **snapshot of the pools currently forming** (Bullet: pickup Banani, 2 seats left, Nusrat dropping at Mohakhali).

Matching then:
1. **Filters pools** by the pooling rule (plan decision A4): **same pickup zone**, **enough seats left**, and **nobody's ride gets longer than 140 %** of what it would be alone.
2. **Tries every place** to insert Rafiq's drop-off into the route and keeps the shortest one that breaks nobody's 140 % limit. The worked example in 4.4 shows Gulshan 1 *before* Mohakhali passing (Nusrat at 114 %), and Gulshan 1 *after* Mohakhali failing (Rafiq at 275 %).
3. **Ranks** the pools that fit (least extra driving first).
4. **Finds candidate drivers**: free, heartbeat alive, within **3 km** (`MATCH_RADIUS_M`) of the pickup zone, nearest first. That's for when no pool fits and a new one must be offered.

All numbers are **whole metres and integer maths** (`in_vehicle × 100 ≤ solo × 140`), with no floating-point surprises.

### Delegates: seat reservation belongs to Trip

This is the key idea of Part 4: **Matching is advisory.** It says *"Rafiq fits in Bullet, plan: B → G1 → M"*. It does **not** put Rafiq in the car. Trip does that, in its own database, in one atomic step that also checks the seats are still free.

Why split it this way:

- **Only one service can guard the seats.** Imagine Rafiq and Shirin both request at the same moment, and Bullet has one seat left. Matching would tell *both* "Bullet fits". If Matching reserved seats, two copies of the truth would have to agree. Instead **Trip** does `UPDATE pools SET seats = seats + 1 WHERE version = <the version Matching saw>` inside `BEGIN IMMEDIATE` (Part 1's write lock). The first one wins; the second finds the version changed and is re-evaluated. The car is **never overbooked**. That's why every pool in the snapshot carries a `version`.
- **"Stateless per call":** Matching keeps **no memory** of rides or pools between calls. Everything it needs about pools is **in the request**. The same question always gets the same answer, retrying is harmless, and it never gets out of sync with Trip.
- **"Never reads `trip.db`":** database-per-service (Part 0). If Matching read Trip's tables, Trip could never change its tables without breaking Matching. With the snapshot, the only contract is the JSON shape of `EvaluateIn`.

### What Matching does *not* do (summary)

| Question | Who answers it |
|---|---|
| Put Rafiq in Bullet / reserve the seat | **Trip** (atomic write) |
| What does Rafiq pay? | Fare (asks Matching for the distance) |
| Is Jashim allowed to work (online)? | Identity (Matching just follows the events) |
| Show Bullet moving on Nusrat's phone | Notification (Matching only publishes the position) |
| Which pools exist right now? | Trip (sends the snapshot each time) |

### Where Matching sits in the call chain

```
Identity ──▶ Trip ──▶ Fare ──▶ Matching
               └────────────────▶ Matching
```

Matching is at the **bottom**: it **calls nobody** over HTTP. So it can never be part of a circle of services waiting on each other (Part 0.2), and it's the **first** service to start in Part 8 (Fare and Trip need it).

---

## What I did for 4.1

Like 2.1 and 3.1, this section is a **scope definition** with no code, so I added no files to `services/matching/` and checked what Matching will depend on:

| Check | Result |
|---|---|
| Part 1 pieces Matching uses: `Bus` (consumer), `ProcessedEventMixin`, `InternalAuth`, `health_router`, `DomainError` | all import OK |
| `.env.example` has the two matching settings | `POOL_MAX_DETOUR_PCT=140`, `MATCH_RADIUS_M=3000` |
| Events Matching listens to carry what it needs | `identity.driver.online/offline` carry `driver_id` (Identity's tests guard this, 3.5); `trip.pool.updated` carries `driver_id`, `pool_id`, `status` in the Part 0.4 registry |
| **Can `fakeredis` stand in for Redis GEO?** Plan step 4.8.4 warns it "lacks full GEOSEARCH support on some versions" | **Yes, with version 2.38 (installed).** `GEOSEARCH ... WITHDIST` near Banani found the Banani driver and correctly left out a Mirpur driver ~4 km away. Pub/Sub works too. So the 4.5 tests can run **without Docker**, like Part 2 and 3 |
| Gateway routes Matching's URLs | `/api/v1/zones` (public) and `/api/v1/driver/location` (60/min) → Matching; `/internal/*` unreachable from outside (Part 2) |

---

## Found while reading ahead (to fix in the right section)

**1. Event-timestamp ordering can go wrong (4.5).** *Fixed in 4.5, together with two more ordering problems found there.* To ignore out-of-date events, `fleet.py` compares event times **as text** (`occurred_at > last`). Part 1's `emit()` writes times with Python's `isoformat()`, which **drops the fraction of a second when it's exactly zero**:

```
2026-09-24T08:41:05Z           ← event exactly on a whole second
2026-09-24T08:41:05.120000Z    ← event 120 ms LATER
text comparison says the later one is newer?  False
```

(`.` sorts before `Z`.) Checked on this machine. If Jashim went offline at exactly `…:05.000000` and came online 120 ms later, Matching could ignore the "online" and never offer him rides until his next status change. It's rare (about 1 event in a million lands on an exact second), but it would be very confusing when it happens. **Proposed fix:** always write 6 decimal places (`isoformat(timespec="microseconds")`) in Part 1's `emit()`, or compare real datetimes in `fleet.py`. I'll raise it in 4.5.

**2. `ProcessedEvent` table not used (4.3/4.7).** The plan's models define a `processed_events` table, but 4.7 says Matching doesn't need it (its Redis updates are safe to repeat). *Decided in 4.3: left out.*

---

## 4.2: the directory structure

### What I created

```
services/matching/
├── Dockerfile                  generic service Dockerfile, port 8002
├── requirements.txt            uvicorn + alembic (no password hashing here)
├── requirements-dev.txt        pytest, pytest-asyncio, fakeredis
├── pytest.ini
├── alembic.ini
├── migrations/
│   ├── env.py                  same as Identity's (models metadata, DB_PATH, batch mode, test override)
│   └── versions/
│       ├── 0001_init.py        tables: zones, zone_distances
│       └── 0002_seed_zones.py  the 9 zones + 3 distance overrides (both directions)
├── app/
│   ├── __init__.py
│   ├── config.py               settings
│   ├── deps.py                 db, auth, bus, settings
│   ├── models.py               tables (4.3)
│   ├── schemas.py              request/response shapes (4.3)
│   ├── geo.py                  DistanceTable (4.3)
│   ├── planner.py              placeholder: the pool planner, code in 4.4
│   ├── fleet.py                placeholder: Redis driver state, code in 4.5
│   ├── consumers.py            placeholder: RabbitMQ consumer, code in 4.7
│   ├── main.py                 placeholder: app + lifespan, code in 4.6 / 4.8
│   └── routers/
│       ├── __init__.py
│       ├── public.py           placeholder: GET /zones
│       ├── driver.py           placeholder: POST /driver/location
│       └── internal.py         placeholder: distance + match/evaluate
└── tests/                      (4.3, see below)
```

### How the files map to 4.1's jobs

| 4.1 job | File |
|---|---|
| The map (zones) + distances | `models.py`, `0002_seed_zones.py`, `geo.py` |
| The matching algorithm | `planner.py` |
| Live positions + "who is free" | `fleet.py` (Redis) + `consumers.py` (keeps it up to date from events) |
| What the outside world can call | `routers/public.py` (anyone), `routers/driver.py` (Jashim's phone), `routers/internal.py` (Trip and Fare only) |

The routers are split **by who calls them**, so each file has one security rule: public needs nothing, driver needs `DRIVER`, and internal needs the internal token for the whole router.

### Decisions

| Plan says | What I did | Why |
|---|---|---|
| `app/db.py` in the folder list | **`deps.py`** instead | Same contradiction as Part 3: the plan's own `routers/internal.py` does `from ..deps import auth, settings`. One `deps.py` in every service. It holds `db`, `auth`, `bus` and re-exports `settings` (the plan imports it from there) |
| no `__init__.py` | added `app/` and `app/routers/` ones | relative imports need them (as in Parts 2 and 3) |
| migration env | **copied Identity's `env.py` and `alembic.ini` unchanged** | they only refer to `app.config.settings` and `app.models.Base`, which Matching has too. The same proven setup, including batch mode for SQLite and the URL override for tests |

`config.py`: `DB_PATH` (default `matching.db`), `REDIS_URL`, `RABBITMQ_URL`, `INTERNAL_TOKEN` (required), **`POOL_MAX_DETOUR_PCT=140`** and **`MATCH_RADIUS_M=3000`** (the two numbers from `.env.example`, same defaults), `LOG_LEVEL`. It reads a local `.env` like Identity's.

The Dockerfile starts with `alembic upgrade head && uvicorn ... --port 8002`. Unlike Identity there's no `app.seed` step, because Matching's seed data (the zones) **is a migration**. See 4.3 for why.

---

## 4.3: the data layer

### Two tables

```
zones                               zone_distances   (only the hand-set exceptions)
  code  PK   e.g. "BANANI"            from_zone  FK → zones.code ┐ PK together
  name       "Banani"                 to_zone    FK → zones.code ┘
  lat, lng   centre point             distance_m  > 0
                                      from_zone <> to_zone
```

- **`zone_distances` holds only the 3 overrides** (×2 directions = 6 rows), not all 36 pairs. Every other distance is **calculated** by `geo.py` when asked. Nothing needs storing, and moving a zone's centre automatically updates all its distances.
- **The rules are in the database:** a distance can't be 0 or negative, can't go from a zone to itself, and must use real zone codes (foreign keys, enforced because Part 1 turns on `PRAGMA foreign_keys`).
- **Zone codes like `BANANI` are the ids**, not UUIDs. They are what every other service stores in `pickup_zone` / `dropoff_zone`, and they're readable in logs and events.

### No `processed_events` table (the one change)

The plan's `models.py` includes `class ProcessedEvent(ProcessedEventMixin, Base)`. But the plan's own 4.7 says: *"all Redis operations are set-based and state-timestamped, so replays are harmless; no `processed_events` write needed here."* Nothing in Part 4 ever reads or writes it. An empty table that looks important would mislead the next person ("why isn't the consumer using it? is that a bug?"), so I **left it out**, with a comment in `models.py` saying why. If Matching ever gets a consumer that *isn't* safe to repeat, adding it back is one line plus a migration.

### Zones as a migration, not a seed script

Identity's demo users are created by `seed.py`. Matching's zones are created by **migration `0002_seed_zones`** (as the plan says). The difference:
- demo users are **sample data**: nice to have, and a real deployment wouldn't want them;
- zones are **reference data**: Matching **doesn't work without them**. Every environment must have exactly the same 9 zones, so they're versioned with the schema.

The migration writes its data **out in full** instead of importing it from `app/`. A migration must do the same thing forever. If `app/` later changes or renames something, re-running `0002` on a fresh database must still produce the original 9 zones. Its `downgrade()` removes the rows again.

### The full distance table (hand-checkable)

Loaded from a freshly migrated database through `geo.py`. **Bold** = hand-set override; everything else = straight line × 1.3, rounded to 100 m:

| from \ to | Banani | G1 | G2 | Mohakhali | Farmgate | Dhanmondi | Mirpur | Uttara | Bashundhara |
|---|---|---|---|---|---|---|---|---|---|
| **Banani** | 0 | **2000** | 1000 | **3500** | 5600 | 7900 | 5400 | 12400 | 7100 |
| **Gulshan 1** | **2000** | 0 | 1700 | **2000** | 4800 | 7300 | 7400 | 14600 | 7400 |
| **Gulshan 2** | 1000 | 1700 | 0 | 2400 | 6000 | 8400 | 6400 | 12900 | 6400 |
| **Mohakhali** | **3500** | **2000** | 2400 | 0 | 3500 | 6000 | 6400 | 14500 | 8700 |
| **Farmgate** | 5600 | 4800 | 6000 | 3500 | 0 | 2500 | 7600 | 17100 | 12200 |
| **Dhanmondi** | 7900 | 7300 | 8400 | 6000 | 2500 | 0 | 8800 | 18700 | 14600 |
| **Mirpur** | 5400 | 7400 | 6400 | 6400 | 7600 | 8800 | 0 | 10100 | 11200 |
| **Uttara** | 12400 | 14600 | 12900 | 14500 | 17100 | 18700 | 10100 | 0 | 12700 |
| **Bashundhara** | 7100 | 7400 | 6400 | 8700 | 12200 | 14600 | 11200 | 12700 | 0 |

What this shows:
- **Symmetric:** A→B always equals B→A.
- **Shortest pair: 1,000 m** (Banani–Gulshan 2), so no two different zones are ever "0 m apart". That matters because the planner (4.4) divides by each rider's solo distance.
- **The overrides really change things.** The formula would give Banani→Mohakhali **2,300 m**, Banani→Gulshan 1 **2,300 m** and Gulshan 1→Mohakhali **1,500 m**. The plan replaces them with 3,500 / 2,000 / 2,000 so the demo story has round numbers: Nusrat alone = 3,500 m; with Rafiq dropped first = 2,000 + 2,000 = **4,000 m** → 4,000 / 3,500 = **114 %** (under 140 %).
- **A harmless quirk:** Banani → Gulshan 2 → Mohakhali (1,000 + 2,400 = 3,400 m) is *shorter* than the hand-set direct 3,500 m. Real roads do this too (the main road isn't always the shortest). It can't hurt anyone: the planner only measures the stops a pool actually visits, and passengers always pay for their solo distance (plan decision A7).

### `geo.py`

Copied from the plan, plus one function:
- `haversine_m()`: straight-line distance on the globe between two lat/lng points.
- `DistanceTable.get(a, b)`: 0 for the same zone, the override if there is one, otherwise the formula. An **unknown zone code → 422 `UNKNOWN_ZONE`** (either end, and it's case-sensitive: `banani` is not `BANANI`).
- `DistanceTable.nearest_zone(lat, lng)`: which zone centre is closest. It's how a GPS ping becomes "Jashim is in Banani" (4.5).
- **Added: `load_distance_table(session)`**, which reads both tables into a `DistanceTable`. Plan step 4.8.5 says the lifespan "loads zones + overrides into `app.state.dist`". This is that loading, written now so the tests can check that the migrated data produces the right table. The table is loaded **once at startup** and kept in memory: 9 zones, and they never change while running.

### `schemas.py`

Copied from the plan. These are the shapes of the requests, checked before any code runs:

| Schema | Used by | Rules |
|---|---|---|
| `LocationPing` | Jashim's phone | lat **23.60–23.95**, lng **90.30–90.55** (a box around Dhaka, containing all 9 zone centres). A ping from Chattogram, or `0,0` from a phone without a GPS fix → 422. `heading` 0–359 or empty |
| `EvaluateIn` | Trip | pickup + drop-off zone, `seats` **1–6**, the `open_pools` snapshot (default: none), `max_candidates` **1–20** (default 5) |
| `OpenPool` / `Stop` | Trip's snapshot | pool id, driver, pickup zone, `remaining_seats` ≥ 0, **`version`** (for Trip's atomic join, 4.1), and the stops (`PICKUP`/`DROPOFF`, zone, done or not) |
| `PoolOption`, `CandidateDriver`, `EvaluateOut` | Matching's answer | the ranked pools with their new route plan, and nearby free drivers |

`NEW_RIDE = "__new__"` marks the not-yet-created ride inside a proposed plan (Trip replaces it with the real ride id). Real ride ids are UUIDs, so it can never clash with one.

Note: `EvaluateIn` doesn't check that zone codes exist. `DistanceTable.get` does that (→ 422), because only it knows the zone list.

### How 4.2 and 4.3 were checked: 49 tests, all passing

Run them with:
```
cd services\matching
..\..\.venv\Scripts\python -m pytest
```

| File | Tests | What it proves |
|---|---|---|
| `test_migrations.py` | 5 | tables created; **the 9 zones match the plan's table exactly** (codes, names, coordinates); the 3 overrides exist **in both directions**; `alembic check`: models and migrations agree; downgrade to `0001` removes the zones but keeps the tables, down to `base` removes everything, and upgrading again works |
| `test_geo.py` | 13 | the plan's named checks (4.8 step 2): **`get("BANANI","MOHAKHALI") == 3500`**, **overrides symmetric**, **unknown zone → 422** (either side, wrong case too). Plus: every one of the 36 pairs is symmetric, ≥ 1,000 m and a multiple of 100; same zone = 0; Farmgate→Dhanmondi = formula = 2,500; `haversine` of 1° latitude = 111,195 m; each centre's nearest zone is itself; points near Banani / Gulshan 1 map correctly; the loader reads 9 zones + 6 overrides |
| `test_models.py` | 6 | the database refuses a 0 m distance, a zone-to-itself distance, an unknown zone, a second override for the same direction, and a duplicate zone code; accepts a valid one |
| `test_schemas.py` | 25 | pings inside the Dhaka box accepted (including exact edges), outside rejected (Chattogram, 0,0, just past each edge); heading range; seats 1–6; `max_candidates` 1–20, default 5; Trip's pool snapshot parses; negative seats rejected; stop kind only PICKUP/DROPOFF |

**Break-it checks:**

| Deliberately broke... | Result |
|---|---|
| inserted overrides in one direction only | 6 failed |
| road factor 1.3 → 1.4 | 1 failed |
| stopped checking the destination zone exists | 1 failed |
| typo in Uttara's latitude (moves it out of Dhaka) | 1 failed |
| **deleted the `from_zone <> to_zone` rule from `models.py`** | **0 failed** |

The last one is a real limitation worth knowing: **`alembic check` doesn't compare CHECK rules.** It notices new or removed tables, columns, indexes and foreign keys, but not a changed or deleted `CheckConstraint`. The tests still pass because the database is built by the migration, which still has the rule. So the *database* stays protected, but `models.py` and the migration can quietly disagree about CHECK rules. The same is true for Identity. The practical rule: **when you change a CHECK rule in `models.py`, write the migration by hand.** Autogenerate won't do it for you.

---

## 4.4: the matching algorithm (`planner.py`)

This is the brain of the pooling idea: **"can this new rider share that car, and in which order should the car drop everyone off?"**

### The rules (plan decision A4)

A new rider may join an open pool only if **all** of these hold:

1. **Same pickup zone.** Everyone in Bullet gets picked up in Banani. The car collects everybody, then drives the drop-offs. (That's also why pickups never need ordering: they're all in one place.)
2. **Enough seats left:** `remaining_seats ≥ seats wanted`.
3. **Nobody rides more than 140 % of their solo distance.** "Solo distance" = straight from the pickup zone to your own drop-off. "In-vehicle distance" = what you actually ride, including other people's drop-offs before yours. For **every** rider, old and new: `in-vehicle × 100 ≤ solo × 140`.

(A fourth rule, "the pool hasn't started yet", is Trip's: it only sends pools that are still `FORMING`.)

### How it works, step by step

For each open pool Trip sent (`plan_for_pool`):

1. Skip it if the pickup zone differs or there aren't enough seats.
2. Work out everyone's **solo distance** (the new rider's too).
3. Try putting the new drop-off **in every possible position** among the existing drop-offs: first, second, ..., last. **The existing riders keep their order**; only the newcomer is slotted in.
4. For each position, drive the route and check rule 3 for **every** rider.
5. Of the positions that pass, keep the one with the **shortest total route**.

Then `rank_pools` sorts all pools that fit: **least extra driving first**, and on a tie, the **lower worst-detour** first.

**Why integers only:** the 140 % test is written as `in_vehicle × 100 ≤ solo × 140`, with no division, so there's no floating-point rounding. Exactly 140 % is allowed; 140.1 % is not. That is checked by a test.

### The worked example, checked by the tests

**Rafiq (Banani → Gulshan 1) joins Nusrat (Banani → Mohakhali) in Bullet:**

| Order tried | Route | Rafiq rides | Nusrat rides | OK? |
|---|---|---|---|---|
| **G1 first** | B → G1 (2000) → M (+2000) = **4000** | 2000 / 2000 = **100 %** | 4000 / 3500 = **114 %** | ✔ |
| G1 last | B → M (3500) → G1 (+2000) = 5500 | 5500 / 2000 = **275 %** | 3500 / 3500 = 100 % | ✘ |

Result: `PICKUP Nusrat @Banani, PICKUP Rafiq @Banani, DROPOFF Rafiq @Gulshan 1, DROPOFF Nusrat @Mohakhali`, total 4,000 m, **+500 m** of extra driving, worst detour **114 %**. Exactly the plan's numbers.

**Shirin wants 2 seats:** Bullet (3 seats) now has 1 left → rejected on seats, before any distance is calculated.

### An extra example: a third rider (hand-checked)

After Rafiq joined, Karim wants Banani → **Gulshan 2** (1 seat; exactly 1 is left):

| Order tried | Route | Karim | Rafiq | Nusrat | OK? |
|---|---|---|---|---|---|
| **G2, G1, M** | 1000 → 2700 → **4700** | 100 % | 2700/2000 = 135 % | 4700/3500 = 134 % | ✔ |
| G1, G2, M | 2000 → 3700 → 6100 | 3700/1000 = 370 % | | | ✘ |
| G1, M, G2 | 2000 → 4000 → 6400 | 6400/1000 = 640 % | | | ✘ |

Karim is dropped **first**. Rafiq and Nusrat keep their order. Total 4,700 m (+700), worst detour 135 %. Bullet is now full (3/3).

### The one change: reject "pickup = drop-off" at the door (a real crash)

If a request had the **same pickup and drop-off** zone, the newcomer's solo distance is **0 m**, and the plan's planner does `in_vehicle × 100 // solo`. I ran the plan's code with such a request:

```
plan's planner with pickup == dropoff -> ZeroDivisionError integer division or modulo by zero
```

That would reach Trip as a bare **500**. Trip's own `RideCreate` already refuses pickup = drop-off (Part 5), so normally this can't happen. But Matching shouldn't rely on its caller being perfect. **Fix:** `EvaluateIn` now has **the same rule as Trip's `RideCreate`** (a `model_validator`), so such a request gets a clean **422** before the planner runs. The planner itself is **exactly the plan's code**, unchanged.

### What the planner deliberately does *not* do

- **It never reorders existing riders.** Nusrat was promised "Rafiq first, then you". A later rider can slot in, but never shuffle the others. That keeps each rider's experience predictable, and the search tiny (for 3 seats: at most 3 positions to try).
- **It doesn't look at where the driver is.** Pools are compared on route shape only. Finding *nearby drivers* is a separate step (4.5), used only when no pool fits.
- **It doesn't reserve anything.** It returns options; Trip books atomically (4.1). Every option carries the pool's `version`, so if two riders are offered the last seat at once, only one of Trip's writes succeeds.

### How 4.4 was checked: 17 new tests (66 in total), all passing

`test_planner.py` runs on the **real distance table** loaded from a migrated database, so the numbers are the ones production will use.

| Group | What it proves |
|---|---|
| **The plan's three (4.8 step 3)** | Rafiq accepted, **Gulshan 1 before Mohakhali**, total 4000 / +500 / 114 %, version passed through; the G1-last order computes to 5500 m with Rafiq at 275 % (never offered); **Shirin (2 seats, 1 left) rejected**; **different pickup zone rejected** (including a case, Gulshan 2 → Uttara, that the 140 % rule alone would *allow* at 139 %; only the same-pickup rule stops it) |
| More cases | the third-rider example above (4700 / +700 / 135 %); existing riders keep their order; same destination costs +0 m at 100 %; Uttara is rejected (145 % / 768 %); a stricter 110 % limit blocks Rafiq; a full pool is rejected; **exactly 140 % allowed, 140.1 % not** (on a small made-up map) |
| Ranking | least extra driving first; wrong-pickup pools left out; ties keep Trip's order; no open pools → empty list |
| Bad input | pickup = drop-off → 422 before planning; unknown drop-off zone → `UNKNOWN_ZONE`; all results are whole numbers |

**Break-it checks:**

| Deliberately broke... | Result |
|---|---|
| exactly 140 % no longer allowed (`>` → `>=`) | 1 failed |
| require a spare seat (`<` → `<=`) | 2 failed |
| ignore the pickup zone | 1 failed (after I strengthened the test, see below) |
| rank *most* extra driving first | 1 failed |
| only try the new drop-off in last position | 5 failed |
| plan without the pickup stops | 1 failed |
| allow pickup = drop-off | 1 failed |

At first, "ignore the pickup zone" was **not** caught. My test case (Gulshan 1 → Mohakhali into a Banani pool) happened to fail the 140 % rule anyway, so it never really tested the pickup rule. I added the Gulshan 2 → Uttara case, which passes the 140 % rule, and now the break is caught. That's exactly why these break-it checks are worth doing.

---

## 4.5: fleet state in Redis (`fleet.py`)

This file keeps Matching's live picture of the drivers: **where** each one is, **whether** they're reachable, and **who is free**.

### What's stored in Redis

| Key | Type | What | Written by |
|---|---|---|---|
| `geo:drivers` | GEO (a sorted set with positions) | every driver's last position, for "who's within 3 km?" | GPS pings; removed when he goes offline |
| `driver:{id}` | HASH | `lat`, `lng`, `zone`, `ping_ts` (from pings) + `online`, `pool_id` (from events) + `state_ts`, `pool_ts` (which events were applied, see below) | pings and events |
| `driver:{id}:hb` | STRING, **expires after 30 s** | the heartbeat: "his phone spoke recently" | every ping |
| `drivers:available` | SET | online **and** not in a live pool | events |
| `loc:pool:{pool_id}` | Pub/Sub channel | live position for the passengers in his pool | pings (only while he has a pool) |

### A GPS ping (`record_ping`), as in the plan

Jashim's phone sends `lat, lng`. In **one Redis transaction**: put him on the map, save position + zone (nearest zone centre, 4.3) + time, refresh the 30 s heartbeat, and read his `pool_id`. If he has passengers, **publish** the position on `loc:pool:{pool_id}`. Notification forwards it to Nusrat's and Rafiq's phones (Part 7).

A ping does **not** make him "available". Only Identity's "online" event does. An offline driver whose app keeps pinging stays invisible to matching.

### Nearby search (`nearby_available`), as in the plan

`GEOSEARCH` around the pickup zone's centre, radius 3 km, nearest first. It asks for **4 × the limit**, then keeps only drivers who are **in the available set *and* have a live heartbeat**, up to the limit. The heartbeat check means a driver who drove into a tunnel or whose phone died 30+ seconds ago isn't offered rides, even though Identity still says "online".

### Keeping "who is free" correct: what I changed, and why

The plan's `on_driver_online / offline / on_pool_updated` update Jashim's state when events arrive from Identity and Trip. There are **three** ways the plan's version could leave a wrong state behind. All three come down to **events not arriving in the order they happened**.

**Why events arrive out of order at all:**
- The consumer processes **up to 20 messages at the same time** (Part 1 sets prefetch 20, and I checked aio-pika's code: every delivered message gets its own task).
- A message whose handler fails goes to the **retry queue and comes back 5 s later** (Part 1). By then newer events have been handled.

**Problem 1: the whole-second timestamp (found in 4.1).** The plan compared event times **as text**. `08:41:05Z` (exactly on the second) sorts *after* `08:41:05.120000Z`, so a later event could be ignored as "older".

**Problem 2: check-then-write race.** The plan first *read* the last applied time, then *wrote* the change in a separate step. Two events handled at the same moment could both pass the check, and whichever wrote last won, even if it was the older one.

**Problem 3: pool events had no ordering at all.** `on_pool_updated` applied whatever came in. If `FORMING` failed once and came back from the retry queue *after* `COMPLETED`, Jashim would be marked **busy with a finished pool**, and never offered rides again until his next pool.

**The fix: one helper, `_apply_if_newer`,** used by all three event handlers:
1. It converts the event time to **whole microseconds** (a number, no text comparison). That fixes problem 1, and also handles `+06:00`-style times correctly.
2. It uses Redis **`WATCH` / `MULTI`**: read the driver's state, decide, write, all as one step. If anything else touched that driver in between (another event, a ping), Redis refuses the write and the helper simply re-reads and tries again. That fixes problem 2.
3. It keeps **two separate "last applied" clocks**: `state_ts` for Identity's online/offline events and `pool_ts` for Trip's pool events. An event older than the last one of *its own kind* is ignored. That fixes problem 3. They're separate because the two services' events are independent: an "offline" at 08:41:05.050 must still apply even if a pool event from 08:41:05.100 happened to be processed first.
4. After every change it **recomputes "available" from the result**: `online = 1` and no `pool_id` → in the set, otherwise out. Offline → also off the map. The plan updated the set piecemeal in separate steps.

It's also **safe to repeat**: the same event twice has the same time, so the second one is ignored ("at least as new was already applied"). That's why Matching needs no `processed_events` table (4.3).

**The plan's code vs. the fix**, running the same scenarios (the plan's `fleet.py` taken straight from the plan file):

| Scenario | Plan's version | Fixed |
|---|---|---|
| offline exactly on the second, online 120 ms later | ✘ stays unavailable | ✔ available |
| older "offline" arrives after a newer "online" | ✔ | ✔ |
| stale `FORMING` comes back from the retry queue after `COMPLETED` | ✘ stuck "busy" | ✔ available |
| online/offline events for 40 drivers handled all at once | **0 / 40** end in the right state | **40 / 40** |

(The last row uses fakeredis in one process. Real Redis would interleave differently, but the gap between "check" and "write" in the plan's version is the same.)

`on_pool_updated` now takes the event's `occurred_at` as an extra argument. `consumers.py` (4.7) will pass it along.

### How 4.5 was checked: 23 new tests (89 in total), all passing

`test_fleet.py` runs on **fakeredis** (4.1 showed it supports `GEOSEARCH`). The plan's step 4.8.4 suggests a real Redis container, but Docker is off, and fakeredis covers every command used here.

| Group | What it proves |
|---|---|
| Timestamps | the whole-second case sorts wrong as text, right as microseconds; `Z`, `+00:00` and `+06:00` for the same moment compare equal |
| Online / offline | online → available; offline → not available **and off the map**; the same event twice → ignored; **offline on the second + online 120 ms later → available**; an older offline arriving late → ignored; **40 drivers × 4 events in random order, all at once → every one ends in the newest state** |
| Pools | `FORMING` / `IN_PROGRESS` → busy; `COMPLETED` / `CANCELLED` → free again; **stale `FORMING` after `COMPLETED` → ignored** (not stuck); the end of an *old* pool doesn't free him from the *current* one; pool ends while he's offline → stays unavailable; a pool event before his online event still works; **a late offline still applies after a newer pool event** |
| GPS pings | zone, position, time and a heartbeat that expires in ≤ 30 s are recorded; with passengers → the exact JSON is published on `loc:pool:bullet-1` (Gulshan 1 coordinates → zone `GULSHAN_1`); without passengers → nothing published; pinging while offline doesn't make him available |
| Nearby search | nearest first (Banani ~50 m, then Gulshan 2 ~800 m); leaves out a driver 10 km away, a busy one, an offline one still pinging, and one whose heartbeat expired; respects the limit; empty map → empty list |

**Break-it checks:**

| Deliberately broke... | Result |
|---|---|
| replays applied again (`>=` → `>`) | 1 failed |
| went back to comparing times as text | 4 failed |
| let the end of *any* pool free the driver | 1 failed |
| kept offline drivers on the map | 1 failed |
| heartbeat never expires | 1 failed |
| nearby search ignores the heartbeat | 1 failed |
| made pool events share the online/offline clock | **0 failed at first** → added two tests (a pool ending after a late "online", and a late "offline" after a pool event) → now 2 fail |

### Same bug elsewhere: Trip (Part 5)

The plan's Trip consumer has the same text comparison: `if shift.state_ts >= ts: return`. So **Trip could also ignore Jashim's "online" if his previous "offline" landed exactly on a whole second.** Matching's fix doesn't depend on it, but the simplest cure for every service is **one line in Part 1's `emit()`**:

```python
"occurred_at": utcnow().isoformat(timespec="microseconds") + "Z",   # always 6 decimals
```

With a fixed width, text order = time order everywhere.

**Done (during Part 5):** `emit()` now writes 6 decimals always, e.g. `2026-09-24T08:41:05.000000Z`. Matching keeps its microsecond comparison anyway: events written in the old format may still be waiting in an outbox or a queue, and it costs nothing.

---

## 4.6: the API endpoints

### The four endpoints

The phone sees `/api/v1/...`; the gateway strips `/api/v1` and forwards (Part 2).

| Method | Route | Who | Answer | Errors | File |
|---|---|---|---|---|---|
| GET | `/zones` | anyone (no login) | 200 `[{code, name, lat, lng}]`, 9 zones sorted by name | — | `routers/public.py` |
| POST | `/driver/location` | DRIVER | **202** `{"zone": "BANANI"}` | 403 not a driver, 422 outside the Dhaka box | `routers/driver.py` |
| GET | `/internal/zones/distance?from=&to=` | other services (Fare) | 200 `{"distance_m": 3500}` | 422 `UNKNOWN_ZONE` | `routers/internal.py` |
| POST | `/internal/match/evaluate` | other services (Trip) | 200 `EvaluateOut` | 422 unknown zone / pickup = drop-off / bad seats | `routers/internal.py` |

As with Identity, **every** route needs the `X-Internal-Token`, so it must have come through the gateway or from another service. "Anyone" means *no login*, not *reachable directly from the internet*.

### `GET /zones`

Returns the 9 zones for the app's pickup and drop-off pickers. Nothing is read from the database per request: the list is loaded **once at startup** into `app.state.zones`.

**Small addition:** the plan says "from in-memory table", but the in-memory `DistanceTable` only holds codes and coordinates, **no names**. So `geo.py` got a second tiny loader, **`load_zones()`**, that the lifespan (4.8) will call next to `load_distance_table()`. It's sorted by name, so the app's list is alphabetical. There's also a `ZoneOut` schema, so the response shape is documented and checked.

### `POST /driver/location`

Jashim's phone sends `{lat, lng, heading?}` every few seconds. The endpoint:
1. checks the role (**drivers only**; Nusrat gets 403),
2. checks the point is inside the Dhaka box (4.3; 422 otherwise),
3. calls `record_ping` (4.5): map, zone, heartbeat, and broadcast if he has passengers,
4. answers **202 Accepted** with his zone.

**Why 202, not 200:** the position is "accepted and on its way". There's nothing to read back and nothing the phone must wait for. It's also the one route with a **60/min** gateway limit (Part 2), which allows a ping every second.

The ping time is written with **6 decimals always** (`isoformat(timespec="microseconds")`), so it never has the whole-second text problem from 4.5.

### `GET /internal/zones/distance`

Fare's question: "how far is Banani → Mohakhali?" → `{"distance_m": 3500}`. The parameter is called `from` in the URL, but `from` is a reserved word in Python, so the code names it `from_` with `alias="from"` (the plan's code).

### `POST /internal/match/evaluate`: the heart of Part 4

The plan's code, unchanged. For Trip's request it:
1. works out the rider's **solo distance** (this also rejects unknown zones with 422),
2. **ranks the open pools** Trip sent with the 140 % planner (4.4),
3. **searches for free drivers** within `MATCH_RADIUS_M` (3 km) of the **pickup zone's centre**, nearest first, up to `max_candidates` (4.5),
4. returns all three.

It returns candidate drivers **even when a pool fits**. Trip decides: it prefers joining a pool (plan decision A5: auto-join) and uses the driver list only when no pool fits.

**The Banani story through the real endpoints** (tested):

| Moment | Trip asks | Matching answers |
|---|---|---|
| Nusrat, Banani → Mohakhali, no pools yet; Jashim online, ~50 m away | evaluate, `open_pools=[]` | solo **3500**, no pools, candidates **[Jashim]** |
| Jashim accepted (pool `bullet-1` FORMING); Rafiq, Banani → Gulshan 1 | evaluate with Bullet's snapshot | solo **2000**, pool **bullet-1**, v1, **+500 m, 114 %**, plan B, B, G1, M; candidates **[]** (Jashim is busy) |
| Shirin, 2 seats; Bullet has 1 left | evaluate with Bullet's snapshot | **no pools** |

### A bug in Part 1, found by these tests (fixed)

Testing "pickup = drop-off → 422" (the rule added in 4.4) returned a **500**. The cause was in **Part 1's `tesla_common/errors.py`**, the shared error handler every service uses:

- When a `model_validator` rejects a request, pydantic puts the **actual `ValueError` object** into the error details (`ctx.error`).
- Part 1's handler put those details straight into a JSON response. Python's JSON encoder can't encode an exception object, so **the error handler itself crashed** → 500.
- Plain field errors (`seats: "two"`) have no such object, which is why Part 1's own checks and Identity's tests never hit it.

**Who it affects:** any service with a `model_validator`, i.e. Matching now, and **Trip's `RideCreate`** in Part 5 (the same pickup ≠ drop-off rule). Without the fix, Nusrat choosing the same zone twice would get "server error" instead of a clear message.

**Fix (one line + import):** pass the details through FastAPI's `jsonable_encoder`, exactly as FastAPI's own built-in handler does. The response is now a proper 422 with the message `"Value error, pickup_zone and dropoff_zone must differ"`.

Because this is Part 1 code, I:
- added this case to **`libs/common/checks/check_api.py`**. On the old `errors.py` it crashes (`TypeError: Object of type ValueError is not JSON serializable`); on the fixed one it passes;
- re-ran **every** suite: Part 1 checks OK, gateway **65**, identity **94**, matching **109**, all passing;
- noted the fix at the end of `exp1.md`.

Unlike the timestamp change (4.5), this one wasn't optional: without it, a 4.6 endpoint returns 500 for a normal user mistake.

### How 4.6 was checked: 20 new tests (109 in total), all passing

The routers read everything from `request.app.state`, so the tests build a small app with `app.state` filled exactly as the lifespan will (zones + distance table from a migrated database, fakeredis). The real `main.py` comes in 4.8.

| Endpoint | What's tested |
|---|---|
| `GET /zones` | 9 zones, Banani first with exact coordinates, sorted by name; works with **no user at all**; without the internal token → 401 |
| `POST /driver/location` | 202 + `{"zone": "BANANI"}` and the state lands in Redis (with a 6-decimal time); **passenger → 403**; Chattogram / 0,0 → 422; no internal token → 401 |
| `GET /internal/zones/distance` | Banani → Mohakhali = 3500; unknown zone → `UNKNOWN_ZONE`; missing `to` → 422; no token / wrong token → 401 |
| `POST /internal/match/evaluate` | **the Banani story** (table above); a driver in Uttara isn't a candidate for Banani; `max_candidates=2` returns the 2 nearest; unknown zone / pickup = drop-off / 7 seats → 422; no token → 401 |

**Break-it checks:**

| Deliberately broke... | Result |
|---|---|
| `/zones` without the gateway check | 1 failed |
| any logged-in user (not just drivers) may send pings | 1 failed |
| search for drivers around the **drop-off** instead of the pickup | 1 failed |
| ignore `max_candidates` | 1 failed |

---

## 4.7: messaging integration

The plan says:

> - **Produces (RabbitMQ):** none. **Produces (Redis Pub/Sub):** `loc:pool:{pool_id}` on every ping of a driver with a live pool.
> - **Consumes:** queue `matching.fleet-state`, bindings `identity.driver.*`, `trip.pool.updated`.

So Matching is the **mirror image of Identity** (Part 3.5): Identity only *sends* events, Matching only *receives* them.

### What it sends: positions, not events

Matching publishes **nothing** on RabbitMQ. Its only "message" is the live position on Redis Pub/Sub, done by `record_ping` (4.5) and tested there:

```
Jashim's ping ──▶ Matching ──PUBLISH loc:pool:bullet-1──▶ Redis ──▶ Notification ──▶ Nusrat's & Rafiq's maps
```

Nothing Matching decides needs to be announced. Its evaluate answers go straight back to Trip over HTTP, and *Trip* announces the resulting ride and pool changes.

### What it receives

```
RabbitMQ exchange tesla.events
   │  identity.driver.online / .offline   (Identity, Part 3)
   │  trip.pool.updated                   (Trip, Part 5)
   ▼
queue matching.fleet-state  ──▶  consumers.handle()  ──▶  fleet.py (4.5)  ──▶  Redis "who is free"
   │ on failure: .retry (5 s) ×3 ──▶ .dlq  (Part 1)
```

| Event | Handled by | Effect |
|---|---|---|
| `identity.driver.online` | `on_driver_online` | online; available if not in a pool |
| `identity.driver.offline` | `on_driver_offline` | offline; unavailable; off the map |
| `trip.pool.updated` | `on_pool_updated` | `FORMING`/`IN_PROGRESS` → busy; `COMPLETED`/`CANCELLED` → free (if it's his current pool) |
| anything else | ignored | (the bindings shouldn't let anything else in anyway) |

### `consumers.py`

The plan's code, with **one change**: it passes the event's **`occurred_at`** to `on_pool_updated` as well. That's the pool-event ordering from 4.5, which stops a stale `FORMING` from the retry queue from leaving Jashim stuck "busy". The queue name and bindings are now named constants (`QUEUE`, `BINDINGS`), so the tests can check them.

**The bindings, checked against the whole event registry (Part 0.4):** of the 9 routing keys in the system, `identity.driver.*` + `trip.pool.updated` let in **exactly** the 3 Matching needs. Not `trip.ride.*`, not `fare.ride.settled`. RabbitMQ's `*` means *exactly one word*, so `identity.driver.*` matches `identity.driver.online` but would not match a future `identity.driver.vehicle.changed`. That keeps Matching from silently receiving events it doesn't understand.

### No `processed_events`, and why that's safe

Most consumers in the system record each `event_id` so a redelivered event is skipped (Part 1's `first_time()`). Matching doesn't (4.3), and 4.5 is why that's safe: every handler **compares the event's time with the last applied one** and ignores anything not newer. The same event delivered twice has the same time, so the second copy changes nothing. There's a test for that: redeliver an already-applied "online" after the set was changed by something else, and the set stays as it is.

### Failures

If a handler raises (Redis down, or a malformed event missing `driver_id`), the exception reaches Part 1's `Bus.consume`, which sends the message to **`matching.fleet-state.retry`** (back after 5 s), and after 3 retries to **`matching.fleet-state.dlq`** for a human to look at. The tests check that both cases really *raise*, instead of being silently swallowed and lost.

---

## 4.8: the step-by-step guide

| Step | Plan says | Where / status |
|---|---|---|
| 1 | models + migrations `0001_init`, `0002_seed_zones` (`op.bulk_insert`) | **4.2 / 4.3** |
| 2 | `geo.py` + tests: `BANANI→MOHAKHALI == 3500`, symmetric overrides, unknown zone → 422 | **4.3** |
| 3 | `planner.py` + tests: Rafiq accepted with G1 before M, Shirin rejected on seats, different pickup rejected | **4.4** |
| 4 | `fleet.py`; test against a real Redis ("fakeredis lacks full GEOSEARCH on some versions") | **4.5**, on fakeredis 2.38, which *does* support it (checked in 4.1). Docker is off |
| 5 | routers; lifespan loads zones + overrides into `app.state.dist`, creates Redis, connects bus, starts consumer | routers **4.6**; **lifespan: done now** |
| 6 | health: DB + Redis + RabbitMQ | **done now** |

### `main.py` (steps 5 and 6)

```
lifespan:
    configure_logging("matching")
    load zones + distance table from matching.db  → app.state.zones, app.state.dist   (once, into memory)
    no zones?  → refuse to start: "run `alembic upgrade head` first"
    Redis client                                   → app.state.redis
    bus.connect()                                  → RabbitMQ (unreachable → refuse to start)
    consumers.start()                              → queue matching.fleet-state
    ── serving ──
    close bus, Redis, database
app: error format + /health (db, redis, rabbitmq) + the three routers
```

**One addition: "refuse to start without zones".** If someone started the service on a database where the seed migration hadn't run, every request would answer 422 `UNKNOWN_ZONE` ("Banani doesn't exist"). That's confusing, and it looks like a user error. Now the service **won't start at all** and says exactly what to do. In Docker this can't happen (the container runs `alembic upgrade head` first), but when running it by hand it can.

**Unlike Identity, there's no outbox relay here**, because Matching publishes no events (4.7). So shutdown is simpler: close the bus, Redis and the database.

### Running the real commands

| Command | Result |
|---|---|
| `alembic upgrade head` (fresh database) | `-> 0001, init`, then `0001 -> 0002, seed zones` |
| `uvicorn app.main:app` **with no RabbitMQ** | zones load, then it refuses to start (`AMQPConnectionError`), as intended |

As with Identity, **Matching against a real RabbitMQ and Redis wasn't run** (Docker is off). Part 1 tested the bus itself against real RabbitMQ, and everything Matching adds on top is tested below with a fake bus and fakeredis.

### Tests: 127, all passing

| File | Tests | Section |
|---|---|---|
| `test_migrations.py` | 5 | 4.3 |
| `test_models.py` | 6 | 4.3 |
| `test_schemas.py` | 25 | 4.3 |
| `test_geo.py` | 13 | 4.3 |
| `test_planner.py` | 17 | 4.4 |
| `test_fleet.py` | 23 | 4.5 |
| `test_api.py` | 20 | 4.6 |
| `test_consumers.py` | 10 | **4.7** |
| `test_main.py` | 8 | **4.8** |

**`test_consumers.py` (4.7)** builds its events with **Part 1's real `emit()`**, the same function Identity and Trip use, so the format is exactly what will arrive over RabbitMQ:
- the bindings let in exactly the 3 needed keys out of the 9 in the registry, and `start()` declares `matching.fleet-state` with them;
- Identity's full online event → online; online then offline → unavailable (added after a break-it check, see below); a pool's life `FORMING` → `COMPLETED` → busy, then free;
- **`emit()` really writes `...08:41:05Z` on a whole second** (confirmed), and an online 120 ms later is still applied (*after the Part 5 fix, this test checks `...08:41:05.000000Z` instead*);
- redelivering the same event changes nothing; other event types touch nothing;
- Redis down → the handler raises (so the bus retries); an event missing `driver_id` → raises (so it ends in the DLQ).

**`test_main.py` (4.8)** runs the **real `app.main.app`** with its lifespan, on a migrated temp database, fakeredis and a fake bus:
- startup loads 9 zones and the distance table, and registers the consumer with the right queue and bindings;
- **the Banani story through the real app:** an `online` event delivered to the registered consumer → Jashim pings from Banani (202, zone BANANI) → evaluate for Nusrat → candidates `[jashim]`, solo 3500;
- all routes mounted; `/health` → 200 with `db`, `redis` and `rabbitmq` all ok; broker gone → 503 naming `rabbitmq`; Redis gone → 503 naming `redis`;
- shutdown closes the bus; **a database without zones → refuses to start** with the "run `alembic upgrade head`" message.

**Break-it checks:**

| Deliberately broke... | Result |
|---|---|
| binding widened to `trip.pool.*` | 2 failed |
| binding `identity.*` (one word, so it matches nothing Identity sends) | 3 failed |
| offline events not handled | **0 failed at first** → added the online-then-offline test → now 1 fails |
| consumer never started | 2 failed |
| allowed starting without zones | 1 failed |
| bus not closed on shutdown | 1 failed |

---

## Things to know before the next sections

- **Build order:** the plan builds Matching **third** (after the common lib and Identity), because Fare and Trip both depend on it.
- **Two data stores:** `matching.db` (SQLite: zones and distances, which barely change) and **Redis** (positions and availability, which change constantly). Zones are loaded **into memory at startup** (4.8 step 5), so answering "Banani → Mohakhali?" never even touches the database.
- **Matching's "who is free" is eventually consistent** (a fraction of a second behind Identity and Trip). By design this is harmless, because Trip re-checks everything atomically when it books.
- **Docker is still off**, but thanks to fakeredis's GEO support that won't block the Matching tests.
- **~~Recommended before Part 5~~ Done:** the one-line `emit()` change above (fixed-width timestamps), so Trip's consumer can't hit the whole-second bug.
- **Part 1's `errors.py` was fixed in 4.6** (422s from `model_validator`s used to crash into 500s). All services use the fixed version automatically: `tesla_common` is installed in editable mode, and Docker copies `libs/common` fresh.
- **`consumers.py` passes `occurred_at` to `on_pool_updated`** (done in 4.7).
- **Running Matching for real** needs the zones migrated (`alembic upgrade head`), plus RabbitMQ and Redis. Without zones or RabbitMQ it refuses to start, on purpose.
- **Trip (Part 5) must send `trip.pool.updated` with `driver_id`, `pool_id`, `status`** (the registry's fields). Matching's consumer depends on exactly those three.
- **Changing a CHECK rule needs a hand-written migration** (Alembic's autogenerate and `alembic check` don't see CHECK rules; found in 4.3).
- **Fare will cache these distances for 24 h** (Part 6). If an override is ever changed, Fare's cache (`fare:dist:*` in Redis) must be cleared, or prices will use the old distance for up to a day.
