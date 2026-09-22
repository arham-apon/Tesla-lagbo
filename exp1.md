# Step 1 Explained: the shared library `tesla_common`

## The big picture in 30 seconds

We are building a ride-pooling backend for Dhaka (Jashim drives Bullet, a 3-seat Tesla; Nusrat, Rafiq and Shirin share rides).
It is split into **6 small services** (Gateway, Identity, Matching, Trip, Fare, Notification).
Each one is its own FastAPI app with its own SQLite database, and they talk to each other through HTTP and RabbitMQ.

All 6 services need the same "plumbing": connecting to a database, sending events, checking who is calling, logging and so on.
Writing that 6 times would mean 6 copies with 6 different sets of bugs.
So **Step 1 builds that plumbing once**, as a small Python package called `tesla_common`, and every service will install it.

> Rule from the plan: this library holds **plumbing only**. It has no business logic (no fares, no pools) and no shared tables. Each service owns its own data.

---

## What I did

| # | What | Where |
|---|---|---|
| 1 | Created the project folders from the plan (§1.2) | `libs/common/`, `services/*`, `scripts/`, `keys/` |
| 2 | Wrote the 8 library files, copied exactly from the plan (§1.3) | `libs/common/tesla_common/*.py` |
| 3 | Made it installable (`pyproject.toml`) | `libs/common/pyproject.toml` |
| 4 | Created a **Python 3.12** virtual env (your PC has 3.13/3.14, the plan needs 3.12) and installed the library with all its dependencies | `.venv/` |
| 5 | Wrote 3 check scripts and ran them (steps 1.4.3, 1.4.4 and 1.4.5) | `libs/common/checks/` |
| 6 | Added project setup files: env template, key script, and a Docker file with only RabbitMQ + Redis | `.env.example`, `scripts/gen_keys.sh`, `docker-compose.yml` |
| 7 | Added `keys/` to `.gitignore` so the JWT private key can never be committed | `.gitignore` |

**Not done on purpose:** step 1.4.6 ("tag the lib v0.1.0") is a git tag, and you said not to touch git. You can run it yourself later.

### Libraries installed (and why)

| Library | Used for |
|---|---|
| `fastapi` | Web framework for every service |
| `sqlalchemy[asyncio]` + `aiosqlite` | Talking to SQLite without blocking (async) |
| `aio-pika` | Talking to RabbitMQ (events) |
| `redis` | Talking to Redis (cache, rate limits, live locations) |
| `httpx` | One service calling another over HTTP |
| `pyjwt[crypto]` | Checking login tokens (JWT, RS256) |
| `pydantic` / `pydantic-settings` | Validating data and reading settings from `.env` |

---

## The code, file by file

### `timeutil.py`: two tiny helpers
- `utcnow()` gives the current time in UTC. Every service uses the same clock format, so timestamps never mix time zones.
- `new_id()` gives a random UUID string, used as the id for rides, pools, events and so on.

### `errors.py`: one error format for everyone
- `DomainError("POOL_FULL", "No seats left", 409)` is how any service says "this request is not allowed".
- `install_error_handlers(app)` turns that into the same JSON shape every time:
  ```json
  {"error": {"code": "POOL_FULL", "message": "No seats left", "request_id": "req-42", "details": null}}
  ```
  Bad input (for example `seats: "two"`) gets the same shape with code `VALIDATION_ERROR` (422).
  **Why:** clients handle one error format instead of six.

### `logging.py`: logs as JSON
Every log line becomes one JSON object with `service`, `level`, `msg` and, when available, `ride_id` / `pool_id` / `event_id`.
**Why:** when 6 services log at once you can search "everything about ride X" across all of them.

### `db.py`: the safe SQLite setup (the most important file)
Each service gets **two connections** to its database:
- **`rw` (read-write):** every transaction starts with `BEGIN IMMEDIATE`, which takes SQLite's write lock **at the start**. If two requests try to write at the same moment, the second **waits** (up to 5 s, `busy_timeout`) instead of both writing.
- **`ro` (read-only):** a normal `BEGIN`. Thanks to **WAL mode**, reads never wait for writers.

**Why this matters:** imagine Rafiq and Shirin both try to take Bullet's last seat at the same moment. Without the up-front lock, both read "1 seat free", both write, and the car is overbooked. With `BEGIN IMMEDIATE` the second one waits, then sees "0 seats free". The check script proves this:
```
 0.00s  A: got write lock, read occupied=0
 0.11s  B: asking for write lock
 0.20s  reader: read occupied=0 without waiting     <- reads are not blocked
 1.00s  A: committed (read 0, wrote 1)
 1.01s  B: got write lock, read occupied=1           <- B waited for A
 1.02s  B: committed (read 1, wrote 2)               <- no lost update
```
Other settings: `foreign_keys=ON` (enforce table links) and `synchronous=NORMAL` (fast and still safe with WAL).

