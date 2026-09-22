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
