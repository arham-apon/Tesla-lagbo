from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from tesla_common.errors import DomainError, install_error_handlers
from tesla_common.health import health_router
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


@app.exception_handler(RedisError)
async def _redis_down(request: Request, exc: RedisError):
    # Fail closed: without Redis we cannot check revocations, rate limits or idempotency keys.
    return JSONResponse({"error": {"code": "DEPENDENCY_UNAVAILABLE", "message": "Redis unavailable",
                                   "request_id": request.headers.get("x-request-id"), "details": None}},
                        status_code=503)


async def _redis_check() -> None:
    await app.state.redis.ping()


def _upstream_check(base_url: str):
    async def check() -> None:
        (await app.state.http.get(f"{base_url}/health", timeout=2.0)).raise_for_status()
    return check


app.include_router(health_router({
    "redis": _redis_check,
    "identity": _upstream_check(settings.IDENTITY_URL),
    "matching": _upstream_check(settings.MATCHING_URL),
    "trip": _upstream_check(settings.TRIP_URL),
    "fare": _upstream_check(settings.FARE_URL),
    "notification": _upstream_check(settings.NOTIFICATION_URL),
}))


@app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(request: Request, path: str):
    st = request.app.state
    # httpx collapses dot segments, so /zones/../internal/x would reach the upstream's /internal/x.
    if any(seg in (".", "..") for seg in path.split("/")):
        raise DomainError("NOT_FOUND", "No such route", 404)
    full_path = f"/api/v1/{path}"
    route = match(st.routes, full_path)
    if route is None:
        raise DomainError("NOT_FOUND", "No such route", 404)
    request_id = request.headers.get("x-request-id") or new_id()
    principal, claims = (None, None) if route.public else await authenticate(request, st.redis, st.public_key)
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
                             upstream_headers(request, principal, claims, settings.INTERNAL_TOKEN, request_id), body)
    except DomainError:
        if rkey:
            await st.redis.delete(rkey)
        raise
    await idempotency.finish(st.redis, rkey, fp, resp.status_code, resp.content)
    out = to_response(resp)
    out.headers["X-Request-Id"] = request_id
    return out
