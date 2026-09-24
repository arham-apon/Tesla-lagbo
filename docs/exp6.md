# Step 6 Explained: the Fare & Billing Service

## Progress

| Section | What it is | Status |
|---|---|---|
| 6.1 | Overview & domain scope | **Done** (explained below) |
| 6.2 | Fare model (the formula, hand-checkable) | **Done** (explained below) |
| 6.3 | Directory structure | **Done** (explained below) |
| 6.4 | Data layer (tariff, quotes, fares, wallets) + pricing + distance cache | **Done** (explained below) |
| 6.5 | API endpoints | **Done** (explained below) |
| 6.6 | Messaging (settling a ride) | Not started |
| 6.7 | Step-by-step build + tests | Not started |

This file grows as each section gets done.

---

## The big picture in 30 seconds

So far:
- **Gateway** (Part 2): the front door.
- **Identity** (Part 3): who everyone is, and whether Jashim is working.
- **Matching** (Part 4): the map and the pooling advice.
- **Trip** (Part 5): books rides, guards Bullet's seats, runs each ride from request to drop-off.

Part 6 builds the service that answers **"how much?"**, twice:

1. **Before the ride (a quote):** Nusrat opens the app, picks Banani → Mohakhali, and sees **৳82.50 alone, ৳72.00 if pooled**. Trip also asks for a quote when she taps "Request" and saves both numbers on her ride.
2. **After the ride (settlement):** Trip announces "Nusrat's ride is complete, it **was** pooled, she pays by **wallet**". Fare works out the final ৳72.00, takes it from her TeslaPay wallet, credits Jashim, writes a permanent record, and announces "settled, PAID". Trip copies that onto her ride (5.7), and Notification tells her phone.

```
                              Fare & Billing :8004
                 ┌───────────────────────────────────────────────────┐
Nusrat's phone ──┼▶ POST /fares/estimate  ("how much?")               │──▶ Matching: Banani → Mohakhali = 3500 m
 (via Gateway)   │   GET /fares/rides/{id}, GET /wallet, POST /wallet/topup   (cached in Redis 24 h)
Jashim's phone ──┼▶ GET /driver/earnings                              │
Trip ────────────┼▶ POST /internal/quotes, GET /internal/quotes/{id}  │
                 │   tariff, quotes, fares, wallets    (fare.db)      │
RabbitMQ ────────┼▶ trip.ride.completed / cancelled                   │──▶ outbox ──▶ fare.ride.settled
                 └───────────────────────────────────────────────────┘
```

---

## 6.1: what Fare owns and what it delegates

The plan says:

> **Owns:** tariff, quotes, final fares (immutable ledger rows), simulated TeslaPay wallets, driver earnings.
> **Delegates:** distances (Matching), deciding whether a ride was pooled (Trip reports it in the event).

### Owns (1): the tariff, "the price list"

Three numbers, stored in the database (not in code), so prices can change without a new release:

| | Value | In taka |
|---|---|---|
| base fare (per seat) | 3000 poysha | ৳30 |
| per km | 1500 poysha | ৳15/km |
| pool discount | 20 % of the distance charge | |

Tariffs have an **id**. A new price list is a **new row**, and the old one stays, because old quotes point at it (see "locked at quote time" in 6.2).

**All money is in poysha** (1 taka = 100 poysha), as **whole numbers**. No `0.1 + 0.2 = 0.30000000000000004` surprises, and the database can check the arithmetic exactly (6.4).

### Owns (2): quotes

A quote is **a promise of a price**: "Banani → Mohakhali, 1 seat, 3500 m: ৳82.50 alone, ৳72.00 pooled, valid 10 minutes, using tariff 1". Trip stores the quote id on the ride (5.5), and checks the quote belongs to **this** passenger, route and seat count, and hasn't expired.

### Owns (3): final fares, "immutable ledger rows"

One row per completed ride, **written once and never changed**: who paid, base, distance charge, discount, total, cash or wallet, PAID or FAILED. That's an accountant's **ledger**: you don't edit a line, you add a new one (a refund would be a new record, not a changed total). If anyone asks "why did I pay ৳72?", this row, plus the quote and tariff it came from, is the answer.

### Owns (4): TeslaPay wallets (simulated)

