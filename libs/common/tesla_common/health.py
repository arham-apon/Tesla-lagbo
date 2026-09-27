from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse


def health_router(checks: dict[str, Callable[[], Awaitable[None]]]) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health():
        results, ok = {}, True
        for name, check in checks.items():
            try:
                await check()
                results[name] = "ok"
            except Exception as exc:
                results[name] = f"fail: {exc}"
                ok = False
        return JSONResponse({"status": "ok" if ok else "degraded", "checks": results}, status_code=200 if ok else 503)

    return router
