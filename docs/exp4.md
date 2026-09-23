# Step 4 Explained: the Location & Matching Service

## Progress

| Section | What it is | Status |
|---|---|---|
| 4.1 | Overview & domain scope | **Done** (explained below) |
| 4.2 | Directory structure | Not started |
| 4.3 | Data layer (zones, distances, schemas) | Not started |
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

**2. `ProcessedEvent` table not used (4.3/4.7).** The plan's models define a `processed_events` table, but 4.7 says Matching doesn't need it (its Redis updates are safe to repeat). I'll decide in 4.3 whether to keep it.

---

## Things to know before the next sections

- **Build order:** the plan builds Matching **third** (after the common lib and Identity), because Fare and Trip both depend on it.
- **Two data stores:** `matching.db` (SQLite: zones and distances, which barely change) and **Redis** (positions and availability, which change constantly). Zones are loaded **into memory at startup** (4.8 step 5), so answering "Banani → Mohakhali?" never even touches the database.
- **Matching's "who is free" is eventually consistent** (a fraction of a second behind Identity and Trip). By design this is harmless, because Trip re-checks everything atomically when it books.
- **Docker is still off**, but thanks to fakeredis's GEO support that won't block the Matching tests.
