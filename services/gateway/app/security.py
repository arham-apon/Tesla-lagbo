import jwt
from fastapi import Request
from redis.asyncio import Redis

from tesla_common.auth import Principal, verify_jwt
from tesla_common.errors import DomainError


async def authenticate(request: Request, redis: Redis, public_key: str) -> tuple[Principal, dict]:
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
    return Principal(user_id=claims["sub"], role=claims["role"], name=claims.get("name", "")), claims
