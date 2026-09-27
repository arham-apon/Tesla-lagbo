"""Step 2.6.3: JWT verification (valid, expired, wrong issuer, revoked)."""
import pytest
from starlette.requests import Request

from tesla_common.errors import DomainError

from app.security import authenticate
from conftest import OTHER_PRIVATE_KEY, PUBLIC_KEY


def request_with(authorization: str | None) -> Request:
    headers = [(b"authorization", authorization.encode())] if authorization is not None else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


async def expect_error(redis, authorization, code):
    with pytest.raises(DomainError) as exc:
        await authenticate(request_with(authorization), redis, PUBLIC_KEY)
    assert (exc.value.code, exc.value.status) == (code, 401)


async def test_valid_token(redis, make_token):
    principal, claims = await authenticate(request_with(f"Bearer {make_token('jashim', 'DRIVER')}"), redis, PUBLIC_KEY)
    assert (principal.user_id, principal.role, principal.name) == ("jashim", "DRIVER", "Jashim")
    assert claims["jti"] == "jti-jashim" and isinstance(claims["exp"], int)


async def test_scheme_is_case_insensitive(redis, make_token):
    principal, _ = await authenticate(request_with(f"bearer {make_token()}"), redis, PUBLIC_KEY)
    assert principal.user_id == "nusrat"


async def test_expired(redis, make_token):
    await expect_error(redis, f"Bearer {make_token(exp_in=-10)}", "TOKEN_EXPIRED")


async def test_wrong_issuer(redis, make_token):
    await expect_error(redis, f"Bearer {make_token(issuer='someone-else')}", "INVALID_TOKEN")


async def test_signed_with_another_key(redis, make_token):
    await expect_error(redis, f"Bearer {make_token(key=OTHER_PRIVATE_KEY)}", "INVALID_TOKEN")


async def test_garbage_token(redis):
    await expect_error(redis, "Bearer not-a-jwt", "INVALID_TOKEN")


async def test_revoked(redis, make_token):
    await redis.set("auth:revoked:jti-logged-out", 1)
    await expect_error(redis, f"Bearer {make_token(jti='jti-logged-out')}", "TOKEN_REVOKED")


@pytest.mark.parametrize("authorization", [None, "", "Basic abc", "Token xyz"])
async def test_missing_or_non_bearer(redis, authorization):
    await expect_error(redis, authorization, "UNAUTHENTICATED")
