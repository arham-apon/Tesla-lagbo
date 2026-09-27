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
