"""Step 1.4.5 — smoke-test auth.py, errors.py, health.py and http.py with a throwaway FastAPI app.

Run:  python libs/common/checks/check_api.py   (no Docker needed)
"""
import asyncio
import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from tesla_common.auth import ISSUER, InternalAuth, Principal, verify_jwt
from tesla_common.errors import DomainError, install_error_handlers
from tesla_common.health import health_router
from tesla_common.http import ServiceClient

TOKEN = "internal-secret"
auth = InternalAuth(TOKEN)


async def ok_check() -> None:
    pass


async def broken_check() -> None:
    raise RuntimeError("redis down")


class Body(BaseModel):
    seats: int


app = FastAPI()
install_error_handlers(app)
app.include_router(health_router({"db": ok_check}))


@app.get("/me")
async def me(p: Principal = Depends(auth.principal())):
    return p


@app.post("/driver-only")
async def driver_only(body: Body, p: Principal = Depends(auth.role("DRIVER"))):
    return {"ok": True, "driver": p.name}


@app.get("/full")
async def full():
    raise DomainError("POOL_FULL", "No seats left in Bullet", 409)


def main() -> None:
    c = TestClient(app)
    trusted = {"X-Internal-Token": TOKEN, "X-User-Id": "u-1", "X-User-Role": "DRIVER", "X-User-Name": "Jashim"}

    r = c.get("/me", headers={**trusted, "X-Internal-Token": "wrong"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED_INTERNAL"
    r = c.get("/me", headers={"X-Internal-Token": TOKEN})
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED"
    r = c.get("/me", headers=trusted)
    assert r.status_code == 200 and r.json()["name"] == "Jashim"
    r = c.post("/driver-only", json={"seats": 1}, headers={**trusted, "X-User-Role": "PASSENGER"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
    r = c.post("/driver-only", json={"seats": "two"}, headers=trusted)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = c.get("/full", headers={"X-Request-Id": "req-42"})
    assert r.status_code == 409 and r.json()["error"] == {
        "code": "POOL_FULL", "message": "No seats left in Bullet", "request_id": "req-42", "details": None}
    print("OK: auth deps (401/401/200/403), validation 422, DomainError 409 with request_id")

    assert c.get("/health").json() == {"status": "ok", "checks": {"db": "ok"}}
    bad = FastAPI()
    bad.include_router(health_router({"db": ok_check, "redis": broken_check}))
    r = TestClient(bad).get("/health")
    assert r.status_code == 503 and r.json()["checks"]["redis"] == "fail: redis down"
    print("OK: /health returns 200 when healthy, 503 'degraded' when a check fails")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    now = int(time.time())
    claims = {"sub": "u-nusrat", "role": "PASSENGER", "jti": "j-1", "iat": now, "exp": now + 60, "iss": ISSUER}
    assert verify_jwt(jwt.encode(claims, priv, algorithm="RS256"), pub)["sub"] == "u-nusrat"
    for broken, err in [({**claims, "exp": now - 1}, jwt.ExpiredSignatureError),
                        ({**claims, "iss": "someone-else"}, jwt.InvalidIssuerError),
                        ({k: v for k, v in claims.items() if k != "jti"}, jwt.MissingRequiredClaimError)]:
        try:
            verify_jwt(jwt.encode(broken, priv, algorithm="RS256"), pub)
            raise AssertionError(f"expected {err.__name__}")
        except err:
            pass
    print("OK: verify_jwt accepts a valid RS256 token; rejects expired / wrong issuer / missing jti")

    async def http_checks() -> None:
        client = ServiceClient("http://127.0.0.1:1", TOKEN, "fare")  # nothing listens on port 1
        try:
            await client.request("GET", "/internal/quotes/x", retries=2)
            raise AssertionError("expected UPSTREAM_UNAVAILABLE")
        except DomainError as e:
            assert e.code == "UPSTREAM_UNAVAILABLE" and e.status == 503
        finally:
            await client.aclose()
    asyncio.run(http_checks())
    print("OK: ServiceClient turns a dead upstream into DomainError UPSTREAM_UNAVAILABLE (503)")


if __name__ == "__main__":
    main()
