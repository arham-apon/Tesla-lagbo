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
