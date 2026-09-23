# Step 2 Explained: the API Gateway

## Progress

| Section | What it is | Status |
|---|---|---|
| 2.1 | Overview & domain scope | **Done** (explained below) |
| 2.2 | Directory structure | Not started |
| 2.3 | Data layer | Not started |
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

## Things to know before the next sections

- **`uvicorn` is not installed yet.** The plan adds it in 2.6 step 1 (`uvicorn[standard]~=0.30`), needed to actually run the gateway.
- **`keys/` is empty.** The gateway reads `jwt_public.pem` at startup. Create the pair with `sh scripts/gen_keys.sh` (Git Bash includes `openssl`). This is needed from 2.4 onward.
- **Something Part 2 must support for Part 3:** the plan's *Logout note* (Part 3, Identity) says the gateway must also forward `X-Token-Jti` and `X-Token-Exp`, so Identity can revoke the token on logout. The `proxy.py` code in 2.4 does **not** include those two lines yet. I'll add them when doing 2.4.
- **Port 8000** is used by the other project's `mse-*` containers (see `exp1.md`). Stop them before running the gateway on 8000.
