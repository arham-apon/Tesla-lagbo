# Step 7 Explained: the Notification Service

## Progress

| Section | What it is | Status |
|---|---|---|
| 7.1 | Overview & domain scope | **Done** (explained below) |
| 7.2 | Directory structure | **Done** (explained below) |
| 7.3 | Data layer (the inbox) | **Done** (explained below) |
| 7.4 | Recipient rules (who hears about what) + connections | **Done** (explained below) |
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

**4. `ride.matched` is sent to the passenger as-is (7.4).** It contains `driver_id` and `joined_existing_pool` besides the driver's name and car. Nothing secret, and no co-rider data, but the plan's privacy column only promises "driver name + Bullet". I'll decide in 7.4 whether to trim it, and test that no co-rider id ever reaches a passenger. *Decided in 7.4: every message is now an allowlist of named fields (see 7.4).*

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
│   └── versions/0001_init.py   the inbox + processed_events (added in 7.3)
├── app/
│   ├── __init__.py
│   ├── config.py               settings
│   ├── deps.py                 db, auth, bus, redis, public_key(), settings
│   ├── models.py               the inbox (7.3)
│   ├── routing.py              recipients(event) (7.4)
│   ├── connections.py          open sockets per user (7.4)
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

No tests in 7.2; the first real ones came with the inbox table in 7.3.

---

## 7.3: the data layer (the inbox)

### Two tables

| Table | One row is... | Notes |
|---|---|---|
| `notifications` | **one message saved for one person** | `id` (grows), `user_id`, `type` (`ride.matched`, `fare.settled`, …), `payload` (the whole message as JSON, exactly as it was pushed), `created_at`, `read_at` (empty until the phone marks it read) |
| `processed_events` | an event already saved | so a redelivered event isn't saved twice (Part 1's mixin, as in Trip and Fare) |

No `outbox`: Notification **sends no events**. It's the end of the line.

### Why the `(user_id, id)` index is the important part

A reconnecting phone asks: **"give me Nusrat's messages with an id after 41, oldest first, at most 50"** (7.5). The index is sorted by person, then by id, so SQLite jumps straight to Nusrat's messages after 41 and reads them **already in order**. It doesn't read anyone else's inbox, and it doesn't sort. A test asks SQLite for its query plan and checks both: the index is used, and there's **no sorting step**.

It also relies on **ids only growing** in the order messages were saved: "after 41" means "newer than 41". A test checks that too.

### What I added, and why

| Added rule | Why |
|---|---|
| **`json_valid(payload)`** (SQLite's built-in JSON check) | the inbox API hands every payload back to the phone as JSON. One broken row would make **every** catch-up for that person fail, over and over. Now the database refuses it at write time, where the bug is, instead of at read time, where it would hurt the user |
| **`length(user_id) > 0`** | a message for "nobody" can never be read, and would hide a routing bug |

I **didn't** add a rule listing the allowed `type`s: new message types (e.g. `fare.refunded` one day) shouldn't need a database migration, and the phone simply ignores types it doesn't know.

### The migration: `0001_init.py`

Autogenerated from the models and hand-checked: the table, the index, both rules, and `processed_events`. The `.gitkeep` from 7.2 is gone now that the folder has a file.

### How 7.3 was checked: 13 tests, all passing

| File | Tests | What |
|---|---|---|
| `test_migrations.py` | 5 | exactly the 2 tables; both rules are in the database; **the catch-up query uses the index and doesn't sort** (SQLite's own query plan); models and migration agree; down to nothing and back |
| `test_models.py` | 8 | a saved message (id, unread, JSON round-trip); **4 kinds of broken payload refused** (plain text, empty, single quotes, cut off); a message for nobody refused; **ids only go up, and "after my first id" returns exactly the rest**; each event recorded once |

Run them with:

```
cd services\notification
..\..\.venv\Scripts\python -m pytest
```

**Break-it checks** (broke the migration or model on purpose, ran the tests, restored):

| Deliberately broke... | Result |
|---|---|
| JSON rule removed | 5 failed |
| "needs a person" rule removed | 2 failed |
| catch-up index removed | 3 failed |
| index on `user_id` only (no `id`) | 1 failed |
| model gains an index the migration lacks | 1 failed (`alembic check`) |

### Good to know

- **Ids come from SQLite's rowid.** SQLite only reuses an id if the **newest** row is deleted. Nothing deletes inbox rows today. If a clean-up job is added one day, it should delete **old** rows (which is the natural thing), never the newest, or a phone's "after 41" could skip a message.
- **The payload is stored exactly as pushed.** So the catch-up shows the phone the same message it would have got live, with the same privacy rules applied (7.4).

---

## 7.4: the recipient rules (`routing.py`) and open sockets (`connections.py`)

### `recipients(event)`: one function, the whole privacy boundary

Every event goes through **one** pure function that returns "send **this** message to **this** person". No database, no sockets, no Redis: event in, list out. Everything a phone is ever told, live or from the inbox, comes from here.

