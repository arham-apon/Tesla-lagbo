# Step 2 Explained: the API Gateway

## Progress

| Section | What it is | Status |
|---|---|---|
| 2.1 | Overview & domain scope | **Done** (explained below) |
| 2.2 | Directory structure | **Done** (explained below) |
| 2.3 | Data layer | **Done** (explained below) |
| 2.4 | Routing table & endpoints (the code) | **Done** (explained below) |
| 2.5 | Messaging integration | **Done** (explained below) |
| 2.6 | Step-by-step build + tests | **Done** (explained below) |

**Part 2 is complete.** The gateway is written and covered by 65 automated tests, all passing. Run them with:

```
cd services\gateway
..\..\.venv\Scripts\python -m pytest
```

---

## The big picture in 30 seconds

Part 1 built the shared plumbing (`tesla_common`). Part 2 builds the **front door**.

Passengers and drivers never talk to Identity, Trip, Fare or Matching directly. Every REST call goes to **one address, the Gateway on port 8000**, which:

1. checks who you are,
2. checks you aren't sending too many requests,
3. makes sure a double-tap doesn't do the same thing twice,
4. forwards the request to the right service.

Think of it as the receptionist of an office building: they check your ID card, send you to the right floor, and have no say in what happens on that floor.

```
Nusrat's phone ──HTTPS──▶  Gateway :8000  ──▶ Identity :8001   (login, profile)
Jashim's phone ─────────▶  (auth, limits,  ──▶ Matching :8002   (zones, GPS)
                            idempotency,   ──▶ Trip     :8003   (rides, pools)
                            routing)       ──▶ Fare     :8004   (fares, wallet)
                                           ──▶ Notification :8005 (inbox)
                               │
                               └── Redis (counters, idempotency keys, revoked tokens)
```

