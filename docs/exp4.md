# Step 4 Explained: the Location & Matching Service

## Progress

| Section | What it is | Status |
|---|---|---|
| 4.1 | Overview & domain scope | **Done** (explained below) |
| 4.2 | Directory structure | **Done** (explained below) |
| 4.3 | Data layer (zones, distances, schemas) | **Done** (explained below) |
| 4.4 | Matching algorithm (the pool planner) | Not started |
| 4.5 | Fleet state in Redis (where drivers are, who's free) | Not started |
| 4.6 | API endpoints | Not started |
| 4.7 | Messaging integration | Not started |
| 4.8 | Step-by-step build + tests | Not started |

This file grows as each section gets done.

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

**1. Event-timestamp ordering can go wrong (4.5).** To ignore out-of-date events, `fleet.py` compares event times **as text** (`occurred_at > last`). Part 1's `emit()` writes times with Python's `isoformat()`, which **drops the fraction of a second when it's exactly zero**:

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

## Things to know before the next sections

- **Build order:** the plan builds Matching **third** (after the common lib and Identity), because Fare and Trip both depend on it.
- **Two data stores:** `matching.db` (SQLite: zones and distances, which barely change) and **Redis** (positions and availability, which change constantly). Zones are loaded **into memory at startup** (4.8 step 5), so answering "Banani → Mohakhali?" never even touches the database.
- **Matching's "who is free" is eventually consistent** (a fraction of a second behind Identity and Trip). By design this is harmless, because Trip re-checks everything atomically when it books.
- **Docker is still off**, but thanks to fakeredis's GEO support that won't block the Matching tests.
- **Changing a CHECK rule needs a hand-written migration** (Alembic's autogenerate and `alembic check` don't see CHECK rules; found in 4.3).
- **Fare will cache these distances for 24 h** (Part 6). If an override is ever changed, Fare's cache (`fare:dist:*` in Redis) must be cleared, or prices will use the old distance for up to a day.
