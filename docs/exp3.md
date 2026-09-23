# Step 3 Explained: the Identity Service

## Progress

| Section | What it is | Status |
|---|---|---|
| 3.1 | Overview & domain scope | **Done** (explained below) |
| 3.2 | Directory structure | **Done** (explained below) |
| 3.3 | Data layer (tables, models, schemas, tokens) | **Done** (explained below) |
| 3.4 | API endpoints | **Done** (explained below) |
| 3.5 | Messaging integration | Not started |
| 3.6 | Step-by-step build + tests | Not started |

This file grows as each section gets done.

---

## The big picture in 30 seconds

Part 2 built the **front door** (the Gateway), which checks every visitor's ID card. Part 3 builds the office that **prints the ID cards**.

Identity (port 8001) is the service that knows **who everyone is**:

- Nusrat, Rafiq and Shirin are **passengers**.
- Jashim is a **driver**, and he drives **Bullet**, a Tesla with **3 seats**.
- Right now Jashim is **online** (working) or **offline** (at home).

When you log in, Identity checks your password and gives you a **JWT**, the signed "ID card" the gateway checks on every request.

```
                  ┌───────────── Identity :8001 ─────────────┐
 register/login ─▶│ users · drivers · vehicles   (identity.db) │──▶ JWT signed with the PRIVATE key
 (via Gateway)    │ online/offline switch                      │──▶ events: identity.driver.online/offline
                  └────────────────────────────────────────────┘        │
                           │ asks before going offline                 ▼
                           ▼                                   Trip + Matching (RabbitMQ)
                    Trip: "does Jashim have passengers?"
```

---

## 3.1: what Identity owns and what it delegates

The plan says:

> **Owns:** users (passenger/driver/admin), password hashing, JWT issuing, driver profile, vehicle and its fixed seat capacity, coarse driver shift status (`OFFLINE`/`ONLINE`).
> **Delegates:** whether a driver is busy (Trip knows pools), GPS (Matching).

What each part means:

### Owns (1): users and their role

- Every person is one row in `users`: name, phone number (the login), password hash and **role**.
- The role is one of **`PASSENGER`**, **`DRIVER`** or **`ADMIN`**. It goes into the JWT, and the gateway passes it on as `X-User-Role`. That's how Trip knows only Jashim may accept ride offers (`auth.role("DRIVER")` from Part 1).
- **Phone number is unique.** Registering the same number twice gets a 409, so one person can't end up with two accounts.
- Only Identity reads or writes the users table (**database per service**, Part 0). When another service needs a name, it gets it from the token or from an event, never by reading `identity.db`.

### Owns (2): password hashing

- Passwords are **never stored**, only a **hash** made with **argon2**. It's a one-way scramble: you can check a password against it, but you can't turn it back into the password.
- argon2 is deliberately **slow and memory-hungry** (a fraction of a second per check). You don't notice that when you log in, but an attacker who stole `identity.db` and tried billions of guesses would be crushed by it. Fast hashes like MD5 or plain SHA-256 are the wrong tool here.
- **Wrong phone** and **wrong password** return the *same* error (`INVALID_CREDENTIALS`). Otherwise an attacker could find out which phone numbers have accounts.
- Together with the gateway's **10 logins/min** limit (Part 2), this makes password guessing impractical.

### Owns (3): JWT issuing, "printing the ID card"

- On login, Identity creates a token containing `sub` (user id), `role`, `name`, `jti` (a unique id for this token), `iss` (`tesla-identity`), `iat` (issued at) and `exp` (expires, **1 hour** by default: `JWT_TTL_SECONDS=3600`).
- It signs the token with the **private key**. **Identity is the only service that has it.** The gateway and Notification get only the **public key**, so they can check a card but never print one.
- **Logout** can't "delete" a token that's already in someone's phone. So Identity writes the token's `jti` into Redis (`auth:revoked:{jti}`) until the token would have expired anyway. The gateway checks that list on every request (Part 2.3). Identity learns the `jti` and expiry from the `X-Token-Jti` / `X-Token-Exp` headers **the gateway already sends** (added in 2.4).

