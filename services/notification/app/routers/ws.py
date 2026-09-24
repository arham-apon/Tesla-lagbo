import asyncio
import logging
import re
import time

import jwt
from fastapi import APIRouter, Query, WebSocket

from tesla_common.auth import verify_jwt

from ..deps import manager, public_key, redis

router = APIRouter()

UNAUTHORISED = 4401        # the plan's close code for a bad, expired or logged-out token
RECHECK_SECONDS = 30.0     # how often an idle socket re-checks its login (patched shorter in tests)


async def _revoked(claims: dict) -> bool:
    # The same "logged out" list the gateway checks, written by Identity on logout (Part 3).
    return bool(await redis.exists(f"auth:revoked:{claims['jti']}"))


async def _still_valid(claims: dict) -> bool:
    return time.time() < claims["exp"] and not await _revoked(claims)


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = Query(default="")):  # missing -> 4401 too, not a bare 403
    # Changes from the plan (7.5 in exp7.md):
    # 1. accept BEFORE closing with 4401: closing an unaccepted socket makes uvicorn answer the handshake with a
    #    plain HTTP 403, so the phone would never see 4401 ("log in again").
    # 2. the login is re-checked while the socket is open (expiry + logout), not only when it opens.
    # 3. binary frames are ignored instead of crashing the loop.
    try:
        claims = verify_jwt(token, public_key())
        ok = not await _revoked(claims)
    except jwt.InvalidTokenError:
        ok = False
    if not ok:
        await ws.accept()
        await ws.close(code=UNAUTHORISED)
        return
    user_id = claims["sub"]
    await manager.connect(user_id, ws)
    try:
        while True:
            wait = max(0.0, min(RECHECK_SECONDS, claims["exp"] - time.time()))
            try:
                message = await asyncio.wait_for(ws.receive(), timeout=wait)
            except asyncio.TimeoutError:
                message = None  # quiet socket: fall through to the login re-check
            if message is not None:
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("text") == "ping":
                    await ws.send_text("pong")
            if not await _still_valid(claims):
                await ws.close(code=UNAUTHORISED)
                return
    finally:
        manager.disconnect(user_id, ws)


class RedactTokenFilter(logging.Filter):
    """uvicorn logs a WebSocket as '"WebSocket /ws?token=eyJ..." [accepted]' (the path WITH its query), which would
    write every login token into the log. Installed on uvicorn's loggers at startup (7.7)."""

    _TOKEN = re.compile(r"(token=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._TOKEN.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = self._TOKEN.sub(r"\1***", record.msg)
        return True
