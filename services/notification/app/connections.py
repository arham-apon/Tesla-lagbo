from collections import defaultdict

from fastapi import WebSocket


class ConnectionManager:
    """The open WebSockets on THIS copy of the service, per user (a phone and a tablet are two sockets).
    With several copies running, each pushes only to the sockets it holds (7.6's broadcast queue)."""

    def __init__(self) -> None:
        self._conns: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, user_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._conns[user_id].add(ws)

    def disconnect(self, user_id: str, ws: WebSocket) -> None:
        self._conns[user_id].discard(ws)
        if not self._conns[user_id]:
            self._conns.pop(user_id, None)

    async def send(self, user_id: str, message: dict) -> None:
        for ws in list(self._conns.get(user_id, ())):
            try:
                await ws.send_json(message)
            except Exception:
                # A phone that went away without saying goodbye: forget the socket, keep serving the others.
                self.disconnect(user_id, ws)