### Owns (4): driver profile and vehicle, with a fixed seat count

- A driver has an extra `drivers` row (licence number, online status) and **one vehicle**: Bullet, plate `DHAKA-TESLA-11`, **3 seats**.
- **Seat capacity lives here, and nowhere else decides it.** It's checked to be between 1 and 6.
- **"Fixed" means it can't change during a shift.** Editing the vehicle while online returns 409 `DRIVER_ONLINE`. Why: when Jashim goes online, Trip and Matching are told "Bullet has 3 seats", and Trip fills pools on that basis. If he could change it to 2 while carrying Nusrat and Rafiq, Trip could still put Shirin in a seat that no longer exists. So he has to go offline, edit, and come back online, which announces the new number.

### Owns (5): coarse shift status, online or offline

- A driver is `ONLINE` ("I'm working, send me rides") or `OFFLINE`. That's all Identity tracks, which is why the plan calls it **coarse**.
- Going online requires a registered vehicle (otherwise 409 `NO_VEHICLE`).
- Each switch is **announced as an event** (`identity.driver.online` / `.offline`) through the **outbox** from Part 1. It's saved in the same transaction as the status change, so the event can't be lost and can't go out for a change that didn't happen.
  - **Trip** uses it to keep its own list of which drivers are on shift (`driver_shifts`).
  - **Matching** uses it to add or remove Jashim from the "available drivers" set in Redis.

### Delegates (1): "is the driver busy?" belongs to Trip

Identity knows Jashim is *online*. It does **not** know whether Bullet is empty, has Nusrat aboard, or is full. That's **Trip's** data (pools and seats).

This matters in exactly one place: **going offline**. Jashim must not go offline with passengers in the car. So Identity **asks Trip** over HTTP (`GET /internal/drivers/{id}/live-pool`) and refuses with 409 `DRIVER_HAS_LIVE_POOL` if Trip says he has a live pool.

**Why ask instead of keeping a copy:** if Identity also tracked "busy", there would be two sources for the same fact, and they would eventually disagree (a missed event, a crash at the wrong moment). One owner means one answer. This is also the **only** synchronous call Identity makes, and it points "down" the chain (Identity → Trip → Fare/Matching). No service calls Identity back, so the calls can never form a circle (Part 0.2).

### Delegates (2): GPS belongs to Matching

Where Jashim is right now (latitude, longitude, zone) changes every few seconds. That belongs in **Matching's Redis** (`geo:drivers`), not in a SQLite row that would be rewritten constantly. Identity never sees a coordinate.

### What Identity does *not* do (summary)

| Question | Who answers it |
|---|---|
| Is this token valid? (on each request) | **Gateway**, with the public key. Identity only issues tokens |
| Where is Jashim? | Matching |
| Does Jashim have passengers right now? | Trip (Identity asks, just before going offline) |
| Is Bullet full? | Trip |
| How much has Jashim earned? | Fare |

---

## What I did for 3.1

Like 2.1, this section is a **scope definition**, with no code (code starts in 3.2/3.3). So I added no files to `services/identity/` and checked what Identity will depend on:

| Check | Result |
|---|---|
| Part 1 pieces Identity uses: `Database`, `OutboxMixin`, `emit`, `Bus`, `run_outbox_relay`, `InternalAuth`, `ServiceClient`, `health_router`, `DomainError`, `ISSUER` | all import OK in `.venv` |
| **Token contract with the gateway:** made a token exactly the way Part 3's `tokens.py` will, and checked it with the gateway's `verify_jwt` | **accepted.** `sub`, `role`, `name` and `jti` come through correctly; lifetime is 3599 s of 3600 (whole seconds) |
| `tokens.py` uses `utcnow()`, which returns UTC time **without** a time zone attached. Could that shift `exp` by your PC's time zone? | **No.** The lifetime above came out right on this machine, so the expiry isn't skewed |
| Gateway sends `X-Token-Jti` / `X-Token-Exp` for logout | yes, done in 2.4 and covered by a test |
| Gateway routes Identity's URLs (`/auth/*`, `/users/*`, `/drivers/me/*`) and keeps `/internal/*` unreachable from outside | yes (Part 2 route table + the `..` fix) |
| `.env.example` has `JWT_PRIVATE_KEY_PATH`, `JWT_PUBLIC_KEY_PATH`, `JWT_TTL_SECONDS` | yes |
| `argon2-cffi` (password hashing) and `alembic` (database migrations) | **not installed yet**. They come with Identity's `requirements.txt` in 3.2 |