### `auth.py`: who is calling?
- The **Gateway** checks the user's login token (JWT). It then forwards the request with headers such as `X-User-Id` and `X-User-Role`, plus a secret `X-Internal-Token`.
- Services trust those `X-User-*` headers **only if** the internal token matches. Otherwise anyone could send `X-User-Role: ADMIN` directly.
- Ready-made FastAPI dependencies:
  - `auth.require_internal()` means only other services may call this route (401 otherwise).
  - `auth.principal()` gives you the logged-in user as a `Principal(user_id, role, name)`.
  - `auth.role("DRIVER")` means only drivers are allowed (403 otherwise).
- `verify_jwt()` checks the token signature (RS256 public key), issuer and expiry, and requires `sub`, `jti`, `role`, `iat` and `exp`.
- `hmac.compare_digest` compares secrets in constant time, so an attacker can't guess the token from how long the check takes.

### `http.py`: calling another service
`ServiceClient` wraps `httpx` with:
- short timeouts (3 s, 1 s to connect), so one slow service can't freeze the others
- the internal token added automatically
- **retries only for GET.** Retrying a POST such as "create ride" could create it twice.
- any failure (down, timeout, 5xx) becomes `DomainError("UPSTREAM_UNAVAILABLE", ..., 503)`

### `events.py`: RabbitMQ events, done safely
This file solves 3 classic problems:

1. **"I saved to the DB but crashed before sending the event."** Solved with the **transactional outbox.**
   `emit()` does not send anything. It writes the event into an `outbox` table **in the same transaction** as the business change, so both are saved or neither is.
   A background loop, `run_outbox_relay()`, reads unsent rows, publishes them to RabbitMQ, then marks them `published_at`.

2. **"The same event arrived twice."** Solved with **idempotent consumers.**
   The relay can crash after publishing but before marking the row, so the event gets sent again. Consumers call `first_time(session, event_id)`: if that id is already in the `processed_events` table, they skip it.

3. **"The handler crashed while processing a message."** Solved with **retry + dead-letter queue.**
   For queue `X`, `Bus.consume()` creates three queues:
   - `X`: the normal queue
   - `X.retry`: messages sit here 5 s, then RabbitMQ sends them back to `X`
   - `X.dlq`: the "parking lot" for messages that failed 4 times, kept for a human to inspect

   Each attempt is counted in the `x-attempt` header.

Also included:
- `OutboxMixin` and `ProcessedEventMixin` are table templates each service reuses.
- The **envelope** (`event_id`, `event_type`, `occurred_at`, `producer`, `version`, `data`) is the standard wrapper around every event.
- `consume_broadcast()` gives a temporary queue per instance. Notification uses it to push to the WebSockets it holds.

### `health.py`: `/health` endpoint
You give it checks (for example "can I reach the DB?"). It returns 200 `ok` if all pass, or 503 `degraded` naming the one that failed. Docker uses this to know when a service is ready.

---

## How the checks were verified (all passing)

| Script | Plan step | Proves |
|---|---|---|
| `check_db.py` | 1.4.3 | Two writers wait for each other (no lost update); readers don't wait |
| `check_events.py` | 1.4.4 | Failing handler: 1 try + 3 retries, 5 s apart, then the message is parked in `.dlq` with `x-attempt=4`. The outbox relay publishes, and a duplicate is dropped by `first_time()` |
| `check_api.py` | 1.4.5 | Auth gives 401/401/200/403, validation gives 422, `DomainError` JSON includes `request_id`, `/health` gives 200/503, JWT rejects expired / wrong-issuer / missing-`jti` tokens, a dead upstream gives 503 |

Run them yourself (PowerShell, from the project folder):
```
.venv\Scripts\python libs\common\checks\check_db.py
.venv\Scripts\python libs\common\checks\check_api.py
```
`check_events.py` needs RabbitMQ running (`docker compose up -d rabbitmq`, see the port note below):
```
.venv\Scripts\python libs\common\checks\check_events.py
```

---

## Things to know

- **Port clash:** another project on your PC (`mse-*` containers) starts with Docker Desktop and already uses ports **5672, 6379 and 8000–8007**. These are the same ports this plan uses. Stop those containers before running `docker compose up` here. (For the test I used a temporary RabbitMQ on port 5673, which is now deleted.)
- **RabbitMQ tip:** don't run `docker exec <rabbit> rabbitmq-diagnostics ...` as root while RabbitMQ is still starting. It creates a root-owned `.erlang.cookie` and RabbitMQ then crashes on boot. Use `docker exec -u rabbitmq ...`.
- `.env` was copied from `.env.example`. It is gitignored, so change the passwords there.
- `services/*` folders are empty on purpose. They get filled in the next parts.
