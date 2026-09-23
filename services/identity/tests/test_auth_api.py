"""3.4: register, login, logout, /users/me, /internal/users/{id}."""
import time

from sqlalchemy import select

from tesla_common.auth import verify_jwt

from app.models import Driver, User
from conftest import GATEWAY, PUBLIC_KEY, as_user

NUSRAT = {"full_name": "Nusrat", "phone": "01711000002", "password": "Pool@1234", "role": "PASSENGER"}
JASHIM = {"full_name": "Jashim", "phone": "01711000001", "password": "Pool@1234", "role": "DRIVER",
          "license_number": "DK-0001"}


async def register(api, body):
    return await api.post("/auth/register", json=body, headers=GATEWAY)


async def test_register_passenger(api, db):
    resp = await register(api, NUSRAT)
    assert resp.status_code == 201
    out = resp.json()
    assert out.keys() == {"id", "full_name", "phone", "role"}  # no password hash in the response
    assert out["role"] == "PASSENGER"
    async with db.ro() as s:
        user = await s.get(User, out["id"])
        assert user.password_hash.startswith("$argon2id$") and "Pool@1234" not in user.password_hash
        assert await s.get(Driver, out["id"]) is None


async def test_register_driver_creates_driver_row(api, db):
    resp = await register(api, JASHIM)
    assert resp.status_code == 201
    async with db.ro() as s:
        driver = await s.get(Driver, resp.json()["id"])
    assert (driver.license_number, driver.status, driver.vehicle) == ("DK-0001", "OFFLINE", None)


async def test_driver_needs_license(api):
    resp = await register(api, JASHIM | {"license_number": None})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "LICENSE_REQUIRED"


async def test_duplicate_phone_409(api):
    await register(api, NUSRAT)
    resp = await register(api, NUSRAT | {"full_name": "Someone Else"})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "PHONE_TAKEN"


async def test_duplicate_license_409_and_nothing_saved(api, db):
    await register(api, JASHIM)
    resp = await register(api, JASHIM | {"phone": "01711000009"})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "LICENSE_TAKEN"
    async with db.ro() as s:
        assert await s.scalar(select(User).where(User.phone == "01711000009")) is None


async def test_bad_input_422(api):
    resp = await register(api, NUSRAT | {"phone": "12345"})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_register_only_through_gateway(api):
    resp = await api.post("/auth/register", json=NUSRAT)  # no X-Internal-Token
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "UNAUTHORIZED_INTERNAL"


async def test_login_returns_token_the_gateway_accepts(api):
    user_id = (await register(api, JASHIM)).json()["id"]
    resp = await api.post("/auth/login", json={"phone": JASHIM["phone"], "password": "Pool@1234"}, headers=GATEWAY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer" and body["expires_in"] == 3600 and body["user"]["id"] == user_id
    claims = verify_jwt(body["access_token"], PUBLIC_KEY)
    assert (claims["sub"], claims["role"], claims["name"]) == (user_id, "DRIVER", "Jashim")


async def test_wrong_password_and_unknown_phone_look_identical(api):
    await register(api, NUSRAT)
    wrong = await api.post("/auth/login", json={"phone": NUSRAT["phone"], "password": "nope-nope"}, headers=GATEWAY)
    unknown = await api.post("/auth/login", json={"phone": "01799999999", "password": "Pool@1234"}, headers=GATEWAY)
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_logout_revokes_token_until_it_expires(api, redis):
    exp = int(time.time()) + 1800
    resp = await api.post("/auth/logout", headers=as_user("u1", **{"X-Token-Jti": "jti-1", "X-Token-Exp": str(exp)}))
    assert resp.status_code == 204
    assert await redis.get("auth:revoked:jti-1") == "1"
    assert 1790 <= await redis.ttl("auth:revoked:jti-1") <= 1800


async def test_logout_of_already_expired_token_writes_nothing(api, redis):
    past = int(time.time()) - 5
    resp = await api.post("/auth/logout", headers=as_user("u1", **{"X-Token-Jti": "jti-2", "X-Token-Exp": str(past)}))
    assert resp.status_code == 204 and not await redis.exists("auth:revoked:jti-2")


async def test_logout_needs_token_context_from_gateway(api):
    resp = await api.post("/auth/logout", headers=as_user("u1"))
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "TOKEN_CONTEXT_MISSING"


async def test_logout_needs_a_user(api):
    resp = await api.post("/auth/logout", headers=GATEWAY)
    assert resp.status_code == 401


async def test_users_me(api):
    user_id = (await register(api, NUSRAT)).json()["id"]
    resp = await api.get("/users/me", headers=as_user(user_id))
    assert resp.status_code == 200 and resp.json()["full_name"] == "Nusrat"
    assert (await api.get("/users/me", headers=as_user("ghost"))).status_code == 404


async def test_internal_user_lookup(api):
    user_id = (await register(api, NUSRAT)).json()["id"]
    assert (await api.get(f"/internal/users/{user_id}", headers=GATEWAY)).json()["phone"] == NUSRAT["phone"]
    assert (await api.get(f"/internal/users/{user_id}")).status_code == 401
    assert (await api.get("/internal/users/ghost", headers=GATEWAY)).status_code == 404