(The live WebSocket goes straight to Notification on 8005, not through the gateway. The plan does this on purpose so the gateway doesn't have to proxy WebSockets.)

---

## 2.1: what the gateway owns and what it delegates

The plan says in two lines:

> **Owns:** request authentication (JWT RS256 verify), rate limiting, idempotency, routing, request ids.
> **Delegates:** everything domain-related. No database. It never inspects business payloads.

Here is what each part means in practice.

### Owns (1): authentication, "who is this?"

- When Nusrat logs in, **Identity** gives her a JWT: a signed token that says "user 42, role PASSENGER, name Nusrat, expires at 09:41".
- Identity signs it with the **private key**. The gateway only has the **public key**, so it can *check* tokens but can never *create* one. If the gateway were hacked, the attacker still could not make fake tokens.
- For each request the gateway checks the signature, the expiry, the issuer (`tesla-identity`) and that the token has not been **revoked**. Logging out writes `auth:revoked:{jti}` to Redis, and the gateway checks that key.
- After a successful check the gateway forwards the request with `X-User-Id`, `X-User-Role` and `X-User-Name` headers plus the secret `X-Internal-Token`. Services trust those headers only when the internal token matches (that is `auth.principal()` from Part 1).
- **Security rule:** the gateway **deletes** any `X-User-*` header the client sent itself. Otherwise Rafiq could send `X-User-Role: DRIVER` and accept rides.
- A few routes are **public** (register, login, list zones), so you can use them before you have a token.

**Why here and not in every service:** checking the token in one place means one piece of code, one public key location and one revocation check. The services behind the gateway only read plain headers.

### Owns (2): rate limiting, "slow down"

- Each route has a per-minute limit: **10/min** for login and register (so passwords can't be guessed quickly), **60/min** for driver GPS updates, **120/min** for everything else.
- Logged-in users are counted by user id. Public routes are counted by IP address.
- Implementation (in 2.4): a Redis counter `rl:{route}:{who}:{minute}` that expires after 60 s. The 11th login attempt within a minute gets **429 RATE_LIMITED**.

**Why here:** blocking abuse at the door means the services never see the extra traffic.

### Owns (3): idempotency, "you already asked that"

- Nusrat is on patchy mobile data. She taps **Request ride**, the screen hangs, and she taps again. Without protection she gets **two rides**.
- The app sends an `Idempotency-Key` header (a random id per tap-intent). The gateway stores the key in Redis:
  - first time: the key is saved as `PENDING` and the request is forwarded
  - same key again while the first request is still running: **409** "still processing"
  - same key again after it finished: the **saved response is replayed** (with `Idempotent-Replay: true`), and Trip is not called again
  - same key but a *different* body: **422**, because the key was reused for a different request
- `POST /api/v1/rides` **requires** the header. Creating a ride twice is the one mistake that costs real money and seats.

**Why here:** every POST gets the same protection without each service writing its own version.

### Owns (4): routing, "which floor?"

- The gateway has a table of URL prefixes, and each prefix points to one service. It picks the **longest matching prefix**:
  - `/api/v1/driver/location` → **Matching** (GPS updates)
  - `/api/v1/driver/offers/...` → **Trip** (matches the shorter `/api/v1/driver`)
  - `/api/v1/driver/earnings` → **Fare**
- Then it removes `/api/v1` and forwards, so `/api/v1/rides` becomes `http://trip:8003/rides`.
- If no prefix matches, the client gets **404 NOT_FOUND** and no service is called.

**Why longest-prefix:** three different services own URLs under `/driver`. Checking the most specific prefix first guarantees each goes to the right place.

### Owns (5): request ids, "follow this request everywhere"

- Every request gets an `X-Request-Id` (the client's own, or a new UUID). It is passed to the service, returned in the response, and shown in error bodies (`"request_id": ...`, from Part 1's `errors.py`) and JSON logs.
- If Nusrat reports "my ride request failed", one id finds every log line across the gateway, Trip, Fare and Matching.

### Delegates: everything domain-related

The gateway **does not know** what a ride, pool, seat, fare or zone is. Examples of what it will never do:

| Question | Who answers it (not the gateway) |
|---|---|
| Does Bullet have a free seat for Shirin? | Trip |
| How much is Banani → Mohakhali? | Fare |
| Which drivers are near Banani? | Matching |
| Is this password correct? | Identity |
| Can Nusrat still cancel? | Trip (state machine) |

**"No database":** the gateway keeps no tables. Its short-lived state lives in Redis:

| Redis key | What | Lifetime |
|---|---|---|
| `rl:*` | rate-limit counters | 60 s |
| `idem:*` | idempotency records | 120 s while pending, 24 h when done |
| `auth:revoked:*` | revoked tokens (**read** only; Identity writes them) | until the token would have expired |

Because of this the gateway is **stateless**: you could run 3 copies behind a load balancer and they would behave the same, because all shared state is in Redis.

**"Never inspects business payloads":** the gateway passes the request body through **unchanged**. It does not parse the JSON or check `seats` or zones. (The idempotency check computes a hash of the raw bytes to detect "same request". That compares bytes; it doesn't read them.) Checking input stays in the service that owns it, so each rule lives in **one** place. If the gateway also checked `seats <= 6`, a later change would have to be made in two places, and eventually the two would disagree.

### The order of checks (and why)

For every `/api/v1/...` request:

```
match route ─▶ verify JWT ─▶ rate limit ─▶ idempotency ─▶ forward ─▶ save response for idempotency
  (404)         (401)          (429)         (400/409/422/replay)  (503/504 if service down)
```

- **Route first:** an unknown URL is rejected right away, before any work.
- **Auth before rate limit:** we need to know *who* is calling to count per user. Public routes are counted per IP instead.
- **Rate limit before idempotency:** a flood of requests should be blocked before it writes keys into Redis.
- **Forward last**, and if the service is down (503/504) or returns 5xx, the idempotency key is **deleted**, so the client can safely retry.

---

## What I did for 2.1

2.1 is a **scope definition**. The plan has no code in it (the code starts in 2.2 and 2.4), so I didn't add any files to `services/gateway/` yet. I checked that everything the gateway needs from Part 1 is ready:

| Gateway needs | From | Check |
|---|---|---|
| `Principal`, `verify_jwt`, issuer `tesla-identity` | `tesla_common.auth` | imports OK in `.venv` (Python 3.12.13) |
| `DomainError`, `install_error_handlers` | `tesla_common.errors` | imports OK |
| `configure_logging` | `tesla_common.logging` | imports OK |
| `new_id` (request ids) | `tesla_common.timeutil` | imports OK |
| `httpx`, `redis.asyncio`, `pyjwt`, `pydantic-settings` | installed in Part 1 | imports OK |
| `REDIS_URL`, `INTERNAL_TOKEN`, `JWT_PUBLIC_KEY_PATH` | `.env.example` | all present |

---

## 2.2: the directory structure

### What I created

```
services/gateway/
├── Dockerfile            how to build the gateway's container
├── requirements.txt      extra packages on top of tesla_common (just uvicorn)
└── app/
    ├── __init__.py       marks app/ as a Python package (see note below)
    ├── main.py           the FastAPI app: startup/shutdown, /health, the catch-all proxy route
    ├── config.py         settings from .env (Redis URL, internal token, key path, service URLs)
    ├── routes_table.py   "which URL prefix goes to which service" + longest-prefix match
    ├── security.py       JWT check + "was this token revoked?"
    ├── ratelimit.py      the per-minute Redis counter
    ├── idempotency.py    the Idempotency-Key logic (PENDING / DONE / replay)
    └── proxy.py          forwarding: which headers to drop, which to add, error mapping
```

In 2.2 each `app/*.py` file only contained a one-line description. The real code came in section **2.4** (now done). That way 2.2 is only the "skeleton" and 2.4 is only the "flesh", matching how the plan is split.

### Why the files are split this way

One file per job from 2.1:

| 2.1 job | File |
|---|---|
| Authentication | `security.py` |
| Rate limiting | `ratelimit.py` |
| Idempotency | `idempotency.py` |
| Routing | `routes_table.py` (where to) + `proxy.py` (how to forward) |
| Request ids + putting it all together | `main.py` |
| Settings | `config.py` |

**Why:** each file can be tested alone. The 2.6 tests do exactly that: `match()` is tested without Redis, the rate limiter without a real upstream, and so on. `main.py` is the only file that knows the *order* of the checks.

What's **not** here, compared with the other services: no `db.py`, no `models.py`, no `migrations/`, no `alembic.ini`. That's 2.1's "no database" shown in the file list.

### `Dockerfile`

This is the plan's **generic service Dockerfile** from Part 8.1, with `SERVICE` defaulting to `gateway`:

```dockerfile
FROM python:3.12-slim
ARG SERVICE=gateway
...
COPY libs/common /libs/common                      # 1. the shared library
COPY services/${SERVICE}/requirements.txt .        # 2. only the requirements file
RUN pip install --no-cache-dir /libs/common -r requirements.txt
COPY services/${SERVICE}/ .                        # 3. then the code
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- **Build context is the repo root**, not `services/gateway/`, because the image also needs `libs/common`.
- **Requirements before code:** Docker caches each step. If you only edit `proxy.py`, step 3 is re-run but the slow `pip install` is reused from cache.
- I added `EXPOSE 8000` and a default `CMD` (the plan's compose file passes the same command anyway), so the image also runs on its own with `docker run`.
- The **JWT public key is not baked into the image**. Compose mounts `keys/jwt_public.pem` into `/run/keys/` at run time (Part 8). Keys should never be inside an image.

### `requirements.txt`

```
uvicorn[standard]~=0.30
```

That's all. Everything else the gateway uses (FastAPI, httpx, redis, pyjwt, pydantic-settings) is already a dependency of `tesla_common`, and the Dockerfile installs that first. `uvicorn` is the server that actually runs the FastAPI app; `[standard]` adds the faster event loop and HTTP parser.

### Two small additions (not in the plan's tree)

| File | Why |
|---|---|
| `services/gateway/app/__init__.py` | `main.py` uses relative imports (`from . import idempotency, ratelimit`, `from .config import settings`). Those need `app` to be a package. Python 3 would often cope without it, but an explicit `__init__.py` avoids surprises with tools like pytest. |
| `.dockerignore` (repo root) | The build context is the whole repo, so without this Docker would upload `.venv/` (hundreds of MB), `.git/`, `.env` and `keys/` on every build. Leaving out `.env` and `keys/` also guarantees secrets never end up in an image. All six services will use it. |

### How 2.2 was checked

| Check | Result |
|---|---|
| Installed `requirements.txt` into `.venv` (`uv pip install`, because the venv was made by uv and has no `pip`) | `uvicorn 0.53.0` installed (matches `~=0.30`, which means ≥0.30 and <1.0) |
| Imported all 8 `app.*` modules from `services/gateway/` | all import cleanly |
| `docker build` | **not run.** Docker Desktop wasn't running. Try it yourself after starting Docker: `docker build -f services/gateway/Dockerfile -t tesla-gateway .` from the repo root. It will build, but the container won't start until 2.4 fills in `main.py`. |

---

## 2.3: the data layer

The plan's whole section is one line:

> None (stateless). Redis keys: `rl:*`, `idem:*`, reads `auth:revoked:*` (see 0.5).

So, like 2.1, **there is no code to write for 2.3**. No tables, no migrations, no models. What 2.3 does define is the **contract**: the exact Redis keys the gateway reads and writes. The code in 2.4 must follow it, and so must Identity (which writes one of the key types). This section spells that contract out.

### Why Redis and not a database

The gateway needs to remember three small things for a short time. Redis fits each one better than a SQLite file:

| Need | Redis feature that does it |
|---|---|
| "How many requests has Nusrat made this minute?" | `INCR` is **atomic**: two requests at the same moment can't both read 9 and both write 10 |
| "Has this Idempotency-Key been seen?" | `SET ... NX` means "only set if it doesn't exist yet", as **one** atomic step. Exactly one of two duplicate taps wins |
| "Forget this after 60 s / 24 h" | every key can have a **TTL** (`EX`); Redis deletes it automatically, with no cleanup job |
| Several gateway copies must agree | all copies talk to the **same** Redis. A SQLite file lives on one machine |

Nothing in these keys is precious. If Redis is wiped, the worst case is that rate-limit counters restart at 0, a retry is sent upstream instead of replayed, and a logged-out token works again until it expires. No ride, fare or user is lost; that data lives in the services' own databases.

### The three key families

#### 1. `rl:*`: rate-limit counters (read + write)

| | |
|---|---|
| **Key** | `rl:{route prefix}:{user id or IP}:{minute number}` |
| **Example** | `rl:/api/v1/auth/login:203.0.113.9:29776412` |
| **Value** | a number: requests so far in this minute |
| **TTL** | 60 s |
| **Commands** | `INCR` then `EXPIRE`, sent together in one pipeline |

The "minute number" is `unix time ÷ 60`, rounded down. At 08:41:59 and 08:42:00 you are in *different* minutes, so a new key starts from 1. This is called a **fixed window**. It's simple, with one known weakness: someone can send 10 logins at 08:41:59 and 10 more at 08:42:00, i.e. 20 in two seconds. For this project that's acceptable. (A "sliding window" fixes it but needs more Redis work per request.)

**Difference from Part 0.5:** the Redis table in 0.5 writes this key as `rl:{principal_or_ip}:{epoch_minute}`, with no route in it. The 2.4 code puts the **route prefix** in the key too. The code is what will actually run, so the real rule is: **each route has its own counter per user**. Rafiq's 120 ride requests don't use up his 120 notification reads. I'm following the code and noting the difference here.

#### 2. `idem:*`: idempotency records (read + write)

| | |
|---|---|
| **Key** | `idem:{user id}:{Idempotency-Key header}` |
| **Example** | `idem:5b1c…e9:tap-7f3a` |
| **Value while running** | `{"state": "PENDING", "fp": "<sha256 of method+path+body>"}` |
| **Value when finished** | `{"state": "DONE", "fp": "...", "status": 201, "body": "<the JSON response>"}` |
| **TTL** | **120 s** while `PENDING`, **24 h** once `DONE` |
| **Commands** | `SET NX EX` (claim), `GET` (check), `SET EX` (store result), `DEL` (give up) |

The lifecycle of Nusrat's double-tap:

```
tap 1: SET idem:nusrat:tap-7f3a {PENDING} NX EX 120   → OK      → forward to Trip
tap 2: SET idem:nusrat:tap-7f3a {PENDING} NX EX 120   → refused → GET → PENDING → 409 "still processing"
Trip answers 201:  SET idem:nusrat:tap-7f3a {DONE, 201, body} EX 86400
tap 3: SET ... NX → refused → GET → DONE, same fp → replay 201 + "Idempotent-Replay: true"
```

Design points in these keys:
- **The user id is part of the key.** If two phones happen to pick the same key string, Nusrat's and Rafiq's requests still can't collide or see each other's responses.
- **Why the short 120 s for `PENDING`:** if the gateway crashes mid-request, the key would otherwise stay "in progress" forever and block every retry. The upstream timeout is 5 s, so 120 s is plenty, and afterwards the key expires by itself.
- **Why 24 h for `DONE`:** a phone that went offline can come back hours later and retry. It still gets the original answer instead of a second ride.
- **`fp` (fingerprint)** is what catches "same key, different request" (→ 422). The hash covers the raw body bytes, so the gateway still never parses the payload (2.1).
- **5xx or upstream down → `DEL`**. The request may not have happened, so the client must be allowed to try again with the same key.

#### 3. `auth:revoked:*`: logged-out tokens (read only)

| | |
|---|---|
| **Key** | `auth:revoked:{jti}` (`jti` = the token's unique id, a claim inside the JWT) |
| **Value** | `1` (only whether the key exists matters) |
| **TTL** | the token's remaining lifetime (`exp − now`) |
| **Written by** | **Identity**, on `POST /auth/logout` (Part 3.5) |
| **Read by** | Gateway (`EXISTS`), and Notification for WebSocket logins (Part 7) |

JWTs can't be "switched off": a signed token is valid until it expires. So logout keeps a **denylist** of token ids. The TTL trick keeps it tiny: once the token would have expired anyway, the signature check rejects it, so the denylist entry is no longer needed and Redis drops it.

### Shared Redis, separate prefixes

All services use the same Redis (`redis://redis:6379/0`). Keys can't clash because every owner has its own prefix:

| Prefix | Owner |
|---|---|
| `rl:`, `idem:` | Gateway |
| `auth:revoked:` | Identity writes; Gateway + Notification read |
| `geo:`, `driver:`, `drivers:`, `loc:pool:` | Matching |
| `fare:dist:` | Fare |
| `notif:pool:` | Notification |

### Two things to decide in 2.4 (found while checking 2.3), both done in 2.4

1. **The gateway must forward `X-Token-Jti` and `X-Token-Exp`** so Identity can write `auth:revoked:{jti}` with the right TTL (Part 3's Logout note). The 2.4 `proxy.py` is missing these. This was noted in 2.1 and is still open.
2. **What happens when Redis is down.** In the 2.4 code, a Redis failure is not a `DomainError`, so clients would get a bare `500 Internal Server Error` instead of the standard `{"error": {...}}` JSON. Every request touches Redis (the rate limiter runs even on public routes), so the gateway **fails closed**: nothing gets through. That is the safe choice, since we'd rather refuse requests than let revoked tokens in. But it should return a proper **503** in the standard format. I'll propose that small change when writing 2.4.

### How 2.3 was checked

- Compared the key names, TTLs and commands in the 2.3 line, the 0.5 Redis table, the 2.5 command list, and the 2.4 code. They all agree except the `rl:` key format (explained above).
- Compared `auth:revoked:{jti}` with Identity's side (Part 3: `SET auth:revoked:{jti} 1 EX <remaining>`). They match.
- No live Redis test yet: 2.3 has no code to run, and Redis isn't running (Docker Desktop is off). The real Redis tests are in 2.6 (rate limit, replay, 422, 5xx deletes the key).

---

## 2.4: the code

This is where the gateway becomes real. All seven `app/*.py` files now contain the plan's code, with the fixes listed further down.

### One request, start to finish

Nusrat taps **Request ride**. Her phone sends:

```
POST /api/v1/rides
Authorization: Bearer eyJ...            (her JWT)
Idempotency-Key: tap-7f3a
{"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 1}
```

`main.py → proxy()` then does, in order:

| Step | Code | What happens | If it fails |
|---|---|---|---|
| 1 | dot-segment check *(new, see fixes)* | path has no `.` or `..` parts | 404 |
| 2 | `match(routes, "/api/v1/rides")` | → `Route("/api/v1/rides", TRIP_URL)` | 404 `NOT_FOUND` |
| 3 | new `request_id` | client's `X-Request-Id`, or a fresh UUID | — |
| 4 | `authenticate()` | token OK, not revoked → `Principal(nusrat, PASSENGER)` + the token's claims | 401 |
| 5 | `ratelimit.enforce()` | `rl:/api/v1/rides:nusrat:<minute>` → 1 (≤ 120) | 429 |
| 6 | `idempotency.begin()` | `SET idem:nusrat:tap-7f3a PENDING NX` → claimed | 400 / 409 / 422, or a replay |
| 7 | `forward()` | `POST http://trip:8003/rides` with the headers below | 503 / 504, key deleted |
| 8 | `idempotency.finish()` | stores `DONE` + Trip's 201 response for 24 h (deleted instead if 5xx) | — |
| 9 | `to_response()` | Trip's answer goes back to Nusrat, plus `X-Request-Id` | — |

What Trip receives:

```
X-Internal-Token: <secret>       ← proves "this came through the gateway"
X-User-Id: <nusrat's id>         ← from the verified token, never from the client
X-User-Role: PASSENGER
X-User-Name: Nusrat
X-Token-Jti / X-Token-Exp        ← for Identity's logout (new, see fixes)
X-Request-Id: <id>
Idempotency-Key, Content-Type, ...  (the rest of the client's headers)
(no Authorization header: the services never see the raw token)
```

### The files

**`config.py`:** a `Settings` class. `pydantic-settings` fills it from environment variables (`.env` in Docker). `REDIS_URL`, `INTERNAL_TOKEN` and `JWT_PUBLIC_KEY_PATH` have no default, so the gateway **refuses to start** if one is missing, instead of running half-configured. The five service URLs default to their Docker Compose names (`http://trip:8003`, ...).

**`routes_table.py`:** the list of 13 prefixes → services, plus `match()`.
- `build_routes()` **sorts longest first**, so `/api/v1/driver/location` (Matching) is tried before `/api/v1/driver` (Trip).
- `match()` requires an exact match or the prefix followed by `/`, so `/api/v1/ridesXYZ` does **not** match `/api/v1/rides`.
- `public=True` only on register, login and zones. `rate_per_min=10` on register/login and `60` on driver location.

**`security.py`:** `authenticate()` reads `Authorization: Bearer ...`, calls Part 1's `verify_jwt()` (signature + issuer + expiry + required claims), then checks `auth:revoked:{jti}` in Redis. Each failure has its own code, so the app knows what to do: `TOKEN_EXPIRED` → silently log in again; `TOKEN_REVOKED` / `INVALID_TOKEN` → send the user to the login screen.

**`ratelimit.py`:** `INCR` + `EXPIRE` in **one pipeline** (a single round trip to Redis, run together). If the count goes over the limit → 429.

**`idempotency.py`:** `begin()` and `finish()`, exactly the life cycle described in 2.3. Only `POST`/`PATCH` with a key are tracked. `GET`, `PUT` and `DELETE` are naturally safe to repeat, so they don't need it.

**`proxy.py`:**
- `upstream_headers()` builds the header list shown above. It **drops** hop-by-hop headers (these only apply to one connection, such as `Connection` and `Host`), `Authorization`, any client-sent `X-Internal-Token`, and anything starting with `X-User-` or `X-Token-`. Then it adds the real ones.
- `forward()` sends the request with the shared `httpx` client (5 s timeout, 1 s to connect). A timeout → **504**; a service that is down → **503**.
- `to_response()` copies the service's answer back and removes `Content-Encoding` (httpx already unzipped the body, so the header would be wrong).

**`main.py`:** creates the Redis connection, the HTTP client, the route table and the public key **once at startup** (`lifespan`) and closes them on shutdown. Then comes the `/health` route and the `proxy()` route that runs the table above.

### What I changed from the plan's code (and why)

| # | Change | File | Why |
|---|---|---|---|
| 1 | **Block `.` and `..` in the path → 404** | `main.py` | **Security hole.** `GET /api/v1/zones/%2e%2e/internal/match/evaluate` matches the *public* `zones` route (no login needed). httpx then collapses `/zones/../internal/...` into `/internal/match/evaluate`, and the gateway adds the secret `X-Internal-Token`. So **anyone on the internet** could call internal-only endpoints. The same works behind a login (`/api/v1/rides/%2e%2e/internal/...` → Trip's internal routes). I checked that httpx really collapses `..`, and that uvicorn passes a raw `..` through unchanged. |
| 2 | **Forward `X-Token-Jti` and `X-Token-Exp`** (and drop client-sent `X-Token-*`) | `proxy.py`, `security.py`, `main.py` | Part 3's logout note needs them. `authenticate()` now also returns the token's claims so `upstream_headers()` can read `jti` and `exp`. Clients can't send their own copies: otherwise someone could revoke *another* user's token through logout. |
| 3 | **Redis down → 503 `DEPENDENCY_UNAVAILABLE`** in the standard error JSON | `main.py` | Promised in 2.3. It was a bare 500 before. It still **fails closed** (nothing gets through without Redis). |
| 4 | **`/health` checks Redis *and* all 5 services** | `main.py` | The plan's own endpoint table says "Redis ping + each upstream `/health`", but its code only pinged Redis. I used Part 1's `health_router`, so the output looks like every other service's: `{"status": "degraded", "checks": {"redis": "ok", "trip": "fail: ..."}}` with 503. |
| 5 | **`X-Request-Id` sent to the service once, not twice** | `proxy.py` | Found by the smoke test. If the client sent its own `X-Request-Id`, the plan's code copied it **and** added the gateway's, so Trip received two headers. |
| 6 | **Repeated query parameters survive** | `proxy.py` | The plan passed `request.query_params` (a dict-like object), which keeps only the **last** value: `?tag=a&tag=b` reached the service as `?tag=b`. Now the raw query string is forwarded unchanged. |

Everything else is copied from the plan exactly.

### How 2.4 was checked

**1. Smoke test, 31/31 passing.** This is a throwaway script (not in the project; the real tests are 2.6). It runs the gateway in-process with **fakeredis** (an in-memory Redis) and fake upstream services, and checks:

| Area | Checks |
|---|---|
| Health | all OK → 200; Trip down → 503 `degraded` naming `trip` |
| Auth | no token / expired / wrong issuer / revoked → 401 with the right code |
| Headers | `X-User-*` come from the token (a client's `X-User-Role: ADMIN` is dropped); `X-Token-*` from the token; real internal token; no `Authorization` upstream; `X-Request-Id` passed through once or generated; `?tag=a&tag=b` kept |
| Routing | `driver/location` → Matching, `driver/offers/...` → Trip, `driver/earnings` → Fare, `drivers/me/online` → Identity; `/zones` works without login and sends no `X-User-*`; unknown path → 404 |
| Security | `zones/%2e%2e/internal/...`, `rides/%2E%2E/internal/...`, `zones/%2e/x` → 404, **and no service was called** |
| Rate limit | 10 logins OK, 11th → 429 |
| Idempotency | no key on `POST /rides` → 400; same key + body → replayed 201 with `Idempotent-Replay: true` and Trip called **once**; same key + different body → 422; Trip 500 → key deleted; Trip down → 503 and key deleted; Nusrat and Rafiq using the same key string don't collide |
| Redis down | → 503 `DEPENDENCY_UNAVAILABLE` in the standard JSON |

**2. Real server.** Started the gateway with `uvicorn` on port 8010, with nothing behind it:
- `GET /health` → 503, each of the 6 checks listed as failed
- `curl --path-as-is .../api/v1/zones/../internal/match/evaluate` (a raw `..` over the network) → 404
- `GET /api/v1/zones` with Redis off → 503 `DEPENDENCY_UNAVAILABLE`

Then I stopped it.

I also installed **`fakeredis`** into `.venv` for the smoke test. 2.6's tests will need it too.

### Known limitations (left as the plan has them)

- **Gateway-made errors don't show the request id.** Errors like 401 or 429 are created by Part 1's error handler, which reads `request_id` from the *incoming* headers. If the client didn't send `X-Request-Id`, the body says `"request_id": null`. Replayed idempotent responses also don't carry `X-Request-Id`. A small middleware could fix both; worth doing later if request ids matter for support.
- **Health failures for services show an empty reason** (`"trip": "fail: "`), because httpx connection errors have no message text. It's cosmetic; the name of the failing service is what matters.
- **`/health` checks the 5 services one after another**, 2 s timeout each, so a fully broken system takes up to ~10 s to answer. Nothing in Compose waits on the gateway's health, so this is harmless.

---

## 2.5: messaging integration

The plan says:

> - **Produces:** none. **Consumes:** none.
> - **Redis:** `INCR`+`EXPIRE` (`rl:*`), `SET NX EX` / `GET` / `DEL` (`idem:*`), `EXISTS` (`auth:revoked:*`).

Like 2.1 and 2.3, this is a **rule to check, not code to write**. The gateway takes no part in the event system, and its Redis use is a fixed, short list.

### Why the gateway sends and receives no events

Every other service is connected to RabbitMQ (`tesla.events`). The gateway deliberately isn't:

- **Nothing happens *in* the gateway that anyone needs to hear about.** Events report business facts, like "ride matched" or "fare settled". The gateway only decides "let this request through or not". The *result* of the request (a ride being created) is announced by the service that did it (Trip), through its outbox, in the same transaction as the database write (Part 1). If the gateway also published "ride requested", it could announce something that Trip then rejected.
- **There's nothing it needs to react to.** It keeps no data about rides or drivers, so no event could change what it does. The one piece of shared state it needs (logged-out tokens) comes from Redis, which it reads on every request anyway.
- **It keeps the front door simple.** No RabbitMQ connection means one less thing that can break the gateway, and no outbox, no consumer and no retry queues to run.

How live updates reach the phones **without** the gateway:

```
Trip ──event──▶ RabbitMQ ──▶ Notification ──WebSocket :8005──▶ Nusrat's phone
```

The phone opens its WebSocket straight to Notification (Part 0.3), not through the gateway. So the gateway handles request → response only; pushed updates go the other way through Notification.

### The complete list of Redis commands

I searched every file in `services/gateway/app/` for Redis calls and for any RabbitMQ code (`aio_pika`, `tesla_common.events`, `Bus`). **No RabbitMQ code at all.** The Redis calls:

| Command | Key | Where | In the 2.5 list? |
|---|---|---|---|
| `INCR` + `EXPIRE` (one pipeline, `MULTI`/`EXEC`) | `rl:*` | `ratelimit.enforce()` | ✅ |
| `SET ... NX EX 120` | `idem:*` | `idempotency.begin()`: claim the key | ✅ |
| `GET` | `idem:*` | `idempotency.begin()`: key already taken, read it | ✅ |
| `SET ... EX 86400` (no `NX`) | `idem:*` | `idempotency.finish()`: store the `DONE` result | ✅ (in the plan's code, just not spelled out in the list) |
| `DEL` | `idem:*` | `idempotency.finish()` on 5xx, and `main.proxy()` when the service is down | ✅ |
| `EXISTS` | `auth:revoked:*` | `security.authenticate()` | ✅ |
| `PING` | none | `/health` | ➕ added in 2.4 (health check) |

So the gateway **writes** only to `rl:*` and `idem:*`, and only **reads** `auth:revoked:*`. It never touches another service's keys (`geo:*`, `driver:*`, `fare:dist:*`, ...) or Redis Pub/Sub.

Per request that's at most **4 round trips to Redis** (`EXISTS`, the rate-limit pipeline, `SET NX`, then either `GET` for a repeat or the final `SET`/`DEL`). Each takes well under a millisecond on the same Docker network, so the gateway adds very little delay.

### One thing to know for Part 8

The plan's `docker-compose.yml` (Part 8) makes **every** service wait for RabbitMQ to be healthy, including the gateway (through the shared `x-service` settings). The gateway doesn't need RabbitMQ, so this only delays its start by a few seconds. It's harmless, and the gateway starts last anyway. `.env` also gives it `RABBITMQ_URL`, which it ignores (`Settings` only reads the variables it declares).

### How 2.5 was checked

- Searched the gateway's code for RabbitMQ use: **none**.
- Listed every Redis call (table above). All match the 2.5 list, apart from the health-check `PING` added in 2.4.
- The 2.4 smoke test already exercised every one of these commands (rate limit, idempotency claim/replay/delete, revoked token, health).

---

## 2.6: the step-by-step guide and the tests

2.6 is the plan's build order for the gateway, with a test to write at each step. The *building* was done in 2.2 and 2.4. What 2.6 adds is **permanent, automated tests** that prove each step works and keep proving it every time the code changes.

### The six steps, and where each one stands

| Step | Plan says | Done in | Test file |
|---|---|---|---|
| 1 | `requirements.txt` + `uvicorn` | 2.2 | — |
| 2 | `config.py`, `routes_table.py`; test that `/driver/location` → Matching and `/driver/offers` → Trip | 2.4 | `test_routes_table.py` |
| 3 | `security.py`; test valid, expired, wrong issuer, revoked tokens | 2.4 | `test_security.py` |
| 4 | `ratelimit.py`; test that the 11th login in a minute gets 429 | 2.4 | `test_ratelimit.py` |
| 5 | `idempotency.py`; test replay, same key + different body → 422, 5xx → key deleted | 2.4 | `test_idempotency.py` |
| 6 | `proxy.py`, `main.py`; confirm a client-sent `X-User-Id` is stripped | 2.4 | `test_proxy.py` |

### What I added

```
services/gateway/
├── pytest.ini              pytest settings (find app/, run async tests)
├── requirements-dev.txt    test tools: pytest, pytest-asyncio, fakeredis
└── tests/
    ├── conftest.py         shared setup: test keys, fake Redis, fake services, token helpers
    ├── test_routes_table.py   step 2  (21 tests)
    ├── test_security.py       step 3  (11 tests)
    ├── test_ratelimit.py      step 4  (5 tests)
    ├── test_idempotency.py    step 5  (10 tests)
    └── test_proxy.py          step 6  (18 tests)
```

The plan's other services put tests in `services/<name>/tests/` using pytest (for example `trip/tests/test_capacity.py`), so the gateway follows the same layout. In Part 1, the "checks" were one-off scripts. These are real tests: `pytest` finds and runs all of them in about 1.5 seconds.

`.dockerignore` now also leaves out `tests/`, `pytest.ini` and `requirements-dev.txt`, so the Docker image contains no test code.

### How the tests work without Docker

The gateway talks to two things: **Redis** and **the five services**. The tests replace both with fakes:

| Real thing | In the tests | Why |
|---|---|---|
| Redis | **fakeredis**, a Redis that lives in memory inside the test, fresh for every test | No Docker needed, and no test can leak counters or keys into the next one |
| Identity, Matching, Trip, Fare, Notification | **`FakeUpstreams`**, a fake that answers every request and **records** it | Tests can check exactly what the gateway *sent* (headers, path, body), and can make a service "down" or "slow" on demand |
| Identity's private key | a **throwaway RSA key pair** made when the tests start | Tests sign their own tokens (valid, expired, wrong issuer, wrong key...) |
| The network | the gateway app is called **in-process** (httpx's `ASGITransport`) | Fast, with no ports (so no clash with the `mse-*` containers) |

`conftest.py` provides these as pytest **fixtures**: ready-made objects a test just asks for by name. For example:

```python
async def test_client_sent_identity_headers_are_stripped(client, upstreams, auth_header):
    #                                                    ↑ gateway  ↑ fake services  ↑ makes "Bearer <jwt>"
```

### What each test file proves

**`test_routes_table.py` (step 2).** Tests `match()` directly, with no Redis and no HTTP.
- The two cases the plan names: `/driver/location` → Matching, `/driver/offers` → Trip. Plus 11 more paths, one or more per service (`/driver/earnings` → Fare, `/drivers/me/online` → Identity, ...).
- Near-misses **don't** match: `/api/v1/ridesXYZ`, `/api/v1/driverX`, `/api/v1`, `/rides`.
- Only register, login and zones are public. Login is limited to 10/min, driver location to 60, everything else to 120. Login is public but **logout is not**.
- The table really is sorted longest-first.

**`test_security.py` (step 3).** Calls `authenticate()` directly.
- The plan's four: a **valid** token → `Principal` + claims; **expired** → `TOKEN_EXPIRED`; **wrong issuer** → `INVALID_TOKEN`; **revoked** → `TOKEN_REVOKED`.
- Extra: a token signed with **someone else's key** → `INVALID_TOKEN` (this is the important one: it proves only Identity can mint tokens). Garbage instead of a JWT → `INVALID_TOKEN`. No header, empty, `Basic ...` or `Token ...` → `UNAUTHENTICATED`. `bearer` in lowercase is accepted.

**`test_ratelimit.py` (step 4).**
- The plan's case: **10 logins → 200, the 11th → 429 `RATE_LIMITED`**, and Identity received only 10 requests.
- **The clock is frozen** in these tests. Otherwise a test that happens to run across a minute boundary (e.g. 08:41:59.9 → 08:42:00.1) would see the counter reset and fail at random. Moving the frozen clock forward 60 s shows the next minute starts fresh.
- Counters are separate **per user** and **per route** (the 2.3 finding), and the Redis key expires within 60 s.

**`test_idempotency.py` (step 5).** Uses a fake Trip that creates a ride on `POST /rides`.
- The plan's three:
  - **Replay:** same key + same body → both 201 with the same ride, the second has `Idempotent-Replay: true`, **Trip was called once**, and the result is stored for 24 h.
  - **Same key + different body** → 422 `IDEMPOTENCY_KEY_REUSED`.
  - **Trip returns 5xx** → key deleted, and a retry with the same key reaches Trip again (not replayed).
- Extra:
  - Trip **down** → 503 and key deleted.
  - A **4xx** (e.g. 409 "you already have a ride") *is* remembered and replayed. That's correct: the answer won't change on retry.
  - A request still in progress → 409 `IDEMPOTENCY_IN_PROGRESS`.
  - No key on `POST /rides` → 400.
  - Other POSTs work without a key.
  - Nusrat and Rafiq can use the same key string without colliding.
  - `GET` is never tracked.

**`test_proxy.py` (step 6).** The full request path through `main.py` and `proxy.py`.
- The plan's case, made stronger: the client sends fake `X-User-Id: jashim`, `X-User-Role: ADMIN`, `X-Token-Jti`, and even a guessed `X-Internal-Token`. Trip receives **exactly one** of each header, all taken from the real token and config.
- On a public route, the fake `X-User-*` headers are dropped and none are added.
- `Authorization` never reaches the services. The `/api/v1` prefix is removed and the body arrives unchanged. `?tag=a&tag=b` survives. `X-Request-Id` is passed once, returned, and generated when missing.
- **The 2.4 security fix:** three `..` / `.` paths → 404, **and no service is called at all**.
- Unknown path → 404, no token → 401 (neither touches a service). A service's own status, body and headers (e.g. a 409) come back unchanged. Service down → 503, service too slow → 504.
- Redis down → 503 `DEPENDENCY_UNAVAILABLE`, even on public routes, and nothing is forwarded.
- `/health`: all six checks `ok` → 200; Trip down → 503 with `trip: fail...` and the rest `ok`.

### Checking that the tests can actually fail

A test that always passes proves nothing. So after they passed, I **broke the code on purpose**, one thing at a time, and ran the tests each time:

| Deliberately broke... | Result |
|---|---|
| removed the `..` check | 3 tests failed |
| stopped stripping client `X-User-*` / `X-Token-*` | 2 failed |
| put back the double `X-Request-Id` | 1 failed |
| put back the plan's `query_params` (repeated params lost) | 1 failed |
| kept the idempotency key after a 5xx | 1 failed |
| allowed 1 extra request over the rate limit | 2 failed |
| skipped the revoked-token check | 1 failed |
| removed the Redis-down handler | 1 failed |

Every break was caught. Afterwards the code was restored and checked: it's byte-for-byte identical to your "Routing Table & Endpoints" commit.

### Not covered by these tests

- **The real Docker image.** Docker Desktop was off, so `docker build` hasn't been run yet (see 2.2 for the command).
- **Real Redis and real services.** The fakes behave like the real things for everything the gateway uses, but the full journey (log in via Identity → request a ride via Trip) can only be tested once those services exist, in Part 8 (`scripts/e2e.sh`).

---

## Things to know before the next sections

- **`uvicorn` is now installed** in `.venv` (done in 2.2, which also covers 2.6 step 1). **`fakeredis`** too (2.4).
- **`.venv` has no `pip`.** It was made with `uv`, so install packages with `uv pip install --python .venv\Scripts\python.exe ...`.
- **`keys/` is empty.** The gateway reads `jwt_public.pem` at startup. Create the pair with `sh scripts/gen_keys.sh` (Git Bash includes `openssl`). The 2.4 checks used throwaway keys.
- **Identity (Part 3) can rely on** `X-Token-Jti` and `X-Token-Exp` arriving on every logged-in request (done in 2.4).
- **Port 8000** is used by the other project's `mse-*` containers (see `exp1.md`). Stop them before running the gateway on 8000.
