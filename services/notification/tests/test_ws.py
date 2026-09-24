"""7.5: WS /ws?token=<JWT>, against a REAL uvicorn server and a real WebSocket client (Starlette's TestClient
hides the difference that matters here: a socket closed before it's accepted becomes a plain HTTP 403)."""
import asyncio
import json
import logging
import types

import fakeredis
import jwt as pyjwt
import pytest
import uvicorn
import websockets
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, WebSocket
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.connections import ConnectionManager
from app.routers import ws as ws_router
from conftest import JASHIM, NUSRAT, RAFIQ


@pytest.fixture
async def server(keys, monkeypatch):
    """A real uvicorn on a free port serving the /ws router, plus /ws-plan: the plan's order (close, no accept)."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = ConnectionManager()
    monkeypatch.setattr(ws_router, "public_key", lambda: keys[1])
    monkeypatch.setattr(ws_router, "redis", redis)
    monkeypatch.setattr(ws_router, "manager", manager)
    monkeypatch.setattr(ws_router, "RECHECK_SECONDS", 0.2)

    app = FastAPI()
    app.include_router(ws_router.router)

    @app.websocket("/ws-plan")
    async def plan_order(ws: WebSocket):
        await ws.close(code=4401)  # what the plan's code does for a bad token

    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="info",
                                        timeout_graceful_shutdown=1))
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.01)
    port = srv.servers[0].sockets[0].getsockname()[1]
    yield types.SimpleNamespace(url=f"ws://127.0.0.1:{port}", redis=redis, manager=manager)
    srv.should_exit = True
    await task
    await redis.aclose()


def jti_of(token: str) -> str:
    return pyjwt.decode(token, options={"verify_signature": False})["jti"]


async def closed_with(conn) -> int:
    """Wait for the server to close the socket; return its close code."""
    with pytest.raises(ConnectionClosed) as e:
        while True:
            await asyncio.wait_for(conn.recv(), timeout=5)
    return e.value.rcvd.code


# ---- a good login -----------------------------------------------------------------------------------------------

async def test_ping_pong(server, make_token):
    async with websockets.connect(f"{server.url}/ws?token={make_token()}") as c:
        await c.send("ping")
        assert await c.recv() == "pong"


async def test_pushed_messages_arrive_and_only_to_their_person(server, make_token):
    async with websockets.connect(f"{server.url}/ws?token={make_token(NUSRAT)}") as phone, \
            websockets.connect(f"{server.url}/ws?token={make_token(NUSRAT)}") as tablet, \
            websockets.connect(f"{server.url}/ws?token={make_token(RAFIQ)}") as rafiq:
        for c in (phone, tablet, rafiq):  # make sure all three are registered
            await c.send("ping")
            assert await c.recv() == "pong"
        await server.manager.send(NUSRAT, {"type": "fare.settled", "data": {"total_poysha": 7200}})
        for c in (phone, tablet):
            assert json.loads(await c.recv()) == {"type": "fare.settled", "data": {"total_poysha": 7200}}
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(rafiq.recv(), timeout=0.3)


async def test_hanging_up_forgets_the_socket(server, make_token):
    async with websockets.connect(f"{server.url}/ws?token={make_token(JASHIM, 'DRIVER')}") as c:
        await c.send("ping")
        await c.recv()
        assert JASHIM in server.manager._conns
    for _ in range(50):
        if JASHIM not in server.manager._conns:
            break
        await asyncio.sleep(0.02)
    assert JASHIM not in server.manager._conns


async def test_binary_frames_are_ignored(server, make_token):
    """Finding 5: the plan's receive_text() would crash on bytes and drop the socket."""
    async with websockets.connect(f"{server.url}/ws?token={make_token()}") as c:
        await c.send(b"\x00\x01")
        await c.send("hello")  # any other text is ignored too
        await c.send("ping")
        assert await c.recv() == "pong"


# ---- a bad login: 4401, which the phone can actually see --------------------------------------------------------

@pytest.mark.parametrize("case", ["garbage", "wrong key", "expired", "wrong issuer", "missing", "no jti"])
async def test_bad_login_is_closed_with_4401(server, keys, make_token, case):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    token = {"garbage": "not-a-jwt", "wrong key": make_token(key=other), "expired": make_token(ttl=-10),
             "wrong issuer": make_token(iss="someone-else"), "missing": None, "no jti": None}[case]
    if case == "no jti":  # correctly signed, but without the id logout needs
        claims = pyjwt.decode(make_token(), options={"verify_signature": False})
        del claims["jti"]
        token = pyjwt.encode(claims, keys[0], algorithm="RS256")
    url = f"{server.url}/ws" + ("" if token is None else f"?token={token}")
    async with websockets.connect(url) as c:
        assert await closed_with(c) == 4401


async def test_the_plans_order_would_have_been_a_bare_403(server):
    """Why the endpoint accepts first: closing an unaccepted socket reaches the phone as HTTP 403, not 4401."""
    with pytest.raises(InvalidStatus) as e:
        async with websockets.connect(f"{server.url}/ws-plan"):
            pass
    assert e.value.response.status_code == 403


async def test_logged_out_token_is_refused_at_the_door(server, make_token, monkeypatch):
    monkeypatch.setattr(ws_router, "RECHECK_SECONDS", 30)  # so only the check at connect time can refuse it
    token = make_token()
    await server.redis.set(f"auth:revoked:{jti_of(token)}", 1)
    async with websockets.connect(f"{server.url}/ws?token={token}") as c:
        assert await closed_with(c) == 4401
    assert NUSRAT not in server.manager._conns  # never registered: no push could have reached it


# ---- finding 1: the socket doesn't outlive the login ------------------------------------------------------------

async def test_logging_out_closes_an_open_socket(server, make_token):
    token = make_token()
    async with websockets.connect(f"{server.url}/ws?token={token}") as c:
        await c.send("ping")
        assert await c.recv() == "pong"
        await server.redis.set(f"auth:revoked:{jti_of(token)}", 1)  # Nusrat logs out on another device
        assert await closed_with(c) == 4401  # within one re-check, without her sending anything


async def test_an_expiring_token_closes_the_socket(server, make_token):
    async with websockets.connect(f"{server.url}/ws?token={make_token(ttl=1.0)}") as c:
        await c.send("ping")
        assert await c.recv() == "pong"
        assert await closed_with(c) == 4401  # about a second later, on its own


# ---- finding 2: the token never reaches the log -----------------------------------------------------------------

async def test_token_in_the_url_is_logged_without_the_filter_and_hidden_with_it(server, make_token, caplog):
    uvicorn_log = logging.getLogger("uvicorn.error")
    token = make_token()
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        async with websockets.connect(f"{server.url}/ws?token={token}") as c:
            await c.send("ping")
            await c.recv()
    assert token in caplog.text  # the problem, as uvicorn really logs it

    caplog.clear()
    redact = ws_router.RedactTokenFilter()
    uvicorn_log.addFilter(redact)
    try:
        with caplog.at_level(logging.INFO, logger="uvicorn.error"):
            async with websockets.connect(f"{server.url}/ws?token={token}") as c:
                await c.send("ping")
                await c.recv()
    finally:
        uvicorn_log.removeFilter(redact)
    assert token not in caplog.text and "token=***" in caplog.text
