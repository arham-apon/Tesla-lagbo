# Dhaka Tesla Pool

**Share a seat. Split the fare. Survive Dhaka traffic.**

> ## 🎥 Demo video (6 min): **[Watch on Loom](https://www.loom.com/share/REPLACE_WITH_YOUR_LINK)**
> 🌐 **Deployment:** no public URL. See [Deployment](#deployment) for why, and for the one-command reproducible Docker deployment.

Dhaka Tesla Pool is a ride-pooling MVP. It uses the PRD's story cast throughout: seed data, tests, demo and this README.

- **Jashim** drives **Bullet**, a three-seat battery "Tesla".
- **Nusrat** (Banani → Mohakhali) and **Rafiq** (Banani → Gulshan 1) share it.
- **Shirin** tries to grab the last seat at the same instant as someone else.

---

## Contents

1. [Summary & problem statement](#summary--problem-statement)
2. [Features implemented](#features-implemented)
3. [Screenshots](#screenshots)
4. [Architecture](#architecture)
5. [Database design (ERD)](#database-design-erd)
6. [Ride & pool lifecycle](#ride--pool-lifecycle)
7. [Matching rule](#matching-rule)
8. [Fare model](#fare-model)
9. [Concurrency: the last-seat problem](#concurrency-the-last-seat-problem)
10. [Tech stack & justification](#tech-stack--justification)
11. [Project structure](#project-structure)
12. [Prerequisites](#prerequisites)
13. [Environment variables](#environment-variables)
14. [Run it with Docker](#run-it-with-docker)
15. [Local development, migrations & seed](#local-development-migrations--seed)
16. [Demo credentials](#demo-credentials)
17. [API overview](#api-overview)
18. [Testing](#testing)
19. [Deployment](#deployment)
20. [Key decisions & trade-offs](#key-decisions--trade-offs)
21. [Known limitations](#known-limitations)
22. [Next improvements](#next-improvements)
23. [Bonus: "If Oi Tesla Goes Viral"](#bonus-if-oi-tesla-goes-viral)
24. [Git workflow](#git-workflow)
25. [AI usage](#ai-usage)

---

## Summary & problem statement

At 8:41 AM on Banani Road 11, Nusrat books a ride to Mohakhali. Two minutes later Rafiq books almost the same route to Gulshan 1. Bullet has three seats and is already going that way. The system has to decide, in about a second:

- whether they can share without anyone's trip getting unreasonably long;
- what each of them pays;
- that the third seat can't be sold twice when Shirin and someone else tap at the same moment.

Jashim needs to see who is riding and what stage the trip is at. Each passenger sees their own fare and status, never anyone else's. Every finished ride keeps enough history to explain exactly what happened.

**Core idea:** riders leaving from the same zone whose drop-offs fit on one route share one vehicle. Each pays for their own direct distance, never for someone else's detour. Capacity is enforced by the database, not by hoping two requests don't collide.

**Assumptions made** (PRD §17). Each is documented and applied consistently:

| # | Assumption | Why |
|---|---|---|
| A1 | Geography = 9 predefined Dhaka zones plus a hand-checkable distance table. No map API. | PRD §4; distances can be verified by hand |
| A2 | Riders pool only with the **same pickup zone**, while the pool hasn't departed, if seats fit, and if no rider's in-vehicle distance exceeds **140 %** of their solo distance | Simple and testable; explains Nusrat + Rafiq |
| A3 | The **first** rider in a pool needs the driver to accept. **Later** compatible riders auto-join, and the driver is notified. | Jashim already committed to pooling; asking him per rider slows matching |
| A4 | A passenger can cancel only in `REQUESTED` / `MATCHED`. After `DRIVER_ARRIVED`, only the driver can cancel (no-show). Nobody can cancel after `STARTED`. | Protects the driver's time |
| A5 | Money is `INTEGER` poysha (100 poysha = ৳1), with floor division | No float drift in ledgers |
| A6 | Passengers pay for their **solo** distance, never for detours; the fare scales per seat | Fair and hand-testable |
| A7 | Payment is Cash or a **simulated TeslaPay wallet**; no real gateway | PRD §5 |

## Features implemented

| Area | Implemented |
|---|---|
| **Passenger** | Sign up / sign in (JWT) · fare estimate (solo and pooled quote, valid 10 min) · request a ride (pickup, destination, seats, cash or wallet) · track status `REQUESTED → MATCHED → DRIVER_ARRIVED → STARTED → COMPLETED / CANCELLED` · ride history (paginated) · cancel while allowed · simulated TeslaPay wallet with top-up · live updates over a WebSocket, plus an inbox to catch up after reconnecting |
| **Driver / Tesla** | Sign in · register a vehicle with fixed seat capacity · go online/offline (blocked while riders are on board) · see ride offers · accept / decline · mark arrival, start, complete · no-show cancel · see the pool's riders, seats and ordered waypoints · pool history · earnings computed from the fare ledger |
| **Pool / split** | Several requests share one Tesla · occupied seats can never exceed capacity (guarded by the database, with a CHECK constraint as backstop) · individual fare per passenger · explicit pool lifecycle `FORMING → IN_PROGRESS → COMPLETED / CANCELLED` · planned stop order with a detour cap · full status history per ride |
| **Platform** | API gateway (JWT, rate limiting, idempotency keys) · transactional outbox with retry and dead-letter queues · idempotent consumers · JSON logs with request ids · `/health` on every service · Docker Compose with migrations, seed data and health checks |
| **Frontend** | Next.js passenger console and driver cockpit: loading, error, empty and disabled states · WCAG 2.1 AA (axe-core in CI tests) · 48 px touch targets · the last-seat contention flow · fare breakdown · design-token system enforced by lint. ⚠️ Runs on an in-browser simulation of the dispatch rules; see [Known limitations](#known-limitations). |

## Screenshots

| Passenger console (desktop) | Last-seat contention: Shirin loses by 340 ms |
|---|---|
| ![Passenger console](docs/screenshots/passenger-console.png) | ![Seat contention](docs/screenshots/seat-contention.png) |
| **Driver cockpit: Jashim's sequential stops** | **Rafiq's verified fare breakdown** |
| ![Driver cockpit](docs/screenshots/driver-cockpit.png) | ![Fare breakdown](docs/screenshots/fare-breakdown.png) |

<img src="docs/screenshots/passenger-mobile.png" alt="Passenger console on a phone" width="280">

## Architecture

```mermaid
flowchart LR
    B[Browser] --> FE[Next.js frontend<br/>passenger console + driver cockpit]
    FE -->|HTTPS REST /api/v1| GW[API Gateway :8000<br/>JWT · rate limit · idempotency]
    FE -->|WSS /ws| NS
    GW --> ID[Identity :8001<br/>users · drivers · vehicles]
    GW --> MS[Matching :8002<br/>zones · distances · driver positions]
    GW --> TS[Trip :8003<br/>rides · pools · state machine]
    GW --> FS[Fare :8004<br/>quotes · fares · wallets]
    GW --> NS[Notification :8005<br/>WebSocket + inbox]
    TS -->|HTTP quote| FS
    TS -->|HTTP evaluate| MS
    FS -->|HTTP distance| MS
    ID -->|HTTP live-pool check| TS
    ID & TS & FS -->|outbox → publish| RMQ[(RabbitMQ<br/>tesla.events)]
    RMQ --> TS & MS & FS & NS
    MS -->|pub/sub loc:pool:*| R[(Redis)]
    R --> NS
    GW --- R
    ID -.-> IDB[(identity.db)]
    MS -.-> MDB[(matching.db)]
    TS -.-> TDB[(trip.db)]
    FS -.-> FDB[(fare.db)]
    NS -.-> NDB[(notification.db)]
```

**How services talk to each other:**

- **HTTP** when the caller can't continue without the answer (Trip needs a price and candidate drivers before it can create a ride).
- **RabbitMQ events** when a service only announces that something happened (`trip.ride.completed` → Fare settles, Notification pushes).
- **Redis pub/sub** for high-frequency, loss-tolerant GPS pings.

The synchronous call graph is acyclic (Identity → Trip → {Fare, Matching}; Fare → Matching), so there's no distributed deadlock.

> **Honest note on PRD §6 and §9.** The PRD mandates a **Node.js** backend. It also warns against adding microservices, Redis or queues "just to look advanced". This backend is **Python/FastAPI split into six services**, with RabbitMQ and Redis. I'm stating that deviation openly rather than hiding it. What each piece buys, and what breaks without it:
> - **Gateway.** One place for auth, rate limiting and idempotency keys. Without it, every service re-implements JWT checks, and a retried `POST /rides` on a flaky mobile network books twice.
> - **RabbitMQ + outbox.** "Ride completed" must reach settlement and notification even if one of them is down. Without it you get synchronous fan-out: a fare-service outage would fail the driver's "Complete" tap.
> - **Redis.** Short-lived state: rate-limit counters, the logout denylist, live driver positions (GEO) and location fan-out. Putting these in SQL would mean write-heavy, lock-contended tables.
>
> A Node.js monolith (NestJS + Postgres) could deliver the same MVP with fewer moving parts. [Key decisions & trade-offs](#key-decisions--trade-offs) says when I'd choose that.

## Database design (ERD)

Each service owns its own database (SQLite, WAL mode). Nothing reads another service's tables. Across services, records are linked **by id only** (e.g. `ride_requests.quote_id` ↔ `quotes.id`) and kept consistent by events plus idempotent consumers (`processed_events`).

```mermaid
erDiagram
    %% identity.db
    USERS ||--o| DRIVERS : "is a"
    DRIVERS ||--o| VEHICLES : owns
    USERS { string id PK
            string full_name
            string phone UK
            string password_hash
            string role "PASSENGER|DRIVER" }
    DRIVERS { string user_id PK,FK
              string license_number UK
              string status "OFFLINE|ONLINE" }
    VEHICLES { string id PK
               string driver_id FK,UK
               string nickname "Bullet"
               string plate UK
               int seat_capacity "1..6" }

    %% trip.db
    POOLS ||--o{ RIDE_REQUESTS : contains
    POOLS ||--o{ POOL_WAYPOINTS : "ordered stops"
    RIDE_REQUESTS ||--o{ RIDE_STATUS_HISTORY : "audit trail"
    RIDE_REQUESTS ||--o{ RIDE_OFFERS : "offered to drivers"
    POOLS { string id PK
            string driver_id
            string vehicle_id
            int max_capacity "CHECK 1..6"
            int occupied_seats "CHECK 0..max_capacity"
            string status "FORMING|IN_PROGRESS|COMPLETED|CANCELLED"
            string pickup_zone
            int version "optimistic lock" }
    RIDE_REQUESTS { string id PK
                    string passenger_id
                    string pool_id FK "null until matched"
                    int seats
                    string pickup_zone "CHECK <> dropoff_zone"
                    string dropoff_zone
                    string status
                    string quote_id
                    int estimated_fare_poysha
                    int final_fare_poysha
                    int version }
    POOL_WAYPOINTS { string id PK
                     string pool_id FK
                     string ride_request_id FK
                     int seq
                     string kind "PICKUP|DROPOFF"
                     datetime done_at }
    RIDE_STATUS_HISTORY { int id PK
                          string ride_id FK
                          string from_status
                          string to_status
                          string actor_role }
    RIDE_OFFERS { string ride_id FK
                  string driver_id
                  string status "OFFERED|ACCEPTED|DECLINED" }

    %% fare.db
    TARIFFS ||--o{ QUOTES : prices
    QUOTES ||--o| FARES : "settled as"
    WALLETS ||--o{ WALLET_TRANSACTIONS : ledger
    TARIFFS { int id PK
              int base_poysha
              int per_km_poysha
              int pool_discount_pct }
    QUOTES { string id PK
             string passenger_id
             int distance_m
             int solo_total_poysha
             int pooled_total_poysha
             datetime expires_at }
    FARES { string id PK
            string ride_id UK
            int base_poysha
            int distance_charge_poysha
            int pool_discount_poysha
            int total_poysha
            string payment_status "PAID|FAILED|REFUNDED" }
    WALLETS { string user_id PK
              int balance_poysha "CHECK >= 0" }
    WALLET_TRANSACTIONS { string id PK
                          string user_id
                          string ride_id
                          string kind
                          int amount_poysha }
```

**Other tables:**

- **matching.db:** `zones(code, name, lat, lng)` and `zone_distances(from_zone, to_zone, distance_m)`.
- **notification.db:** `notifications(id, user_id, type, payload JSON, read_at)`.
- **trip.db:** `driver_shifts`, a projection of each driver's online state and capacity, built from identity events.
- **identity, trip and fare:** each has an `outbox` table.
- **trip, fare and notification:** each has a `processed_events` table.

Constraints worth pointing at:

- `ck_pool_capacity` (`0 ≤ occupied_seats ≤ max_capacity`)
- `ck_ride_zones` (pickup ≠ drop-off)
- `ck_fare_arithmetic` (`total = base + distance − discount`) and `ck_fare_discount_only_pooled`
- `ck_wallet_nonneg` (wallet balance ≥ 0)
- enumerated statuses
- unique phone, licence and plate
- partial unique indexes: **one live pool per driver** and **one active ride per passenger**
- `ix_pool_matching (status, pickup_zone)` for finding open pools
- `ix_ride_passenger_created` for history

## Ride & pool lifecycle

```mermaid
stateDiagram-v2
    [*] --> REQUESTED
    REQUESTED --> MATCHED: driver accepts / system auto-joins
    REQUESTED --> CANCELLED: passenger · system (expired)
    MATCHED --> DRIVER_ARRIVED: driver
    MATCHED --> CANCELLED: passenger · driver
    DRIVER_ARRIVED --> STARTED: driver
    DRIVER_ARRIVED --> CANCELLED: driver only (no-show)
    STARTED --> COMPLETED: driver
    COMPLETED --> [*]
    CANCELLED --> [*]
```

Every transition goes through one table, `services/trip/app/state_machine.py`, which lists **who** may make it. Anything else gets `409 INVALID_TRANSITION`. Each transition also:

- writes a `ride_status_history` row;
- writes an outbox event in the **same** transaction.

The PRD's `MATCHED/ACCEPTED` is a single `MATCHED` state: whether it came from a driver acceptance or an auto-join is recorded in history (`actor_role`).

**Pool lifecycle:** `FORMING` (accepting riders) → `IN_PROGRESS` (first rider boarded; no new riders can join) → `COMPLETED` / `CANCELLED`.

## Matching rule

A new request can join an existing pool only if **all** of these hold:

1. same pickup zone;
2. the pool is still `FORMING`;
3. `remaining_seats ≥ seats`;
4. after inserting the new drop-off at its best position in the stop order, **every** rider's in-vehicle distance is ≤ **140 %** of their direct distance.

Among compatible pools, the one that adds the least route distance wins. If no pool fits, the request goes to the nearest online drivers (Redis GEO, 3 km radius) as an offer.

**Nusrat + Rafiq, by hand:**

- Direct distances: Banani → Mohakhali is 3.5 km, Banani → Gulshan 1 is 2.0 km, Gulshan 1 → Mohakhali is 2.0 km.
- Best order: B → G1 → M.
  - Rafiq rides 2.0 km (100 % of his direct distance).
  - Nusrat rides 2.0 + 2.0 = 4.0 km (4.0 / 3.5 = **114 %** ≤ 140 %).
- So Rafiq **joins Bullet**, and the stop order is Banani (Nusrat) → Banani (Rafiq) → Gulshan 1 (drop Rafiq) → Mohakhali (drop Nusrat).

## Fare model

```
passengerFare = (baseFare + distanceCharge − poolDiscount) × seats
  baseFare       = 3,000 poysha            (৳30.00)
  distanceCharge = distance_m × 1,500 / 1000   (৳15.00 per km, floor)
  poolDiscount   = 20 % of distanceCharge      (only if the ride was pooled, floor)
```

**Hand check with the story** (1 seat each, pooled):

| | Distance | Base | Distance charge | Pool discount | **Pays** |
|---|---|---|---|---|---|
| Nusrat, Banani → Mohakhali | 3.5 km | 3,000 | 5,250 | −1,050 | **7,200 poysha = ৳72.00** (solo would be ৳82.50) |
| Rafiq, Banani → Gulshan 1 | 2.0 km | 3,000 | 3,000 | −600 | **5,400 poysha = ৳54.00** (solo would be ৳60.00) |

**Money is stored as `INTEGER` poysha, not decimal.**

- **Why:** every operation (quotes, settlement, wallet ledger, earnings) is integer addition, subtraction or floor division. So totals always reconcile exactly, and SQLite has no native decimal type.
- **How it's shown:** formatting to "৳72.00" happens only at the edge, in the client.
- **When it's settled:** the fare becomes final when the ride completes. `pooled` is decided at that moment: a ride that ended up alone pays the solo price.
- **Payment:** Cash, or a simulated TeslaPay wallet debit. An insufficient wallet balance falls back to collecting cash, and the driver is told.

## Concurrency: the last-seat problem

> Bullet has 1 seat left. Nusrat and Shirin both try to claim it at nearly the same instant, and both initially see one seat available.

**How it's handled now:**

1. **No read-then-write.** Joining a pool is one conditional statement:

   ```sql
   UPDATE pools SET occupied_seats = occupied_seats + :seats, version = version + 1
   WHERE id = :pool AND status = 'FORMING' AND version = :version
     AND occupied_seats + :seats <= max_capacity
   ```

   Exactly one request gets `rowcount = 1`. The other gets `0`. Its ride stays `REQUESTED`, and matching falls through to other pools or drivers. It never over-books and never errors out. (`services/trip/app/pooling.py`)
2. **SQLite `BEGIN IMMEDIATE`** takes the write lock at the start of the transaction. Two joins can't interleave, and the planner's result is re-validated through `version`.
3. **Backstop:** the `CHECK (occupied_seats <= max_capacity)` constraint makes over-booking impossible even if application code were wrong.
4. **Double taps:** `POST /rides` requires an `Idempotency-Key`. The gateway replays the stored response for a repeated key and rejects a reused key with a different body. A per-passenger "one active ride" rule adds a second guard.
5. **Driver race:** two drivers accepting the same ride is resolved the same way, by a conditional `UPDATE ride_offers … WHERE status='OFFERED'`.

**Proof:** `services/trip/tests/test_concurrency.py` fires 10 riders at 1 seat, and 2 drivers at 1 ride. It asserts exactly one winner and a consistent pool. The test is repeated 50× by `scripts/test_all.py --race 50`.

**At larger scale:** move Trip to PostgreSQL. The *same* compare-and-set `UPDATE` works under row-level locks, so there's no global write lock. Shard pools by city region so contention stays local. Keep idempotency keys in Redis with TTLs. Details are in the [Bonus](#bonus-if-oi-tesla-goes-viral).

## Tech stack & justification

The PRD mandates Node.js for the backend and React/Next.js for the frontend. The frontend follows the mandate; the backend doesn't (see the honest note above). For each choice below: what I picked, realistic alternatives, why it fits a ride-pooling MVP, and what would make me switch.

| Concern | Picked | Alternatives | Why it fits this MVP | Switch when… |
|---|---|---|---|---|
| Backend language / framework | **Python 3.12 + FastAPI** ⚠️ *deviates from the Node.js mandate* | Node.js + NestJS/Fastify (mandated), Go | Async I/O for HTTP, WebSocket and AMQP in one runtime. Pydantic gives validation and OpenAPI from the same types. My strongest language, so correctness could come first. | The team standardises on TypeScript, or the evaluation requires the mandate (a NestJS port would keep the same module boundaries) |
| Architecture | **6 services + gateway** | Modular monolith | Isolates the contention hotspot (Trip) from pricing and notifications. Each piece can be reasoned about and tested alone. | Team < 3 people or a single deploy target: a modular monolith is cheaper to run |
| Database | **SQLite per service (WAL)** | PostgreSQL, MySQL | Zero-ops. One writer per database makes the capacity check trivially race-free. A real relational schema with CHECKs and FKs. | More than one Trip instance is needed → PostgreSQL (the SQL barely changes) |
| ORM / migrations | **SQLAlchemy 2.0 (async) + Alembic** | raw SQL, Tortoise, Prisma (Node) | Explicit transactions (`BEGIN IMMEDIATE`) and conditional `UPDATE`s stay visible. Versioned migrations. | — |
| Validation | **Pydantic v2** | marshmallow, zod (Node) | Request/response schemas double as API documentation | — |
| Auth | **JWT RS256** (argon2 password hashes), verified at the gateway, revocation list in Redis | sessions + cookies, OAuth provider | Stateless for services; the private key lives only in Identity; logout works via a `jti` denylist | Third-party login or refresh tokens become a requirement |
| Messaging | **RabbitMQ** (topic exchange, retry and DLQ queues) + **transactional outbox** | synchronous HTTP fan-out, Kafka, Redis streams | At-least-once delivery that survives a broker restart. A service being down doesn't fail the driver's tap. | Event replay or very high throughput → Kafka |
| Cache / realtime state | **Redis** | Postgres tables, in-memory | Rate-limit counters, idempotency keys, logout denylist, GEO for nearby drivers, pub/sub for GPS | — |
| API style | **REST** (JSON, `/api/v1`) + one **WebSocket** for pushes | GraphQL, gRPC | Small resource set with clear verbs (`/rides/{id}/cancel`); easy to test with curl | Clients need flexible aggregate reads → GraphQL at the edge |
| Frontend | **Next.js 16 (App Router), TypeScript, Tailwind v3, Radix UI, Framer Motion** | Vite + React Router, MUI | App Router + TypeScript as the PRD recommends. Radix gives accessible primitives. Tailwind locked to a design-token scale. | — |
| Styling | **Tailwind with a replaced theme** (only tokens exist) + custom ESLint rules banning arbitrary values | CSS modules, styled-components | Consistency is enforced, not hoped for | — |
| Tests | **pytest** (+ fakeredis, respx, fake bus) · **Vitest + Testing Library** · **Playwright + axe-core** | Jest, Cypress | Fast suites that need no Docker; browser e2e that measures touch targets and runs WCAG checks | — |
| Hosting | **Docker Compose** (reproducible) | Render / Railway / Fly free tiers | See [Deployment](#deployment) | A free tier that fits 8 containers appears, or the stack is collapsed |

## Project structure

```
.
├── docker-compose.yml          whole system: 6 services + RabbitMQ + Redis, health-checked boot order
├── .env.example                every setting, no secrets
├── libs/common/tesla_common/   shared library: DB setup, outbox + event bus, auth, errors, logging, HTTP client
├── services/
│   ├── gateway/                JWT, rate limit, idempotency, reverse proxy (no database)
│   ├── identity/               users, drivers, vehicles, JWT issuing, online/offline      → identity.db
│   ├── matching/               zones, distance table, pool planner, Redis GEO fleet        → matching.db
│   ├── trip/                   rides, pools, waypoints, offers, state machine, sweeper     → trip.db
│   ├── fare/                   tariff, quotes, settlement, wallets, earnings               → fare.db
│   └── notification/           event → recipient routing, WebSocket, inbox                 → notification.db
│       (each: app/ · migrations/ (Alembic) · tests/ · Dockerfile · requirements*.txt)
├── scripts/
│   ├── e2e.py                  the Banani story against the running stack (52 checks)
│   ├── test_all.py             every backend test suite in one command
│   ├── flows.py, alerts.py     extra scenario runner and alert-signal checks
│   └── gen_keys.sh             generates the RS256 key pair into keys/
├── frontend/                   Next.js app (see frontend/README.md)
│   ├── src/design/tokens.ts    single source of design tokens
│   ├── src/types/mobility.ts   domain contracts
│   ├── src/domain/             money, fare, lifecycle, zones, cast
│   ├── src/sim/                in-browser dispatch model (port of the backend rules)
│   ├── src/components/         passenger/, driver/, dispatch/, ui/, shell/, map/
│   └── e2e/                    Playwright + axe-core
└── docs/                       exp1–exp8.md (build log per part), screenshots/
```

## Prerequisites

- **Docker Desktop** (Compose v2). This is all you need to run the system.
- **Node.js ≥ 22** and npm, for the frontend.
- **Python 3.12**, for running backend tests outside Docker.
- **Git Bash, WSL or macOS/Linux shell**, for `scripts/gen_keys.sh` (uses `openssl`).

## Environment variables

Copy `.env.example` to `.env`. Values in `.env.example` are placeholders. **Never commit `.env` or `keys/`** (both are git-ignored).

| Variable | Used by | Meaning |
|---|---|---|
| `RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS` | rabbitmq | Broker credentials |
| `RABBITMQ_URL` | identity, trip, fare, matching, notification | `amqp://user:pass@rabbitmq:5672/` |
| `REDIS_URL` | gateway, matching, fare, notification, identity | `redis://redis:6379/0` |
| `INTERNAL_TOKEN` | all | Shared secret the gateway adds so services reject direct calls. Use a long random string. |
| `JWT_PRIVATE_KEY_PATH` | identity | RS256 private key (mounted from `keys/`) |
| `JWT_PUBLIC_KEY_PATH` | gateway, notification | RS256 public key |
| `JWT_TTL_SECONDS` | identity | Token lifetime (default 3600) |
| `POOL_MAX_DETOUR_PCT` | matching | Detour cap for pooling (default 140) |
| `MATCH_RADIUS_M` | matching | Radius for candidate drivers (default 3000) |
| `RIDE_REQUEST_TTL_SECONDS` | trip | Unaccepted request expiry (default 180) |
| `LOG_LEVEL` | all | `INFO` by default |
| `GATEWAY_PORT`, `NOTIFICATION_PORT`, `RABBITMQ_PORT`, `RABBITMQ_UI_PORT`, `REDIS_PORT` | compose | Optional host-port overrides if 8000/8005/5672/15672/6379 are taken |

Frontend: none required.

## Run it with Docker

```bash
cp .env.example .env                 # then set INTERNAL_TOKEN and the RabbitMQ password
sh scripts/gen_keys.sh               # creates keys/jwt_private.pem + keys/jwt_public.pem
docker compose up --build -d         # migrations + seed run automatically on start
docker compose ps                    # wait until every service is "healthy"
```

- **Gateway:** http://localhost:8000 (e.g. `curl http://localhost:8000/api/v1/zones`)
- **WebSocket:** `ws://localhost:8005/ws?token=<JWT>`
- **RabbitMQ UI:** http://localhost:15672

**Boot order** comes from `depends_on` + health checks: RabbitMQ and Redis → Matching → Fare → Trip → Identity → Notification → Gateway.

If those ports are busy, move them:

```bash
GATEWAY_PORT=18000 NOTIFICATION_PORT=18005 RABBITMQ_PORT=25672 RABBITMQ_UI_PORT=35672 REDIS_PORT=16379 docker compose up -d
```

Stop with `docker compose stop` (keeps data). `docker compose down -v` wipes all databases.

**Frontend:**

```bash
cd frontend
npm install
npm run dev                         # http://localhost:3000
```

## Local development, migrations & seed

- **Migrations** are Alembic, one history per service (`services/<name>/migrations/versions/`). Each container runs `alembic upgrade head` before starting. To run one by hand: `docker compose exec trip alembic upgrade head`.
- **Seed data** is the story cast:
  - Identity seed (`python -m app.seed`, runs on every start and is idempotent): Jashim with Bullet (plate `DHAKA-TESLA-11`, 3 seats), Nusrat, Rafiq and Shirin, with fixed ids so other services line up.
  - Matching migration `0002_seed_zones`: the 9 zones and hand-set distances.
  - Fare migration `0002_seed_tariff`: the tariff.
- **One service without Docker** (fast iteration):

  ```bash
  python -m venv .venv && .venv/Scripts/pip install -e libs/common -r services/trip/requirements-dev.txt
  cd services/trip && ../../.venv/Scripts/python -m pytest
  ```

## Demo credentials

Every seeded account uses the password **`Pool@1234`** (demo only).

| Who | Role | Phone |
|---|---|---|
| Jashim | Driver of **Bullet** (`DHAKA-TESLA-11`, 3 seats) | `01711000001` |
| Nusrat | Passenger | `01711000002` |
| Rafiq | Passenger | `01711000003` |
| Shirin | Passenger | `01711000004` |

```bash
curl -s -X POST localhost:8000/api/v1/auth/login -H 'content-type: application/json' \
  -d '{"phone":"01711000002","password":"Pool@1234"}'
```

## API overview

All routes go through the gateway at `/api/v1`. Errors share one shape: `{"error": {"code": "SEAT_TAKEN", "message": "…", "request_id": "…"}}`. Each service also serves OpenAPI docs at `/docs` inside the network.

| Method & path | Who | Purpose |
|---|---|---|
| `POST /auth/register` · `POST /auth/login` · `POST /auth/logout` | public / any | Sign up (passenger or driver with licence), JWT login, revoke token |
| `GET /users/me` | any | Profile |
| `GET /drivers/me` · `PUT /drivers/me/vehicle` · `POST /drivers/me/online` · `POST /drivers/me/offline` | driver | Profile, register the Tesla + capacity, shift (offline blocked while riders are on board) |
| `GET /zones` | public | The 9 Dhaka zones |
| `POST /driver/location` | driver | GPS ping (Redis GEO + live location to riders) |
| `POST /fares/estimate` | passenger | Solo and pooled quote, valid 10 min |
| `GET /fares/rides/{id}` | passenger / driver | Final fare of your own ride |
| `GET /wallet` · `POST /wallet/topup` | passenger | Simulated TeslaPay |
| `GET /driver/earnings` | driver | Totals from the fare ledger |
| `POST /rides` *(Idempotency-Key required)* | passenger | Request a ride: `{pickup_zone, dropoff_zone, seats, payment_method, quote_id}` |
| `GET /rides` · `GET /rides/{id}` · `POST /rides/{id}/cancel` | passenger | History (paginated), detail with status history, cancel |
| `GET /driver/offers` · `POST /driver/offers/{ride}/accept` · `…/decline` | driver | Offers for rides nobody has taken yet |
| `GET /driver/pool` · `GET /driver/pools` | driver | Live pool (riders, seats, waypoints), past pools |
| `POST /driver/rides/{id}/arrive` · `…/start` · `…/complete` · `…/cancel` | driver | Move a rider through the lifecycle (own pool only) |
| `GET /notifications` · `POST /notifications/{id}/read` | any | Inbox catch-up after reconnect |
| `WS /ws?token=…` (port 8005) | any | Live `ride.offer`, `ride.matched`, `ride.status`, `pool.updated`, `fare.settled` pushes. Each recipient gets only an allow-listed subset of fields, so passengers never see co-riders' fares. |

## Testing

```bash
.venv/Scripts/python scripts/test_all.py            # every backend suite, no Docker needed
.venv/Scripts/python scripts/test_all.py --race 50  # plus the race tests repeated 50×
.venv/Scripts/python scripts/e2e.py                 # the Banani story against the running stack
cd frontend && npm run verify                       # typecheck, lint, unit tests, build
cd frontend && npm run e2e                          # Playwright + axe-core (uses installed Chrome)
```

The backend has about 600 test functions across 7 suites (trip 183, fare 108, matching 105, notification 83, identity 74, gateway 44, common 11). The frontend has 73 unit tests and 14 e2e tests.

What the PRD asks to be tested, and where:

| PRD requirement | Where |
|---|---|
| Bullet's capacity can never be exceeded | `services/trip/tests/test_capacity.py`, `test_pooling.py` |
| Invalid state transitions are rejected | `services/trip/tests/test_state_machine.py`, `test_lifecycle.py` |
| Nusrat's and Rafiq's pooled fares calculate correctly | `services/fare/tests/test_pricing.py`, `test_settlement.py` |
| Users can't modify another user's ride | `services/trip/tests/test_ownership.py` (and gateway strips client-sent `X-User-*` headers: `services/gateway/tests/test_proxy.py`) |
| Cancellation rules hold | `services/trip/tests/test_lifecycle.py`, `test_api.py` |
| Two concurrent requests can't corrupt pool capacity | `services/trip/tests/test_concurrency.py` (10 riders → 1 seat; 2 drivers → 1 ride) |
| Whole system end to end | `scripts/e2e.py`: Nusrat books, Jashim accepts, Rafiq auto-joins, Shirin's 2-seat request is refused a seat, arrive/start/complete, fares settle, WebSocket pushes, no dead-lettered or unpublished events |

Also tested:

- no event is lost if RabbitMQ restarts (outbox);
- consumers are idempotent (`processed_events`);
- the frontend's WCAG contrast of every token pair, 48×48 touch targets and axe-core scans.

## Deployment

**No public deployment.** The PRD allows only free tiers, and this system needs 8 long-running containers: 6 services, RabbitMQ and Redis, with persistent volumes. That doesn't fit the free tiers of Render, Railway or Fly, which offer one or two sleeping instances, no free managed RabbitMQ, and ephemeral disks, which break SQLite. Paying is not allowed.

**Reproducible deployment instead:** any machine with Docker can run the exact stack with `docker compose up --build -d` (see [Run it with Docker](#run-it-with-docker)). Migrations, seed and health checks are automatic. The frontend is a standard Next.js app, deployable to Vercel's free tier; it becomes useful there once it calls the API (see limitations).

## Key decisions & trade-offs

1. **Capacity enforced by a conditional `UPDATE` + CHECK constraint, not read-then-write.**
   - *Trade-off:* the loser of a race has to retry matching, instead of getting a seat that "looked" free.
2. **SQLite per service.**
   - *Gain:* zero-ops, and a single writer makes the capacity invariant easy to prove.
   - *Cost:* each service runs as **one container**; no horizontal scaling until Trip and Fare move to PostgreSQL.
3. **Transactional outbox + RabbitMQ instead of synchronous fan-out.**
   - *Gain:* "Complete" never fails because Fare or Notification is down.
   - *Cost:* eventual consistency. A fare appears a moment after completion, and consumers must be idempotent.
4. **Same-pickup-zone pooling with a 140 % detour cap.**
   - Easy to verify by hand and to explain to a rider.
   - *Cost:* it misses good matches from neighbouring zones.
5. **Only the first rider needs driver acceptance.**
   - Faster matching.
   - *Cost:* Jashim can't refuse a specific co-rider.
6. **Microservices in Python instead of the mandated Node.js monolith.**
   - Clear ownership boundaries.
   - *Cost:* more operational surface, and a deviation from the brief that I own and justify above.

## Known limitations

- ⚠️ **The frontend isn't wired to the API yet.** The Next.js consoles run on an in-browser port of the dispatch rules (`frontend/src/sim/`), with the same pooling rule, 140 % cap, acceptance flow and zone distances. They demonstrate every flow and state, but don't call the gateway. The engine exposes async commands shaped like the REST calls, so an HTTP adapter can replace the simulation without touching the UI.
- ⚠️ **The frontend demo fare differs from the backend.** The UI follows the design guideline's formula (৳22.50/km, 30 % off the subtotal, so Rafiq = ৳52.50). The fare service charges ৳15/km and 20 % off the distance charge (Rafiq = ৳54.00). The **backend is authoritative**; the UI must show the server's breakdown once wired.
- A `REQUESTED` ride nobody accepts is **cancelled after 3 min, not re-matched**, and it's only offered to drivers nearby at request time.
- "Pooled" is decided when each ride completes. A rider dropped before a co-rider's no-show still pays the pooled price.
- The WebSocket runs on its own port (8005) instead of through the gateway.
- RabbitMQ and Redis have no volumes. `docker compose down` loses in-flight messages and the logout denylist; use `stop`.
- Tokens are 1-hour access tokens with no refresh token.

## Next improvements

1. HTTP adapter in the frontend (login, quote, `POST /rides` with an `Idempotency-Key`, polling plus WebSocket), rendering the server's fare breakdown.
2. PostgreSQL for Trip and Fare (row-lock compare-and-set, read replicas for history); volumes for RabbitMQ and Redis.
3. Re-match expiring requests instead of cancelling them; nearby-zone pickups (H3 cells).
4. Refresh tokens; a single ingress (Traefik) for TLS and the WebSocket.
5. Ratings persisted server-side; a driver's per-rider confirmation window.

## Bonus: "If Oi Tesla Goes Viral"

Target: 1 M passengers, 100 k drivers.

```mermaid
flowchart LR
    C[Apps] --> CDN[CDN + WAF]
    CDN --> LB[Load balancer]
    LB --> GW[Gateway ×N<br/>stateless · JWT · rate limit]
    GW --> TRIP[Trip ×N<br/>sharded by city region]
    GW --> MATCH[Matching ×N]
    GW --> FARE[Fare ×N]
    TRIP --> PG[(Postgres per region<br/>primary + read replicas)]
    FARE --> PGF[(Postgres)]
    MATCH --> RC[(Redis Cluster<br/>GEO / H3 per region)]
    TRIP & FARE --> BUS[(Kafka / RabbitMQ quorum)]
    BUS --> NOTIF[Notification ×N<br/>WS gateways]
    NOTIF --> RC
    OBS[Metrics · traces · logs] -.- GW & TRIP & MATCH & FARE
```

- **Horizontal scaling and load balancing.** Stateless gateways and services behind a load balancer. WebSocket nodes use sticky connections, with routing through Redis pub/sub.
- **Database.** PostgreSQL per bounded context, partitioned by city region.
  - Indexes on `(pool status, driver_id)` and `(passenger_id, created_at)`.
  - Read replicas serve history and earnings.
  - PgBouncer for connections.
- **DB contention.** The capacity check stays a single-row compare-and-set, so contention is per pool and never global. Sharding by region keeps hot rows local.
- **Geospatial search.** H3 cells or Redis GEO per region for nearby drivers. Matching reads positions from memory, never from SQL.
- **Ride matching.** Batch requests per cell every ~1 s and solve the assignment per batch, instead of first-come-first-served, which improves pooling rates.
- **Queues and events.** Keep the outbox. Move to Kafka for replay and throughput. Partition keys (`pool_id`) preserve per-pool ordering.
- **Caching.** Zones, distance matrix and tariffs are cached at the edge and in process. Quotes live in Redis with a TTL.
- **Real-time.** A dedicated WebSocket tier. Location updates are throttled (e.g. 1 per 3 s) and sent only to that pool's members.
- **Rate limiting and idempotency.** Token buckets per user and per IP at the gateway. Idempotency keys in Redis for every `POST` that creates state.
- **Retry and failure.** Exponential backoff with jitter, retry queue then DLQ with alerting, circuit breakers on synchronous calls (Trip → Fare/Matching), and graceful degradation (quote from the cached tariff if Fare is slow).
- **Security.** Short-lived JWTs with refresh tokens and rotation. mTLS or a service mesh between services. Secrets in a vault. Per-recipient field allow-lists (already built).
- **Observability.** RED metrics per endpoint, distributed tracing (the `request_id` is already propagated), SLO alerts on match latency and the DLQ being non-empty.
- **Deployment.** Containers on Kubernetes or ECS with blue/green or canary releases. Expand/contract migrations so there is zero downtime.

## Git workflow

- **Branches:** long-lived `master` / `main`, with `feature/*` branches per logical piece of work:
  - `feature/initial-setup`, `feature/api-gateway`, `feature/passenger-auth`, `feature/driver-flow`
  - `feature/tesla-pooling`, `feature/fare-billing`, `feature/notifications`
  - `feature/orchestration-tests`, `feature/frontend-app`
- **Flow:** build on the feature branch → merge into `master` when it works → cut `pre-release` for integration fixes and docs → cut `release/v1.0.0`. That release is the version shown in the video.
- **Commits** follow `<type>(<scope>): <description>`, e.g.:
  - `feat(trip): enforce ride status transitions per actor with a state machine`
  - `feat(fare): settle completed rides with wallet debit, driver credit and settled event`
  - `build(docker): add full compose stack with ports`
- Each backend part has a build log in `docs/expN.md`: what was done, what changed from the plan and why, and how it was checked.

## AI usage

AI was used as an engineering tool, openly. Every part of this repository has been reviewed, and I can explain, debug and change it.

| Tool | Used for |
|---|---|
| **Claude (Claude Code)** | Drafting the implementation plan; generating and refactoring service code and tests part by part; writing the Next.js frontend from the design guideline; debugging (Docker port clashes, health-check timing); drafting documentation, this README and the video script |
| *(add any others you used: ChatGPT, Copilot, docs, Stack Overflow)* | |

**One accepted suggestion.** Enforce seat capacity with a single conditional `UPDATE … WHERE occupied_seats + seats <= max_capacity AND version = :v` inside `BEGIN IMMEDIATE`, plus a `CHECK` constraint. I accepted it because it makes the last-seat race impossible by construction rather than by timing. I verified it with the 10-riders-1-seat test, repeated 50 times.

**One rejected / changed suggestion.** The plan had the Notification service forward each event's whole `data` payload to its recipients. I changed it to a **per-recipient allow-list of fields** (`services/notification/app/routing.py`). Otherwise, any field Trip or Fare adds to an event later could leak to a passenger's phone, such as a co-rider's fare. A second example: in the frontend, AI proposed `pointer-events: none` for disabled buttons, as the design guideline says. I changed it to `aria-disabled` plus a blocked click, so the disabled button stays focusable and screen readers can still read *why* it's disabled.

> *Before submitting:* adjust this section to your own experience. The PRD scores honesty and understanding, not "least AI used".
