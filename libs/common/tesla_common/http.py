import httpx

from .errors import DomainError
from .logging import current_request_id


class ServiceClient:
    def __init__(self, base_url: str, internal_token: str, service_name: str):
        self.name = service_name
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(3.0, connect=1.0),
            headers={"X-Internal-Token": internal_token},
        )

    async def request(self, method: str, path: str, *, request_id: str | None = None, retries: int = 0, **kw) -> httpx.Response:
        request_id = request_id or current_request_id()  # 8.6: the id travels even if a caller forgot to pass it
        headers = kw.pop("headers", {}) | ({"X-Request-Id": request_id} if request_id else {})
        attempts = retries + 1 if method == "GET" else 1
        last_exc: Exception | None = None
        for _ in range(attempts):
            try:
                resp = await self._client.request(method, path, headers=headers, **kw)
                if resp.status_code >= 500:
                    raise DomainError("UPSTREAM_ERROR", f"{self.name} returned {resp.status_code}", 503)
                return resp
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
        raise DomainError("UPSTREAM_UNAVAILABLE", f"{self.name} unavailable: {last_exc}", 503)

    async def aclose(self) -> None:
        await self._client.aclose()