---

## 3.2: the directory structure

### What I created

```
services/identity/
├── Dockerfile               the generic service Dockerfile, set up for identity on port 8001
├── requirements.txt         uvicorn + argon2-cffi (passwords) + alembic (migrations)
├── requirements-dev.txt     test tools (pytest, pytest-asyncio)
├── pytest.ini
├── alembic.ini              Alembic settings (made by `alembic init -t async migrations`)
├── migrations/
│   ├── env.py               how Alembic connects: models' metadata, DB_PATH, batch mode
│   ├── script.py.mako       template for new migration files (Alembic default)
│   └── versions/
│       └── 0001_init.py     creates users, drivers, vehicles, outbox (made in 3.3)
├── app/
│   ├── __init__.py
│   ├── main.py              placeholder: code in 3.4 / 3.6
│   ├── config.py            settings from the environment
│   ├── deps.py              the shared objects: db, auth, trip_client (made in 3.3)
│   ├── models.py            tables (3.3)
│   ├── schemas.py           request/response shapes (3.3)
│   ├── tokens.py            JWT issuing (3.3)
│   ├── seed.py              placeholder: demo users, code in 3.6
│   └── routers/
│       ├── __init__.py
│       ├── auth.py          placeholder: register / login / logout, code in 3.4
│       ├── drivers.py       placeholder: profile, vehicle, online / offline, code in 3.4
│       └── internal.py      placeholder: /internal/users/{id}, code in 3.4
└── tests/                   (3.3, see below)
```

### Compared with the gateway: this service has a database

The gateway had no `models.py` and no `migrations/` (Part 2: "no database"). Identity **owns** `identity.db`, so it gets three things the gateway didn't:

- **`models.py`:** the tables, described in Python.
- **`migrations/` + `alembic.ini`:** **Alembic** turns the models into real tables and, later, into safe step-by-step changes ("add a column") without deleting anyone's data. Each change is one numbered file in `versions/`. The Docker container runs `alembic upgrade head` on every start, which brings the database up to the newest version (and does nothing if it already is).
- **`deps.py`:** the database connection, shared by all routers.

### `migrations/env.py`, the three settings that matter

1. **`target_metadata = Base.metadata`:** tells Alembic what the tables *should* look like (from `models.py`). `alembic revision --autogenerate` compares that with the real database and writes the difference as a migration.
2. **URL from `DB_PATH`:** the database file comes from the same setting the app uses (`/data/identity.db` in Docker), so the app and its migrations can never point at different files. Tests can override it to use a throwaway file.
3. **`render_as_batch=True`:** SQLite can't change most things about an existing table (e.g. add a constraint). "Batch mode" works around this by building a new table, copying the rows across, and swapping it in. Without it, later migrations would fail on SQLite.

### `config.py`

| Setting | Default | Why |
|---|---|---|
| `DB_PATH` | `identity.db` | Docker sets `/data/identity.db` (a volume, so the data survives restarts) |
| `REDIS_URL`, `RABBITMQ_URL`, `INTERNAL_TOKEN`, `JWT_PRIVATE_KEY_PATH` | **none** | Required: the service refuses to start without them rather than half-working |
| `JWT_TTL_SECONDS` | `3600` | tokens last 1 hour |
| `TRIP_URL` | `http://trip:8003` | for the "can Jashim go offline?" question |

It also reads a **`.env` file** from the folder you run it in, if there is one. The plan's local-run instructions (Part 8.3) rely on that: "run `alembic upgrade head && uvicorn ...` with a local `.env` pointing to localhost". Real environment variables still win, and unknown variables in `.env` are ignored.

### `Dockerfile` and `requirements.txt`

