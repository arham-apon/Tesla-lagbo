"""3.3: tokens Identity issues are exactly what the gateway's verify_jwt accepts."""
import time

import jwt
import pytest

from tesla_common.auth import verify_jwt

from app.tokens import issue_token
from conftest import PRIVATE_KEY, PUBLIC_KEY

JASHIM = "11111111-1111-4111-8111-111111111111"


def test_token_accepted_by_gateway_check():
    token, jti = issue_token(PRIVATE_KEY, JASHIM, "DRIVER", "Jashim", 3600)
    claims = verify_jwt(token, PUBLIC_KEY)
    assert (claims["sub"], claims["role"], claims["name"], claims["jti"]) == (JASHIM, "DRIVER", "Jashim", jti)
    assert claims["iss"] == "tesla-identity"


def test_lifetime_is_ttl_and_not_shifted_by_timezone():
    token, _ = issue_token(PRIVATE_KEY, JASHIM, "DRIVER", "Jashim", 3600)
    claims = verify_jwt(token, PUBLIC_KEY)
    assert claims["exp"] - claims["iat"] == 3600
    assert abs(claims["iat"] - time.time()) < 5  # utcnow() is naive UTC; PyJWT must treat it as UTC


def test_every_token_gets_its_own_jti():
    jtis = {issue_token(PRIVATE_KEY, JASHIM, "DRIVER", "Jashim", 60)[1] for _ in range(20)}
    assert len(jtis) == 20  # logout revokes one token, not all of a user's devices


def test_expired_token_rejected():
    token, _ = issue_token(PRIVATE_KEY, JASHIM, "DRIVER", "Jashim", -1)
    with pytest.raises(jwt.ExpiredSignatureError):
        verify_jwt(token, PUBLIC_KEY)
