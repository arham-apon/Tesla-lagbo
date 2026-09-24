# Step 7 Explained: the Notification Service

## Progress

| Section | What it is | Status |
|---|---|---|
| 7.1 | Overview & domain scope | **Done** (explained below) |
| 7.2 | Directory structure | **Done** (explained below) |
| 7.3 | Data layer (the inbox) | Not started |
| 7.4 | Recipient rules (who hears about what) + connections | Not started |
| 7.5 | API endpoints (WebSocket + inbox) | Not started |
| 7.6 | Messaging (inbox queue, broadcast queue, live location) | Not started |
| 7.7 | Step-by-step build + tests | Not started |

This file grows as each section gets done.

---

## The big picture in 30 seconds

So far:
- **Gateway** (Part 2): the front door.
- **Identity** (Part 3): who everyone is.
- **Matching** (Part 4): the map, where the cars are, the pooling advice.
- **Trip** (Part 5): books rides, runs each ride, guards Bullet's seats.
- **Fare** (Part 6): prices and settles every ride.

All of them **announce** what happened (`trip.ride.matched`, `fare.ride.settled`, …), but nobody has told the **phones** yet. Part 7 is the service that does:

- Jashim's phone buzzes: **"New ride: Banani → Mohakhali, 1 seat, ৳82.50"**.
- Nusrat's phone: **"Jashim in Bullet is on the way"**, then a little car **moving on the map**, then **"You paid ৳72.00"**.
- If her phone was off for a minute, she **catches up** on everything she missed when it reconnects.

```
                                Notification :8005
                     ┌─────────────────────────────────────────────────────┐
RabbitMQ ────────────┼▶ trip.ride.*, trip.pool.updated, fare.ride.settled   │
  (all services' news)│      │                                             │
                     │      ├─ routing: who may hear this, and what exactly│
                     │      ├─▶ inbox (notification.db)  "what was sent"   │
                     │      └─▶ open WebSockets ────────────────────────────┼──▶ Nusrat's / Jashim's phone (live)
Redis Pub/Sub ───────┼▶ loc:pool:* (Jashim's GPS, from Matching) ──────────┼──▶ Nusrat's map (the car moving)
Nusrat's phone ──────┼▶ WS /ws?token=<JWT>        (straight here, port 8005)│
 (via Gateway) ──────┼▶ GET /notifications?after_id=  (catch-up)           │
                     └─────────────────────────────────────────────────────┘
```

---

## 7.1: what Notification owns and what it delegates

The plan says:

> **Owns:** WebSocket connections, the recipient routing rules (who hears about what), and a persisted inbox so offline clients can catch up.
> **Delegates:** all domain state. It only translates events into messages.

### Owns (1): WebSocket connections

An ordinary web request is "ask, get an answer, done". A **WebSocket** is a line that **stays open**, so the server can speak first: "your driver has arrived" arrives the moment it happens, without the phone asking every few seconds.

- The phone opens `ws://…:8005/ws?token=<its login token>` **directly**, not through the gateway (the gateway only proxies ordinary requests, plan 8.7). So **Notification checks the login itself**: the token's signature with Identity's **public** key (RS256, Part 3), and the same **"logged out" list** in Redis that the gateway uses (`auth:revoked:{jti}`, written by Identity on logout).
- Bad or revoked token → the socket is closed with code **4401** ("unauthorised", in the 4000s range apps may use for their own codes).
- The app may send `"ping"` and get `"pong"`, to keep the line alive through mobile networks.
- One person can have **several** sockets (phone and tablet); all of them get every message.

### Owns (2): the recipient rules, "who hears about what"

This is the part that needs the most care, because **a message to the wrong person is a privacy leak**:

| News | Who hears it | What they must **not** see |
|---|---|---|
| a ride needs a driver | each **candidate driver** Matching found | the passenger's phone number |
| matched | the passenger and the driver | |
| arrived / started | the passenger and the driver | |
| cancelled | the passenger, and the driver if there was one | |
| the pool changed (stops, seats) | **the driver only** | passengers never get co-rider data |
| fare settled | **that** passenger, and the driver | **Rafiq never sees Nusrat's fare** |
| the car moved | the pool's current passengers | the driver's id (only lat, lng, zone, time are sent) |

All of this lives in **one function**, `recipients(event)`, so it can be read, and tested, in one place.

### Owns (3): the inbox

Every message (except the very frequent pool updates for the driver) is also **saved**, per person, in `notification.db`. When Nusrat's phone reconnects after a tunnel, it asks `GET /notifications?after_id=<the last one I saw>` and gets exactly what it missed. The inbox is also the record of **what was sent**, if anyone asks "was I told?".

### Delegates: all domain state

Notification doesn't know what a ride **is**. It never decides anything, never calls another service, and never changes anyone's data. It takes other services' news and turns it into messages. That's why it can be simple, and why it's safe to restart at any time.

