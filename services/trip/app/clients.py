from datetime import datetime, timezone

import httpx
from pydantic import BaseModel

from tesla_common.errors import DomainError
from tesla_common.http import ServiceClient
from tesla_common.timeutil import utcnow


class Quote(BaseModel):
    quote_id: str
    passenger_id: str
    pickup_zone: str
    dropoff_zone: str
    seats: int
    distance_m: int
    solo_total_poysha: int
    pooled_total_poysha: int
    expires_at: datetime


def naive_utc(dt: datetime) -> datetime:
    # utcnow() is naive UTC. If Fare ever sends "...Z", comparing aware with naive would raise TypeError (a 500).
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _ok(resp: httpx.Response, service: str) -> httpx.Response:
    # ServiceClient only raises on 5xx. A 401 (wrong internal token) or 404 would otherwise be parsed as a quote
    # or an evaluation and crash as a 500. Callers handle the 4xx they expect before calling this.
    if resp.status_code not in (200, 201):
        raise DomainError("UPSTREAM_ERROR", f"{service} returned {resp.status_code}", 503)
    return resp


class FareClient:
    def __init__(self, client: ServiceClient):
        self.c = client

    async def quote_for(self, passenger_id: str, pickup: str, dropoff: str, seats: int,
                        quote_id: str | None, request_id: str | None) -> Quote:
        if quote_id:
            resp = await self.c.request("GET", f"/internal/quotes/{quote_id}", request_id=request_id, retries=1)
            if resp.status_code == 404:
                raise DomainError("QUOTE_NOT_FOUND", "Quote not found", 422)
            q = Quote.model_validate(_ok(resp, "fare").json())
            if (q.passenger_id, q.pickup_zone, q.dropoff_zone, q.seats) != (passenger_id, pickup, dropoff, seats):
                raise DomainError("QUOTE_MISMATCH", "Quote does not match this request", 422)
            if naive_utc(q.expires_at) < utcnow():
                raise DomainError("QUOTE_EXPIRED", "Quote expired; request a new estimate", 422)
            return q
        resp = await self.c.request("POST", "/internal/quotes", request_id=request_id,
                                    json={"passenger_id": passenger_id, "pickup_zone": pickup,
                                          "dropoff_zone": dropoff, "seats": seats})
        if resp.status_code == 422:
            raise DomainError("UNKNOWN_ZONE", resp.json()["error"]["message"], 422)
        return Quote.model_validate(_ok(resp, "fare").json())


class MatchingClient:
    def __init__(self, client: ServiceClient):
        self.c = client

    async def evaluate(self, payload: dict, request_id: str | None) -> dict:
        resp = await self.c.request("POST", "/internal/match/evaluate", json=payload, request_id=request_id)
        if resp.status_code == 422:
            raise DomainError("UNKNOWN_ZONE", "Unknown zone", 422)
        return _ok(resp, "matching").json()