Each person has a balance, and every change is a **transaction row**: `TOPUP`, `RIDE_DEBIT` (Nusrat pays), `DRIVER_CREDIT` (Jashim earns). No real payment gateway (plan decision): top-up is a button that adds money. Balances can never go below zero (a database CHECK rule, 6.4). If Nusrat's wallet is short, the payment is **FAILED**, nothing is taken, and Notification tells Jashim to collect cash.

### Owns (5): driver earnings

"How much did I make today?" is simply the sum of Jashim's fares, split into cash and wallet. Nothing extra is stored: it's worked out from the ledger, so it can't disagree with it.

### Delegates (1): distances belong to Matching

Fare asks Matching `GET /internal/zones/distance?from=BANANI&to=MOHAKHALI` → 3500 m (Part 4) and **caches the answer in Redis for 24 h** (`fare:dist:BANANI:MOHAKHALI`). One owner of distances means Nusrat is **priced** on the same 3500 m she's **routed** on (4.1). The cache means Fare doesn't call Matching on every estimate. The cost: if a distance override ever changes, Fare's cache must be cleared (noted in 4.8).

### Delegates (2): "was it pooled?" belongs to Trip

Fare doesn't look at pools. Trip decides (5.5's rule: 2 or more rides in the pool that weren't cancelled) and puts `pooled: true/false` in `trip.ride.completed`. Fare trusts it. So there's one definition of "pooled", in the service that knows who was in the car.

### Where Fare sits

```
Identity ──▶ Trip ──▶ Fare ──▶ Matching
```

Fare is called by **Trip** (quotes) and calls only **Matching** (distances). Nothing Fare calls ever calls back into Fare, so there's no circle (Part 0.2). Settlement doesn't need a call at all: it's driven by Trip's **event**, so a slow Fare never delays a drop-off.

### What Fare does *not* do

| Question | Who answers it |
|---|---|
| How far is Banani → Mohakhali? | Matching |
| Was Nusrat's ride pooled? | Trip |
| Who is this user? | Gateway + Identity |
| Put Nusrat in Bullet / guard the seats | Trip |
| Push "you paid ৳72.00" to her phone | Notification (listens to `fare.ride.settled`) |
| Store the final fare on her ride history | Trip (copies it from `fare.ride.settled`, 5.7) |

---

## What I did for 6.1

Like 4.1 and 5.1, this section is a **scope definition** with no code, so I added no files to `services/fare/` and checked what Fare will depend on:

