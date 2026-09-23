# Step 2 Explained: the API Gateway

## Progress

| Section | What it is | Status |
|---|---|---|
| 2.1 | Overview & domain scope | **Done** (explained below) |
| 2.2 | Directory structure | **Done** (explained below) |
| 2.3 | Data layer | **Done** (explained below) |
| 2.4 | Routing table & endpoints (the code) | Not started |
| 2.5 | Messaging integration | Not started |
| 2.6 | Step-by-step build + tests | Not started |

This file grows as each section gets done.

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

Each `app/*.py` file only contains a one-line description for now. The real code is in section **2.4**, and I'll paste it in then. That way 2.2 is only the "skeleton" and 2.4 is only the "flesh", matching how the plan is split.

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

### Two things to decide in 2.4 (found while checking 2.3)

1. **The gateway must forward `X-Token-Jti` and `X-Token-Exp`** so Identity can write `auth:revoked:{jti}` with the right TTL (Part 3's Logout note). The 2.4 `proxy.py` is missing these. This was noted in 2.1 and is still open.
2. **What happens when Redis is down.** In the 2.4 code, a Redis failure is not a `DomainError`, so clients would get a bare `500 Internal Server Error` instead of the standard `{"error": {...}}` JSON. Every request touches Redis (the rate limiter runs even on public routes), so the gateway **fails closed**: nothing gets through. That is the safe choice, since we'd rather refuse requests than let revoked tokens in. But it should return a proper **503** in the standard format. I'll propose that small change when writing 2.4.

### How 2.3 was checked

- Compared the key names, TTLs and commands in the 2.3 line, the 0.5 Redis table, the 2.5 command list, and the 2.4 code. They all agree except the `rl:` key format (explained above).
- Compared `auth:revoked:{jti}` with Identity's side (Part 3: `SET auth:revoked:{jti} 1 EX <remaining>`). They match.
- No live Redis test yet: 2.3 has no code to run, and Redis isn't running (Docker Desktop is off). The real Redis tests are in 2.6 (rate limit, replay, 422, 5xx deletes the key).

---

## Things to know before the next sections

- **`uvicorn` is now installed** in `.venv` (done in 2.2, which also covers 2.6 step 1).
- **`.venv` has no `pip`.** It was made with `uv`, so install packages with `uv pip install --python .venv\Scripts\python.exe ...`.
- **`keys/` is empty.** The gateway reads `jwt_public.pem` at startup. Create the pair with `sh scripts/gen_keys.sh` (Git Bash includes `openssl`). This is needed from 2.4 onward.
- **Something Part 2 must support for Part 3:** the plan's *Logout note* (Part 3, Identity) says the gateway must also forward `X-Token-Jti` and `X-Token-Exp`, so Identity can revoke the token on logout. The `proxy.py` code in 2.4 does **not** include those two lines yet. I'll add them when doing 2.4.
- **Port 8000** is used by the other project's `mse-*` containers (see `exp1.md`). Stop them before running the gateway on 8000.
