import json
import logging
import sys
import time
from contextvars import ContextVar
from datetime import datetime, timezone

from .timeutil import new_id

# Plan 8.6: every log line written while handling a request carries that request's id, and every line written while
# handling an event carries the event's id, without each log call having to pass them.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
event_id_var: ContextVar[str | None] = ContextVar("event_id", default=None)

_FIELDS = ("request_id", "event_id", "ride_id", "pool_id", "method", "path", "status", "duration_ms")


def current_request_id() -> str | None:
    return request_id_var.get()


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _FIELDS:
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


class ContextFilter(logging.Filter):
    """Copies the current request/event id onto each record (unless the call passed its own)."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, var in (("request_id", request_id_var), ("event_id", event_id_var)):
            value = var.get()
            if value and not hasattr(record, key):
                setattr(record, key, value)
        return True


def configure_logging(service: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    handler.addFilter(ContextFilter())  # on the handler, so it also sees records propagated from other loggers
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # uvicorn's own lines (startup, errors) through the same JSON handler. Its plain-text access line is replaced
    # by RequestContextMiddleware's, which carries the request id, status and duration.
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers[:] = []
    access.propagate = False


class RequestContextMiddleware:
    """Plain ASGI (not BaseHTTPMiddleware, so the context variable reaches the endpoint and streaming is untouched).

    Takes X-Request-Id from the caller (the gateway sets it; services receive it) or makes one, keeps it for the whole
    request, returns it in the response, and writes one JSON access line: method, path (no query: it can hold
    secrets), status, duration. Health checks (Docker asks every 5 s) are logged at DEBUG.
    """

    def __init__(self, app):
        self.app = app
        self.log = logging.getLogger("access")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        rid = incoming or new_id()
        token = request_id_var.set(rid)
        status, start = 500, time.perf_counter()

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers") or [])
                if not any(k.lower() == b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", rid.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            ms = round((time.perf_counter() - start) * 1000, 1)
            path = scope.get("path", "")
            self.log.log(logging.DEBUG if path == "/health" else logging.INFO,
                         "%s %s %s %sms", scope.get("method"), path, status, ms,
                         extra={"method": scope.get("method"), "path": path, "status": status, "duration_ms": ms})
            request_id_var.reset(token)


def install_request_context(app) -> None:
    app.add_middleware(RequestContextMiddleware)