| Event | Who | Message | What they get |
|---|---|---|---|
| `trip.ride.requested` | each candidate driver | `ride.offer` | ride id, zones, seats, **estimate**. No passenger id, name or phone |
| `trip.ride.matched` | the passenger | `ride.matched` | ride, pool, **driver's name, "Bullet"**, seats |
| | the driver | `ride.matched` | ride, pool, seats, joined an existing pool or not |
| `trip.ride.status_changed` | passenger + driver | `ride.status` | ride, pool, from → to, who did it (role) |
| `trip.ride.cancelled` | passenger (+ driver if there was one) | `ride.cancelled` | ride, pool, from, cancelled by, reason. **Not the quote id** |
| `trip.pool.updated` | **the driver only** | `pool.updated` | his pool: status, seats, riders, stops |
| `fare.ride.settled` | **that** passenger | `fare.settled` | **her own fare in full**: base, distance, discount, total, how paid, PAID/FAILED |
| | the driver | `fare.settled` | ride, total, how paid, **PAID/FAILED**: enough to know he must collect cash |
| anything else (`trip.ride.completed`, a future type, …) | nobody | | `completed` is bound (`trip.ride.*`) but says nothing new: Fare's `settled` follows with the price |

A message looks like `{"type": "ride.matched", "event_id": "…", "at": "2026-09-24T08:41:05.000000Z", "data": {…}}`. The `event_id` lets the app ignore a duplicate (7.1: the push side may deliver twice).

### The change: allowlists instead of "forward the event"

| Plan's code | Problem | What I did |
|---|---|---|
| `msg("ride.matched", d)`: the event's **whole** `data` goes to each recipient (the same for status, cancelled and settled) | today's events hold nothing secret for their recipients, but **tomorrow's might**. If Trip adds, say, the list of co-riders to `trip.ride.matched`, every passenger would silently receive it. Privacy would depend on another service never adding a field | every recipient gets an **allowlist**: named fields only. A field an event gains later **stays out** until someone decides it should be sent |

It also resolved finding 4 (the passenger's `ride.matched` now carries exactly "driver name + Bullet", as the plan's privacy column promises, plus the ride's own ids), and trimmed two things nobody needed: the **quote id** in cancel messages (it's between Trip and Fare), and the passenger's **fare breakdown** in the driver's copy (he needs the total and whether it was paid, to collect cash).

### `ConnectionManager`: the plan's code

Keeps, **for this copy of the service**, each person's open sockets (phone + tablet = 2). `send(user, message)` writes to each of them. A socket that fails (the phone vanished without saying goodbye) is **dropped** and the others still get the message. Asking to send to someone with no sockets does nothing. `deps.manager` is now the one shared instance (promised in 7.2).

**Known limitation:** `send` writes to sockets **one after another**. A phone on a very slow network would delay the same message to that person's other sockets, and the push loop (7.6) handles one event at a time. For a demo it's fine. At scale, each socket would get its own small send queue.

### How 7.4 was checked: 26 new tests (39 in total), all passing

`test_routing.py` builds events with **Part 1's real `emit()`**, with **the registry's exact payloads** (the ones Trip's and Fare's contract tests guard), plus a **planted `secret` field** in every event:

- **one test per event type** (plan step 7.7.2): offers to each candidate (and to nobody when no driver was near); matched → passenger gets "Jashim / Bullet", driver gets his own fields; status → both; cancelled before a driver → passenger only, after → both, no quote id; pool → **driver only**; settled → her full fare, his total + FAILED; other events → nobody; the message envelope;
- **a whole evening** (Nusrat and Rafiq in Bullet, Shirin requests and cancels, both fares settle, Nusrat's wallet payment FAILED), then:
  - **"Rafiq never receives a message containing Nusrat's fare"** (the plan's test): no `7200`, no her fare id, no her ride id in anything sent to Rafiq;
  - **each passenger only ever hears about their own ride**;
  - **no passenger ever gets a pool update, or another passenger's id**;
  - **the planted field never escapes** (the allowlist change);
  - nobody is ever messaged as an empty id;
  - everyone gets exactly the expected sequence (Nusrat: matched, arrived, started, fare; Karim: one offer; Jashim: 4 pool updates, 2 fares, …);
  - every message passes **SQLite's own `json_valid`**, so it fits the 7.3 inbox.

`test_connections.py` (6): connect + send; **phone and tablet both get it**; **nobody gets someone else's message**; a dead socket is dropped and the other still gets both messages; the last socket gone forgets the person (and twice is harmless); sending to someone offline creates nothing.

**Break-it checks** (broke the code on purpose, ran all tests, restored):

| Deliberately broke... | Result |
|---|---|
| **the plan's behaviour**: whole event data forwarded, no allowlist | 6 failed |
| pool updates also sent to the pool's passengers | 5 failed |
| offer includes the passenger's id | 1 failed |
| driver gets the passenger's full fare breakdown | 1 failed |
| "no driver yet" guard removed (cancel before matching) | 2 failed |
| cancel message leaks the quote id | 1 failed |
| dead socket not dropped | 1 failed |
| sending to someone offline creates an empty entry | 1 failed |

---

## Things to know before the next sections

- **Build order:** the plan builds Notification **after** Trip and Fare (0.7), because it only listens to their events. Both are done, and their events are guarded by contract tests.
- **Data stores:** `notification.db` (the inbox) and **Redis** (the "logged out" list, the pool member sets, and the live-location channels).
- **The recipient rules are the privacy boundary** of the whole system for anything pushed to phones, and they're **allowlists**: a new field in an event reaches no phone until it's added to `routing.py` on purpose.
- **Notification never changes other services' data and calls nobody.** If it's down, rides still work, and phones catch up from the inbox.
- **Phones connect to port 8005 directly for the socket**, and through the gateway for the inbox.