| Check | Result |
|---|---|
| Part 1 pieces Fare uses: `Bus`, `emit`, `first_time`, `run_outbox_relay`, `OutboxMixin`/`ProcessedEventMixin`, `InternalAuth`, `ServiceClient`, `health_router`, `DomainError` | all used by Trip already, all import OK |
| Matching's distance endpoint | `GET /internal/zones/distance?from=&to=` → `{"distance_m": 3500}`, 422 `UNKNOWN_ZONE` (Part 4, tested) |
| **What Trip will call** (5.5's `FareClient`) | `POST /internal/quotes` and `GET /internal/quotes/{id}` are both in Fare's plan (6.5). Trip reads `quote_id, passenger_id, pickup_zone, dropoff_zone, seats, distance_m, solo_total_poysha, pooled_total_poysha, expires_at`; Fare's `QuoteOut` has all of them (plus the breakdowns) |
| **What Trip sends** | `trip.ride.completed` carries everything settlement reads (`ride_id, passenger_id, driver_id, quote_id, seats, pooled, payment_method`); `trip.ride.cancelled` carries `quote_id`. Checked against Trip's contract test (5.5) |
| **What Trip reads back** | Trip's consumer (5.7) takes `ride_id`, `total_poysha`, `payment_status` from `fare.ride.settled`; the plan's settlement sends all three |
| Gateway routes | `/api/v1/fares`, `/api/v1/wallet`, `/api/v1/driver/earnings` → Fare (Part 2). `/driver/earnings` wins over Trip's `/driver` because the gateway tries the longest prefix first |
| Seed wallets need Identity's fixed ids (6.7 step 6) | Identity's seed has them: Jashim `1111…`, Nusrat `2222…`, Rafiq `3333…`, Shirin `4444…` |

---

## 6.2: the fare model

### The formula

```
distance_charge = distance_m × per_km_poysha ÷ 1000          (round down)
pool_discount   = distance_charge × pool_discount_pct ÷ 100  (round down; 0 if not pooled)
per_seat        = base_poysha + distance_charge − pool_discount
total           = per_seat × seats
```

In words: **৳30 to get in, ৳15 per km, and 20 % off the km part if you shared the car.** Every seat pays the same.

Things worth noticing:
- **`distance_m` is the rider's *solo* distance**, Banani → Mohakhali = 3500 m, even if the pooled route went via Gulshan 1 and took longer. Nusrat isn't charged for Rafiq's detour. The 140 % rule (4.4) limits how long that detour can be.
- **The discount is only on the distance part**, not the ৳30 base. A pooled ride is always cheaper, but never below the base.
- **A 2-seat booking pays twice** the per-seat price: Nusrat + a friend, pooled, Banani → Mohakhali = 2 × 7200 = **14,400** (৳144.00).
- **"Round down" happens per seat, then × seats.** So the total is always exactly base + distance − discount, which is what the `fares` table's CHECK rule demands (6.4).

### The plan's three examples, by hand

| Rider | Trip | distance | base | distance charge | discount | **Total** |
|---|---|---|---|---|---|---|
| Nusrat | Banani → Mohakhali, 1 seat, **pooled** | 3500 m | 3000 | 3500 × 1500 ÷ 1000 = **5250** | 5250 × 20 ÷ 100 = **1050** | 3000 + 5250 − 1050 = **7200 (৳72.00)** |
| Rafiq | Banani → Gulshan 1, 1 seat, **pooled** | 2000 m | 3000 | 2000 × 1500 ÷ 1000 = **3000** | 3000 × 20 ÷ 100 = **600** | 3000 + 3000 − 600 = **5400 (৳54.00)** |
| Nusrat alone | Banani → Mohakhali, 1 seat, solo | 3500 m | 3000 | **5250** | **0** | 3000 + 5250 = **8250 (৳82.50)** |

Pooling saves Nusrat ৳10.50 (1050 poysha). These three rows are the PRD's "pooled fares calculate correctly" tests, and plan step 6.7.2 turns them into `test_pricing.py`.

### "Tariff locked at quote time"

The quote records **which tariff** priced it (`quotes.tariff_id`). Settlement prices the ride with **that** tariff and the **quote's** distance, not today's. So:
- if the price list changes while Nusrat is in the car, she still pays what she was shown;
- if a distance override changes mid-ride, same thing.

What *can* change between quote and settlement is only **pooled or not**, and that's the point: she's shown both prices, and pays the pooled one only if she really shared.

### What I did for 6.2

The model is a formula with no code of its own (the code, `pricing.py`, comes in 6.4), so I **checked it against Matching's real distance table**. I migrated a throwaway Matching database and priced **every one of the 72 zone pairs** with tariff v1 (a script in the scratchpad, not in the project):

| Check | Result |
|---|---|
| Banani → Mohakhali | 3500 m → **8250 solo, 7200 pooled**, the plan's numbers |
| Banani → Gulshan 1 | 2000 m → 6000 solo, **5400 pooled**, the plan's number |
| Gulshan 1 → Mohakhali | 2000 m → 6000 / 5400 |
| cheapest pair | Banani → Gulshan 2, 1000 m → ৳45.00 solo / ৳42.00 pooled |
| most expensive pair | Uttara → Dhanmondi, 18,700 m → ৳310.50 solo / ৳254.40 pooled |
| distances the same both ways | yes, all 72 |
| **does "round down" ever cut anything?** | **no, not once** (see below) |
| same zone (Banani → Banani) | Matching answers **0 m** (see finding 1) |

**Why rounding never bites with tariff v1:** Matching rounds every distance to **100 m** (4.3). 100 m × ৳15/km = 150 poysha exactly, and 20 % of any multiple of 150 is a multiple of 30. So every charge and discount is a whole number of poysha, and the floors in the formula are only there for **future** tariffs (say ৳17/km), where they guarantee whole poysha and never overcharge.

---

## Found while reading ahead (to fix in the right section)

**1. Pickup = drop-off would be priced at ৳30 (6.5).** *Fixed: the database refuses such a quote (6.4), and the request is refused at the door with a 422 (6.5).* Matching's distance for the same zone is **0 m**, so an estimate for Banani → Banani would quote the base fare. Trip already refuses this (5.3), but `POST /fares/estimate` is also called directly by the app. Fare's request shape needs the same "must differ" rule.

**2. `quotes.voided` is written but never read (6.5/6.6).** When Trip cancels a ride, settlement marks the quote `voided`, but `GET /internal/quotes/{id}` doesn't look at it. So Nusrat could cancel, then request again with the **same** quote within its 10 minutes, and Trip would accept it. It's harmless for money (each ride settles once, on its own `ride_id`), but then "voided" means nothing. I'll decide in 6.5: either refuse voided quotes, or drop the flag. *Decided in 6.5: voided quotes are refused.*

**3. A missing quote or tariff crashes settlement (6.6).** `quote = await s.get(Quote, ...)` then `quote.tariff_id`: an unknown `quote_id` gives `AttributeError`. The bus then retries 3 times and dead-letters it, which is the right outcome, but with a confusing error message. A clear error will make the dead-letter queue readable.

**4. `REFUNDED` is allowed but nothing produces it (6.4).** The `fares` status rule accepts `PAID / FAILED / REFUNDED`; there's no refund flow in the plan. It's harmless, and leaves room for one later. *Kept as is in 6.4.*

**5. Trip's test fakes use made-up prices (Part 5).** Trip's `FakeFare` quotes 11000 / 8800, and the 5.8 end-to-end test settles ৳105.00. They're only fakes, so nothing is wrong, but once Fare exists the tests would read better with the real 8250 / 7200. Optional tidy-up, later.

---

## 6.3: the directory structure

### What I created

```
services/fare/
├── Dockerfile                  generic service Dockerfile, port 8004
├── requirements.txt            uvicorn + alembic
├── requirements-dev.txt        pytest, pytest-asyncio, fakeredis (the distance cache), respx (fake Matching)
├── pytest.ini
├── alembic.ini
├── migrations/
│   ├── env.py                  same as Trip's (including the "don't silence loggers" fix from 5.5)
│   └── versions/
│       ├── 0001_init.py        all 7 tables (6.4)
│       └── 0002_seed_tariff.py tariff v1: 3000 / 1500 / 20 (6.4, plan step 6.7.1)
├── app/
│   ├── __init__.py
│   ├── config.py               settings
│   ├── deps.py                 db, auth, bus, redis, matching_http, settings
│   ├── models.py               tables (6.4)
│   ├── pricing.py              compute() (6.4)
│   ├── distance.py             Matching + Redis cache (6.4)
│   ├── schemas.py              request/response shapes (6.5)
│   ├── quotes.py               making and reading quotes (added in 6.5)
│   ├── settlement.py           placeholder: settling a ride, code in 6.6
│   ├── seed.py                 placeholder: demo wallets, code in 6.7
│   ├── main.py                 placeholder: app + lifespan, code in 6.7
│   └── routers/
│       ├── __init__.py
│       ├── fares.py            placeholder: /fares/estimate, /fares/rides/{id}
│       ├── wallet.py           placeholder: /wallet, /wallet/topup
│       ├── driver.py           placeholder: /driver/earnings
│       └── internal.py         placeholder: /internal/quotes (Trip)
└── tests/                      (6.4, see below)
```

### How the files map to 6.1's jobs

| 6.1 job | File |
|---|---|
| the price list | `models.py` (`tariffs`), `0002_seed_tariff.py` |
| the formula | `pricing.py` |
| distances (from Matching, cached) | `distance.py` |
| quotes | `models.py` (`quotes`), `routers/fares.py` + `routers/internal.py` |
| the ledger + wallets | `models.py`, `settlement.py` |
| driver earnings | `routers/driver.py` (worked out from the ledger) |

As in Matching and Trip, routers are split **by who calls them**, so each file has one security rule.

### Decisions

| Plan says | What I did | Why |
|---|---|---|
| no `__init__.py`, no `tests/`, no `pytest.ini`, no `requirements-dev.txt` in the folder list | added them | the same as Parts 3–5 |
| migration env | **copied Trip's** `env.py`, `alembic.ini`, `script.py.mako` | same proven setup, and it already has 5.5's logger fix |
| (no `config.py` contents given) | `DB_PATH` (`fare.db`), `REDIS_URL`, `RABBITMQ_URL`, `INTERNAL_TOKEN` (required), `MATCHING_URL`, **`QUOTE_TTL_SECONDS=600`**, `LOG_LEVEL` | the plan says quotes "expire in 10 min"; a setting keeps that number in one place |
| `deps.py` | `db`, `auth`, `bus`, **`redis`** (the distance cache) and **`matching_http`** | what plan step 6.7.7's lifespan needs |

**For 6.7:** the Dockerfile runs `alembic upgrade head` then `uvicorn`. When `seed.py` is written (demo wallets), the Dockerfile will need the `python -m app.seed` step in between, like Identity's.

---

## 6.4: the data layer

### Seven tables

| Table | One row is... | Rules the **database** enforces |
|---|---|---|
| `tariffs` | a price list | no negative prices; discount 0–100 %; **only one active** (**added**) |
| `quotes` | a promised price | 1–6 seats; the tariff must exist; **pickup ≠ drop-off**, **distance > 0**, **0 ≤ pooled ≤ solo** (**added**) |
| `fares` | a settled ride (ledger line) | **total = base + distance − discount**; total ≥ 0; `CASH`/`WALLET`; `PAID`/`FAILED`/`REFUNDED`; **one per ride**; its quote must exist; **no negative parts, 1–6 seats, and a discount only if pooled** (**added**) |
| `wallets` | a TeslaPay balance | **never below zero** |
| `wallet_transactions` | one money movement | `TOPUP`/`RIDE_DEBIT`/`DRIVER_CREDIT`; never 0; **one of each kind per ride per person** (no double debit); the wallet must exist; **the sign matches the kind**, and **ride money names its ride** (**added**) |
| `outbox`, `processed_events` | outgoing events / events already handled | Part 1's mixins, unchanged |

### The one-line rules that make money safe

**`total_poysha = base_poysha + distance_charge_poysha − pool_discount_poysha`** (the plan's). A fare that doesn't add up **can't be saved**. Since the formula works per seat and then multiplies (6.2), the rule holds for 2-seat fares too.

**`balance_poysha >= 0`** (the plan's). Settlement takes money with `UPDATE ... WHERE balance >= total`. If the wallet is short, 0 rows change and the payment is `FAILED` (6.6). Even if that check were forgotten, the database would refuse to let Nusrat's balance go negative.

**`UNIQUE (ride_id, kind, user_id)`** (the plan's). Nusrat can be debited for ride 1 **once**. A replayed event can't charge her twice, even if every other guard failed. Top-ups have no ride id, and SQLite treats each empty ride id as different, so she can top up as often as she likes.

### What I added, and why

| Added rule | Why |
|---|---|
| **only one active tariff** (a partial unique index, like Trip's) | an estimate uses "the active tariff". With two, the price would depend on which row the database happened to return. Changing prices = retire tariff 1 and add tariff 2 **in one transaction** (a test does exactly that). Tariff 1 stays, because old quotes point at it |
| quote: **pickup ≠ drop-off**, **distance > 0** | finding 1 from 6.2: Matching says 0 m for the same zone, which would quote the bare ৳30 base |
| quote: **0 ≤ pooled ≤ solo** | a quote that shows pooling as **dearer** would be a pricing bug; the database won't store one |
| fare: **a discount only if pooled** | the ledger must never show a pool discount on a solo ride. That's exactly the kind of mistake an auditor looks for |
| fare: **no negative parts**, **1–6 seats** | without it, a "discount" of −1050 would still satisfy the arithmetic rule (3000 + 5250 − (−1050) = 9300). A test proves the gap is closed |
| transaction: **sign matches kind** | a `RIDE_DEBIT` that **gives** money, or a `TOPUP` that **takes** it, is refused |
| transaction: **ride money names its ride; top-ups don't** | a debit without a ride id would escape the "no double debit" rule (empty ride ids never clash) |

All of these are **cheap for correct code and fatal for wrong code**: the plan's settlement (6.6) already writes rows that satisfy every one of them.

### `pricing.py`, as in the plan

`compute(tariff, distance_m, seats, pooled)` → base, distance charge, discount and total, **totals for all seats**. It's pure arithmetic in whole poysha: no database, no clock. It's used twice per quote (solo and pooled) and once at settlement.

### `distance.py`: two fixes

| Plan's code | Problem | Fix |
|---|---|---|
| only 422 from Matching is handled | a **401** (wrong token) or **404** reaches `resp.json()["distance_m"]` → **crash, 500**. The same trap as Trip's clients (5.5) and Identity (Part 3) | anything but 200 → **503 `UPSTREAM_ERROR`** |
| `await redis.get(...)` / `set(...)` unguarded | the cache only **saves a call**, yet a Redis outage would make **every estimate fail** | cache errors are logged and skipped; the price comes straight from Matching |

The rest is the plan's: key `fare:dist:{from}:{to}`, 24 h, one retry for Matching (it's a GET, so a retry is safe).

### Migrations

- **`0001_init.py`**: autogenerated from the models, then **hand-checked**: all 18 CHECK rules, the plan's and mine, plus the `WHERE active = 1` of the one-active index (the same autogenerate risk as Trip's, 5.3).
- **`0002_seed_tariff.py`**: tariff 1 = 3000 / 1500 / 20, active (plan step 6.7.1). Like Matching's zone seed, the numbers are **written out** in the migration, not imported from the app. It also says how to change prices later: a new migration adds tariff 2 and retires tariff 1, but never deletes it.

### How 6.3 and 6.4 were checked: 77 tests, all passing

| File | Tests | What |
|---|---|---|
| `test_pricing.py` | 21 | **the plan's three rows: 7200, 5400, 8250** (plan step 6.7.2, the PRD's "pooled fares calculate correctly"); 2 seats = 14,400; **total = its parts for every distance from 100 m to 30 km × 1–6 seats × pooled or not**; pooled never dearer and never below the base; the discount is only on the km part; a future ৳17/km tariff **rounds down, never up**; whole poysha always; 0 % and 100 % discount tariffs |
| `test_migrations.py` | 6 | exactly the 7 tables; every rule by name; the one-active index keeps its `WHERE`; **tariff v1 is seeded**; models and migration agree; downgrade step by step and back |
| `test_models.py` | 41 | every rule above: bad tariffs; **a second active tariff refused, but "retire 1, add 2" works**; bad quotes (incl. **same zone**, **0 m**, **pooled dearer than solo**, unknown tariff); Nusrat's ৳72 fare; fares that **don't add up**, **discount on a solo ride**, **negative discount**, bad seats/method/status, unknown quote; one fare per ride; a 2-seat fare adds up; **a wallet can't go below zero**; money moves the right way (and 8 ways it can't); **no double debit**; many top-ups are fine |
| `test_distance.py` | 9 | asks Matching (token, request id, from/to) and **caches for 24 h**; the second ask doesn't call Matching; each direction is its own entry; unknown zone → 422, nothing cached; **401/404 → 503**; Matching down → 503; **a timeout is retried once**; **Redis down still gives a price** |

Every module (including the placeholders) imports, and `config.py` gives the defaults above.

Run them with:

```
cd services\fare
..\..\.venv\Scripts\python -m pytest
```

**Break-it checks** (broke the migration or code on purpose, ran all tests, restored):

| Deliberately broke... | Result |
|---|---|
| same-zone quote allowed | 2 failed |
| discount on a solo ride allowed | 2 failed |
| money sign not checked | 4 failed |
| fare arithmetic not checked | 2 failed |
| wallet may go negative | 2 failed |
| one-active index loses its `WHERE` | 1 failed |
| pricing: discount on the base too | 7 failed |
| pricing: rounds **up** instead of down | 1 failed |
| pricing: seats ignored | 11 failed |
| distance: no cache | 1 failed |
| distance: 401/404 fix reverted | 2 failed |
| distance: Redis errors fatal again | 1 failed |

---

## 6.5: the API endpoints

The phone calls `/api/v1/fares/...`, `/api/v1/wallet...` and `/api/v1/driver/earnings`; the gateway checks the login and forwards (Part 2). Trip calls `/internal/quotes` directly with the internal token.

### The 7 endpoints

| Method | Route | Who | Answer | Errors |
|---|---|---|---|---|
| POST | `/fares/estimate` | PASSENGER | **201** a quote: solo and pooled price, both breakdowns, valid 10 min | 422 bad input / same zone / unknown zone; 503 Matching down or no active tariff |
| GET | `/fares/rides/{ride_id}` | PASSENGER or DRIVER | **200** the settled fare | 404 not settled yet, or **not yours** |
| GET | `/wallet` | PASSENGER or DRIVER | **200** balance + latest transactions, newest first (`?limit=`, default 20) | — (no wallet yet → balance 0) |
| POST | `/wallet/topup` | PASSENGER | **200** new balance | 422 outside 1–500,000 poysha |
| GET | `/driver/earnings` | DRIVER | **200** `{rides, total, cash, wallet}` | — |
| POST | `/internal/quotes` | Trip (internal token) | **201** a quote for `passenger_id` | 422, 503 |
| GET | `/internal/quotes/{id}` | Trip (internal token) | **200** the quote | 404 unknown **or voided** |

### Nusrat asks "how much?"

```
POST /fares/estimate  {"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 1}
→ 201 {"quote_id": "…", "distance_m": 3500,
       "solo_total_poysha": 8250,   "solo":   {base 3000, distance 5250, discount 0,    total 8250},
       "pooled_total_poysha": 7200, "pooled": {base 3000, distance 5250, discount 1050, total 7200},
       "expires_at": "…+10 min"}
```

1. The request shape refuses bad input **before** anything else: zone codes like `BANANI`, 1–6 seats, **pickup ≠ drop-off** (finding 1, now closed at the door as well as in the database).
2. The distance comes from `distance.py` (Matching, cached 24 h), **before** the write lock, so a slow Matching never blocks other writers.
3. Then, in one transaction: the **active** tariff, `compute()` twice (solo and pooled), and the quote saved **with its tariff id**.

The app later sends that `quote_id` to `POST /rides` (Trip), so the price she saw is the price she books.

### The shared piece: `quotes.py` (added)

The plan says Trip's internal quote is "the same as estimate, `passenger_id` in body". So the work lives **once**, in a small new module, `quotes.py`, used by both routers:
- `create_quote(...)`: the three steps above;
- `get_quote(...)`: reads a quote back for Trip;
- `quote_out(...)`: builds the answer. The two breakdowns are **recomputed from the quote's own tariff and distance**, so they always match the saved totals, even after prices change (a test changes the tariff and gets the **same** quote back).

### Decisions

| Plan says | What I did | Why |
|---|---|---|
| `GET /internal/quotes/{id}` → 404 if missing | also **404 if voided** (finding 2) | a cancelled ride's quote can't book another ride. Trip already turns this 404 into `QUOTE_NOT_FOUND` for the phone |
| estimate "422 zone" | **same-zone refused at the door** (`EstimateIn`, same rule as Trip's `RideCreate`) | Matching would say 0 m → a ৳30 quote. The database also refuses it (6.4) |
| (no rule for "which tariff") | the **active** one; none → **503 `NO_ACTIVE_TARIFF`** | the 6.4 "only one active" rule makes this unambiguous |
| quotes "expire in 10 min" | `QUOTE_TTL_SECONDS` (600) from settings | one place for the number |
| `/driver/earnings`: "sum over fares" | **cash** = cash fares + **wallet fares that FAILED**; **wallet** = wallet fares PAID; **refunded fares don't count** | when Nusrat's wallet is short, Jashim **collects cash** (plan 6.6). Counting that as "wallet" would show money that never reached his wallet |
| `GET /fares/rides/{id}`: passenger or driver "only if … = me" | a passenger is matched on `passenger_id`, a driver on `driver_id`, anyone else **404** (not 403) | same as Trip: Rafiq can't even learn that Nusrat's ride has a fare |
| `GET /wallet` "any" | PASSENGER or DRIVER | Jashim sees his `DRIVER_CREDIT`s; only passengers can top up |

### How money stays right here

- **Top-up is one statement**: "insert the wallet, or add to it if it exists" (the plan's SQLite upsert), plus a `TOPUP` transaction, **in one transaction**. **10 top-ups at the same moment** add up to exactly 10 × ৳10 with 10 transaction rows (a test).
- **The wallet list is private**: Rafiq sees ৳0 and no transactions after Nusrat tops up.
- **Double-tapping "top up"** would add money twice unless the app sends an `Idempotency-Key`. The gateway replays the first answer for any `POST` with a key, but **requires** one only on `POST /rides` (Part 2). It's simulated money, so this is noted, not changed.

### Trip's real client, against Fare's real endpoints

Each service was tested against the **plan** on its own. `test_trip_contract.py` checks that they fit **each other**: Trip's `clients.py` (5.5) imports nothing from Trip's app, so the test loads **Trip's actual `FareClient`** and points it at **Fare's actual routers**, in-process:

| Trip does… | Fare answers… | Trip ends up with… |
|---|---|---|
| new quote for Nusrat | 201 quote | 8250 / 7200, 3500 m |
| re-checks the app's quote | 200, Fare's time **without** a zone | accepted (Trip's 5.5 expiry fix handles either form) |
| Rafiq uses Nusrat's quote | 200 | **`QUOTE_MISMATCH`** |
| an expired quote | 200 | **`QUOTE_EXPIRED`** |
| a voided or unknown quote | 404 | **`QUOTE_NOT_FOUND`** (422 to the phone) |
| an unknown zone | 422 with Matching's message | `UNKNOWN_ZONE` "Unknown zone MOTIJHEEL" |

### How 6.5 was checked: 43 new tests (120 in total), all passing

Matching is faked with `respx`, using the real override distances (Banani ↔ Mohakhali 3500 m, Banani ↔ Gulshan 1 2000 m).

| File | Tests | What |
|---|---|---|
| `test_api.py` | 37 | estimate: **8250 / 7200 with both breakdowns**; saved with tariff 1, **expires in 10 min**; Rafiq **5400**, 2 seats **16,500 / 14,400**; Matching asked **once**, then cached; a new active tariff → new prices; no active tariff → 503; bad input (incl. **same zone**) → 422 **without asking Matching**; unknown zone → 422 with its message; drivers can't estimate (403), no user (401); `X-Request-Id` reaches Matching. Internal: Trip's quote, read back identical; Trip's fields present; not found; **voided → 404**; **a quote keeps its price after a tariff change**; token required; `passenger_id` required. Fares: Nusrat **and** Jashim see it; **Rafiq, another driver, and Nusrat-pretending-to-be-a-driver get 404**; not settled → 404. Wallet: none → 0; top-up creates then adds; limits 0 / −100 / 500,001 refused, 500,000 allowed; drivers can't top up but see their credits; **wallets are private**; newest first + limit; **10 concurrent top-ups all count**. Earnings: **cash, failed-wallet-as-cash, paid wallet, refunded excluded, another driver's excluded**; none yet → zeros; passengers 403 |
| `test_trip_contract.py` | 6 | the table above: **Trip's real `FareClient` ↔ Fare's real routers** |

**Break-it checks** (broke the code on purpose, ran all tests, restored):

| Deliberately broke... | Result |
|---|---|
| same zone allowed at the door | 1 failed |
| voided quotes still served | 2 failed |
| quote priced with any tariff, not the active one | 1 failed |
| quote lifetime ignored (1 day) | 1 failed |
| a fare visible to anyone | 1 failed |
| a driver matched on `passenger_id` | 2 failed |
| top-up **replaces** the balance instead of adding | 3 failed |
| top-up leaves no transaction | 3 failed |
| wallet shows everyone's transactions | 1 failed |
| earnings: failed wallet not counted as cash | 1 failed |
| earnings: refunds counted | 1 failed |
| internal routes without the token | 1 failed |

---

## Things to know before the next sections

- **Build order:** the plan's recommended order (0.7) builds Fare's **quotes** before Trip and **settlement** after. This project did Trip first, with Fare faked, so both halves come now.
- **Data stores:** `fare.db` (SQLite: tariff, quotes, fares, wallets) and **Redis** (the 24 h distance cache only).
- **Money is whole poysha everywhere.** No floats, ever.
- **A voided quote is gone for booking** (404 to Trip). Settlement (6.6) is what voids it, when Trip cancels a ride.
- **Changing prices** = a new migration: add tariff 2 as active, retire tariff 1 (never delete it). The one-active rule makes a half-done change impossible.
- **Changing a CHECK rule needs a hand-written migration** (as in Matching and Trip). `test_migrations.py` lists every rule by name.
- **The fare row is a ledger line:** written once, never updated.
- **Settlement is driven by Trip's event, not a call**, so it has to be safe to receive twice (the plan uses three layers for that, 6.6).