Same generic Dockerfile as the gateway, with `SERVICE=identity`, port **8001**, and the plan's start command (Part 8):

```
alembic upgrade head  &&  python -m app.seed  &&  uvicorn app.main:app --port 8001
   (create/update tables)   (demo users, only once)   (start serving)
```

`requirements.txt` adds **`argon2-cffi~=23.1`** (password hashing, as the plan says) and **`alembic~=1.13`**. Both are now installed in `.venv` (argon2-cffi 23.1.0, alembic 1.20.0).

### Two decisions where the plan disagrees with itself

| Plan's folder list says | Plan's code says | What I did |
|---|---|---|
| `app/db.py` and `app/clients.py` | `routers/drivers.py` does `from ..deps import auth, db, trip_client`, and the text says "`deps.py` instantiates `db`, `auth`, `trip_client`" | Made **one `deps.py`**, following the code. The Trip, Fare and Notification folder lists all use `deps.py` too, so all services now look alike. `db.py` and `clients.py` would have been one-line files. |
| no `__init__.py` files | relative imports (`from ..deps`) | Added `app/__init__.py` and `app/routers/__init__.py`, as for the gateway (2.2) |

I also added **`*.db`, `*.db-wal`, `*.db-shm` to `.gitignore`**. From now on services create SQLite files, and a database full of password hashes must never be committed by accident.

---

## 3.3: the data layer

### The four tables

```
users ─────────────┐ 1
  id (PK)          │
  full_name        │
  phone  UNIQUE    │
  password_hash    │
  role  ∈ {PASSENGER, DRIVER, ADMIN}
  created_at       │
                   │ 0..1   (only drivers have one)
drivers ◀──────────┘
  user_id (PK, FK → users.id)
  license_number  UNIQUE
  status  ∈ {OFFLINE, ONLINE}      default OFFLINE
  updated_at                       changes on every update
     │ 1
     │ 0..1  (a driver has at most one vehicle)
vehicles
  id (PK)
  driver_id  FK → drivers.user_id, UNIQUE
  nickname, make, model
  plate  UNIQUE
  seat_capacity  BETWEEN 1 AND 6

outbox        (Part 1's OutboxMixin: events waiting to be published)
```

Why the shape:

- **`drivers` is a separate table, not extra columns on `users`.** Passengers have no licence or status. Keeping them in their own table means no half-empty rows, and "is this user a driver?" is just "does a `drivers` row exist?".
- **The driver's primary key *is* the user id.** One user can be at most one driver.
- **`vehicles.driver_id` is `UNIQUE`:** Jashim can register only one car. Bullet is the car.
- **`ondelete="RESTRICT"`:** you can't delete a user who is a driver, or a driver who has a vehicle. Nothing gets silently orphaned.
- **Rules are in the database, not only in code:** unique phone/plate/licence, allowed roles/statuses, 1–6 seats. Even a bug in the API code (or a manual SQL edit) can't create a 7-seat Tesla or a user with role `SUPERUSER`.
- **Ids are UUID strings** (`new_id()` from Part 1), so the seed data can use the fixed ids from 3.6 and every service agrees on who "Nusrat" is.

### `models.py`: the tables in Python

