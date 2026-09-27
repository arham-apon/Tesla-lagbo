import httpx
from fastapi import Request, Response

from tesla_common.auth import Principal
from tesla_common.errors import DomainError

HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer",
              "proxy-authorization", "proxy-authenticate", "host", "content-length", "authorization",
              "x-internal-token"}
# Only the gateway may set these; any client-sent copies are dropped.
TRUSTED_PREFIXES = ("x-user-", "x-token-")
# Re-set below; dropping the client copy avoids sending the header twice.
GATEWAY_SET = {"x-request-id"}


def upstream_headers(request: Request, principal: Principal | None, claims: dict | None,
                     internal_token: str, request_id: str) -> dict:
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in HOP_BY_HOP | GATEWAY_SET and not k.lower().startswith(TRUSTED_PREFIXES)}
    headers["X-Internal-Token"] = internal_token
    headers["X-Request-Id"] = request_id
    if principal:
        headers["X-User-Id"] = principal.user_id
        headers["X-User-Role"] = principal.role
        headers["X-User-Name"] = principal.name
        # Part 3 logout note: Identity revokes the token for its remaining lifetime.
        headers["X-Token-Jti"] = claims["jti"]
        headers["X-Token-Exp"] = str(claims["exp"])
    return headers


async def forward(client: httpx.AsyncClient, request: Request, url: str, headers: dict, body: bytes) -> httpx.Response:
    try:
        # Raw query string so repeated params (?a=1&a=2) survive.
        return await client.request(request.method, url, params=request.url.query, content=body, headers=headers)
    except httpx.TimeoutException:
        raise DomainError("UPSTREAM_TIMEOUT", "Upstream timed out", 504)
    except httpx.TransportError:
        raise DomainError("UPSTREAM_UNAVAILABLE", "Upstream unavailable", 503)


def to_response(resp: httpx.Response) -> Response:
    headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP | {"content-encoding"}}
    return Response(content=resp.content, status_code=resp.status_code, headers=headers)
