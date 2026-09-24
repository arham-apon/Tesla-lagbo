"""8.6: request and event ids reach every log line, the response, error bodies and outgoing calls."""
import io
import json
import logging

import httpx
import pytest
from fastapi import FastAPI, WebSocket

from tesla_common.errors import DomainError, install_error_handlers
from tesla_common.http import ServiceClient
from tesla_common.logging import (ContextFilter, JsonFormatter, configure_logging, current_request_id, event_id_var,
                                  install_request_context)


@pytest.fixture
def logs():
    """configure_logging, but writing into a buffer; returns a function giving the JSON lines so far."""
    configure_logging("test-service", "DEBUG")
    buf = io.StringIO()
    handler = logging.getLogger().handlers[0]
    handler.setStream(buf)
    yield lambda: [json.loads(line) for line in buf.getvalue().splitlines()]
    logging.getLogger().handlers[:] = []


def app_with(**routes):
    app = FastAPI()
    install_error_handlers(app)
    install_request_context(app)
    log = logging.getLogger("trip.pooling")

    @app.get("/rides/{ride_id}")
    async def ride(ride_id: str):
        log.info("looking up ride")
        return {"request_id": current_request_id()}

    @app.get("/boom")
    async def boom():
        raise DomainError("RIDE_NOT_FOUND", "Ride not found", 404)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        await socket.send_text(str(current_request_id()))
        await socket.close()

    return app


async def call(app, path, headers=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://svc") as c:
        return await c.get(path, headers=headers or {})


# ---- the request id -----------------------------------------------------------------------------------------------

async def test_the_gateways_id_is_kept_and_returned(logs):
    resp = await call(app_with(), "/rides/r1", {"X-Request-Id": "req-42"})
    assert resp.headers["x-request-id"] == "req-42" and resp.json() == {"request_id": "req-42"}
    lines = logs()
    inside = next(l for l in lines if l["msg"] == "looking up ride")
    assert inside["request_id"] == "req-42"  # a log call inside the endpoint, without passing it


async def test_a_new_id_when_none_came_in(logs):
    resp = await call(app_with(), "/rides/r1")
    rid = resp.headers["x-request-id"]
    assert len(rid) == 36 and resp.json() == {"request_id": rid}  # the same id inside and outside


async def test_one_json_access_line_per_request(logs):
    await call(app_with(), "/rides/r1", {"X-Request-Id": "req-7"})
    (access,) = [l for l in logs() if l["logger"] == "access"]
    assert (access["method"], access["path"], access["status"], access["request_id"]) == \
           ("GET", "/rides/r1", 200, "req-7")
    assert access["level"] == "INFO" and access["duration_ms"] >= 0 and access["service"] == "test-service"


async def test_the_query_string_is_not_in_the_access_line(logs):
    await call(app_with(), "/rides/r1?secret=abc")
    (access,) = [l for l in logs() if l["logger"] == "access"]
    assert "secret" not in json.dumps(access) and access["path"] == "/rides/r1"


async def test_errors_carry_the_id_in_body_header_and_log(logs):
    resp = await call(app_with(), "/boom")
    rid = resp.headers["x-request-id"]
    assert resp.status_code == 404 and resp.json()["error"]["request_id"] == rid  # even when the id was made here
    (access,) = [l for l in logs() if l["logger"] == "access"]
    assert (access["status"], access["request_id"]) == (404, rid)


async def test_health_checks_are_quiet(logs):
    await call(app_with(), "/health")
    (access,) = [l for l in logs() if l["logger"] == "access"]
    assert access["level"] == "DEBUG"  # Docker asks every 5 s; INFO logs stay readable


def test_websockets_pass_through_untouched():
    from fastapi.testclient import TestClient
    with TestClient(app_with()).websocket_connect("/ws") as ws:
        assert ws.receive_text() == "None"  # not an HTTP request: no request id, and the socket works


async def test_the_id_does_not_leak_into_the_next_request(logs):
    app = app_with()
    first = await call(app, "/rides/r1", {"X-Request-Id": "req-1"})
    second = await call(app, "/rides/r1")
    assert first.json()["request_id"] == "req-1" and second.json()["request_id"] != "req-1"
    assert current_request_id() is None


# ---- it travels on to the next service ---------------------------------------------------------------------------

async def test_outgoing_calls_carry_the_current_id():
    seen = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["id"] = request.headers.get("x-request-id")
        return httpx.Response(200, json={})

    app = FastAPI()
    install_request_context(app)
    client = ServiceClient("http://fare", "token", "fare")
    await client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="http://fare")

    @app.get("/rides")
    async def rides():
        await client.request("GET", "/internal/quotes/q1")  # the caller didn't pass request_id
        return {}

    await call(app, "/rides", {"X-Request-Id": "req-99"})
    assert seen["id"] == "req-99"
    await client.aclose()


# ---- the event id ------------------------------------------------------------------------------------------------

def test_event_handlers_log_with_the_event_id():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter("fare"))
    handler.addFilter(ContextFilter())
    log = logging.getLogger("fare.settlement")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    token = event_id_var.set("evt-123")  # what Bus.consume does around each handler
    try:
        log.info("settling ride")
    finally:
        event_id_var.reset(token)
        log.removeHandler(handler)
    assert json.loads(buf.getvalue())["event_id"] == "evt-123"


# ---- uvicorn's own lines -----------------------------------------------------------------------------------------

def test_uvicorn_lines_become_json_and_its_plain_access_line_goes_quiet(logs):
    logging.getLogger("uvicorn.error").info("Application startup complete.")
    logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET /rides HTTP/1.1" 200')
    lines = logs()
    assert [l["msg"] for l in lines] == ["Application startup complete."]  # JSON, and no duplicate access line