### Two queues: "must not lose" and "right now"

Each event is handled **twice**, differently:

| | `notification.inbox` queue | a broadcast queue (one per running copy) |
|---|---|---|
| job | **save** to the inbox | **push** to open sockets |
| if it fails | retried, then dead-lettered (Part 1's bus) | dropped: the inbox has it anyway |
| receiving twice | harmless (`processed_events`) | harmless (the phone may show it twice at worst) |
| with 2 copies of Notification running | only **one** saves it | **each** copy pushes to the sockets **it** holds |

The second column is why scaling needs no "sticky" routing: Nusrat's socket can be on any copy, and every copy hears every event.

### The moving car: Redis Pub/Sub, not RabbitMQ

Jashim's position arrives every few seconds and is useless 5 seconds later (Part 0.3's rule: "high-frequency, loss-tolerant → Redis Pub/Sub"). Matching publishes it on `loc:pool:{pool_id}` (4.5). Notification listens to **all** `loc:pool:*` channels, looks up **who is in that pool** (a Redis set it keeps from `trip.pool.updated`), and forwards only `lat, lng, zone, time`.

### Where Notification sits

```
Identity ──▶ Trip ──▶ Fare ──▶ Matching          (HTTP calls)
   everyone ──── events ────▶ Notification ──▶ phones
```

Notification **calls nobody** and **nobody calls it** except the phones. It only **listens**, so it can never slow down a booking. If it's down, rides still work; the inbox catches phones up when it's back.

---

## What I did for 7.1

Like 5.1 and 6.1, this is a **scope definition** with no code of its own. I checked what Notification will depend on:

