import os
import time

import fakeredis
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def _keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


PRIVATE_KEY, PUBLIC_KEY = _keypair()
OTHER_PRIVATE_KEY, _ = _keypair()
INTERNAL_TOKEN = "test-internal-token"
CLIENT_IP = "203.0.113.9"

# app.config builds Settings() at import time, so the environment must be set first.
os.environ.update(
    REDIS_URL="redis://unused", INTERNAL_TOKEN=INTERNAL_TOKEN, JWT_PUBLIC_KEY_PATH="unused",
    IDENTITY_URL="http://identity:8001", MATCHING_URL="http://matching:8002", TRIP_URL="http://trip:8003",
    FARE_URL="http://fare:8004", NOTIFICATION_URL="http://notification:8005",
)

from tesla_common.auth import ISSUER  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.routes_table import build_routes  # noqa: E402


class FakeUpstreams:
    """Stands in for Identity/Matching/Trip/Fare/Notification and records every forwarded request."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.down: set[str] = set()      # hostnames that refuse connections
        self.slow: set[str] = set()      # hostnames that time out
        self.handlers: dict[tuple[str, str], callable] = {}  # (method, path) -> request -> Response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host in self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.host in self.slow:
            raise httpx.ReadTimeout("timed out", request=request)
        handler = self.handlers.get((request.method, request.url.path))
        if handler:
            return handler(request)
        return httpx.Response(200, json={"service": request.url.host, "path": request.url.path})

    @property
    def last(self) -> httpx.Request:
        return self.calls[-1]


@pytest.fixture
def upstreams() -> FakeUpstreams:
    return FakeUpstreams()


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
async def client(redis, upstreams):
    """The gateway app, driven in-process, with fake Redis and fake upstream services."""
    app.state.redis = redis
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(upstreams))
    app.state.routes = build_routes(settings)
    app.state.public_key = PUBLIC_KEY
    transport = httpx.ASGITransport(app=app, client=(CLIENT_IP, 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
        yield c
    await app.state.http.aclose()


@pytest.fixture
def make_token():
    def make(sub: str = "nusrat", role: str = "PASSENGER", *, exp_in: int = 600, issuer: str = ISSUER,
             jti: str | None = None, key: str = PRIVATE_KEY) -> str:
        now = int(time.time())
        claims = {"sub": sub, "role": role, "name": sub.title(), "jti": jti or f"jti-{sub}",
                  "iat": now, "exp": now + exp_in, "iss": issuer}
        return jwt.encode(claims, key, algorithm="RS256")
    return make


@pytest.fixture
def auth_header(make_token):
    def header(sub: str = "nusrat", role: str = "PASSENGER", **kw) -> dict:
        return {"Authorization": f"Bearer {make_token(sub, role, **kw)}"}
    return header