Copied from the plan, **plus one line** (next section). Points worth knowing:
- `Driver.vehicle` is loaded with `lazy="selectin"`. When you load a driver, SQLAlchemy fetches the vehicle straight away in a second query. That matters in async code, because touching a not-yet-loaded relationship later would crash (async SQLAlchemy can't secretly run a query in the background).
- `class Outbox(OutboxMixin, Base)` gives Identity its own `outbox` table from the Part 1 template. `drivers/me/online` will write the status change and the event in one transaction.

### The one change to the plan's models (a real bug)

**Problem:** the plan's register endpoint (3.4) creates a **`users` row and a `drivers` row in the same transaction**. With the plan's models, that **fails**: `FOREIGN KEY constraint failed`.

**Why:** when you `add()` several objects and commit, SQLAlchemy decides the INSERT order by looking at **`relationship()`s** between the classes. `Driver` → `Vehicle` has one, so vehicles are always inserted after drivers. But `Driver` → `User` only had a foreign-key *column*, no relationship, so SQLAlchemy didn't know users must come first. It inserted the driver first, and the database (correctly) rejected a driver pointing at a user that didn't exist yet.

**Fix:** one line on `Driver`:

```python
user: Mapped[User] = relationship(lazy="raise")
```

Now SQLAlchemy knows the order: users → drivers → vehicles. `lazy="raise"` means it never loads the user behind your back: if code ever reads `driver.user` without loading it explicitly, it gets a clear error instead of a confusing async crash. The plan's `_load()` already fetches `User` and `Driver` together with a join, so nothing else changes.

This is an **ORM-only** change: the database tables are identical (the migration check below confirms it). I proved it both ways: without the line, 15 tests fail; with it, all pass.

### `migrations/versions/0001_init.py`

Generated with `alembic revision --autogenerate -m init --rev-id 0001` (plan 3.6 step 2) and reviewed line by line against the models. It has every table, the `published_at` index on `outbox`, all three CHECK rules, all four UNIQUE rules and both foreign keys with `RESTRICT`. It also has a working `downgrade()`.

### `schemas.py`: what the API accepts and returns

Copied from the plan. These are checked **before** anything touches the database:

| Schema | Rules |
|---|---|
| `RegisterIn` | name 2–100 chars; **phone must be a Bangladeshi mobile number** (`01` + operator digit 3–9 + 8 digits, e.g. `01711000002`); password 8–128 chars; role **only `PASSENGER` or `DRIVER`**, so nobody can register themselves as `ADMIN` |
| `LoginIn` | same phone rule; password is any string (a wrong one just fails to match) |
| `VehicleIn` | seat capacity 1–6 (same rule as the database: two layers of protection) |
| `UserOut` | id, name, phone, role. **`password_hash` is not in it, so it can never leak into a response** |
| `TokenOut` | the login answer: `access_token`, `token_type: "bearer"`, `expires_in`, and the user |
| `DriverOut` | user + licence + status + vehicle (or `null`) |

Note: "a `DRIVER` must give a `license_number`" is **not** in the schema. The plan checks that in the register endpoint (3.4).

### `tokens.py`: printing the ID card

Copied from the plan. `issue_token()` builds the claims (`sub`, `role`, `name`, `jti`, `iss`, `iat`, `exp`), signs them with the **private key** (RS256), and returns the token **and its `jti`**. Every token gets a fresh random `jti`, so logging out on one phone revokes only that phone's token.

### `deps.py`

```python
db = Database(settings.DB_PATH)                     # Part 1: WAL, foreign keys ON, BEGIN IMMEDIATE writes
auth = InternalAuth(settings.INTERNAL_TOKEN)        # Part 1: auth.role("DRIVER") etc.
trip_client = ServiceClient(settings.TRIP_URL, ...) # Part 1: for the offline check
```

Exactly what the plan's note says `deps.py` contains. 3.4 added Redis (for logout) and the private-key loader.

### How 3.2 and 3.3 were checked: 45 tests, all passing

Run them with:
```
cd services\identity
..\..\.venv\Scripts\python -m pytest
```

Every test gets a **fresh database built by the real migration** (not by `create_all`), so the tests check what will actually run in Docker.

| File | Tests | What it proves |
|---|---|---|
| `test_migrations.py` | 3 | `upgrade head` creates exactly `users`, `drivers`, `vehicles`, `outbox`. **`alembic check` finds no difference between models and migration** (catches someone editing `models.py` and forgetting a migration). Downgrade and upgrade again both work |
| `test_models.py` | 17 | user + driver saved together (the bug above); driver loads with Bullet (3 seats, `OFFLINE` by default). **The database refuses:** duplicate phone, unknown role, 0 / 7 / −1 seats (6 is fine), a second car for Jashim, a duplicate plate, a duplicate licence, a car for a non-existent driver, status `BUSY`, deleting a user who is a driver. `updated_at` moves on change. **Outbox:** the event is saved with the status change, and if the transaction crashes, *both* disappear |
| `test_schemas.py` | 21 | valid/invalid phone numbers (too short, too long, `012...`, `02...`, `+880...`, letters); password under 8 rejected; `ADMIN` / unknown roles can't register; seats 1–6 only; `UserOut` never contains the password hash; `DriverOut` builds from database objects |
| `test_tokens.py` | 4 | an issued token **passes the gateway's `verify_jwt`** with the right user, role, name, `jti` and issuer; lifetime exactly 3600 s and not shifted by your PC's time zone; 20 tokens → 20 different `jti`s; an expired token is rejected |

Note: the "vehicle for a non-existent driver" test only passes because Part 1's `db.py` turns on `PRAGMA foreign_keys`. SQLite ignores foreign keys unless told otherwise. So this test also protects that Part 1 setting.

---

## 3.4: the API endpoints

### The endpoint list

What the services see is the gateway URL **minus `/api/v1`**, so a phone calling `POST /api/v1/auth/login` reaches Identity's `POST /auth/login`.

| Method | Route | Who | Success | Errors | File |
|---|---|---|---|---|---|
| POST | `/auth/register` | anyone (via gateway) | 201 `UserOut` | 409 `PHONE_TAKEN`, 409 `LICENSE_TAKEN`, 422 `LICENSE_REQUIRED` / `VALIDATION_ERROR` | `routers/auth.py` |
| POST | `/auth/login` | anyone (via gateway) | 200 `TokenOut` | 401 `INVALID_CREDENTIALS` | `routers/auth.py` |
| POST | `/auth/logout` | any logged-in user | 204 | 400 `TOKEN_CONTEXT_MISSING` | `routers/auth.py` |
| GET | `/users/me` | any logged-in user | 200 `UserOut` | 404 | `routers/auth.py` |
| GET | `/drivers/me` | DRIVER | 200 `DriverOut` | 404 | `routers/drivers.py` |
| PUT | `/drivers/me/vehicle` | DRIVER | 200 `VehicleOut` | 409 `DRIVER_ONLINE`, 409 `PLATE_TAKEN` | `routers/drivers.py` |
| POST | `/drivers/me/online` | DRIVER | 200 `DriverOut` | 409 `NO_VEHICLE` | `routers/drivers.py` |
| POST | `/drivers/me/offline` | DRIVER | 200 `DriverOut` | 409 `DRIVER_HAS_LIVE_POOL`, 503 if Trip can't answer | `routers/drivers.py` |
| GET | `/internal/users/{id}` | other services only | 200 `UserOut` | 404 | `routers/internal.py` |

Every route needs the **`X-Internal-Token`**, meaning the request must have come through the gateway (or from another service). "Anyone" means *no login needed*, not *reachable from the internet directly*. Wrong role → 403 `FORBIDDEN` (from Part 1's `auth.role("DRIVER")`).

### Register (`POST /auth/register`)

Nusrat signs up:

1. The schema checks the input (3.3). A `DRIVER` must also send a `license_number`, else 422 `LICENSE_REQUIRED`.
2. **Hash the password first, outside the database lock.** argon2 takes ~75 ms on purpose. Doing that *inside* the write transaction would hold SQLite's single write lock for 75 ms per signup, blocking everyone else's writes. It also runs in a **thread** (`run_in_threadpool`), so the server keeps answering other requests meanwhile.
3. In **one** transaction: phone already used → 409 `PHONE_TAKEN`; licence already used → 409 `LICENSE_TAKEN`; otherwise insert `users` (+ `drivers` for Jashim). Because it's one `BEGIN IMMEDIATE` transaction, two people registering the same phone at the same instant can't both pass the check: the second waits, then sees the first.
4. Return `UserOut` (no password hash).

The user's id is set **in Python** (`new_id()`) before insert, so the `drivers` row can use it in the same transaction. This also relies on the 3.3 relationship fix.

### Login (`POST /auth/login`)

1. Look up the phone number (read-only session: never blocks writers).
2. Check the password with argon2. **If the phone doesn't exist, check against a dummy hash anyway.** Otherwise "unknown phone" would answer instantly and "wrong password" after 75 ms, and an attacker could time the responses to learn which numbers have accounts. Measured on this PC:

   | Case | Time |
   |---|---|
   | wrong password, real user | 74.8 ms |
   | unknown phone (dummy check) | 75.5 ms |

   Both also return the **exact same** 401 body.
3. On success, `issue_token()` (3.3) signs a 1-hour JWT with the private key. The response is `{access_token, token_type: "bearer", expires_in: 3600, user}`.

### Logout (`POST /auth/logout`)

The gateway has already verified the token and forwards its `X-Token-Jti` and `X-Token-Exp` (Part 2.4). Identity writes `auth:revoked:{jti}` to Redis with a TTL of `exp − now`, so the key disappears exactly when the token would have expired anyway. Afterwards, the gateway rejects that token with 401 `TOKEN_REVOKED`.
- A token that has **already expired** → nothing to write (still 204).
- Headers missing (the request didn't come through the gateway) → 400 `TOKEN_CONTEXT_MISSING`.

### Vehicle (`PUT /drivers/me/vehicle`), an "upsert"

"Upsert" = **create if missing, update if present**. Jashim's first PUT creates Bullet. A later PUT changes Bullet (same vehicle id), never a second car.
- **Online → 409 `DRIVER_ONLINE`.** The seat count was announced when he went online (3.1), so it can't change mid-shift.
- **Plate used by *another* driver → 409 `PLATE_TAKEN`.** Re-sending his own plate is fine.

### Going online (`POST /drivers/me/online`)

The plan's code, unchanged. No vehicle → 409 `NO_VEHICLE`. Otherwise, **in one transaction**: set `ONLINE` and put an `identity.driver.online` event in the outbox:

```json
{"driver_id": "1111...", "driver_name": "Jashim", "vehicle_id": "...",
 "vehicle_nickname": "Bullet", "plate": "DHAKA-TESLA-11", "seat_capacity": 3}
```

That event carries everything Trip and Matching need, so they never have to call Identity back. **Already online → 200 and no second event**, so tapping the button twice is harmless.

### Going offline (`POST /drivers/me/offline`)

1. **Ask Trip first**, *before* opening a transaction (never hold the database lock while waiting on the network): `GET /internal/drivers/{id}/live-pool`, with the request id passed along and one retry.
2. Trip says `{"pool_id": "..."}` → 409 `DRIVER_HAS_LIVE_POOL`. Jashim stays online.
3. Trip says `{"pool_id": null}` → set `OFFLINE` + outbox event `identity.driver.offline` in one transaction.

**One change from the plan (a real bug):** the plan went straight to `resp.json().get("pool_id")`. Part 1's `ServiceClient` only treats **5xx** as failure. So if Trip answered **401** (e.g. the two services have different `INTERNAL_TOKEN`s after a config mistake) or **404**, the error body has no `pool_id`, and that reads as "no pool": **Jashim could go offline with Nusrat and Rafiq still in the car.** Now anything other than 200 → 503, and he stays online. That's **fail closed**: when unsure, keep the safe state.

**The known gap the plan documents:** between Trip answering "no pool" and Identity's commit, a few milliseconds pass. In that window Jashim could still accept a brand-new offer. Trip checks its own `driver_shifts` copy before accepting, and that copy flips to offline as soon as Trip processes the `identity.driver.offline` event, so the window is tiny. The plan accepts it rather than adding a cross-service lock. Doing it perfectly would need both services in one transaction, which separate databases can't have.

### `deps.py` additions

- `redis`, for logout's `auth:revoked:*` key.
- `private_key()` reads `jwt_private.pem` **once** and caches it. Identity is the only service that has this file (Part 8 mounts the whole `keys/` folder only into Identity).

### How 3.4 was checked: 77 tests, all passing

The plan builds `main.py` (RabbitMQ bus, outbox relay, health) in 3.6 step 6. For 3.4, the tests mount the three routers on a small test app, with:
- a fresh migrated database per test (as in 3.3),
- **fakeredis** for logout,
- a **fake Trip** that can answer "no pool", "pool X", 401/404/500, or be down (Trip itself is built in Part 5),
- request headers exactly as the gateway sends them (`X-Internal-Token`, `X-User-*`, `X-Token-*`).

| File | Tests | What it proves |
|---|---|---|
| `test_auth_api.py` | 15 | register a passenger (argon2id hash stored, never the password; no hash in the response); register a driver (drivers row, `OFFLINE`, no vehicle); driver without licence → 422; duplicate phone → 409; duplicate licence → 409 **and nothing half-saved**; bad phone → 422; no internal token → 401; **login token passes the gateway's `verify_jwt`**; wrong password and unknown phone give **identical** 401s; logout stores the revocation with TTL ≈ remaining lifetime; already-expired → nothing stored; missing token headers → 400; `/users/me`; `/internal/users/{id}` needs the internal token |
| `test_drivers_api.py` | 17 | **passenger → `/drivers/me/online` = 403** (plan 3.6 step 8); starts offline with no vehicle; online without vehicle → 409 and no event; vehicle upsert keeps the same id; plate clash → 409; 9 seats → 422; online announces Bullet with all fields; online twice → one event; **edit while online → 409** (plan 3.6 step 8) and the seat count is unchanged; offline asks Trip at the right URL with the internal token and request id, then emits `offline`; **passengers aboard → 409, still online, no event**; Trip down / 500 / **401 / 404** → 503, still online; offline when already offline → no event |
| earlier files | 45 | migrations, models, schemas, tokens (3.3) |

**Checking the tests can fail**, as in 2.6. I broke the code on purpose, one thing at a time:

| Deliberately broke... | Result |
|---|---|
| put back the plan's offline check (no "must be 200") | 2 failed (the 401 and 404 cases) |
| allowed vehicle edits while online | 1 failed |
| emitted "online" every time | 1 failed |
| ignored Trip's live pool | 1 failed |
| let register work without the internal token | 1 failed |
| made logout store nothing | 1 failed |
| removed the duplicate-licence check | 1 failed |

All caught; the code was restored and checked identical afterwards.

**Not covered yet:** that the outbox events actually reach RabbitMQ (needs `main.py`'s outbox relay, 3.6), and the real Trip (Part 5).

---

## Things to know before the next sections

- **Identity is built before Trip** (plan build order: step 2 vs step 5), but going offline calls Trip. Until Part 5 exists, the offline check can only be tested against a **fake Trip**, the same trick used for the gateway tests. In a real run before Trip exists, `POST /drivers/me/offline` will answer **503** (by design: fail closed).
- **`main.py` is still a placeholder.** The endpoints exist and are tested, but the service can't be started with `uvicorn` until 3.6 step 6 wires up the app, RabbitMQ and the outbox relay.
- **No way to create an `ADMIN` yet.** The role exists, but registration only accepts `PASSENGER` or `DRIVER`, and the seed data has no admin. That's fine for now (nothing requires an admin); worth knowing if an admin tool is wanted later.
- **A small known gap, from the plan itself:** between "Trip says Jashim has no pool" and "Jashim is marked offline", a few milliseconds pass in which he could still accept a new offer. Trip closes this as soon as it processes the `offline` event. The plan says to document it rather than fix it; explained in 3.4 ("Going offline").
- **`argon2-cffi` and `alembic` are now installed** in `.venv` (3.2).
- **The gateway's `config.py` doesn't read a local `.env`** the way Identity's now does (Part 8.3's "run a service outside Docker" needs it). It's a small fix, worth making before Part 8. It doesn't affect Docker, where `env_file: .env` provides the variables.
- **Future migrations and unnamed UNIQUE rules:** the plan gives the CHECK rules names (`ck_user_role`...) but not the UNIQUE ones. That's fine now; if a later migration ever needs to *drop* one of those UNIQUE rules on SQLite, it will have to name it by hand.
- **`keys/` is still empty.** Identity needs the **private** key to sign tokens: run `sh scripts/gen_keys.sh` before running it for real (3.6 step 3).
- **Demo users** (from 3.6): Jashim, Nusrat, Rafiq and Shirin get **fixed ids** (e.g. Nusrat = `22222222-...`) so every service's seed data lines up. Password for all: `Pool@1234`.