| Check | Result |
|---|---|
| Part 1's `verify_jwt` (RS256, issuer, required claims incl. `jti`) | exists, used by the gateway |
| The "logged out" list | Identity writes `auth:revoked:{jti}` on logout (`routers/auth.py`); the gateway checks the same key. Notification will too |
| `Bus.consume_broadcast` (the per-copy queue) | exists in Part 1 (an exclusive, auto-deleted queue per running copy) |
| **What each event carries** (the recipient rules read these) | `trip.ride.requested`: `candidate_driver_ids`, zones, seats, estimate ✓; `matched` / `status_changed` / `cancelled`: `passenger_id`, `driver_id` (null when cancelled before matching) ✓; `trip.pool.updated`: `driver_id`, `pool_id`, `status`, `member_passenger_ids` ✓; `fare.ride.settled`: `passenger_id`, `driver_id` ✓. Trip's and Fare's contract tests (5.5, 6.6) guard all of these |
| **Matching's location message** | `loc:pool:{pool_id}` with `{pool_id, driver_id, lat, lng, zone, ts}` (`fleet.py`), published only while the driver has a pool. The relay forwards `lat, lng, zone, ts`, so all four are there |
| Gateway | `/api/v1/notifications` → `http://notification:8005` (the inbox). `/ws` is **not** proxied: phones connect to port 8005 directly (plan's compose file publishes `8005:8005`) |
| WebSocket support | `uvicorn[standard]` already installs the `websockets` library (17.1), so nothing extra is needed |

---

## Found while reading ahead (to fix in the right section)

**1. A socket outlives the login (7.5).** The token and the "logged out" list are checked **once**, when the socket opens. If Nusrat logs out, or her token expires an hour later, an already-open socket keeps receiving her messages. With a 1-hour token it's a small window, but "log out" should mean it. **Proposed:** close the socket when the token's `exp` passes, and re-check the "logged out" list now and then (e.g. on each ping).

**2. The token travels in the URL (7.5).** `/ws?token=…` is the usual way (a browser can't add headers to a WebSocket), but URLs end up in **access logs**. Uvicorn's log line includes the query string (checked in its source: it logs `"WebSocket /ws?token=…" [accepted]` using the path **with** the query), so every login token would be written to Notification's log. **Proposed:** keep the query parameter, but make sure the access log doesn't print it.

**3. A Redis hiccup can lose a pool's member list (7.6).** In the plan's `persist`, the inbox rows **and the "already processed" mark** are committed first, and **then** the Redis member set is written. If Redis fails at that moment, the event is retried, but the retry sees "already processed" and stops, so the member list is **never** written. Nusrat wouldn't see the car moving until the next pool change. **Proposed:** the member-set write is a full replacement, so it's safe to repeat. Do it even when the event was already processed.

**4. `ride.matched` is sent to the passenger as-is (7.4).** It contains `driver_id` and `joined_existing_pool` besides the driver's name and car. Nothing secret, and no co-rider data, but the plan's privacy column only promises "driver name + Bullet". I'll decide in 7.4 whether to trim it, and test that no co-rider id ever reaches a passenger.

**5. A binary WebSocket message would crash the loop (7.5).** `receive_text()` raises if the phone sends bytes, and only `WebSocketDisconnect` is caught, so the socket would be dropped with an error logged, not cleanly. It's harmless but noisy; the loop should ignore anything that isn't `"ping"`.

---

## 7.2: the directory structure

### What I created

```
services/notification/
├── Dockerfile                  generic service Dockerfile, port 8005 (no seed: nothing to seed)
├── requirements.txt            uvicorn[standard] (brings websockets) + alembic
├── requirements-dev.txt        pytest, pytest-asyncio, fakeredis (Pub/Sub, sets, denylist)
├── pytest.ini
├── alembic.ini
├── migrations/
│   ├── env.py                  same as Trip's and Fare's (incl. the "don't silence loggers" fix)
│   └── versions/               empty for now; 0001_init.py comes in 7.3
├── app/
│   ├── __init__.py
│   ├── config.py               settings
│   ├── deps.py                 db, auth, bus, redis, public_key(), settings
│   ├── models.py               placeholder: the inbox, code in 7.3
│   ├── routing.py              placeholder: recipients(event), code in 7.4
│   ├── connections.py          placeholder: open sockets per user, code in 7.4
│   ├── consumers.py            placeholder: persist + push, code in 7.6
│   ├── live_location.py        placeholder: loc:pool:* relay, code in 7.6
│   ├── main.py                 placeholder: app + lifespan, code in 7.7
│   └── routers/
│       ├── __init__.py
│       ├── ws.py               placeholder: WS /ws, code in 7.5
│       └── inbox.py            placeholder: /notifications, code in 7.5
└── tests/
    └── conftest.py             sets the environment before app.config is imported
```

### How the files map to 7.1's jobs

| 7.1 job | File |
|---|---|
| WebSocket connections | `routers/ws.py` (open, check login, keep), `connections.py` (who holds which socket) |
| who hears about what | `routing.py` |
| the inbox | `models.py`, `routers/inbox.py` |
| the two queues | `consumers.py` |
| the moving car | `live_location.py` |

`routing.py` is kept **separate from everything else** on purpose: it's a pure function (event in, list of (person, message) out) with no database, sockets or Redis. So the privacy rules can be tested exhaustively on their own (plan step 7.7.2).

### Decisions

| Plan says | What I did | Why |
|---|---|---|
| no `__init__.py`, `tests/`, `pytest.ini`, `requirements-dev.txt` in the folder list | added them | as in Parts 3–6 |
| migration env | copied Fare's `env.py`, `alembic.ini`, `script.py.mako` | same proven setup, with 5.5's logger fix |
| `migrations/versions/0001_init.py` | **not created yet** (a `.gitkeep` keeps the folder) | a migration without a revision id breaks Alembic; it comes with the model in 7.3 (as in Trip, 5.2) |
| `ws.py` uses `settings.public_key` | **`deps.public_key()`**, read from `JWT_PUBLIC_KEY_PATH` **once** and cached | settings hold the **path** (as in Identity and the gateway); reading the file on every connection would be wasteful, and at import time it would make the tests need a real key file |
| `ws.py` imports `manager` from deps | `manager` joins `deps.py` in 7.4, with `connections.py` | importing it now would break the import (same as Trip's clients in 5.2) |
| Dockerfile | generic, port 8005, **no seed step** | the plan's compose command is `alembic upgrade head && uvicorn …`; there's nothing to seed |

`config.py`: `DB_PATH` (`notification.db`), `REDIS_URL`, `RABBITMQ_URL`, `INTERNAL_TOKEN` (the inbox routes come through the gateway), **`JWT_PUBLIC_KEY_PATH`** (the socket's login check; the compose file mounts `jwt_public.pem`), `LOG_LEVEL`.

### How 7.2 was checked

| Check | Result |
|---|---|
| `app.config` with the required settings | defaults as above |
| `app.deps` | builds `db`, `auth`, `bus`, and a Redis client with `decode_responses=True` (the member sets and messages are text) |
| every module, including the 8 placeholders | imports |
| Dockerfile | `SERVICE=notification`, `EXPOSE 8005`, `alembic upgrade head && uvicorn … --port 8005` |

No tests yet: the first real ones come with the inbox table in 7.3 and the recipient rules in 7.4.

---

## Things to know before the next sections

- **Build order:** the plan builds Notification **after** Trip and Fare (0.7), because it only listens to their events. Both are done, and their events are guarded by contract tests.
- **Data stores:** `notification.db` (the inbox) and **Redis** (the "logged out" list, the pool member sets, and the live-location channels).
- **The recipient rules are the privacy boundary** of the whole system for anything pushed to phones. They'll get a test per event type, including "Rafiq never receives a message containing Nusrat's fare" (plan step 7.7.2).
- **Notification never changes other services' data and calls nobody.** If it's down, rides still work, and phones catch up from the inbox.
- **Phones connect to port 8005 directly for the socket**, and through the gateway for the inbox.
