# Dhaka Tesla Pool — Backend Implementation Plan

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.0 (async) + aiosqlite · Alembic · Pydantic v2 · RabbitMQ (`aio-pika`) · Redis (`redis.asyncio`) · Docker Compose
**Scope:** Backend services only. No frontend, templates or UI code.
**Cast used everywhere (seed data, tests, cURL):** Jashim (driver) · Bullet (3-seat Tesla) · Nusrat, Rafiq, Shirin (passengers)

---

## Table of Contents

- [Part 0 — System Topology & Inter-Service Contract Map](#part-0--system-topology--inter-service-contract-map)
- [Part 1 — Shared Library `tesla_common`](#part-1--shared-library-tesla_common)
- [Part 2 — API Gateway](#part-2--api-gateway)
- [Part 3 — Identity Service](#part-3--identity-service)
- [Part 4 — Location & Matching Service](#part-4--location--matching-service)
- [Part 5 — Trip & Pooling Service](#part-5--trip--pooling-service)
- [Part 6 — Fare & Billing Service](#part-6--fare--billing-service)
- [Part 7 — Notification Service](#part-7--notification-service)
- [Part 8 — Orchestration, Testing & Run Order](#part-8--orchestration-testing--run-order)

---

## Part 0 — System Topology & Inter-Service Contract Map

### 0.1 Assumptions & Deviations (put these in the README)

| # | Decision | Reason |
|---|---|---|
| A1 | Backend is Python/FastAPI microservices, not the PRD's mandated Node.js monolith. | Chosen stack for this build. PRD §6/§9 must be answered in the README: why each service/broker exists and what breaks without it. |
| A2 | One SQLite file per service, WAL mode, `BEGIN IMMEDIATE` for writes. | Database-per-service isolation. SQLite allows one writer at a time, which makes the seat-capacity check race-free on a single host. |
| A3 | Geography = 9 predefined Dhaka zones + a zone distance table. No map API. | PRD §4. Distances are hand-checkable. |
| A4 | **Pooling rule:** riders pool only if they share the **same pickup zone**, the pool has not started yet (`FORMING`), seats fit, and no rider's in-vehicle distance exceeds **140 %** of their solo distance. | Simple, testable, explains Nusrat + Rafiq (same Banani pickup, compatible drop-offs). |
| A5 | First rider in a pool needs a **driver accept**. Later riders **auto-join** an existing compatible pool; the driver is notified. | Jashim already committed to pooling by accepting; asking him again per rider slows matching. |
| A6 | Money is `INTEGER` poysha (100 poysha = ৳1). Integer division, floor. | No float rounding drift in ledgers. |
| A7 | Passengers pay for their **solo distance**, never for detours. Pool discount = 20 % of distance charge. Fare scales per seat. | Fair and hand-testable. |
| A8 | Passenger can cancel only in `REQUESTED` / `MATCHED`. After `DRIVER_ARRIVED` only the driver can cancel (no-show). | Protects the driver's time. |
| A9 | Delivery is **at-least-once** (transactional outbox). Every consumer is idempotent via a `processed_events` table. | Prevents lost or double-applied events. |

### 0.2 Services & Single Responsibilities

| Service | Port | Owns | Single Responsibility | Storage |
|---|---|---|---|---|
| **API Gateway** | 8000 | Nothing persistent | Edge: JWT verification, rate limiting, idempotency keys, routing | Redis only |
| **Identity** | 8001 | Users, drivers, vehicles | "Who is this and what do they drive?" Issues JWTs, driver online/offline | `identity.db` |
| **Location & Matching** | 8002 | Zones, distance table, live driver positions | "Where are drivers, and which pool/driver fits this request?" | `matching.db` + Redis GEO |
| **Trip & Pooling** | 8003 | Ride requests, pools, waypoints, status history, offers | "What is the state of each ride and pool?" Enforces capacity and the state machine | `trip.db` |
| **Fare & Billing** | 8004 | Tariffs, quotes, fares, wallets | "How much does each passenger pay?" Quotes and settlement | `fare.db` |
| **Notification** | 8005 | Notification inbox | "Tell the right person, now." WebSocket push + persisted inbox | `notification.db` + Redis |

```mermaid
flowchart LR
    C[Passenger / Driver clients] -->|HTTPS REST| GW[API Gateway :8000]
    C -->|WSS| NS[Notification :8005]
    GW --> ID[Identity :8001]
    GW --> MS[Location & Matching :8002]
    GW --> TS[Trip & Pooling :8003]
    GW --> FS[Fare & Billing :8004]
    GW --> NS
    TS -->|HTTP quote| FS
    TS -->|HTTP evaluate| MS
    FS -->|HTTP distance| MS
    ID -->|HTTP live-pool check| TS
    ID & TS & FS -->|publish via outbox| RMQ[(RabbitMQ tesla.events)]
    RMQ --> TS & MS & FS & NS
    MS -->|PUBLISH loc:pool:*| R[(Redis)]
    R -->|PSUBSCRIBE| NS
    GW --- R
    MS --- R
    ID -.-> IDB[(identity.db)]
    MS -.-> MDB[(matching.db)]
    TS -.-> TDB[(trip.db)]
    FS -.-> FDB[(fare.db)]
    NS -.-> NDB[(notification.db)]
```

**Synchronous call graph is acyclic:** Identity → Trip → {Fare, Matching}; Fare → Matching. No service calls back up the chain, so no distributed deadlock.

### 0.3 Communication Matrix

| Source | Destination | Mechanism | Endpoint / Routing key | Purpose |
|---|---|---|---|---|
| Client | Gateway | HTTP | `/api/v1/*` | All REST traffic |
| Client | Notification | WebSocket | `/ws?token=<JWT>` | Live status, offers, driver location |
| Gateway | Identity / Matching / Trip / Fare / Notification | HTTP (reverse proxy) | path-prefix routing | Forward authenticated requests with `X-User-*` + `X-Internal-Token` |
| Trip | Fare | HTTP | `POST /internal/quotes`, `GET /internal/quotes/{id}` | Price must be known before the ride is created |
| Trip | Matching | HTTP | `POST /internal/match/evaluate` | Needs compatible pools + candidate drivers immediately |
| Fare | Matching | HTTP | `GET /internal/zones/distance` | Zone distance for pricing (cached in Redis 24 h) |
| Identity | Trip | HTTP | `GET /internal/drivers/{id}/live-pool` | Block going offline with passengers on board |
| Identity | Trip, Matching | RabbitMQ | `identity.driver.online` / `.offline` | Driver shift projection; availability set |
| Trip | Matching | RabbitMQ | `trip.pool.updated` | Remove/add driver from available set |
| Trip | Fare | RabbitMQ | `trip.ride.completed`, `trip.ride.cancelled` | Settle or void |
| Trip | Notification | RabbitMQ | `trip.ride.*`, `trip.pool.updated` | Push status/offers/waypoints |
| Fare | Trip | RabbitMQ | `fare.ride.settled` | Store final fare on ride history |
| Fare | Notification | RabbitMQ | `fare.ride.settled` | Push "you paid ৳72.00" |
| Matching | Notification | Redis Pub/Sub | channel `loc:pool:{pool_id}` | Live vehicle position (too frequent for RabbitMQ) |

**Rule of thumb used:** HTTP when the caller cannot continue without the answer. RabbitMQ when the caller only needs to announce that something happened. Redis Pub/Sub for high-frequency, loss-tolerant telemetry.

### 0.4 Event Registry

**Exchange:** `tesla.events` · type `topic` · durable.
**Retry/DLQ:** per queue, via the default exchange (`""`): `<queue>.retry` (TTL 5 s → dead-letters back to `<queue>`) and `<queue>.dlq` (parking lot). Max 3 retries. Details in Part 1.

**Envelope (every message):**
```json
{
  "event_id": "uuid4",
  "event_type": "trip.ride.matched",
  "occurred_at": "2026-09-22T08:41:05.120Z",
  "producer": "trip-service",
  "version": 1,
  "data": { }
}
```

| Routing key | Producer | Trigger | `data` schema |
|---|---|---|---|
| `identity.driver.online` | Identity | Driver goes online | `{driver_id: uuid, driver_name: str, vehicle_id: uuid, vehicle_nickname: str, plate: str, seat_capacity: int}` |
| `identity.driver.offline` | Identity | Driver goes offline | `{driver_id: uuid}` |
| `trip.ride.requested` | Trip | Ride created and not auto-joined | `{ride_id, passenger_id, pickup_zone, dropoff_zone, seats: int, estimated_fare_poysha: int, candidate_driver_ids: [uuid]}` |
| `trip.ride.matched` | Trip | Joined a pool or driver accepted | `{ride_id, pool_id, passenger_id, driver_id, driver_name, vehicle_nickname, seats, joined_existing_pool: bool}` |
| `trip.ride.status_changed` | Trip | `DRIVER_ARRIVED`, `STARTED` | `{ride_id, pool_id, passenger_id, driver_id, from_status, to_status, actor_role}` |
| `trip.ride.cancelled` | Trip | Any → `CANCELLED` | `{ride_id, pool_id: uuid\|null, passenger_id, driver_id: uuid\|null, from_status, cancelled_by: PASSENGER\|DRIVER\|SYSTEM, reason, quote_id}` |
| `trip.ride.completed` | Trip | `STARTED` → `COMPLETED` | `{ride_id, pool_id, passenger_id, driver_id, quote_id, seats, payment_method: CASH\|WALLET, pooled: bool, co_rider_count: int}` |
| `trip.pool.updated` | Trip | Any pool change | `{pool_id, driver_id, status, occupied_seats, max_capacity, member_passenger_ids: [uuid], waypoints: [{seq, kind, zone, ride_id, done: bool}]}` |
| `fare.ride.settled` | Fare | Fare computed and paid/failed | `{fare_id, ride_id, passenger_id, driver_id, base_poysha, distance_charge_poysha, pool_discount_poysha, total_poysha, payment_method, payment_status: PAID\|FAILED}` |

| Queue | Owner | Bindings | Notes |
|---|---|---|---|
| `trip.driver-shift` | Trip | `identity.driver.*` | Maintains `driver_shifts` projection |
| `trip.fare-settled` | Trip | `fare.ride.settled` | Writes `final_fare_poysha` onto ride |
| `matching.fleet-state` | Matching | `identity.driver.*`, `trip.pool.updated` | Maintains Redis availability |
| `fare.ride-lifecycle` | Fare | `trip.ride.completed`, `trip.ride.cancelled` | Settlement |
| `notification.inbox` | Notification | `trip.ride.*`, `trip.pool.updated`, `fare.ride.settled` | Durable; writes inbox rows |
| *(server-named, exclusive)* | Notification (per instance) | same as above | Non-durable; pushes to sockets held by that instance |

### 0.5 Redis Usage Map

| Service | Key / Channel | Type | TTL | Purpose |
|---|---|---|---|---|
| Gateway | `rl:{principal_or_ip}:{epoch_minute}` | STRING (counter) | 60 s | Fixed-window rate limit |
| Gateway, Notification | `auth:revoked:{jti}` | STRING | token's remaining lifetime | JWT denylist (logout). Written by Identity |
| Gateway | `idem:{user_id}:{Idempotency-Key}` | STRING (JSON) | 120 s while `PENDING`, 24 h once `DONE` | Replays duplicate POSTs from flaky mobile networks |
| Matching | `geo:drivers` | GEO (sorted set) | none (members removed on offline) | Nearby driver search |
| Matching | `driver:{driver_id}` | HASH `{lat, lng, zone, online, pool_id, state_ts}` | none | Driver live state |
| Matching | `driver:{driver_id}:hb` | STRING | 30 s | Heartbeat; missing ⇒ treated as unreachable |
| Matching | `drivers:available` | SET | none | Online drivers with no live pool |
| Matching | `loc:pool:{pool_id}` | Pub/Sub channel | — | Live position fan-out |
| Fare | `fare:dist:{from}:{to}` | STRING (int metres) | 24 h | Cache of Matching's distance table |
| Notification | `notif:pool:{pool_id}:members` | SET | 6 h | Passenger ids to receive live location |

### 0.6 End-to-End Journey (the Banani story)

```mermaid
sequenceDiagram
    participant N as Nusrat
    participant GW as Gateway
    participant T as Trip
    participant F as Fare
    participant M as Matching
    participant Q as RabbitMQ
    participant NS as Notification
    participant J as Jashim
    N->>GW: POST /rides (Banani→Mohakhali, 1 seat, Idempotency-Key)
    GW->>T: forward + X-User-Id
    T->>F: POST /internal/quotes
    F-->>T: solo 8250, pooled 7200
    T->>M: POST /internal/match/evaluate (open pools = [])
    M-->>T: compatible_pools=[], candidate_drivers=[Jashim]
    T->>Q: trip.ride.requested (outbox)
    Q->>NS: push offer to Jashim
    J->>GW: POST /driver/offers/{ride}/accept
    GW->>T: create pool (1/3), ride MATCHED
    Note over T: Rafiq requests Banani→Gulshan 1
    T->>M: evaluate (open pools=[Bullet 1/3])
    M-->>T: Bullet compatible, plan B→G1→M
    T->>T: BEGIN IMMEDIATE; conditional UPDATE seats 1→2
    T->>Q: trip.ride.matched + trip.pool.updated
    Note over T: Shirin requests 2 seats → 1 left → not joined
```

### 0.7 Recommended Build Order (maps to `feature/*` branches)

1. `feature/common-lib` → Part 1
2. `feature/identity-auth` → Part 3
3. `feature/matching-zones` → Part 4
4. `feature/fare-quotes` → Part 6 (quotes only)
5. `feature/tesla-pooling` → Part 5
6. `feature/fare-settlement` → Part 6 (consumer + wallet)
7. `feature/notifications` → Part 7
8. `feature/gateway` → Part 2
9. `build/docker-compose` → Part 8

---

## Part 1 — Shared Library `tesla_common`

### 1.1 Overview & Scope
Holds cross-cutting code only: DB engine setup, event bus (publish, outbox relay, consumer with retry/DLQ), internal auth dependencies, HTTP client wrapper, error model, logging. **No domain logic and no shared models** — each service defines its own tables.

### 1.2 Directory Structure
```
dhaka-tesla-pool/
├── docker-compose.yml
├── .env.example
├── keys/                      # gitignored; jwt_private.pem, jwt_public.pem
├── scripts/
│   ├── gen_keys.sh
│   └── e2e.sh
├── libs/common/
│   ├── pyproject.toml
│   └── tesla_common/
│       ├── __init__.py
│       ├── timeutil.py
│       ├── db.py
│       ├── events.py
│       ├── auth.py
│       ├── http.py
│       ├── errors.py
│       ├── logging.py
│       └── health.py
└── services/
    ├── gateway/  identity/  matching/  trip/  fare/  notification/
```

`libs/common/pyproject.toml`
```toml
[project]
name = "tesla-common"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi~=0.115",
  "sqlalchemy[asyncio]~=2.0",
  "aiosqlite~=0.20",
  "aio-pika~=9.4",
  "redis~=5.0",
  "httpx~=0.27",
  "pyjwt[crypto]~=2.9",
  "pydantic~=2.8",
  "pydantic-settings~=2.4",
]
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
```

### 1.3 Code

`tesla_common/timeutil.py`
```python
from datetime import datetime, timezone
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid4())
```

`tesla_common/db.py` — two engines per service: `rw` (every transaction is `BEGIN IMMEDIATE`, i.e. takes SQLite's write lock up front) and `ro` (deferred, never blocks writers in WAL mode).
```python
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def make_engine(db_path: str, *, immediate: bool) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", connect_args={"timeout": 5})

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _record):
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return engine


class Database:
    def __init__(self, db_path: str):
        self.rw_engine = make_engine(db_path, immediate=True)
        self.ro_engine = make_engine(db_path, immediate=False)
        self.rw = async_sessionmaker(self.rw_engine, expire_on_commit=False)
        self.ro = async_sessionmaker(self.ro_engine, expire_on_commit=False)

    async def ro_session(self) -> AsyncIterator[AsyncSession]:
        async with self.ro() as s:
            yield s

    async def dispose(self) -> None:
        await self.rw_engine.dispose()
        await self.ro_engine.dispose()
```

`tesla_common/errors.py`
```python
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status


def _body(code: str, message: str, request: Request, details=None) -> dict:
    return {"error": {"code": code, "message": message,
                      "request_id": request.headers.get("x-request-id"), "details": details}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError):
        return JSONResponse(_body(exc.code, exc.message, request), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return JSONResponse(_body("VALIDATION_ERROR", "Invalid request", request, exc.errors()), status_code=422)
```

`tesla_common/logging.py`
```python
import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("request_id", "event_id", "ride_id", "pool_id"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(service: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
```

`tesla_common/auth.py` — services trust `X-User-*` headers **only** when `X-Internal-Token` matches. The gateway strips any client-sent `X-User-*` headers.
```python
import hmac
from typing import Literal

import jwt
from fastapi import Depends, Header
from pydantic import BaseModel

from .errors import DomainError

Role = Literal["PASSENGER", "DRIVER", "ADMIN"]
ISSUER = "tesla-identity"


class Principal(BaseModel):
    user_id: str
    role: Role
    name: str


class InternalAuth:
    def __init__(self, internal_token: str):
        self._token = internal_token

    def require_internal(self):
        async def dep(x_internal_token: str = Header(default="")) -> None:
            if not hmac.compare_digest(x_internal_token, self._token):
                raise DomainError("UNAUTHORIZED_INTERNAL", "Missing or invalid internal token", 401)
        return dep

    def principal(self):
        internal = self.require_internal()

        async def dep(
            _: None = Depends(internal),
            x_user_id: str = Header(default=""),
            x_user_role: str = Header(default=""),
            x_user_name: str = Header(default=""),
        ) -> Principal:
            if not x_user_id or x_user_role not in ("PASSENGER", "DRIVER", "ADMIN"):
                raise DomainError("UNAUTHENTICATED", "Authentication required", 401)
            return Principal(user_id=x_user_id, role=x_user_role, name=x_user_name)
        return dep

    def role(self, *roles: Role):
        principal_dep = self.principal()

        async def dep(p: Principal = Depends(principal_dep)) -> Principal:
            if p.role not in roles:
                raise DomainError("FORBIDDEN", f"Requires role {', '.join(roles)}", 403)
            return p
        return dep


def verify_jwt(token: str, public_key: str) -> dict:
    return jwt.decode(
        token, public_key, algorithms=["RS256"], issuer=ISSUER,
        options={"require": ["exp", "iat", "sub", "jti", "role"]},
    )
```

`tesla_common/http.py`
```python
import httpx

from .errors import DomainError


class ServiceClient:
    def __init__(self, base_url: str, internal_token: str, service_name: str):
        self.name = service_name
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(3.0, connect=1.0),
            headers={"X-Internal-Token": internal_token},
        )

    async def request(self, method: str, path: str, *, request_id: str | None = None, retries: int = 0, **kw) -> httpx.Response:
        headers = kw.pop("headers", {}) | ({"X-Request-Id": request_id} if request_id else {})
        attempts = retries + 1 if method == "GET" else 1
        last_exc: Exception | None = None
        for _ in range(attempts):
            try:
                resp = await self._client.request(method, path, headers=headers, **kw)
                if resp.status_code >= 500:
                    raise DomainError("UPSTREAM_ERROR", f"{self.name} returned {resp.status_code}", 503)
                return resp
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
        raise DomainError("UPSTREAM_UNAVAILABLE", f"{self.name} unavailable: {last_exc}", 503)

    async def aclose(self) -> None:
        await self._client.aclose()
```

`tesla_common/events.py` — bus, transactional outbox, consumer with retry + DLQ, idempotency helper.
```python
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message
from aio_pika.abc import AbstractIncomingMessage
from sqlalchemy import DateTime, Integer, String, Text, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from .timeutil import new_id, utcnow

log = logging.getLogger("tesla.events")
EXCHANGE = "tesla.events"
Handler = Callable[[dict], Awaitable[None]]


class OutboxMixin:
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True)
    routing_key: Mapped[str] = mapped_column(String(100))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class ProcessedEventMixin:
    __tablename__ = "processed_events"
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def emit(session: AsyncSession, outbox_model, producer: str, routing_key: str, data: dict) -> str:
    event_id = new_id()
    envelope = {
        "event_id": event_id,
        "event_type": routing_key,
        "occurred_at": utcnow().isoformat() + "Z",
        "producer": producer,
        "version": 1,
        "data": data,
    }
    session.add(outbox_model(event_id=event_id, routing_key=routing_key, payload=json.dumps(envelope)))
    return event_id


async def first_time(session: AsyncSession, processed_model, event_id: str) -> bool:
    if await session.get(processed_model, event_id) is not None:
        return False
    session.add(processed_model(event_id=event_id))
    return True


class Bus:
    def __init__(self, url: str):
        self.url = url
        self.connection: aio_pika.abc.AbstractRobustConnection | None = None
        self.channel: aio_pika.abc.AbstractChannel | None = None
        self.exchange: aio_pika.abc.AbstractExchange | None = None

    async def connect(self, prefetch: int = 20) -> None:
        self.connection = await aio_pika.connect_robust(self.url)
        self.channel = await self.connection.channel(publisher_confirms=True)
        await self.channel.set_qos(prefetch_count=prefetch)
        self.exchange = await self.channel.declare_exchange(EXCHANGE, ExchangeType.TOPIC, durable=True)

    async def publish_envelope(self, routing_key: str, envelope: dict) -> None:
        await self.exchange.publish(
            Message(
                json.dumps(envelope).encode(),
                content_type="application/json",
                delivery_mode=DeliveryMode.PERSISTENT,
                message_id=envelope["event_id"],
            ),
            routing_key=routing_key,
        )

    async def consume(self, queue_name: str, bindings: list[str], handler: Handler,
                      max_retries: int = 3, retry_delay_ms: int = 5000) -> None:
        ch = self.channel
        await ch.declare_queue(f"{queue_name}.dlq", durable=True)
        await ch.declare_queue(
            f"{queue_name}.retry", durable=True,
            arguments={"x-message-ttl": retry_delay_ms,
                       "x-dead-letter-exchange": "",
                       "x-dead-letter-routing-key": queue_name},
        )
        queue = await ch.declare_queue(queue_name, durable=True)
        for rk in bindings:
            await queue.bind(self.exchange, routing_key=rk)

        async def on_message(msg: AbstractIncomingMessage) -> None:
            try:
                envelope = json.loads(msg.body)
            except json.JSONDecodeError:
                await self._reroute(msg, f"{queue_name}.dlq", attempt=0, error="invalid JSON")
                return
            try:
                await handler(envelope)
                await msg.ack()
            except Exception as exc:
                attempt = int((msg.headers or {}).get("x-attempt", 0)) + 1
                target = f"{queue_name}.retry" if attempt <= max_retries else f"{queue_name}.dlq"
                log.exception("handler failed", extra={"event_id": envelope.get("event_id")})
                await self._reroute(msg, target, attempt=attempt, error=str(exc))

        await queue.consume(on_message)

    async def consume_broadcast(self, bindings: list[str], handler: Handler) -> None:
        queue = await self.channel.declare_queue(exclusive=True, auto_delete=True)
        for rk in bindings:
            await queue.bind(self.exchange, routing_key=rk)

        async def on_message(msg: AbstractIncomingMessage) -> None:
            async with msg.process(requeue=False):
                await handler(json.loads(msg.body))

        await queue.consume(on_message)

    async def _reroute(self, msg: AbstractIncomingMessage, target: str, *, attempt: int, error: str) -> None:
        try:
            headers = dict(msg.headers or {}) | {
                "x-attempt": attempt,
                "x-error": error[:500],
                "x-original-routing-key": msg.routing_key or "",
            }
            await self.channel.default_exchange.publish(
                Message(msg.body, headers=headers, content_type="application/json",
                        delivery_mode=DeliveryMode.PERSISTENT, message_id=msg.message_id),
                routing_key=target,
            )
            await msg.ack()
        except Exception:
            log.exception("reroute failed; requeueing")
            await msg.nack(requeue=True)

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()


async def run_outbox_relay(ro: async_sessionmaker, rw: async_sessionmaker, outbox_model, bus: Bus,
                           stop: asyncio.Event, interval: float = 0.5, batch: int = 100) -> None:
    while not stop.is_set():
        try:
            async with ro() as s:
                rows = (await s.execute(
                    select(outbox_model).where(outbox_model.published_at.is_(None))
                    .order_by(outbox_model.id).limit(batch)
                )).scalars().all()
            published: list[int] = []
            for row in rows:
                await bus.publish_envelope(row.routing_key, json.loads(row.payload))
                published.append(row.id)
            if published:
                async with rw.begin() as s:
                    await s.execute(update(outbox_model).where(outbox_model.id.in_(published))
                                    .values(published_at=utcnow()))
        except Exception:
            log.exception("outbox relay iteration failed")
        await asyncio.sleep(interval)
```
Why this shape: the DB write and the event are committed in **one SQLite transaction**; the relay publishes afterwards. A crash between publish and marking `published_at` causes a duplicate, which consumers drop via `first_time()`. The relay never holds the write lock during network I/O.

`tesla_common/health.py`
```python
from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse


def health_router(checks: dict[str, Callable[[], Awaitable[None]]]) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health():
        results, ok = {}, True
        for name, check in checks.items():
            try:
                await check()
                results[name] = "ok"
            except Exception as exc:
                results[name] = f"fail: {exc}"
                ok = False
        return JSONResponse({"status": "ok" if ok else "degraded", "checks": results}, status_code=200 if ok else 503)

    return router
```

### 1.4 Step-by-Step Implementation Guide
1. Create `libs/common` with the tree above; `pip install -e libs/common` into each service's venv.
2. Write `timeutil.py`, `errors.py`, `logging.py` first (no dependencies).
3. Write `db.py`; verify with a scratch script that two concurrent `rw` transactions serialize (second waits on `busy_timeout`).
4. Write `events.py`; test against a local RabbitMQ: publish a message, raise inside the handler, and confirm it lands in `<queue>.retry`, returns after 5 s, and ends in `<queue>.dlq` after the 4th failure.
5. Write `auth.py`, `http.py`, `health.py`.
6. Tag the lib `v0.1.0`; services pin to it.

---

## Part 2 — API Gateway

### 2.1 Overview & Domain Scope
**Owns:** request authentication (JWT RS256 verify), rate limiting, idempotency, routing, request ids.
**Delegates:** everything domain-related. No database. It never inspects business payloads.

### 2.2 Directory Structure
```
services/gateway/
├── Dockerfile
├── requirements.txt
└── app/
    ├── main.py
    ├── config.py
    ├── routes_table.py
    ├── security.py
    ├── ratelimit.py
    ├── idempotency.py
    └── proxy.py
```

### 2.3 Data Layer
None (stateless). Redis keys: `rl:*`, `idem:*`, reads `auth:revoked:*` (see 0.5).

### 2.4 Routing Table & Endpoints

`app/routes_table.py`
```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    prefix: str
    upstream: str
    public: bool = False
    rate_per_min: int = 120


def build_routes(s) -> list[Route]:
    routes = [
        Route("/api/v1/auth/register", s.IDENTITY_URL, public=True, rate_per_min=10),
        Route("/api/v1/auth/login", s.IDENTITY_URL, public=True, rate_per_min=10),
        Route("/api/v1/auth", s.IDENTITY_URL),
        Route("/api/v1/users", s.IDENTITY_URL),
        Route("/api/v1/drivers/me", s.IDENTITY_URL),
        Route("/api/v1/zones", s.MATCHING_URL, public=True),
        Route("/api/v1/driver/location", s.MATCHING_URL, rate_per_min=60),
        Route("/api/v1/fares", s.FARE_URL),
        Route("/api/v1/wallet", s.FARE_URL),
        Route("/api/v1/driver/earnings", s.FARE_URL),
        Route("/api/v1/rides", s.TRIP_URL),
        Route("/api/v1/driver", s.TRIP_URL),
        Route("/api/v1/notifications", s.NOTIFICATION_URL),
    ]
    return sorted(routes, key=lambda r: len(r.prefix), reverse=True)


def match(routes: list[Route], path: str) -> Route | None:
    for r in routes:
        if path == r.prefix or path.startswith(r.prefix + "/"):
            return r
    return None
```

| Method | Route | Auth | Behaviour |
|---|---|---|---|
| ANY | `/api/v1/{path}` | per table | Longest-prefix match → verify JWT (unless public) → rate limit → idempotency → proxy to `upstream + path without /api/v1` |
| GET | `/health` | none | Redis ping + each upstream `/health` |

`app/security.py`
```python
import jwt
from fastapi import Request
from redis.asyncio import Redis

from tesla_common.auth import Principal, verify_jwt
from tesla_common.errors import DomainError


async def authenticate(request: Request, redis: Redis, public_key: str) -> Principal:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise DomainError("UNAUTHENTICATED", "Bearer token required", 401)
    try:
        claims = verify_jwt(header[7:], public_key)
    except jwt.ExpiredSignatureError:
        raise DomainError("TOKEN_EXPIRED", "Token expired", 401)
    except jwt.InvalidTokenError:
        raise DomainError("INVALID_TOKEN", "Invalid token", 401)
    if await redis.exists(f"auth:revoked:{claims['jti']}"):
        raise DomainError("TOKEN_REVOKED", "Token revoked", 401)
    return Principal(user_id=claims["sub"], role=claims["role"], name=claims.get("name", ""))
```

`app/ratelimit.py`
```python
import time

from redis.asyncio import Redis

from tesla_common.errors import DomainError


async def enforce(redis: Redis, key_id: str, limit: int, window: int = 60) -> None:
    bucket = int(time.time() // window)
    key = f"rl:{key_id}:{bucket}"
    async with redis.pipeline(transaction=True) as p:
        p.incr(key)
        p.expire(key, window)
        count, _ = await p.execute()
    if count > limit:
        raise DomainError("RATE_LIMITED", f"Limit {limit}/min exceeded", 429)
```

`app/idempotency.py`
```python
import hashlib
import json

from fastapi import Request, Response
from redis.asyncio import Redis

from tesla_common.errors import DomainError

REQUIRED_ON = {("POST", "/api/v1/rides")}


async def begin(request: Request, redis: Redis, user_id: str, body: bytes) -> tuple[str | None, Response | None, str]:
    key = request.headers.get("idempotency-key")
    if (request.method, request.url.path) in REQUIRED_ON and not key:
        raise DomainError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required", 400)
    fingerprint = hashlib.sha256(request.method.encode() + request.url.path.encode() + body).hexdigest()
    if not key or request.method not in ("POST", "PATCH"):
        return None, None, fingerprint
    rkey = f"idem:{user_id}:{key}"
    if await redis.set(rkey, json.dumps({"state": "PENDING", "fp": fingerprint}), nx=True, ex=120):
        return rkey, None, fingerprint
    stored = json.loads(await redis.get(rkey) or "{}")
    if stored.get("fp") != fingerprint:
        raise DomainError("IDEMPOTENCY_KEY_REUSED", "Key already used for a different request", 422)
    if stored.get("state") == "PENDING":
        raise DomainError("IDEMPOTENCY_IN_PROGRESS", "Original request still processing", 409)
    return None, Response(content=stored["body"].encode(), status_code=stored["status"],
                          media_type="application/json", headers={"Idempotent-Replay": "true"}), fingerprint


async def finish(redis: Redis, rkey: str | None, fingerprint: str, status: int, body: bytes) -> None:
    if rkey is None:
        return
    if status >= 500:
        await redis.delete(rkey)
        return
    await redis.set(rkey, json.dumps({"state": "DONE", "fp": fingerprint, "status": status,
                                      "body": body.decode()}), ex=86400)
```

`app/proxy.py`
```python
import httpx
from fastapi import Request, Response

from tesla_common.auth import Principal
from tesla_common.errors import DomainError

HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer",
              "proxy-authorization", "proxy-authenticate", "host", "content-length", "authorization",
              "x-internal-token"}


def upstream_headers(request: Request, principal: Principal | None, internal_token: str, request_id: str) -> dict:
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in HOP_BY_HOP and not k.lower().startswith("x-user-")}
    headers["X-Internal-Token"] = internal_token
    headers["X-Request-Id"] = request_id
    if principal:
        headers["X-User-Id"] = principal.user_id
        headers["X-User-Role"] = principal.role
        headers["X-User-Name"] = principal.name
    return headers


async def forward(client: httpx.AsyncClient, request: Request, url: str, headers: dict, body: bytes) -> httpx.Response:
    try:
        return await client.request(request.method, url, params=request.query_params, content=body, headers=headers)
    except httpx.TimeoutException:
        raise DomainError("UPSTREAM_TIMEOUT", "Upstream timed out", 504)
    except httpx.TransportError:
        raise DomainError("UPSTREAM_UNAVAILABLE", "Upstream unavailable", 503)


def to_response(resp: httpx.Response) -> Response:
    headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP | {"content-encoding"}}
    return Response(content=resp.content, status_code=resp.status_code, headers=headers)
```

`app/main.py`
```python
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from redis.asyncio import Redis

from tesla_common.errors import DomainError, install_error_handlers
from tesla_common.logging import configure_logging
from tesla_common.timeutil import new_id

from . import idempotency, ratelimit
from .config import settings
from .proxy import forward, to_response, upstream_headers
from .routes_table import build_routes, match
from .security import authenticate


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("gateway")
    app.state.redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=1.0))
    app.state.routes = build_routes(settings)
    app.state.public_key = Path(settings.JWT_PUBLIC_KEY_PATH).read_text()
    yield
    await app.state.http.aclose()
    await app.state.redis.aclose()


app = FastAPI(title="Tesla Pool Gateway", lifespan=lifespan)
install_error_handlers(app)


@app.get("/health")
async def health(request: Request):
    await request.app.state.redis.ping()
    return {"status": "ok"}


@app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(request: Request, path: str):
    st = request.app.state
    full_path = f"/api/v1/{path}"
    route = match(st.routes, full_path)
    if route is None:
        raise DomainError("NOT_FOUND", "No such route", 404)
    request_id = request.headers.get("x-request-id") or new_id()
    principal = None if route.public else await authenticate(request, st.redis, st.public_key)
    client_id = principal.user_id if principal else (request.client.host if request.client else "anon")
    await ratelimit.enforce(st.redis, f"{route.prefix}:{client_id}", route.rate_per_min)
    body = await request.body()
    rkey, replay, fp = (None, None, "")
    if principal:
        rkey, replay, fp = await idempotency.begin(request, st.redis, principal.user_id, body)
        if replay is not None:
            return replay
    url = route.upstream + full_path.removeprefix("/api/v1")
    try:
        resp = await forward(st.http, request, url,
                             upstream_headers(request, principal, settings.INTERNAL_TOKEN, request_id), body)
    except DomainError:
        if rkey:
            await st.redis.delete(rkey)
        raise
    await idempotency.finish(st.redis, rkey, fp, resp.status_code, resp.content)
    out = to_response(resp)
    out.headers["X-Request-Id"] = request_id
    return out
```

`app/config.py`
```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    REDIS_URL: str
    INTERNAL_TOKEN: str
    JWT_PUBLIC_KEY_PATH: str
    IDENTITY_URL: str = "http://identity:8001"
    MATCHING_URL: str = "http://matching:8002"
    TRIP_URL: str = "http://trip:8003"
    FARE_URL: str = "http://fare:8004"
    NOTIFICATION_URL: str = "http://notification:8005"


settings = Settings()
```

### 2.5 Messaging Integration
- **Produces:** none. **Consumes:** none.
- **Redis:** `INCR`+`EXPIRE` (`rl:*`), `SET NX EX` / `GET` / `DEL` (`idem:*`), `EXISTS` (`auth:revoked:*`).

### 2.6 Step-by-Step Implementation Guide
1. `requirements.txt`: `-e /libs/common` is installed by the Dockerfile; add `uvicorn[standard]~=0.30`.
2. Write `config.py`, then `routes_table.py`; unit test `match()` so `/api/v1/driver/location` resolves to Matching and `/api/v1/driver/offers` to Trip.
3. Write `security.py`; test with a token signed by a test key pair (valid, expired, wrong issuer, revoked).
4. Write `ratelimit.py`; test 11th login in one minute returns 429.
5. Write `idempotency.py`; test: same key + same body → replay with `Idempotent-Replay: true`; same key + different body → 422; 5xx → key deleted.
6. Write `proxy.py` and `main.py`. Confirm a client-sent `X-User-Id` header is stripped.

---

## Part 3 — Identity Service

### 3.1 Overview & Domain Scope
**Owns:** users (passenger/driver/admin), password hashing, JWT issuing, driver profile, vehicle and its fixed seat capacity, coarse driver shift status (`OFFLINE`/`ONLINE`).
**Delegates:** whether a driver is busy (Trip knows pools), GPS (Matching).

### 3.2 Directory Structure
```
services/identity/
├── Dockerfile
├── requirements.txt          # + argon2-cffi~=23.1
├── alembic.ini
├── migrations/
│   ├── env.py
│   └── versions/0001_init.py
└── app/
    ├── main.py
    ├── config.py
    ├── db.py
    ├── models.py
    ├── schemas.py
    ├── tokens.py
    ├── clients.py
    ├── seed.py
    └── routers/
        ├── auth.py
        ├── drivers.py
        └── internal.py
```

### 3.3 Data Layer

| Table | Key columns | Constraints / Indexes |
|---|---|---|
| `users` | `id` PK, `full_name`, `phone`, `password_hash`, `role`, `created_at` | `phone` UNIQUE; `role IN (PASSENGER, DRIVER, ADMIN)` |
| `drivers` | `user_id` PK/FK→users, `license_number`, `status`, `updated_at` | `license_number` UNIQUE; `status IN (OFFLINE, ONLINE)` |
| `vehicles` | `id` PK, `driver_id` FK UNIQUE, `nickname`, `make`, `model`, `plate`, `seat_capacity` | `plate` UNIQUE; `seat_capacity BETWEEN 1 AND 6` |
| `outbox` | from `OutboxMixin` | `event_id` UNIQUE, index `published_at` |

`app/models.py`
```python
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from tesla_common.events import OutboxMixin
from tesla_common.timeutil import new_id, utcnow


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    full_name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(20), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (CheckConstraint("role IN ('PASSENGER','DRIVER','ADMIN')", name="ck_user_role"),)


class Driver(Base):
    __tablename__ = "drivers"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True)
    license_number: Mapped[str] = mapped_column(String(50), unique=True)
    status: Mapped[str] = mapped_column(String(10), default="OFFLINE")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    vehicle: Mapped["Vehicle | None"] = relationship(back_populates="driver", uselist=False, lazy="selectin")
    __table_args__ = (CheckConstraint("status IN ('OFFLINE','ONLINE')", name="ck_driver_status"),)


class Vehicle(Base):
    __tablename__ = "vehicles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    driver_id: Mapped[str] = mapped_column(ForeignKey("drivers.user_id", ondelete="RESTRICT"), unique=True)
    nickname: Mapped[str] = mapped_column(String(50))
    make: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(50))
    plate: Mapped[str] = mapped_column(String(30), unique=True)
    seat_capacity: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    driver: Mapped[Driver] = relationship(back_populates="vehicle")
    __table_args__ = (CheckConstraint("seat_capacity BETWEEN 1 AND 6", name="ck_vehicle_capacity"),)


class Outbox(OutboxMixin, Base):
    pass
```

`app/schemas.py`
```python
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Phone = Field(pattern=r"^01[3-9]\d{8}$")


class RegisterIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=100)
    phone: str = Phone
    password: str = Field(min_length=8, max_length=128)
    role: Literal["PASSENGER", "DRIVER"]
    license_number: str | None = Field(default=None, max_length=50)


class LoginIn(BaseModel):
    phone: str = Phone
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    full_name: str
    phone: str
    role: str


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserOut


class VehicleIn(BaseModel):
    nickname: str = Field(min_length=1, max_length=50)
    make: str = Field(max_length=50)
    model: str = Field(max_length=50)
    plate: str = Field(min_length=3, max_length=30)
    seat_capacity: int = Field(ge=1, le=6)


class VehicleOut(VehicleIn):
    model_config = ConfigDict(from_attributes=True)
    id: str


class DriverOut(BaseModel):
    user: UserOut
    license_number: str
    status: Literal["OFFLINE", "ONLINE"]
    vehicle: VehicleOut | None
```

`app/tokens.py`
```python
from datetime import timedelta

import jwt

from tesla_common.auth import ISSUER
from tesla_common.timeutil import new_id, utcnow


def issue_token(private_key: str, user_id: str, role: str, name: str, ttl_seconds: int) -> tuple[str, str]:
    now = utcnow()
    jti = new_id()
    token = jwt.encode(
        {"sub": user_id, "role": role, "name": name, "jti": jti, "iss": ISSUER,
         "iat": now, "exp": now + timedelta(seconds=ttl_seconds)},
        private_key, algorithm="RS256",
    )
    return token, jti
```

### 3.4 API Endpoints

| Method | Route | Auth | Success | Errors | Core logic |
|---|---|---|---|---|---|
| POST | `/auth/register` | public | 201 `UserOut` | 409 `PHONE_TAKEN`, 422 | Hash with argon2. `DRIVER` requires `license_number`; insert `drivers` row too |
| POST | `/auth/login` | public | 200 `TokenOut` | 401 `INVALID_CREDENTIALS` | Verify hash (same error for unknown phone and wrong password) |
| POST | `/auth/logout` | any | 204 | — | Revokes the current token (see note) |
| GET | `/users/me` | any | 200 `UserOut` | 404 | |
| GET | `/drivers/me` | DRIVER | 200 `DriverOut` | 404 | |
| PUT | `/drivers/me/vehicle` | DRIVER | 200 `VehicleOut` | 409 `DRIVER_ONLINE`, 409 `PLATE_TAKEN` | Upsert; editing forbidden while online (capacity must not change mid-shift) |
| POST | `/drivers/me/online` | DRIVER | 200 `DriverOut` | 409 `NO_VEHICLE` | Set `ONLINE`; emit `identity.driver.online` |
| POST | `/drivers/me/offline` | DRIVER | 200 `DriverOut` | 409 `DRIVER_HAS_LIVE_POOL` | HTTP Trip `/internal/drivers/{id}/live-pool`; if null → `OFFLINE`, emit `identity.driver.offline` |
| GET | `/internal/users/{id}` | internal | 200 `UserOut` | 404 | Used by admin/debug tooling |

**Logout note:** the gateway additionally forwards `X-Token-Jti` and `X-Token-Exp` (add two lines to `upstream_headers`). Identity sets `auth:revoked:{jti}` with `EX = exp - now`.

`app/routers/drivers.py` (online/offline — the only non-trivial writes)
```python
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from tesla_common.auth import Principal
from tesla_common.errors import DomainError
from tesla_common.events import emit

from ..deps import auth, db, trip_client
from ..models import Driver, Outbox, User
from ..schemas import DriverOut

router = APIRouter(prefix="/drivers/me", tags=["drivers"])
PRODUCER = "identity-service"


async def _load(s, user_id: str) -> tuple[User, Driver]:
    row = (await s.execute(select(User, Driver).join(Driver, Driver.user_id == User.id)
                           .where(User.id == user_id))).first()
    if row is None:
        raise DomainError("DRIVER_NOT_FOUND", "Driver profile not found", 404)
    return row[0], row[1]


@router.post("/online", response_model=DriverOut)
async def go_online(p: Principal = Depends(auth.role("DRIVER"))):
    async with db.rw.begin() as s:
        user, driver = await _load(s, p.user_id)
        if driver.vehicle is None:
            raise DomainError("NO_VEHICLE", "Register a vehicle first", 409)
        if driver.status != "ONLINE":
            driver.status = "ONLINE"
            v = driver.vehicle
            emit(s, Outbox, PRODUCER, "identity.driver.online", {
                "driver_id": user.id, "driver_name": user.full_name, "vehicle_id": v.id,
                "vehicle_nickname": v.nickname, "plate": v.plate, "seat_capacity": v.seat_capacity,
            })
    return _to_out(user, driver)


@router.post("/offline", response_model=DriverOut)
async def go_offline(request: Request, p: Principal = Depends(auth.role("DRIVER"))):
    resp = await trip_client.request("GET", f"/internal/drivers/{p.user_id}/live-pool",
                                     request_id=request.headers.get("x-request-id"), retries=1)
    if resp.json().get("pool_id"):
        raise DomainError("DRIVER_HAS_LIVE_POOL", "Finish or cancel your current pool first", 409)
    async with db.rw.begin() as s:
        user, driver = await _load(s, p.user_id)
        if driver.status != "OFFLINE":
            driver.status = "OFFLINE"
            emit(s, Outbox, PRODUCER, "identity.driver.offline", {"driver_id": user.id})
    return _to_out(user, driver)
```
(`deps.py` instantiates `db = Database(settings.DB_PATH)`, `auth = InternalAuth(settings.INTERNAL_TOKEN)`, `trip_client = ServiceClient(settings.TRIP_URL, ...)`. `_to_out` maps ORM → `DriverOut`.)

Race note: auto-join only targets existing pools, so after Trip answers "no pool" the only way Jashim gets a new pool is by accepting an offer himself. Trip's accept checks `driver_shifts.is_online`, which flips once `identity.driver.offline` is consumed (milliseconds). Document this small window as a known limitation.

### 3.5 Messaging Integration
- **Produces:** `identity.driver.online`, `identity.driver.offline` (via outbox relay).
- **Consumes:** none.
- **Redis:** `SET auth:revoked:{jti} 1 EX <remaining>` on logout.

### 3.6 Step-by-Step Implementation Guide
1. `alembic init -t async migrations`; in `env.py` set `target_metadata = Base.metadata`, URL from settings, and `context.configure(..., render_as_batch=True)` (SQLite cannot `ALTER` most things).
2. Write `models.py`, run `alembic revision --autogenerate -m init`, review the file, `alembic upgrade head`.
3. `scripts/gen_keys.sh`: `openssl genrsa -out keys/jwt_private.pem 2048 && openssl rsa -in keys/jwt_private.pem -pubout -out keys/jwt_public.pem`. Only Identity mounts the private key.
4. Write `schemas.py`, `tokens.py`, `routers/auth.py` (register/login/logout).
5. Write `routers/drivers.py` and `routers/internal.py`.
6. `main.py` lifespan: configure logging → `Bus.connect()` → start `run_outbox_relay` task → include routers + `health_router({"db": ..., "rabbitmq": ..., "redis": ...})` → on shutdown set stop event, cancel task, close bus, dispose DB.
7. `seed.py` (idempotent: skip if phone exists). Fixed UUIDs so other services' seeds line up:

| Name | Role | Phone | UUID |
|---|---|---|---|
| Jashim | DRIVER | 01711000001 | `11111111-1111-4111-8111-111111111111` |
| Nusrat | PASSENGER | 01711000002 | `22222222-2222-4222-8222-222222222222` |
| Rafiq | PASSENGER | 01711000003 | `33333333-3333-4333-8333-333333333333` |
| Shirin | PASSENGER | 01711000004 | `44444444-4444-4444-8444-444444444444` |
| Bullet | vehicle of Jashim, 3 seats, plate `DHAKA-TESLA-11` | — | `b1111111-1111-4111-8111-111111111111` |

   Demo password for all: `Pool@1234`.
8. Tests: register/login, duplicate phone 409, passenger calling `/drivers/me/online` → 403, vehicle edit while online → 409.

---

## Part 4 — Location & Matching Service

### 4.1 Overview & Domain Scope
**Owns:** the zone list, zone-to-zone distances, live driver positions (Redis GEO), driver availability set, and the **matching algorithm** (pool compatibility + candidate driver search).
**Delegates:** seat reservation (Trip). Matching is **advisory and stateless per call**: it ranks options; Trip performs the atomic write. Matching never reads `trip.db` — Trip sends the open-pool snapshot in the request.

### 4.2 Directory Structure
```
services/matching/
├── Dockerfile
├── requirements.txt
├── alembic.ini
├── migrations/versions/{0001_init.py, 0002_seed_zones.py}
└── app/
    ├── main.py
    ├── config.py
    ├── db.py
    ├── models.py
    ├── schemas.py
    ├── geo.py              # DistanceTable
    ├── planner.py          # pool insertion + detour rule
    ├── fleet.py            # Redis driver state
    ├── consumers.py
    └── routers/{public.py, driver.py, internal.py}
```

### 4.3 Data Layer

`app/models.py`
```python
from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tesla_common.events import ProcessedEventMixin


class Base(DeclarativeBase):
    pass


class Zone(Base):
    __tablename__ = "zones"
    code: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)


class ZoneDistance(Base):
    __tablename__ = "zone_distances"
    from_zone: Mapped[str] = mapped_column(ForeignKey("zones.code"), primary_key=True)
    to_zone: Mapped[str] = mapped_column(ForeignKey("zones.code"), primary_key=True)
    distance_m: Mapped[int] = mapped_column(Integer)
    __table_args__ = (CheckConstraint("distance_m > 0", name="ck_distance_positive"),
                      CheckConstraint("from_zone <> to_zone", name="ck_distance_distinct"))


class ProcessedEvent(ProcessedEventMixin, Base):
    pass
```

Seed (migration `0002_seed_zones.py`):

| code | name | lat | lng |
|---|---|---|---|
| BANANI | Banani | 23.7937 | 90.4066 |
| GULSHAN_1 | Gulshan 1 | 23.7806 | 90.4163 |
| GULSHAN_2 | Gulshan 2 | 23.7925 | 90.4144 |
| MOHAKHALI | Mohakhali | 23.7780 | 90.4050 |
| FARMGATE | Farmgate | 23.7580 | 90.3897 |
| DHANMONDI | Dhanmondi | 23.7465 | 90.3760 |
| MIRPUR | Mirpur | 23.8069 | 90.3687 |
| UTTARA | Uttara | 23.8759 | 90.3795 |
| BASHUNDHARA | Bashundhara | 23.8193 | 90.4526 |

Distance overrides (inserted in both directions) — these make the demo hand-checkable:

| From | To | distance_m |
|---|---|---|
| BANANI | MOHAKHALI | 3500 |
| BANANI | GULSHAN_1 | 2000 |
| GULSHAN_1 | MOHAKHALI | 2000 |

All other pairs: `round(haversine_m × 1.3, -2)` (1.3 = road-winding factor), computed in `geo.py`.

`app/geo.py`
```python
import math

from tesla_common.errors import DomainError


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class DistanceTable:
    ROAD_FACTOR = 1.3

    def __init__(self, zones: dict[str, tuple[float, float]], overrides: dict[tuple[str, str], int]):
        self.zones = zones
        self.overrides = overrides

    def require(self, code: str) -> None:
        if code not in self.zones:
            raise DomainError("UNKNOWN_ZONE", f"Unknown zone {code}", 422)

    def get(self, a: str, b: str) -> int:
        self.require(a)
        self.require(b)
        if a == b:
            return 0
        if (a, b) in self.overrides:
            return self.overrides[(a, b)]
        (la, lo), (lb, lob) = self.zones[a], self.zones[b]
        return int(round(haversine_m(la, lo, lb, lob) * self.ROAD_FACTOR, -2))

    def nearest_zone(self, lat: float, lng: float) -> str:
        return min(self.zones, key=lambda c: haversine_m(lat, lng, *self.zones[c]))
```

`app/schemas.py`
```python
from typing import Literal

from pydantic import BaseModel, Field

NEW_RIDE = "__new__"


class Stop(BaseModel):
    ride_id: str
    kind: Literal["PICKUP", "DROPOFF"]
    zone: str
    done: bool = False


class OpenPool(BaseModel):
    pool_id: str
    driver_id: str
    pickup_zone: str
    remaining_seats: int = Field(ge=0)
    version: int
    stops: list[Stop]


class EvaluateIn(BaseModel):
    pickup_zone: str
    dropoff_zone: str
    seats: int = Field(ge=1, le=6)
    open_pools: list[OpenPool] = []
    max_candidates: int = Field(default=5, ge=1, le=20)


class PoolOption(BaseModel):
    pool_id: str
    version: int
    total_route_m: int
    added_route_m: int
    max_detour_pct: int
    plan: list[Stop]


class CandidateDriver(BaseModel):
    driver_id: str
    distance_m: int


class EvaluateOut(BaseModel):
    solo_distance_m: int
    compatible_pools: list[PoolOption]
    candidate_drivers: list[CandidateDriver]


class LocationPing(BaseModel):
    lat: float = Field(ge=23.60, le=23.95)
    lng: float = Field(ge=90.30, le=90.55)
    heading: int | None = Field(default=None, ge=0, le=359)
```

### 4.4 Matching Algorithm

`app/planner.py` — integer-only; ratio compared as `in_vehicle × 100 ≤ solo × MAX_DETOUR_PCT`.
```python
from .geo import DistanceTable
from .schemas import NEW_RIDE, EvaluateIn, OpenPool, PoolOption, Stop


def _route(dist: DistanceTable, start: str, drops: list[Stop]) -> tuple[int, dict[str, int]]:
    total, prev, reached = 0, start, {}
    for st in drops:
        total += dist.get(prev, st.zone)
        prev = st.zone
        reached[st.ride_id] = total
    return total, reached


def plan_for_pool(dist: DistanceTable, pool: OpenPool, req: EvaluateIn, max_detour_pct: int) -> PoolOption | None:
    if pool.pickup_zone != req.pickup_zone or pool.remaining_seats < req.seats:
        return None
    pickups = [s for s in pool.stops if s.kind == "PICKUP"]
    drops = [s for s in pool.stops if s.kind == "DROPOFF"]
    solo = {s.ride_id: dist.get(pool.pickup_zone, s.zone) for s in drops}
    solo[NEW_RIDE] = dist.get(req.pickup_zone, req.dropoff_zone)
    current_total, _ = _route(dist, pool.pickup_zone, drops)

    best: PoolOption | None = None
    new_drop = Stop(ride_id=NEW_RIDE, kind="DROPOFF", zone=req.dropoff_zone)
    for i in range(len(drops) + 1):
        order = drops[:i] + [new_drop] + drops[i:]
        total, reached = _route(dist, pool.pickup_zone, order)
        worst = 0
        feasible = True
        for ride_id, in_vehicle in reached.items():
            if in_vehicle * 100 > solo[ride_id] * max_detour_pct:
                feasible = False
                break
            worst = max(worst, in_vehicle * 100 // solo[ride_id])
        if feasible and (best is None or total < best.total_route_m):
            plan = pickups + [Stop(ride_id=NEW_RIDE, kind="PICKUP", zone=req.pickup_zone)] + order
            best = PoolOption(pool_id=pool.pool_id, version=pool.version, total_route_m=total,
                              added_route_m=total - current_total, max_detour_pct=worst, plan=plan)
    return best


def rank_pools(dist: DistanceTable, req: EvaluateIn, max_detour_pct: int) -> list[PoolOption]:
    options = [o for p in req.open_pools if (o := plan_for_pool(dist, p, req, max_detour_pct))]
    return sorted(options, key=lambda o: (o.added_route_m, o.max_detour_pct))
```

**Worked example (Rafiq joining Nusrat):** pool pickup BANANI, drops `[Nusrat→MOHAKHALI]`.
- Insert Gulshan 1 first: route B→G1 (2000) →M (+2000) = 4000. Rafiq 2000/2000 = 100 %. Nusrat 4000/3500 = 114 % ≤ 140 % ✔.
- Insert Gulshan 1 last: B→M (3500) →G1 (+2000) = 5500. Rafiq 5500/2000 = 275 % ✘.
- Chosen plan: `PICKUP Nusrat @BANANI, PICKUP Rafiq @BANANI, DROPOFF Rafiq @GULSHAN_1, DROPOFF Nusrat @MOHAKHALI`.
- Shirin (2 seats) → `remaining_seats = 1 < 2` → pool filtered out.

### 4.5 Fleet State in Redis

`app/fleet.py`
```python
import json

from redis.asyncio import Redis

from .geo import DistanceTable

GEO = "geo:drivers"
AVAILABLE = "drivers:available"


def h(driver_id: str) -> str:
    return f"driver:{driver_id}"


async def record_ping(r: Redis, dist: DistanceTable, driver_id: str, lat: float, lng: float, ts: str) -> str:
    zone = dist.nearest_zone(lat, lng)
    async with r.pipeline(transaction=True) as p:
        p.geoadd(GEO, [lng, lat, driver_id])
        p.hset(h(driver_id), mapping={"lat": lat, "lng": lng, "zone": zone, "ping_ts": ts})
        p.set(f"{h(driver_id)}:hb", 1, ex=30)
        p.hget(h(driver_id), "pool_id")
        *_, pool_id = await p.execute()
    if pool_id:
        await r.publish(f"loc:pool:{pool_id}",
                        json.dumps({"pool_id": pool_id, "driver_id": driver_id, "lat": lat, "lng": lng,
                                    "zone": zone, "ts": ts}))
    return zone


async def _is_newer(r: Redis, driver_id: str, occurred_at: str) -> bool:
    last = await r.hget(h(driver_id), "state_ts")
    return last is None or occurred_at > last


async def on_driver_online(r: Redis, driver_id: str, occurred_at: str) -> None:
    if not await _is_newer(r, driver_id, occurred_at):
        return
    async with r.pipeline(transaction=True) as p:
        p.hset(h(driver_id), mapping={"online": 1, "state_ts": occurred_at})
        p.hget(h(driver_id), "pool_id")
        _, pool_id = await p.execute()
    if not pool_id:
        await r.sadd(AVAILABLE, driver_id)


async def on_driver_offline(r: Redis, driver_id: str, occurred_at: str) -> None:
    if not await _is_newer(r, driver_id, occurred_at):
        return
    async with r.pipeline(transaction=True) as p:
        p.hset(h(driver_id), mapping={"online": 0, "state_ts": occurred_at})
        p.srem(AVAILABLE, driver_id)
        p.zrem(GEO, driver_id)
        await p.execute()


async def on_pool_updated(r: Redis, driver_id: str, pool_id: str, status: str) -> None:
    if status in ("FORMING", "IN_PROGRESS"):
        async with r.pipeline(transaction=True) as p:
            p.hset(h(driver_id), "pool_id", pool_id)
            p.srem(AVAILABLE, driver_id)
            await p.execute()
        return
    current = await r.hget(h(driver_id), "pool_id")
    if current == pool_id:
        await r.hdel(h(driver_id), "pool_id")
        if await r.hget(h(driver_id), "online") == "1":
            await r.sadd(AVAILABLE, driver_id)


async def nearby_available(r: Redis, lat: float, lng: float, radius_m: int, limit: int) -> list[tuple[str, int]]:
    hits = await r.geosearch(GEO, longitude=lng, latitude=lat, radius=radius_m, unit="m",
                             sort="ASC", count=limit * 4, withdist=True)
    if not hits:
        return []
    async with r.pipeline(transaction=False) as p:
        for member, _ in hits:
            p.sismember(AVAILABLE, member)
            p.exists(f"{h(member)}:hb")
        flags = await p.execute()
    out = []
    for i, (member, d) in enumerate(hits):
        if flags[2 * i] and flags[2 * i + 1]:
            out.append((member, int(d)))
        if len(out) == limit:
            break
    return out
```

### 4.6 API Endpoints

| Method | Route | Auth | Success | Errors | Core logic |
|---|---|---|---|---|---|
| GET | `/zones` | public | 200 `[{code, name, lat, lng}]` | — | From in-memory table |
| POST | `/driver/location` | DRIVER | 202 `{zone}` | 422 outside Dhaka bbox | `record_ping` |
| GET | `/internal/zones/distance?from=&to=` | internal | 200 `{distance_m}` | 422 `UNKNOWN_ZONE` | `DistanceTable.get` |
| POST | `/internal/match/evaluate` | internal | 200 `EvaluateOut` | 422 | `rank_pools` + `nearby_available` around pickup centroid (radius `MATCH_RADIUS_M`, default 3000) |

`app/routers/internal.py`
```python
from fastapi import APIRouter, Depends, Query, Request

from ..deps import auth, settings
from ..fleet import nearby_available
from ..planner import rank_pools
from ..schemas import CandidateDriver, EvaluateIn, EvaluateOut

router = APIRouter(prefix="/internal", dependencies=[Depends(auth.require_internal())])


@router.get("/zones/distance")
async def distance(request: Request, from_: str = Query(alias="from"), to: str = Query()):
    return {"distance_m": request.app.state.dist.get(from_, to)}


@router.post("/match/evaluate", response_model=EvaluateOut)
async def evaluate(body: EvaluateIn, request: Request):
    dist = request.app.state.dist
    solo = dist.get(body.pickup_zone, body.dropoff_zone)
    pools = rank_pools(dist, body, settings.POOL_MAX_DETOUR_PCT)
    lat, lng = dist.zones[body.pickup_zone]
    drivers = await nearby_available(request.app.state.redis, lat, lng, settings.MATCH_RADIUS_M, body.max_candidates)
    return EvaluateOut(solo_distance_m=solo, compatible_pools=pools,
                       candidate_drivers=[CandidateDriver(driver_id=d, distance_m=m) for d, m in drivers])
```

### 4.7 Messaging Integration
- **Produces (RabbitMQ):** none. **Produces (Redis Pub/Sub):** `loc:pool:{pool_id}` on every ping of a driver with a live pool.
- **Consumes:** queue `matching.fleet-state`, bindings `identity.driver.*`, `trip.pool.updated`.

`app/consumers.py`
```python
from functools import partial

from redis.asyncio import Redis

from . import fleet


async def handle(redis: Redis, env: dict) -> None:
    data, kind, ts = env["data"], env["event_type"], env["occurred_at"]
    if kind == "identity.driver.online":
        await fleet.on_driver_online(redis, data["driver_id"], ts)
    elif kind == "identity.driver.offline":
        await fleet.on_driver_offline(redis, data["driver_id"], ts)
    elif kind == "trip.pool.updated":
        await fleet.on_pool_updated(redis, data["driver_id"], data["pool_id"], data["status"])


async def start(bus, redis: Redis) -> None:
    await bus.consume("matching.fleet-state", ["identity.driver.*", "trip.pool.updated"], partial(handle, redis))
```
Idempotency: all Redis operations are set-based and state-timestamped, so replays are harmless; no `processed_events` write needed here. Failures (Redis down) → retry ×3 → `matching.fleet-state.dlq`.

### 4.8 Step-by-Step Implementation Guide
1. Models + migrations `0001_init` and `0002_seed_zones` (use `op.bulk_insert`).
2. `geo.py` with unit tests: `get("BANANI","MOHAKHALI") == 3500`, symmetric overrides, unknown zone → 422.
3. `planner.py` with unit tests reproducing the worked example (Rafiq accepted, order G1 before M; Shirin rejected on seats; different pickup zone rejected).
4. `fleet.py`; integration-test against a real Redis (`fakeredis` lacks full GEOSEARCH support on some versions — prefer a throwaway Redis container).
5. Routers; lifespan loads zones + overrides into `app.state.dist`, creates Redis client, connects bus, starts consumer.
6. Health: DB + Redis + RabbitMQ.

---

## Part 5 — Trip & Pooling Service

### 5.1 Overview & Domain Scope
**Owns:** ride requests, pools, pool membership (via `ride_requests.pool_id`), waypoints, status history (audit), driver offers, and a local projection of driver shifts. **This is the only service allowed to change seat counts.**
**Delegates:** pricing (Fare), compatibility + driver search (Matching), user identity (JWT via gateway).

### 5.2 Directory Structure
```
services/trip/
├── Dockerfile
├── requirements.txt
├── alembic.ini
├── migrations/versions/0001_init.py
├── app/
│   ├── main.py
│   ├── config.py
│   ├── deps.py
│   ├── models.py
│   ├── schemas.py
│   ├── state_machine.py
│   ├── snapshots.py        # pool snapshot + event payload builders
│   ├── pooling.py          # request / join / accept
│   ├── lifecycle.py        # transitions + pool side effects
│   ├── clients.py          # FareClient, MatchingClient
│   ├── consumers.py
│   ├── workers.py          # stale-request sweeper
│   └── routers/{passenger.py, driver.py, internal.py}
└── tests/
    ├── conftest.py
    ├── test_state_machine.py
    ├── test_capacity.py
    ├── test_concurrency.py
    └── test_ownership.py
```

### 5.3 Data Layer

| Table | Purpose | Key constraints |
|---|---|---|
| `driver_shifts` | Projection of Identity driver/vehicle (name, nickname, capacity, online) | PK `driver_id`; `seat_capacity BETWEEN 1 AND 6` |
| `pools` | One shared trip for one vehicle | `0 ≤ occupied_seats ≤ max_capacity`; status enum; **partial UNIQUE (driver_id) WHERE status IN (FORMING, IN_PROGRESS)** |
| `ride_requests` | One passenger booking | seats 1–6; `pickup_zone <> dropoff_zone`; status enum; **partial UNIQUE (passenger_id) WHERE status is active** |
| `pool_waypoints` | Ordered stops | UNIQUE (pool_id, seq) |
| `ride_status_history` | Append-only audit ("explain exactly what happened") | index (ride_id, id) |
| `ride_offers` | Which drivers were offered a REQUESTED ride | PK (ride_id, driver_id); index (driver_id, status) |
| `outbox`, `processed_events` | Messaging | — |

**Pool status:** `FORMING` (accepting joins) → `IN_PROGRESS` (first rider `STARTED`; closed to joins) → `COMPLETED` | `CANCELLED`. "Full" is **derived** (`occupied_seats = max_capacity`), not stored — one less state to keep in sync.

`app/models.py`
```python
from datetime import datetime

from sqlalchemy import (Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String,
                        UniqueConstraint, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tesla_common.events import OutboxMixin, ProcessedEventMixin
from tesla_common.timeutil import new_id, utcnow

ACTIVE_RIDE_STATUSES = ("REQUESTED", "MATCHED", "DRIVER_ARRIVED", "STARTED")
LIVE_POOL_STATUSES = ("FORMING", "IN_PROGRESS")


class Base(DeclarativeBase):
    pass


class DriverShift(Base):
    __tablename__ = "driver_shifts"
    driver_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    driver_name: Mapped[str] = mapped_column(String(100))
    vehicle_id: Mapped[str] = mapped_column(String(36))
    vehicle_nickname: Mapped[str] = mapped_column(String(50))
    seat_capacity: Mapped[int] = mapped_column(Integer)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False)
    state_ts: Mapped[str] = mapped_column(String(40), default="")
    __table_args__ = (CheckConstraint("seat_capacity BETWEEN 1 AND 6", name="ck_shift_capacity"),)


class Pool(Base):
    __tablename__ = "pools"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    driver_id: Mapped[str] = mapped_column(ForeignKey("driver_shifts.driver_id", ondelete="RESTRICT"))
    vehicle_id: Mapped[str] = mapped_column(String(36))
    vehicle_nickname: Mapped[str] = mapped_column(String(50))
    driver_name: Mapped[str] = mapped_column(String(100))
    max_capacity: Mapped[int] = mapped_column(Integer)
    occupied_seats: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(15), default="FORMING")
    pickup_zone: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (
        CheckConstraint("max_capacity BETWEEN 1 AND 6", name="ck_pool_max"),
        CheckConstraint("occupied_seats >= 0 AND occupied_seats <= max_capacity", name="ck_pool_capacity"),
        CheckConstraint("status IN ('FORMING','IN_PROGRESS','COMPLETED','CANCELLED')", name="ck_pool_status"),
        Index("uq_pool_one_live_per_driver", "driver_id", unique=True,
              sqlite_where=text("status IN ('FORMING','IN_PROGRESS')")),
        Index("ix_pool_matching", "status", "pickup_zone"),
    )


class RideRequest(Base):
    __tablename__ = "ride_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    passenger_id: Mapped[str] = mapped_column(String(36))
    passenger_name: Mapped[str] = mapped_column(String(100))
    pool_id: Mapped[str | None] = mapped_column(ForeignKey("pools.id", ondelete="RESTRICT"))
    seats: Mapped[int] = mapped_column(Integer)
    pickup_zone: Mapped[str] = mapped_column(String(30))
    dropoff_zone: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="REQUESTED")
    payment_method: Mapped[str] = mapped_column(String(10))
    quote_id: Mapped[str] = mapped_column(String(36))
    solo_distance_m: Mapped[int] = mapped_column(Integer)
    estimated_fare_poysha: Mapped[int] = mapped_column(Integer)
    estimated_pooled_fare_poysha: Mapped[int] = mapped_column(Integer)
    final_fare_poysha: Mapped[int | None] = mapped_column(Integer)
    payment_status: Mapped[str | None] = mapped_column(String(10))
    cancel_reason: Mapped[str | None] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    __table_args__ = (
        CheckConstraint("seats BETWEEN 1 AND 6", name="ck_ride_seats"),
        CheckConstraint("pickup_zone <> dropoff_zone", name="ck_ride_zones"),
        CheckConstraint("status IN ('REQUESTED','MATCHED','DRIVER_ARRIVED','STARTED','COMPLETED','CANCELLED')",
                        name="ck_ride_status"),
        CheckConstraint("payment_method IN ('CASH','WALLET')", name="ck_ride_payment"),
        CheckConstraint("estimated_fare_poysha >= 0", name="ck_ride_fare"),
        CheckConstraint("status = 'REQUESTED' OR status = 'CANCELLED' OR pool_id IS NOT NULL",
                        name="ck_ride_pool_when_matched"),
        Index("uq_ride_one_active_per_passenger", "passenger_id", unique=True,
              sqlite_where=text("status IN ('REQUESTED','MATCHED','DRIVER_ARRIVED','STARTED')")),
        Index("ix_ride_passenger_created", "passenger_id", "created_at"),
        Index("ix_ride_status_created", "status", "created_at"),
        Index("ix_ride_pool", "pool_id"),
    )


class PoolWaypoint(Base):
    __tablename__ = "pool_waypoints"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pool_id: Mapped[str] = mapped_column(ForeignKey("pools.id", ondelete="CASCADE"))
    ride_request_id: Mapped[str] = mapped_column(ForeignKey("ride_requests.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(10))
    zone: Mapped[str] = mapped_column(String(30))
    done_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (
        UniqueConstraint("pool_id", "seq", name="uq_waypoint_seq"),
        CheckConstraint("seq > 0", name="ck_waypoint_seq"),
        CheckConstraint("kind IN ('PICKUP','DROPOFF')", name="ck_waypoint_kind"),
    )


class RideStatusHistory(Base):
    __tablename__ = "ride_status_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ride_id: Mapped[str] = mapped_column(ForeignKey("ride_requests.id", ondelete="CASCADE"))
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str] = mapped_column(String(36))
    actor_role: Mapped[str] = mapped_column(String(10))
    reason: Mapped[str | None] = mapped_column(String(200))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (Index("ix_history_ride", "ride_id", "id"),)


class RideOffer(Base):
    __tablename__ = "ride_offers"
    ride_id: Mapped[str] = mapped_column(ForeignKey("ride_requests.id", ondelete="CASCADE"), primary_key=True)
    driver_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(10), default="OFFERED")
    distance_m: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        CheckConstraint("status IN ('OFFERED','ACCEPTED','DECLINED')", name="ck_offer_status"),
        Index("ix_offer_driver", "driver_id", "status"),
    )


class Outbox(OutboxMixin, Base):
    pass


class ProcessedEvent(ProcessedEventMixin, Base):
    pass
```

`app/schemas.py`
```python
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RideStatus = Literal["REQUESTED", "MATCHED", "DRIVER_ARRIVED", "STARTED", "COMPLETED", "CANCELLED"]
ZoneCode = Field(pattern=r"^[A-Z0-9_]{2,30}$")


class RideCreate(BaseModel):
    pickup_zone: str = ZoneCode
    dropoff_zone: str = ZoneCode
    seats: int = Field(ge=1, le=6)
    payment_method: Literal["CASH", "WALLET"] = "CASH"
    quote_id: str | None = None

    @model_validator(mode="after")
    def _distinct(self):
        if self.pickup_zone == self.dropoff_zone:
            raise ValueError("pickup_zone and dropoff_zone must differ")
        return self


class CancelIn(BaseModel):
    reason: str = Field(default="changed_plans", max_length=200)


class DriverBrief(BaseModel):
    driver_id: str
    driver_name: str
    vehicle_nickname: str


class RideOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    status: RideStatus
    pickup_zone: str
    dropoff_zone: str
    seats: int
    pool_id: str | None
    payment_method: str
    estimated_fare_poysha: int
    estimated_pooled_fare_poysha: int
    final_fare_poysha: int | None
    payment_status: str | None
    cancel_reason: str | None
    created_at: datetime
    updated_at: datetime
    driver: DriverBrief | None = None


class HistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    from_status: str | None
    to_status: str
    actor_role: str
    reason: str | None
    at: datetime


class RideDetailOut(RideOut):
    history: list[HistoryOut]


class WaypointOut(BaseModel):
    seq: int
    kind: Literal["PICKUP", "DROPOFF"]
    zone: str
    ride_id: str
    passenger_name: str
    done: bool


class PoolRider(BaseModel):
    ride_id: str
    passenger_name: str
    seats: int
    status: RideStatus
    pickup_zone: str
    dropoff_zone: str


class PoolOut(BaseModel):
    id: str
    status: str
    vehicle_nickname: str
    occupied_seats: int
    max_capacity: int
    riders: list[PoolRider]
    waypoints: list[WaypointOut]


class OfferOut(BaseModel):
    ride_id: str
    passenger_name: str
    pickup_zone: str
    dropoff_zone: str
    seats: int
    estimated_fare_poysha: int
    distance_m: int
    offered_at: datetime
```
Driver sees passenger **names and stops** but **not fares** of passengers; passengers see only their own ride (PRD: "their own fare and their own status").

### 5.4 State Machine

| From → To | Allowed actor | Pool side effect |
|---|---|---|
| — → REQUESTED | PASSENGER | none |
| REQUESTED → MATCHED | SYSTEM (auto-join) / DRIVER (accept) | seats += n; waypoints re-planned |
| REQUESTED → CANCELLED | PASSENGER / SYSTEM (`NO_DRIVER_FOUND`) | none |
| MATCHED → DRIVER_ARRIVED | DRIVER | none |
| MATCHED → CANCELLED | PASSENGER / DRIVER | seats −= n; waypoints re-sequenced; pool may dissolve |
| DRIVER_ARRIVED → STARTED | DRIVER | pickup done; pool `FORMING → IN_PROGRESS` |
| DRIVER_ARRIVED → CANCELLED | DRIVER (`PASSENGER_NO_SHOW`) | as MATCHED → CANCELLED |
| STARTED → COMPLETED | DRIVER | dropoff done; seats −= n; pool completes when no active rides left |

Anything else → `409 INVALID_TRANSITION`. Terminal: `COMPLETED`, `CANCELLED`.

`app/state_machine.py`
```python
from typing import Literal

from tesla_common.errors import DomainError

Actor = Literal["PASSENGER", "DRIVER", "SYSTEM"]

TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("REQUESTED", "MATCHED"): frozenset({"SYSTEM", "DRIVER"}),
    ("REQUESTED", "CANCELLED"): frozenset({"PASSENGER", "SYSTEM"}),
    ("MATCHED", "DRIVER_ARRIVED"): frozenset({"DRIVER"}),
    ("MATCHED", "CANCELLED"): frozenset({"PASSENGER", "DRIVER"}),
    ("DRIVER_ARRIVED", "STARTED"): frozenset({"DRIVER"}),
    ("DRIVER_ARRIVED", "CANCELLED"): frozenset({"DRIVER"}),
    ("STARTED", "COMPLETED"): frozenset({"DRIVER"}),
}


def assert_transition(current: str, target: str, actor: Actor) -> None:
    if actor not in TRANSITIONS.get((current, target), frozenset()):
        raise DomainError("INVALID_TRANSITION", f"{current} → {target} is not allowed for {actor}", 409)
```

### 5.5 Core Logic

`app/snapshots.py`
```python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ACTIVE_RIDE_STATUSES, Pool, PoolWaypoint, RideRequest


async def pool_view(s: AsyncSession, pool: Pool) -> dict:
    rides = (await s.execute(select(RideRequest).where(RideRequest.pool_id == pool.id))).scalars().all()
    names = {r.id: r.passenger_name for r in rides}
    wps = (await s.execute(select(PoolWaypoint).where(PoolWaypoint.pool_id == pool.id)
                           .order_by(PoolWaypoint.seq))).scalars().all()
    return {
        "id": pool.id, "status": pool.status, "vehicle_nickname": pool.vehicle_nickname,
        "occupied_seats": pool.occupied_seats, "max_capacity": pool.max_capacity,
        "riders": [{"ride_id": r.id, "passenger_name": r.passenger_name, "seats": r.seats, "status": r.status,
                    "pickup_zone": r.pickup_zone, "dropoff_zone": r.dropoff_zone} for r in rides],
        "waypoints": [{"seq": w.seq, "kind": w.kind, "zone": w.zone, "ride_id": w.ride_request_id,
                       "passenger_name": names.get(w.ride_request_id, ""), "done": w.done_at is not None}
                      for w in wps],
        "_member_ids": [r.passenger_id for r in rides if r.status in ACTIVE_RIDE_STATUSES],
    }


def pool_updated_payload(pool: Pool, view: dict) -> dict:
    return {
        "pool_id": pool.id, "driver_id": pool.driver_id, "status": pool.status,
        "occupied_seats": pool.occupied_seats, "max_capacity": pool.max_capacity,
        "member_passenger_ids": view["_member_ids"],
        "waypoints": [{k: w[k] for k in ("seq", "kind", "zone", "ride_id", "done")} for w in view["waypoints"]],
    }


async def replace_waypoints(s: AsyncSession, pool_id: str, stops: list[dict]) -> None:
    existing = (await s.execute(select(PoolWaypoint).where(PoolWaypoint.pool_id == pool_id))).scalars().all()
    done = {(w.ride_request_id, w.kind): w.done_at for w in existing}
    for w in existing:
        await s.delete(w)
    await s.flush()
    for seq, st in enumerate(stops, start=1):
        s.add(PoolWaypoint(pool_id=pool_id, ride_request_id=st["ride_id"], seq=seq, kind=st["kind"],
                           zone=st["zone"], done_at=done.get((st["ride_id"], st["kind"]))))
```

`app/clients.py`
```python
from datetime import datetime

from pydantic import BaseModel

from tesla_common.errors import DomainError
from tesla_common.http import ServiceClient


class Quote(BaseModel):
    quote_id: str
    passenger_id: str
    pickup_zone: str
    dropoff_zone: str
    seats: int
    distance_m: int
    solo_total_poysha: int
    pooled_total_poysha: int
    expires_at: datetime


class FareClient:
    def __init__(self, client: ServiceClient):
        self.c = client

    async def quote_for(self, passenger_id: str, pickup: str, dropoff: str, seats: int,
                        quote_id: str | None, request_id: str | None) -> Quote:
        if quote_id:
            resp = await self.c.request("GET", f"/internal/quotes/{quote_id}", request_id=request_id, retries=1)
            if resp.status_code == 404:
                raise DomainError("QUOTE_NOT_FOUND", "Quote not found", 422)
            q = Quote.model_validate(resp.json())
            if (q.passenger_id, q.pickup_zone, q.dropoff_zone, q.seats) != (passenger_id, pickup, dropoff, seats):
                raise DomainError("QUOTE_MISMATCH", "Quote does not match this request", 422)
            if q.expires_at < datetime.utcnow():
                raise DomainError("QUOTE_EXPIRED", "Quote expired; request a new estimate", 422)
            return q
        resp = await self.c.request("POST", "/internal/quotes", request_id=request_id,
                                    json={"passenger_id": passenger_id, "pickup_zone": pickup,
                                          "dropoff_zone": dropoff, "seats": seats})
        if resp.status_code == 422:
            raise DomainError("UNKNOWN_ZONE", resp.json()["error"]["message"], 422)
        return Quote.model_validate(resp.json())


class MatchingClient:
    def __init__(self, client: ServiceClient):
        self.c = client

    async def evaluate(self, payload: dict, request_id: str | None) -> dict:
        resp = await self.c.request("POST", "/internal/match/evaluate", json=payload, request_id=request_id)
        if resp.status_code == 422:
            raise DomainError("UNKNOWN_ZONE", "Unknown zone", 422)
        return resp.json()
```

`app/pooling.py` — request, auto-join (compare-and-set), offers, driver accept.
```python
import enum

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tesla_common.auth import Principal
from tesla_common.errors import DomainError
from tesla_common.events import emit
from tesla_common.timeutil import new_id, utcnow

from .clients import FareClient, MatchingClient
from .models import (DriverShift, Outbox, Pool, PoolWaypoint, RideOffer, RideRequest, RideStatusHistory)
from .schemas import RideCreate
from .snapshots import pool_updated_payload, pool_view, replace_waypoints

PRODUCER = "trip-service"
NEW_RIDE = "__new__"


class JoinResult(enum.Enum):
    JOINED = "joined"
    STALE = "stale"
    NONE = "none"


def history(s: AsyncSession, ride_id: str, frm: str | None, to: str, actor_id: str, role: str, reason=None):
    s.add(RideStatusHistory(ride_id=ride_id, from_status=frm, to_status=to, actor_id=actor_id,
                            actor_role=role, reason=reason))


async def open_pool_snapshot(ro: async_sessionmaker, pickup_zone: str, seats: int) -> list[dict]:
    async with ro() as s:
        pools = (await s.execute(
            select(Pool).where(Pool.status == "FORMING", Pool.pickup_zone == pickup_zone,
                               Pool.max_capacity - Pool.occupied_seats >= seats)
        )).scalars().all()
        out = []
        for p in pools:
            wps = (await s.execute(select(PoolWaypoint).where(PoolWaypoint.pool_id == p.id)
                                   .order_by(PoolWaypoint.seq))).scalars().all()
            out.append({"pool_id": p.id, "driver_id": p.driver_id, "pickup_zone": p.pickup_zone,
                        "remaining_seats": p.max_capacity - p.occupied_seats, "version": p.version,
                        "stops": [{"ride_id": w.ride_request_id, "kind": w.kind, "zone": w.zone,
                                   "done": w.done_at is not None} for w in wps]})
        return out


async def try_join(rw: async_sessionmaker, ride_id: str, seats: int, options: list[dict]) -> JoinResult:
    if not options:
        return JoinResult.NONE
    async with rw.begin() as s:
        for opt in options:
            res = await s.execute(
                update(Pool)
                .where(Pool.id == opt["pool_id"], Pool.status == "FORMING", Pool.version == opt["version"],
                       Pool.occupied_seats + seats <= Pool.max_capacity)
                .values(occupied_seats=Pool.occupied_seats + seats, version=Pool.version + 1)
            )
            if res.rowcount != 1:
                continue
            ride_res = await s.execute(
                update(RideRequest).where(RideRequest.id == ride_id, RideRequest.status == "REQUESTED")
                .values(status="MATCHED", pool_id=opt["pool_id"], version=RideRequest.version + 1,
                        updated_at=utcnow())
            )
            if ride_res.rowcount != 1:
                raise DomainError("RIDE_NO_LONGER_REQUESTED", "Ride was cancelled during matching", 409)
            stops = [{**st, "ride_id": ride_id if st["ride_id"] == NEW_RIDE else st["ride_id"]} for st in opt["plan"]]
            await replace_waypoints(s, opt["pool_id"], stops)
            history(s, ride_id, "REQUESTED", "MATCHED", "system", "SYSTEM", "auto_joined_pool")
            await s.flush()
            pool = await s.get(Pool, opt["pool_id"])
            await s.refresh(pool)
            ride = await s.get(RideRequest, ride_id)
            await s.refresh(ride)
            view = await pool_view(s, pool)
            emit(s, Outbox, PRODUCER, "trip.ride.matched", {
                "ride_id": ride_id, "pool_id": pool.id, "passenger_id": ride.passenger_id,
                "driver_id": pool.driver_id, "driver_name": pool.driver_name,
                "vehicle_nickname": pool.vehicle_nickname, "seats": seats, "joined_existing_pool": True})
            emit(s, Outbox, PRODUCER, "trip.pool.updated", pool_updated_payload(pool, view))
            return JoinResult.JOINED
    return JoinResult.STALE


async def request_ride(p: Principal, body: RideCreate, *, ro, rw, fare: FareClient, matching: MatchingClient,
                       request_id: str | None) -> str:
    quote = await fare.quote_for(p.user_id, body.pickup_zone, body.dropoff_zone, body.seats, body.quote_id, request_id)
    ride_id = new_id()
    try:
        async with rw.begin() as s:
            s.add(RideRequest(id=ride_id, passenger_id=p.user_id, passenger_name=p.name, seats=body.seats,
                              pickup_zone=body.pickup_zone, dropoff_zone=body.dropoff_zone,
                              payment_method=body.payment_method, quote_id=quote.quote_id,
                              solo_distance_m=quote.distance_m, estimated_fare_poysha=quote.solo_total_poysha,
                              estimated_pooled_fare_poysha=quote.pooled_total_poysha))
            await s.flush()
            history(s, ride_id, None, "REQUESTED", p.user_id, "PASSENGER")
    except IntegrityError:
        raise DomainError("ACTIVE_RIDE_EXISTS", "You already have an active ride", 409)

    evaluation: dict = {"candidate_drivers": []}
    for _ in range(3):
        snapshot = await open_pool_snapshot(ro, body.pickup_zone, body.seats)
        evaluation = await matching.evaluate({"pickup_zone": body.pickup_zone, "dropoff_zone": body.dropoff_zone,
                                              "seats": body.seats, "open_pools": snapshot}, request_id)
        result = await try_join(rw, ride_id, body.seats, evaluation["compatible_pools"])
        if result is JoinResult.JOINED:
            return ride_id
        if result is JoinResult.NONE:
            break

    async with rw.begin() as s:
        candidates = evaluation["candidate_drivers"]
        for c in candidates:
            s.add(RideOffer(ride_id=ride_id, driver_id=c["driver_id"], distance_m=c["distance_m"]))
        emit(s, Outbox, PRODUCER, "trip.ride.requested", {
            "ride_id": ride_id, "passenger_id": p.user_id, "pickup_zone": body.pickup_zone,
            "dropoff_zone": body.dropoff_zone, "seats": body.seats,
            "estimated_fare_poysha": quote.solo_total_poysha,
            "candidate_driver_ids": [c["driver_id"] for c in candidates]})
    return ride_id


async def accept_offer(rw: async_sessionmaker, driver: Principal, ride_id: str) -> str:
    async with rw.begin() as s:
        offer = await s.get(RideOffer, (ride_id, driver.user_id))
        if offer is None or offer.status != "OFFERED":
            raise DomainError("OFFER_NOT_FOUND", "No open offer for this ride", 404)
        shift = await s.get(DriverShift, driver.user_id)
        if shift is None or not shift.is_online:
            raise DomainError("DRIVER_OFFLINE", "Go online before accepting rides", 409)
        ride = await s.get(RideRequest, ride_id)
        if ride.seats > shift.seat_capacity:
            raise DomainError("VEHICLE_TOO_SMALL", f"{shift.vehicle_nickname} has {shift.seat_capacity} seats", 409)
        pool = Pool(driver_id=shift.driver_id, vehicle_id=shift.vehicle_id, vehicle_nickname=shift.vehicle_nickname,
                    driver_name=shift.driver_name, max_capacity=shift.seat_capacity, occupied_seats=ride.seats,
                    pickup_zone=ride.pickup_zone)
        s.add(pool)
        try:
            await s.flush()
        except IntegrityError:
            raise DomainError("DRIVER_HAS_LIVE_POOL", "You already have a live pool; new riders join it automatically", 409)
        res = await s.execute(
            update(RideRequest).where(RideRequest.id == ride_id, RideRequest.status == "REQUESTED")
            .values(status="MATCHED", pool_id=pool.id, version=RideRequest.version + 1, updated_at=utcnow()))
        if res.rowcount != 1:
            raise DomainError("RIDE_NO_LONGER_AVAILABLE", "Ride was taken or cancelled", 409)
        offer.status = "ACCEPTED"
        await replace_waypoints(s, pool.id, [
            {"ride_id": ride_id, "kind": "PICKUP", "zone": ride.pickup_zone},
            {"ride_id": ride_id, "kind": "DROPOFF", "zone": ride.dropoff_zone}])
        history(s, ride_id, "REQUESTED", "MATCHED", driver.user_id, "DRIVER", "driver_accepted")
        await s.flush()
        view = await pool_view(s, pool)
        emit(s, Outbox, PRODUCER, "trip.ride.matched", {
            "ride_id": ride_id, "pool_id": pool.id, "passenger_id": ride.passenger_id, "driver_id": pool.driver_id,
            "driver_name": pool.driver_name, "vehicle_nickname": pool.vehicle_nickname, "seats": ride.seats,
            "joined_existing_pool": False})
        emit(s, Outbox, PRODUCER, "trip.pool.updated", pool_updated_payload(pool, view))
        return pool.id
```

**Why the seat check is race-free (the Shirin/Nusrat last-seat problem):**
1. `BEGIN IMMEDIATE` — only one write transaction runs at a time in `trip.db`; the second waits (`busy_timeout`).
2. The capacity check lives **inside** the `UPDATE ... WHERE occupied_seats + :n <= max_capacity` (compare-and-set). The loser re-reads the committed value, matches 0 rows, and moves on.
3. `version` guards the waypoint plan: if the pool changed since Matching evaluated it, the plan is stale → re-evaluate (max 3 times).
4. `CHECK (occupied_seats <= max_capacity)` is the last line of defence; even buggy code cannot commit an overbooked pool.
5. At scale (PostgreSQL): the same `UPDATE ... WHERE` statement works unchanged under row locks — no application change.

`app/lifecycle.py` — generic transition + pool effects.
```python
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tesla_common.auth import Principal
from tesla_common.errors import DomainError
from tesla_common.events import emit
from tesla_common.timeutil import utcnow

from .models import ACTIVE_RIDE_STATUSES, Outbox, Pool, PoolWaypoint, RideRequest
from .pooling import PRODUCER, history
from .snapshots import pool_updated_payload, pool_view, replace_waypoints
from .state_machine import Actor, assert_transition


def _actor(p: Principal | None) -> Actor:
    return "SYSTEM" if p is None else ("DRIVER" if p.role == "DRIVER" else "PASSENGER")


async def _load_owned(s: AsyncSession, ride_id: str, p: Principal | None) -> tuple[RideRequest, Pool | None]:
    ride = await s.get(RideRequest, ride_id)
    pool = await s.get(Pool, ride.pool_id) if ride and ride.pool_id else None
    if ride is None:
        raise DomainError("RIDE_NOT_FOUND", "Ride not found", 404)
    if p is not None:
        owns = ride.passenger_id == p.user_id if p.role == "PASSENGER" else (pool is not None and pool.driver_id == p.user_id)
        if not owns:
            raise DomainError("RIDE_NOT_FOUND", "Ride not found", 404)
    return ride, pool


async def _release_seats(s: AsyncSession, pool: Pool, ride: RideRequest, cancelled: bool) -> None:
    await s.execute(update(Pool).where(Pool.id == pool.id)
                    .values(occupied_seats=Pool.occupied_seats - ride.seats, version=Pool.version + 1))
    wps = (await s.execute(select(PoolWaypoint).where(PoolWaypoint.pool_id == pool.id)
                           .order_by(PoolWaypoint.seq))).scalars().all()
    if cancelled:
        stops = [{"ride_id": w.ride_request_id, "kind": w.kind, "zone": w.zone}
                 for w in wps if w.ride_request_id != ride.id]
        await replace_waypoints(s, pool.id, stops)
    else:
        for w in wps:
            if w.ride_request_id == ride.id and w.kind == "DROPOFF":
                w.done_at = utcnow()
    await s.flush()
    await s.refresh(pool)
    active = await s.scalar(select(func.count()).select_from(RideRequest)
                            .where(RideRequest.pool_id == pool.id, RideRequest.status.in_(ACTIVE_RIDE_STATUSES)))
    if active == 0:
        completed = await s.scalar(select(func.count()).select_from(RideRequest)
                                   .where(RideRequest.pool_id == pool.id, RideRequest.status == "COMPLETED"))
        pool.status = "COMPLETED" if completed else "CANCELLED"
        pool.completed_at = utcnow()


async def transition(rw: async_sessionmaker, ride_id: str, target: str, p: Principal | None,
                     reason: str | None = None) -> None:
    async with rw.begin() as s:
        ride, pool = await _load_owned(s, ride_id, p)
        actor = _actor(p)
        assert_transition(ride.status, target, actor)
        frm = ride.status
        res = await s.execute(
            update(RideRequest).where(RideRequest.id == ride.id, RideRequest.version == ride.version)
            .values(status=target, version=RideRequest.version + 1, updated_at=utcnow(),
                    cancel_reason=reason if target == "CANCELLED" else RideRequest.cancel_reason))
        if res.rowcount != 1:
            raise DomainError("CONCURRENT_MODIFICATION", "Ride changed; retry", 409)
        await s.refresh(ride)
        history(s, ride.id, frm, target, p.user_id if p else "system", actor, reason)

        if pool is not None:
            if target == "STARTED":
                await s.execute(update(PoolWaypoint).where(PoolWaypoint.pool_id == pool.id,
                                                           PoolWaypoint.ride_request_id == ride.id,
                                                           PoolWaypoint.kind == "PICKUP").values(done_at=utcnow()))
                if pool.status == "FORMING":
                    pool.status, pool.started_at = "IN_PROGRESS", utcnow()
            elif target in ("CANCELLED", "COMPLETED"):
                await _release_seats(s, pool, ride, cancelled=target == "CANCELLED")

        base = {"ride_id": ride.id, "pool_id": pool.id if pool else None, "passenger_id": ride.passenger_id,
                "driver_id": pool.driver_id if pool else None}
        if target == "CANCELLED":
            emit(s, Outbox, PRODUCER, "trip.ride.cancelled", base | {
                "from_status": frm, "cancelled_by": actor, "reason": reason, "quote_id": ride.quote_id})
        elif target == "COMPLETED":
            riders = await s.scalar(select(func.count()).select_from(RideRequest)
                                    .where(RideRequest.pool_id == pool.id, RideRequest.status != "CANCELLED"))
            emit(s, Outbox, PRODUCER, "trip.ride.completed", base | {
                "quote_id": ride.quote_id, "seats": ride.seats, "payment_method": ride.payment_method,
                "pooled": riders >= 2, "co_rider_count": riders - 1})
        else:
            emit(s, Outbox, PRODUCER, "trip.ride.status_changed", base | {
                "from_status": frm, "to_status": target, "actor_role": actor})
        if pool is not None:
            await s.flush()
            view = await pool_view(s, pool)
            emit(s, Outbox, PRODUCER, "trip.pool.updated", pool_updated_payload(pool, view))
```
Ownership failures return **404, not 403**, so Rafiq cannot even learn that Nusrat's ride id exists.

**`pooled` rule:** a ride is pooled if its pool has ≥ 2 non-cancelled rides when it completes. Rafiq completes first (Nusrat still `STARTED`) → pooled. Nusrat completes last (Rafiq `COMPLETED`) → pooled. If Rafiq had cancelled, Nusrat pays the solo fare.

`app/workers.py` — expire requests nobody accepted.
```python
import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from tesla_common.errors import DomainError
from tesla_common.timeutil import utcnow

from .lifecycle import transition
from .models import RideRequest

log = logging.getLogger("trip.sweeper")


async def expire_stale_requests(ro, rw, ttl_seconds: int, stop: asyncio.Event, every: float = 15.0) -> None:
    while not stop.is_set():
        try:
            cutoff = utcnow() - timedelta(seconds=ttl_seconds)
            async with ro() as s:
                ids = (await s.execute(select(RideRequest.id).where(
                    RideRequest.status == "REQUESTED", RideRequest.created_at < cutoff).limit(100))).scalars().all()
            for ride_id in ids:
                try:
                    await transition(rw, ride_id, "CANCELLED", None, "NO_DRIVER_FOUND")
                except DomainError:
                    pass
        except Exception:
            log.exception("sweeper iteration failed")
        await asyncio.sleep(every)
```

### 5.6 API Endpoints

| Method | Route | Auth | Success | Errors | Core logic |
|---|---|---|---|---|---|
| POST | `/rides` | PASSENGER (+ `Idempotency-Key` at gateway) | 201 `RideOut` (`MATCHED` if auto-joined else `REQUESTED`) | 409 `ACTIVE_RIDE_EXISTS`, 422 zone/quote | `pooling.request_ride` |
| GET | `/rides` | PASSENGER | 200 `[RideOut]` | — | Own rides, `?status=`, `?limit=20&before=<iso>` |
| GET | `/rides/{id}` | PASSENGER | 200 `RideDetailOut` | 404 | Own ride + history + driver brief |
| POST | `/rides/{id}/cancel` | PASSENGER | 200 `RideOut` | 404, 409 `INVALID_TRANSITION` | `transition(..., "CANCELLED")` |
| GET | `/driver/offers` | DRIVER | 200 `[OfferOut]` | — | Offers with status `OFFERED` whose ride is still `REQUESTED` |
| POST | `/driver/offers/{ride_id}/accept` | DRIVER | 200 `PoolOut` | 404, 409 | `pooling.accept_offer` |
| POST | `/driver/offers/{ride_id}/decline` | DRIVER | 204 | 404 | offer → `DECLINED` |
| GET | `/driver/pool` | DRIVER | 200 `PoolOut` / 204 | — | Live pool with riders + waypoints |
| GET | `/driver/pools` | DRIVER | 200 `[PoolOut]` | — | Past pools (history) |
| POST | `/driver/rides/{id}/arrive` | DRIVER | 200 `PoolOut` | 404, 409 | → `DRIVER_ARRIVED` |
| POST | `/driver/rides/{id}/start` | DRIVER | 200 `PoolOut` | 404, 409 | → `STARTED` |
| POST | `/driver/rides/{id}/complete` | DRIVER | 200 `PoolOut` | 404, 409 | → `COMPLETED` |
| POST | `/driver/rides/{id}/cancel` | DRIVER | 200 `PoolOut` | 404, 409 | → `CANCELLED` (`reason` required, e.g. `PASSENGER_NO_SHOW`) |
| GET | `/internal/drivers/{id}/live-pool` | internal | 200 `{pool_id: str\|null}` | — | Used by Identity |

`app/routers/passenger.py` (representative)
```python
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select

from tesla_common.auth import Principal
from tesla_common.errors import DomainError

from ..deps import auth, db, fare_client, matching_client
from ..lifecycle import transition
from ..models import Pool, RideRequest, RideStatusHistory
from ..pooling import request_ride
from ..schemas import CancelIn, DriverBrief, RideCreate, RideDetailOut, RideOut

router = APIRouter(prefix="/rides", tags=["passenger"])
passenger = auth.role("PASSENGER")


async def _ride_out(ride_id: str, user_id: str, detail: bool = False):
    async with db.ro() as s:
        ride = await s.get(RideRequest, ride_id)
        if ride is None or ride.passenger_id != user_id:
            raise DomainError("RIDE_NOT_FOUND", "Ride not found", 404)
        out = RideOut.model_validate(ride)
        if ride.pool_id:
            pool = await s.get(Pool, ride.pool_id)
            out.driver = DriverBrief(driver_id=pool.driver_id, driver_name=pool.driver_name,
                                     vehicle_nickname=pool.vehicle_nickname)
        if not detail:
            return out
        hist = (await s.execute(select(RideStatusHistory).where(RideStatusHistory.ride_id == ride_id)
                                .order_by(RideStatusHistory.id))).scalars().all()
        return RideDetailOut(**out.model_dump(), history=hist)


@router.post("", response_model=RideOut, status_code=status.HTTP_201_CREATED)
async def create(body: RideCreate, request: Request, p: Principal = Depends(passenger)):
    ride_id = await request_ride(p, body, ro=db.ro, rw=db.rw, fare=fare_client, matching=matching_client,
                                 request_id=request.headers.get("x-request-id"))
    return await _ride_out(ride_id, p.user_id)


@router.get("/{ride_id}", response_model=RideDetailOut)
async def get_one(ride_id: str, p: Principal = Depends(passenger)):
    return await _ride_out(ride_id, p.user_id, detail=True)


@router.post("/{ride_id}/cancel", response_model=RideOut)
async def cancel(ride_id: str, body: CancelIn, p: Principal = Depends(passenger)):
    await transition(db.rw, ride_id, "CANCELLED", p, body.reason)
    return await _ride_out(ride_id, p.user_id)
```

### 5.7 Messaging Integration

**Produces:** `trip.ride.requested`, `trip.ride.matched`, `trip.ride.status_changed`, `trip.ride.cancelled`, `trip.ride.completed`, `trip.pool.updated` — all through the outbox, in the same transaction as the state change.

**Consumes:**

`app/consumers.py`
```python
from functools import partial

from sqlalchemy import update

from tesla_common.events import first_time

from .models import DriverShift, ProcessedEvent, RideRequest


async def on_driver_shift(rw, env: dict) -> None:
    d, ts = env["data"], env["occurred_at"]
    async with rw.begin() as s:
        if not await first_time(s, ProcessedEvent, env["event_id"]):
            return
        shift = await s.get(DriverShift, d["driver_id"])
        if shift and shift.state_ts and shift.state_ts >= ts:
            return
        if env["event_type"] == "identity.driver.online":
            if shift is None:
                shift = DriverShift(driver_id=d["driver_id"])
                s.add(shift)
            shift.driver_name = d["driver_name"]
            shift.vehicle_id = d["vehicle_id"]
            shift.vehicle_nickname = d["vehicle_nickname"]
            shift.seat_capacity = d["seat_capacity"]
            shift.is_online = True
        elif shift is not None:
            shift.is_online = False
        if shift is not None:
            shift.state_ts = ts


async def on_fare_settled(rw, env: dict) -> None:
    d = env["data"]
    async with rw.begin() as s:
        if not await first_time(s, ProcessedEvent, env["event_id"]):
            return
        await s.execute(update(RideRequest).where(RideRequest.id == d["ride_id"])
                        .values(final_fare_poysha=d["total_poysha"], payment_status=d["payment_status"]))


async def start(bus, rw) -> None:
    await bus.consume("trip.driver-shift", ["identity.driver.*"], partial(on_driver_shift, rw))
    await bus.consume("trip.fare-settled", ["fare.ride.settled"], partial(on_fare_settled, rw))
```
Failure: exception → rollback (including the `processed_events` row) → retry ×3 at 5 s → DLQ. Because dedupe row and side effect commit together, a retry after partial failure is safe.

**Redis:** none required. (Trip's consistency comes from SQLite; adding a Redis lock here would be a second source of truth.)

### 5.8 Lifespan

`app/main.py`
```python
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from tesla_common.errors import install_error_handlers
from tesla_common.events import run_outbox_relay
from tesla_common.health import health_router
from tesla_common.logging import configure_logging

from . import consumers
from .deps import bus, db, fare_http, matching_http, settings
from .models import Outbox
from .routers import driver, internal, passenger
from .workers import expire_stale_requests


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("trip-service")
    await bus.connect()
    await consumers.start(bus, db.rw)
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(run_outbox_relay(db.ro, db.rw, Outbox, bus, stop)),
        asyncio.create_task(expire_stale_requests(db.ro, db.rw, settings.RIDE_REQUEST_TTL_SECONDS, stop)),
    ]
    yield
    stop.set()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await fare_http.aclose()
    await matching_http.aclose()
    await bus.close()
    await db.dispose()


async def _db_check():
    async with db.ro() as s:
        await s.execute(text("SELECT 1"))


async def _bus_check():
    if bus.connection is None or bus.connection.is_closed:
        raise RuntimeError("rabbitmq disconnected")


app = FastAPI(title="Trip & Pooling Service", lifespan=lifespan)
install_error_handlers(app)
app.include_router(passenger.router)
app.include_router(driver.router)
app.include_router(internal.router)
app.include_router(health_router({"db": _db_check, "rabbitmq": _bus_check}))
```

### 5.9 Step-by-Step Implementation Guide
1. `models.py` → Alembic autogenerate → **manually verify** the two partial unique indexes and all CHECK constraints appear in the migration (autogenerate can miss `sqlite_where`; add with `op.create_index(..., sqlite_where=sa.text(...))`).
2. `state_machine.py` + `test_state_machine.py` (parametrize every pair; only the 7 allowed pairs pass).
3. `snapshots.py`, `clients.py` (test with `respx` mocking Fare/Matching).
4. `pooling.py`: implement `try_join` first and test it directly against a temp SQLite file.
5. `lifecycle.py`; tests: cancel after `DRIVER_ARRIVED` by passenger → 409; cancel in `MATCHED` frees the seat; last rider completing sets pool `COMPLETED`.
6. `consumers.py`, `workers.py`.
7. Routers + `main.py`.
8. `test_concurrency.py` (see Part 8.5).

---

## Part 6 — Fare & Billing Service

### 6.1 Overview & Domain Scope
**Owns:** tariff, quotes, final fares (immutable ledger rows), simulated TeslaPay wallets, driver earnings.
**Delegates:** distances (Matching), deciding whether a ride was pooled (Trip reports it in the event).

### 6.2 Fare Model (hand-testable)

```
distance_charge = distance_m × per_km_poysha ÷ 1000          (floor)
pool_discount   = distance_charge × pool_discount_pct ÷ 100  (floor; 0 if not pooled)
per_seat        = base_poysha + distance_charge − pool_discount
total           = per_seat × seats
```
Tariff v1: `base = 3000` (৳30), `per_km = 1500` (৳15/km), `pool_discount_pct = 20`. `distance_m` = rider's **solo** distance.

| Rider | Trip | distance_m | base | distance charge | discount | **Total** |
|---|---|---|---|---|---|---|
| Nusrat | Banani → Mohakhali, 1 seat, pooled | 3500 | 3000 | 5250 | 1050 | **7200 (৳72.00)** |
| Rafiq | Banani → Gulshan 1, 1 seat, pooled | 2000 | 3000 | 3000 | 600 | **5400 (৳54.00)** |
| Nusrat solo | same | 3500 | 3000 | 5250 | 0 | 8250 (৳82.50) |

Tariff is locked at quote time (`quotes.tariff_id`), so a tariff change mid-ride does not change what the rider was shown.

### 6.3 Directory Structure
```
services/fare/
├── Dockerfile
├── requirements.txt
├── alembic.ini
├── migrations/versions/{0001_init.py, 0002_seed_tariff.py}
└── app/
    ├── main.py
    ├── config.py
    ├── deps.py
    ├── models.py
    ├── schemas.py
    ├── pricing.py
    ├── distance.py      # Matching client + Redis cache
    ├── settlement.py
    ├── seed.py          # cast wallets
    └── routers/{fares.py, wallet.py, driver.py, internal.py}
```

### 6.4 Data Layer

`app/models.py`
```python
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tesla_common.events import OutboxMixin, ProcessedEventMixin
from tesla_common.timeutil import new_id, utcnow


class Base(DeclarativeBase):
    pass


class Tariff(Base):
    __tablename__ = "tariffs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_poysha: Mapped[int] = mapped_column(Integer)
    per_km_poysha: Mapped[int] = mapped_column(Integer)
    pool_discount_pct: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (
        CheckConstraint("base_poysha >= 0 AND per_km_poysha >= 0", name="ck_tariff_nonneg"),
        CheckConstraint("pool_discount_pct BETWEEN 0 AND 100", name="ck_tariff_pct"),
    )


class Quote(Base):
    __tablename__ = "quotes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    passenger_id: Mapped[str] = mapped_column(String(36), index=True)
    pickup_zone: Mapped[str] = mapped_column(String(30))
    dropoff_zone: Mapped[str] = mapped_column(String(30))
    seats: Mapped[int] = mapped_column(Integer)
    distance_m: Mapped[int] = mapped_column(Integer)
    tariff_id: Mapped[int] = mapped_column(ForeignKey("tariffs.id"))
    solo_total_poysha: Mapped[int] = mapped_column(Integer)
    pooled_total_poysha: Mapped[int] = mapped_column(Integer)
    voided: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (CheckConstraint("seats BETWEEN 1 AND 6", name="ck_quote_seats"),)


class Fare(Base):
    __tablename__ = "fares"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ride_id: Mapped[str] = mapped_column(String(36), unique=True)
    passenger_id: Mapped[str] = mapped_column(String(36))
    driver_id: Mapped[str] = mapped_column(String(36))
    quote_id: Mapped[str] = mapped_column(ForeignKey("quotes.id"))
    seats: Mapped[int] = mapped_column(Integer)
    pooled: Mapped[bool] = mapped_column(Boolean)
    base_poysha: Mapped[int] = mapped_column(Integer)
    distance_charge_poysha: Mapped[int] = mapped_column(Integer)
    pool_discount_poysha: Mapped[int] = mapped_column(Integer)
    total_poysha: Mapped[int] = mapped_column(Integer)
    payment_method: Mapped[str] = mapped_column(String(10))
    payment_status: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        CheckConstraint("total_poysha = base_poysha + distance_charge_poysha - pool_discount_poysha",
                        name="ck_fare_arithmetic"),
        CheckConstraint("total_poysha >= 0", name="ck_fare_nonneg"),
        CheckConstraint("payment_method IN ('CASH','WALLET')", name="ck_fare_method"),
        CheckConstraint("payment_status IN ('PAID','FAILED','REFUNDED')", name="ck_fare_status"),
        Index("ix_fare_passenger", "passenger_id", "created_at"),
        Index("ix_fare_driver", "driver_id", "created_at"),
    )


class Wallet(Base):
    __tablename__ = "wallets"
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    balance_poysha: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (CheckConstraint("balance_poysha >= 0", name="ck_wallet_nonneg"),)


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("wallets.user_id"))
    ride_id: Mapped[str | None] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(15))
    amount_poysha: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        CheckConstraint("kind IN ('TOPUP','RIDE_DEBIT','DRIVER_CREDIT')", name="ck_wtx_kind"),
        CheckConstraint("amount_poysha <> 0", name="ck_wtx_nonzero"),
        UniqueConstraint("ride_id", "kind", "user_id", name="uq_wtx_ride_kind_user"),
        Index("ix_wtx_user", "user_id", "created_at"),
    )


class Outbox(OutboxMixin, Base):
    pass


class ProcessedEvent(ProcessedEventMixin, Base):
    pass
```
Note `ck_fare_arithmetic` stores **totals for all seats**, so the identity holds for multi-seat fares too.

`app/pricing.py`
```python
from dataclasses import dataclass

from .models import Tariff


@dataclass(frozen=True)
class Breakdown:
    base_poysha: int
    distance_charge_poysha: int
    pool_discount_poysha: int
    total_poysha: int


def compute(t: Tariff, distance_m: int, seats: int, pooled: bool) -> Breakdown:
    distance_charge = distance_m * t.per_km_poysha // 1000
    discount = distance_charge * t.pool_discount_pct // 100 if pooled else 0
    return Breakdown(
        base_poysha=t.base_poysha * seats,
        distance_charge_poysha=distance_charge * seats,
        pool_discount_poysha=discount * seats,
        total_poysha=(t.base_poysha + distance_charge - discount) * seats,
    )
```

`app/distance.py`
```python
from redis.asyncio import Redis

from tesla_common.errors import DomainError
from tesla_common.http import ServiceClient


async def zone_distance(redis: Redis, matching: ServiceClient, a: str, b: str, request_id: str | None) -> int:
    key = f"fare:dist:{a}:{b}"
    cached = await redis.get(key)
    if cached is not None:
        return int(cached)
    resp = await matching.request("GET", "/internal/zones/distance", params={"from": a, "to": b},
                                  request_id=request_id, retries=1)
    if resp.status_code == 422:
        raise DomainError("UNKNOWN_ZONE", resp.json()["error"]["message"], 422)
    d = int(resp.json()["distance_m"])
    await redis.set(key, d, ex=86400)
    return d
```

### 6.5 API Endpoints

| Method | Route | Auth | Success | Errors | Core logic |
|---|---|---|---|---|---|
| POST | `/fares/estimate` | PASSENGER | 201 `QuoteOut` | 422 zone | Distance → `compute()` twice (solo, pooled) → persist quote, expires in 10 min |
| GET | `/fares/rides/{ride_id}` | PASSENGER / DRIVER | 200 `FareOut` | 404 | Passenger: only if `passenger_id` = me. Driver: only if `driver_id` = me |
| GET | `/wallet` | any | 200 `{balance_poysha, transactions[]}` | — | Missing wallet → balance 0 |
| POST | `/wallet/topup` | PASSENGER | 200 `{balance_poysha}` | 422 | Simulated; `1 ≤ amount ≤ 500000`; upsert + TOPUP txn |
| GET | `/driver/earnings` | DRIVER | 200 `{rides, total_poysha, cash_poysha, wallet_poysha}` | — | Sum over `fares` where `driver_id` = me |
| POST | `/internal/quotes` | internal | 201 `QuoteOut` | 422 | Same as estimate, `passenger_id` in body |
| GET | `/internal/quotes/{id}` | internal | 200 `QuoteOut` | 404 | Used by Trip |

`QuoteOut`: `{quote_id, passenger_id, pickup_zone, dropoff_zone, seats, distance_m, solo_total_poysha, pooled_total_poysha, solo: Breakdown, pooled: Breakdown, expires_at}`.

Wallet top-up upsert:
```python
from sqlalchemy.dialects.sqlite import insert

stmt = insert(Wallet).values(user_id=user_id, balance_poysha=amount)
stmt = stmt.on_conflict_do_update(index_elements=[Wallet.user_id],
                                  set_={"balance_poysha": Wallet.balance_poysha + stmt.excluded.balance_poysha})
await s.execute(stmt)
s.add(WalletTransaction(user_id=user_id, kind="TOPUP", amount_poysha=amount))
```

### 6.6 Messaging Integration

**Consumes:** `fare.ride-lifecycle` ← `trip.ride.completed`, `trip.ride.cancelled`.
**Produces:** `fare.ride.settled`.

`app/settlement.py`
```python
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from tesla_common.events import emit, first_time

from .models import Fare, Outbox, ProcessedEvent, Quote, Tariff, Wallet, WalletTransaction
from .pricing import compute

PRODUCER = "fare-service"


async def ensure_wallet(s, user_id: str) -> None:
    await s.execute(insert(Wallet).values(user_id=user_id, balance_poysha=0)
                    .on_conflict_do_nothing(index_elements=[Wallet.user_id]))


async def handle(rw, env: dict) -> None:
    d = env["data"]
    async with rw.begin() as s:
        if not await first_time(s, ProcessedEvent, env["event_id"]):
            return
        if env["event_type"] == "trip.ride.cancelled":
            await s.execute(update(Quote).where(Quote.id == d["quote_id"]).values(voided=True))
            return

        if await s.scalar(select(Fare.id).where(Fare.ride_id == d["ride_id"])):
            return
        quote = await s.get(Quote, d["quote_id"])
        tariff = await s.get(Tariff, quote.tariff_id)
        b = compute(tariff, quote.distance_m, d["seats"], d["pooled"])

        status = "PAID"
        if d["payment_method"] == "WALLET":
            res = await s.execute(update(Wallet)
                                  .where(Wallet.user_id == d["passenger_id"], Wallet.balance_poysha >= b.total_poysha)
                                  .values(balance_poysha=Wallet.balance_poysha - b.total_poysha))
            if res.rowcount == 1:
                s.add(WalletTransaction(user_id=d["passenger_id"], ride_id=d["ride_id"], kind="RIDE_DEBIT",
                                        amount_poysha=-b.total_poysha))
                await ensure_wallet(s, d["driver_id"])
                await s.execute(update(Wallet).where(Wallet.user_id == d["driver_id"])
                                .values(balance_poysha=Wallet.balance_poysha + b.total_poysha))
                s.add(WalletTransaction(user_id=d["driver_id"], ride_id=d["ride_id"], kind="DRIVER_CREDIT",
                                        amount_poysha=b.total_poysha))
            else:
                status = "FAILED"

        fare = Fare(ride_id=d["ride_id"], passenger_id=d["passenger_id"], driver_id=d["driver_id"],
                    quote_id=quote.id, seats=d["seats"], pooled=d["pooled"], base_poysha=b.base_poysha,
                    distance_charge_poysha=b.distance_charge_poysha, pool_discount_poysha=b.pool_discount_poysha,
                    total_poysha=b.total_poysha, payment_method=d["payment_method"], payment_status=status)
        s.add(fare)
        await s.flush()
        emit(s, Outbox, PRODUCER, "fare.ride.settled", {
            "fare_id": fare.id, "ride_id": fare.ride_id, "passenger_id": fare.passenger_id,
            "driver_id": fare.driver_id, "base_poysha": fare.base_poysha,
            "distance_charge_poysha": fare.distance_charge_poysha,
            "pool_discount_poysha": fare.pool_discount_poysha, "total_poysha": fare.total_poysha,
            "payment_method": fare.payment_method, "payment_status": fare.payment_status})
```
`FAILED` wallet payment ⇒ Notification tells Jashim to collect cash.

Idempotency layers: `processed_events` (same event twice) + `fares.ride_id UNIQUE` (two different events for one ride) + `uq_wtx_ride_kind_user` (no double debit).

**Redis:** `GET/SET fare:dist:{a}:{b} EX 86400`.

### 6.7 Step-by-Step Implementation Guide
1. Models, migrations; `0002_seed_tariff` inserts tariff id 1 (3000, 1500, 20).
2. `pricing.py` + tests for the three rows of the table in 6.2 (these are the PRD's "pooled fares calculate correctly" tests).
3. `distance.py`; routers `fares.py`, `internal.py`.
4. `settlement.py` + tests: duplicate event → one fare; wallet short → `FAILED`, balance unchanged.
5. `wallet.py`, `driver.py` routers.
6. `seed.py`: wallets Nusrat 50000, Rafiq 50000, Shirin 20000, Jashim 0 (fixed UUIDs from Part 3).
7. Lifespan: bus connect → consumer → outbox relay → Redis client → Matching `ServiceClient`.

---

## Part 7 — Notification Service

### 7.1 Overview & Domain Scope
**Owns:** WebSocket connections, the recipient routing rules (who hears about what), and a persisted inbox so offline clients can catch up.
**Delegates:** all domain state. It only translates events into messages.

### 7.2 Directory Structure
```
services/notification/
├── Dockerfile
├── requirements.txt
├── alembic.ini
├── migrations/versions/0001_init.py
└── app/
    ├── main.py
    ├── config.py
    ├── deps.py
    ├── models.py
    ├── routing.py        # event → [(user_id, message)]
    ├── connections.py
    ├── consumers.py
    ├── live_location.py  # Redis PSUBSCRIBE loc:pool:*
    └── routers/{ws.py, inbox.py}
```

### 7.3 Data Layer
```python
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tesla_common.events import ProcessedEventMixin
from tesla_common.timeutil import utcnow


class Base(DeclarativeBase):
    pass


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(36))
    type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (Index("ix_notif_user_id", "user_id", "id"),)


class ProcessedEvent(ProcessedEventMixin, Base):
    pass
```

### 7.4 Recipient Rules

| Event | Recipients | Message `type` | Privacy rule |
|---|---|---|---|
| `trip.ride.requested` | each `candidate_driver_ids` | `ride.offer` | Shows zones, seats, estimate; no phone |
| `trip.ride.matched` | passenger, driver | `ride.matched` | Passenger gets driver name + "Bullet" |
| `trip.ride.status_changed` | passenger, driver | `ride.status` | — |
| `trip.ride.cancelled` | passenger, driver (if any) | `ride.cancelled` | — |
| `trip.pool.updated` | driver only | `pool.updated` | Passengers do not receive co-rider data |
| `fare.ride.settled` | passenger (own fare), driver | `fare.settled` | Each passenger gets only their own |

`app/routing.py`
```python
def recipients(env: dict) -> list[tuple[str, dict]]:
    t, d = env["event_type"], env["data"]
    msg = lambda kind, body: {"type": kind, "event_id": env["event_id"], "at": env["occurred_at"], "data": body}
    if t == "trip.ride.requested":
        body = {k: d[k] for k in ("ride_id", "pickup_zone", "dropoff_zone", "seats", "estimated_fare_poysha")}
        return [(drv, msg("ride.offer", body)) for drv in d["candidate_driver_ids"]]
    if t == "trip.ride.matched":
        return [(d["passenger_id"], msg("ride.matched", d)), (d["driver_id"], msg("ride.matched", d))]
    if t in ("trip.ride.status_changed", "trip.ride.cancelled"):
        kind = "ride.status" if t.endswith("status_changed") else "ride.cancelled"
        out = [(d["passenger_id"], msg(kind, d))]
        if d.get("driver_id"):
            out.append((d["driver_id"], msg(kind, d)))
        return out
    if t == "trip.pool.updated":
        return [(d["driver_id"], msg("pool.updated", d))]
    if t == "fare.ride.settled":
        return [(d["passenger_id"], msg("fare.settled", d)), (d["driver_id"], msg("fare.settled", d))]
    return []
```

`app/connections.py`
```python
from collections import defaultdict

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._conns: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, user_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._conns[user_id].add(ws)

    def disconnect(self, user_id: str, ws: WebSocket) -> None:
        self._conns[user_id].discard(ws)
        if not self._conns[user_id]:
            self._conns.pop(user_id, None)

    async def send(self, user_id: str, message: dict) -> None:
        for ws in list(self._conns.get(user_id, ())):
            try:
                await ws.send_json(message)
            except Exception:
                self.disconnect(user_id, ws)
```

### 7.5 API Endpoints

| Method | Route | Auth | Success | Core logic |
|---|---|---|---|---|
| WS | `/ws?token=<JWT>` | JWT verified here (RS256 public key) + denylist | 101 | Close `4401` on bad token. Keeps socket; client may send `"ping"` → `"pong"` |
| GET | `/notifications?after_id=&limit=50` | any (via gateway) | 200 `[{id, type, payload, created_at, read_at}]` | Catch-up after reconnect |
| POST | `/notifications/{id}/read` | any | 204 | Only own rows |

`app/routers/ws.py`
```python
import jwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from tesla_common.auth import verify_jwt

from ..deps import manager, redis, settings

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = Query(...)):
    try:
        claims = verify_jwt(token, settings.public_key)
    except jwt.InvalidTokenError:
        await ws.close(code=4401)
        return
    if await redis.exists(f"auth:revoked:{claims['jti']}"):
        await ws.close(code=4401)
        return
    user_id = claims["sub"]
    await manager.connect(user_id, ws)
    try:
        while True:
            if await ws.receive_text() == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(user_id, ws)
```

### 7.6 Messaging Integration

`app/consumers.py`
```python
import json
from functools import partial

from tesla_common.events import first_time

from .connections import ConnectionManager
from .models import Notification, ProcessedEvent
from .routing import recipients

BINDINGS = ["trip.ride.*", "trip.pool.updated", "fare.ride.settled"]


async def persist(rw, redis, env: dict) -> None:
    async with rw.begin() as s:
        if not await first_time(s, ProcessedEvent, env["event_id"]):
            return
        for user_id, message in recipients(env):
            if message["type"] != "pool.updated":
                s.add(Notification(user_id=user_id, type=message["type"], payload=json.dumps(message)))
    if env["event_type"] == "trip.pool.updated":
        d = env["data"]
        key = f"notif:pool:{d['pool_id']}:members"
        async with redis.pipeline(transaction=True) as p:
            p.delete(key)
            if d["member_passenger_ids"] and d["status"] in ("FORMING", "IN_PROGRESS"):
                p.sadd(key, *d["member_passenger_ids"])
                p.expire(key, 6 * 3600)
            await p.execute()


async def push(manager: ConnectionManager, env: dict) -> None:
    for user_id, message in recipients(env):
        await manager.send(user_id, message)


async def start(bus, rw, redis, manager: ConnectionManager) -> None:
    await bus.consume("notification.inbox", BINDINGS, partial(persist, rw, redis))
    await bus.consume_broadcast(BINDINGS, partial(push, manager))
```
- `notification.inbox`: durable, retry/DLQ, idempotent — the source of truth for "what was sent".
- Broadcast queue: one per instance, best-effort. Each instance pushes only to sockets it holds, so scaling Notification horizontally needs no sticky routing.

`app/live_location.py` (Redis Pub/Sub → sockets)
```python
import asyncio
import json
import logging

log = logging.getLogger("notification.live")


async def relay_locations(redis, manager, stop: asyncio.Event) -> None:
    pubsub = redis.pubsub()
    await pubsub.psubscribe("loc:pool:*")
    try:
        while not stop.is_set():
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is None or msg["type"] != "pmessage":
                continue
            data = json.loads(msg["data"])
            members = await redis.smembers(f"notif:pool:{data['pool_id']}:members")
            payload = {"type": "vehicle.location", "data": {k: data[k] for k in ("lat", "lng", "zone", "ts")}}
            for user_id in members:
                await manager.send(user_id, payload)
    except Exception:
        log.exception("location relay crashed")
    finally:
        await pubsub.punsubscribe("loc:pool:*")
        await pubsub.aclose()
```

### 7.7 Step-by-Step Implementation Guide
1. Models + migration.
2. `routing.py` with a unit test per event type, including "Rafiq never receives a message containing Nusrat's fare".
3. `connections.py`, `routers/ws.py` (test with `TestClient.websocket_connect`).
4. `consumers.py`, `live_location.py`.
5. `routers/inbox.py`.
6. Lifespan: bus → consumers → `asyncio.create_task(relay_locations(...))`.

---

## Part 8 — Orchestration, Testing & Run Order

### 8.1 Environment

`.env.example`
```dotenv
RABBITMQ_DEFAULT_USER=tesla
RABBITMQ_DEFAULT_PASS=change-me
RABBITMQ_URL=amqp://tesla:change-me@rabbitmq:5672/
REDIS_URL=redis://redis:6379/0
INTERNAL_TOKEN=change-me-to-a-long-random-string
JWT_PRIVATE_KEY_PATH=/run/keys/jwt_private.pem
JWT_PUBLIC_KEY_PATH=/run/keys/jwt_public.pem
JWT_TTL_SECONDS=3600
POOL_MAX_DETOUR_PCT=140
MATCH_RADIUS_M=3000
RIDE_REQUEST_TTL_SECONDS=180
LOG_LEVEL=INFO
```

Generic service `Dockerfile` (build context = repo root; change `SERVICE`/`PORT`):
```dockerfile
FROM python:3.12-slim
ARG SERVICE
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY libs/common /libs/common
COPY services/${SERVICE}/requirements.txt .
RUN pip install --no-cache-dir /libs/common -r requirements.txt
COPY services/${SERVICE}/ .
RUN mkdir -p /data
```

`docker-compose.yml`
```yaml
x-service: &service
  env_file: .env
  restart: unless-stopped
  volumes:
    - ./keys/jwt_public.pem:/run/keys/jwt_public.pem:ro
  depends_on: &infra
    rabbitmq: { condition: service_healthy }
    redis: { condition: service_healthy }

x-health: &health
  interval: 5s
  timeout: 3s
  retries: 10

services:
  rabbitmq:
    image: rabbitmq:3.13-management-alpine
    environment:
      RABBITMQ_DEFAULT_USER: ${RABBITMQ_DEFAULT_USER}
      RABBITMQ_DEFAULT_PASS: ${RABBITMQ_DEFAULT_PASS}
    ports: ["5672:5672", "15672:15672"]
    healthcheck: { test: ["CMD", "rabbitmq-diagnostics", "-q", "ping"], <<: *health }

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    healthcheck: { test: ["CMD", "redis-cli", "ping"], <<: *health }

  matching:
    <<: *service
    build: { context: ., dockerfile: services/matching/Dockerfile, args: { SERVICE: matching } }
    environment: { DB_PATH: /data/matching.db, PORT: "8002" }
    volumes: [matching-data:/data]
    command: sh -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8002"
    healthcheck: { test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8002/health')"], <<: *health }

  fare:
    <<: *service
    build: { context: ., dockerfile: services/fare/Dockerfile, args: { SERVICE: fare } }
    environment: { DB_PATH: /data/fare.db, MATCHING_URL: "http://matching:8002" }
    volumes: [fare-data:/data]
    command: sh -c "alembic upgrade head && python -m app.seed && uvicorn app.main:app --host 0.0.0.0 --port 8004"
    depends_on: { <<: *infra, matching: { condition: service_healthy } }
    healthcheck: { test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8004/health')"], <<: *health }

  trip:
    <<: *service
    build: { context: ., dockerfile: services/trip/Dockerfile, args: { SERVICE: trip } }
    environment: { DB_PATH: /data/trip.db, FARE_URL: "http://fare:8004", MATCHING_URL: "http://matching:8002" }
    volumes: [trip-data:/data]
    command: sh -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8003"
    depends_on: { <<: *infra, fare: { condition: service_healthy }, matching: { condition: service_healthy } }
    healthcheck: { test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8003/health')"], <<: *health }

  identity:
    <<: *service
    build: { context: ., dockerfile: services/identity/Dockerfile, args: { SERVICE: identity } }
    environment: { DB_PATH: /data/identity.db, TRIP_URL: "http://trip:8003" }
    volumes:
      - identity-data:/data
      - ./keys:/run/keys:ro
    command: sh -c "alembic upgrade head && python -m app.seed && uvicorn app.main:app --host 0.0.0.0 --port 8001"
    depends_on: { <<: *infra, trip: { condition: service_healthy } }
    healthcheck: { test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8001/health')"], <<: *health }

  notification:
    <<: *service
    build: { context: ., dockerfile: services/notification/Dockerfile, args: { SERVICE: notification } }
    environment: { DB_PATH: /data/notification.db }
    volumes:
      - notification-data:/data
      - ./keys/jwt_public.pem:/run/keys/jwt_public.pem:ro
    ports: ["8005:8005"]
    command: sh -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8005"
    healthcheck: { test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8005/health')"], <<: *health }

  gateway:
    <<: *service
    build: { context: ., dockerfile: services/gateway/Dockerfile, args: { SERVICE: gateway } }
    ports: ["8000:8000"]
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    depends_on:
      <<: *infra
      identity: { condition: service_healthy }
      trip: { condition: service_healthy }
      fare: { condition: service_healthy }
      matching: { condition: service_healthy }
      notification: { condition: service_healthy }

volumes:
  matching-data:
  fare-data:
  trip-data:
  identity-data:
  notification-data:
```
Only the gateway (8000), notification WebSocket (8005) and RabbitMQ UI (15672) are exposed. SQLite files live in named volumes; **each service runs a single container** (SQLite is single-host — see 8.7).

### 8.2 Boot Sequence

| Order | Component | Waits for | Why |
|---|---|---|---|
| 1 | RabbitMQ, Redis | — | Everyone connects on startup |
| 2 | Matching | infra | No sync dependencies; others call it |
| 3 | Fare | Matching | Quotes need distances |
| 4 | Trip | Fare, Matching | Ride creation calls both |
| 5 | Identity | Trip | Offline check calls Trip; seeds cast |
| 6 | Notification | infra | Only consumes |
| 7 | Gateway | all | Entry point last |

Queues are declared by their **consumers** at startup. An event published before its consumer ever started is dropped by the topic exchange — the boot order above ensures every consumer exists before Identity/Trip/Fare produce anything. In CI, assert with `rabbitmqctl list_queues`.

### 8.3 Local Run
```bash
git clone <repo> && cd dhaka-tesla-pool
./scripts/gen_keys.sh
cp .env.example .env
docker compose up --build -d
docker compose ps            # all healthy
open http://localhost:15672  # tesla / change-me
```
Without Docker for the apps (fast iteration): run only infra with `docker compose up -d rabbitmq redis`, then per service `cd services/<svc> && alembic upgrade head && uvicorn app.main:app --reload --port <port>` with a local `.env` pointing to `localhost`.

### 8.4 End-to-End Verification (the Banani story)

`scripts/e2e.sh` — requires `curl`, `jq`.
```bash
#!/usr/bin/env bash
set -euo pipefail
API=http://localhost:8000/api/v1
PW='Pool@1234'
login() { curl -sf -X POST $API/auth/login -H 'Content-Type: application/json' \
          -d "{\"phone\":\"$1\",\"password\":\"$PW\"}" | jq -r .access_token; }

J=$(login 01711000001); N=$(login 01711000002); R=$(login 01711000003); S=$(login 01711000004)

echo "1. Jashim goes online and pings from Banani"
curl -sf -X POST $API/drivers/me/online -H "Authorization: Bearer $J" | jq '{status, vehicle: .vehicle.nickname}'
curl -sf -X POST $API/driver/location -H "Authorization: Bearer $J" -H 'Content-Type: application/json' \
     -d '{"lat":23.7937,"lng":90.4066}'
sleep 1

echo "2. Nusrat gets a quote (expect solo 8250, pooled 7200)"
Q=$(curl -sf -X POST $API/fares/estimate -H "Authorization: Bearer $N" -H 'Content-Type: application/json' \
     -d '{"pickup_zone":"BANANI","dropoff_zone":"MOHAKHALI","seats":1}')
echo "$Q" | jq '{solo_total_poysha, pooled_total_poysha}'
QID=$(echo "$Q" | jq -r .quote_id)

echo "3. Nusrat requests (expect REQUESTED)"
KEY_N=$(uuidgen)
NR=$(curl -sf -X POST $API/rides -H "Authorization: Bearer $N" -H "Idempotency-Key: $KEY_N" \
      -H 'Content-Type: application/json' \
      -d "{\"pickup_zone\":\"BANANI\",\"dropoff_zone\":\"MOHAKHALI\",\"seats\":1,\"payment_method\":\"WALLET\",\"quote_id\":\"$QID\"}")
NRID=$(echo "$NR" | jq -r .id); echo "$NR" | jq '{id, status}'

echo "3b. Same Idempotency-Key replays the same ride (no duplicate)"
curl -si -X POST $API/rides -H "Authorization: Bearer $N" -H "Idempotency-Key: $KEY_N" \
     -H 'Content-Type: application/json' \
     -d "{\"pickup_zone\":\"BANANI\",\"dropoff_zone\":\"MOHAKHALI\",\"seats\":1,\"payment_method\":\"WALLET\",\"quote_id\":\"$QID\"}" \
     | grep -i idempotent-replay

echo "4. Jashim sees the offer and accepts (pool 1/3)"
sleep 1
curl -sf $API/driver/offers -H "Authorization: Bearer $J" | jq '.[] | {ride_id, passenger_name}'
curl -sf -X POST $API/driver/offers/$NRID/accept -H "Authorization: Bearer $J" | jq '{occupied_seats, max_capacity}'

echo "5. Rafiq auto-joins (expect MATCHED, pool 2/3, drop Gulshan 1 before Mohakhali)"
RR=$(curl -sf -X POST $API/rides -H "Authorization: Bearer $R" -H "Idempotency-Key: $(uuidgen)" \
      -H 'Content-Type: application/json' -d '{"pickup_zone":"BANANI","dropoff_zone":"GULSHAN_1","seats":1}')
RRID=$(echo "$RR" | jq -r .id); echo "$RR" | jq '{status, driver}'
curl -sf $API/driver/pool -H "Authorization: Bearer $J" | jq '{occupied_seats, waypoints: [.waypoints[] | "\(.seq) \(.kind) \(.zone) \(.passenger_name)"]}'

echo "6. Shirin wants 2 seats; only 1 left (expect REQUESTED, not in Bullet)"
curl -sf -X POST $API/rides -H "Authorization: Bearer $S" -H "Idempotency-Key: $(uuidgen)" \
     -H 'Content-Type: application/json' -d '{"pickup_zone":"BANANI","dropoff_zone":"MOHAKHALI","seats":2}' | jq '{status, pool_id}'

echo "7. Rafiq cannot read Nusrat's ride (expect 404)"
curl -s -o /dev/null -w '%{http_code}\n' $API/rides/$NRID -H "Authorization: Bearer $R"

echo "8. Run the pool"
for RID in $NRID $RRID; do
  curl -sf -X POST $API/driver/rides/$RID/arrive -H "Authorization: Bearer $J" > /dev/null
  curl -sf -X POST $API/driver/rides/$RID/start  -H "Authorization: Bearer $J" > /dev/null
done

echo "9. Nusrat tries to cancel after start (expect 409 INVALID_TRANSITION)"
curl -s -X POST $API/rides/$NRID/cancel -H "Authorization: Bearer $N" -H 'Content-Type: application/json' -d '{}' | jq .error.code

curl -sf -X POST $API/driver/rides/$RRID/complete -H "Authorization: Bearer $J" > /dev/null
curl -sf -X POST $API/driver/rides/$NRID/complete -H "Authorization: Bearer $J" | jq '{status}'
sleep 2

echo "10. Fares (expect Nusrat 7200 PAID, Rafiq 5400 PAID)"
curl -sf $API/fares/rides/$NRID -H "Authorization: Bearer $N" | jq '{total_poysha, pool_discount_poysha, payment_status}'
curl -sf $API/fares/rides/$RRID -H "Authorization: Bearer $R" | jq '{total_poysha, pool_discount_poysha, payment_status}'
echo "    Rafiq reading Nusrat's fare (expect 404)"
curl -s -o /dev/null -w '%{http_code}\n' $API/fares/rides/$NRID -H "Authorization: Bearer $R"

echo "11. History + audit trail"
curl -sf $API/rides/$NRID -H "Authorization: Bearer $N" | jq '{status, final_fare_poysha, history: [.history[] | "\(.from_status) → \(.to_status) by \(.actor_role)"]}'

echo "12. Jashim can now go offline"
curl -sf -X POST $API/drivers/me/offline -H "Authorization: Bearer $J" | jq .status
```

Live channel check in a second terminal before step 3:
```bash
websocat "ws://localhost:8005/ws?token=$N"
# expect: ride.matched, ride.status (DRIVER_ARRIVED, STARTED), vehicle.location, fare.settled
```

Post-run checks:
- RabbitMQ UI → Queues: every `*.dlq` has 0 messages; `*.retry` empty.
- `docker compose exec trip sqlite3 /data/trip.db "SELECT COUNT(*) FROM outbox WHERE published_at IS NULL;"` → 0.
- `docker compose exec trip sqlite3 /data/trip.db "SELECT id, occupied_seats, max_capacity, status FROM pools;"` → Bullet's pool `COMPLETED`, seats never above 3.

### 8.5 Test Plan (maps to PRD §12)

| PRD requirement | Test file | Assertion |
|---|---|---|
| Bullet's capacity can never be exceeded | `trip/tests/test_capacity.py` | Direct `UPDATE pools SET occupied_seats = 4` raises `IntegrityError` (CHECK); `try_join` with 2 seats on 2/3 → `STALE`, seats stay 2 |
| Two concurrent requests can't corrupt capacity | `trip/tests/test_concurrency.py` | Below |
| Invalid transitions rejected | `trip/tests/test_state_machine.py` | Parametrized over all 36 pairs × 3 actors |
| Pooled fares correct | `fare/tests/test_pricing.py` | 7200 / 5400 / 8250 |
| Users can't modify another's ride | `trip/tests/test_ownership.py` | Rafiq cancel Nusrat's ride → 404; Jashim acting on a ride not in his pool → 404 |
| Cancellation rules | `trip/tests/test_lifecycle.py` | Passenger cancel in `DRIVER_ARRIVED` → 409; cancel in `MATCHED` frees seat and removes waypoints |
| Idempotent settlement | `fare/tests/test_settlement.py` | Same event twice → one fare, one debit |

`trip/tests/test_concurrency.py`
```python
import asyncio

import pytest
from sqlalchemy import select

from app.models import Pool, PoolWaypoint, RideRequest
from app.pooling import JoinResult, try_join

JASHIM = "11111111-1111-4111-8111-111111111111"


@pytest.mark.asyncio
async def test_last_seat_goes_to_exactly_one_rider(db, seed_shift, make_ride):
    async with db.rw.begin() as s:
        pool = Pool(driver_id=JASHIM, vehicle_id="b1111111-1111-4111-8111-111111111111",
                    vehicle_nickname="Bullet", driver_name="Jashim", max_capacity=3,
                    occupied_seats=2, pickup_zone="BANANI")
        s.add(pool)
    nusrat_ride = await make_ride(passenger="Nusrat", seats=1)
    shirin_ride = await make_ride(passenger="Shirin", seats=1)

    def option(ride_id: str) -> list[dict]:
        return [{"pool_id": pool.id, "version": 1, "plan": [
            {"ride_id": "__new__", "kind": "PICKUP", "zone": "BANANI"},
            {"ride_id": "__new__", "kind": "DROPOFF", "zone": "MOHAKHALI"}]}]

    results = await asyncio.gather(
        try_join(db.rw, nusrat_ride, 1, option(nusrat_ride)),
        try_join(db.rw, shirin_ride, 1, option(shirin_ride)),
    )

    assert sorted(r.value for r in results) == ["joined", "stale"]
    async with db.ro() as s:
        fresh = await s.get(Pool, pool.id)
        matched = (await s.execute(select(RideRequest).where(RideRequest.pool_id == pool.id))).scalars().all()
    assert fresh.occupied_seats == 3
    assert len(matched) == 1
```
`conftest.py` creates a temp-file SQLite DB (not `:memory:` — each aiosqlite connection would get its own in-memory DB, hiding the race), runs `Base.metadata.create_all`, and seeds the `driver_shifts` row for Jashim. Run `pytest -n 0 --count 50` (pytest-repeat) on this test to shake out flakiness.

### 8.6 Observability Minimum
- JSON logs with `request_id` propagated Gateway → service → outgoing HTTP (`X-Request-Id`) and copied into event envelopes' logs via `event_id`.
- `/health` on every service (DB, RabbitMQ, Redis where used).
- Alert-worthy signals: DLQ depth > 0, outbox rows unpublished > 30 s, `RATE_LIMITED` spikes.

### 8.7 Known Limitations & Scale Path

| Limitation now | Why acceptable for MVP | Change at 1M riders / 100k drivers |
|---|---|---|
| SQLite ⇒ one container per service, one writer per DB | Correctness is simple and provable | Trip/Fare → PostgreSQL; keep the same `UPDATE ... WHERE` compare-and-set (row lock), PgBouncer, read replicas for history |
| Same-pickup-zone pooling | Hand-checkable rule | H3 cells for pickup proximity; insertion heuristic over real ETA |
| Driver accept needed only for first rider | Fewer round trips | Per-rider driver confirmation window if drivers ask for it |
| Stale `REQUESTED` rides are cancelled, not re-matched | Simple, visible behaviour | Sweeper re-runs `evaluate` before expiring |
| Notification WS exposed on its own port | Avoids WS proxying in the gateway | Single ingress (Traefik/Nginx) terminating TLS and upgrading WS |
| Redis single node | Loss only affects ephemeral state (positions, rate counters) | Redis Cluster; geo keys sharded by city region |
| RabbitMQ single node | Outbox guarantees no lost events if the broker restarts | Quorum queues, 3-node cluster |
