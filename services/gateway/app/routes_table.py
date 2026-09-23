from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    prefix: str
    upstream: str
    public: bool = False
    rate_per_min: int = 120


def build_routes(s) -> list[Route]:
    routes = [
        Route("/api/v1/auth/register", s.IDENTITY_URL, public=True, rate_per_min=10),
        Route("/api/v1/auth/login", s.IDENTITY_URL, public=True, rate_per_min=10),
        Route("/api/v1/auth", s.IDENTITY_URL),
        Route("/api/v1/users", s.IDENTITY_URL),
        Route("/api/v1/drivers/me", s.IDENTITY_URL),
        Route("/api/v1/zones", s.MATCHING_URL, public=True),
        Route("/api/v1/driver/location", s.MATCHING_URL, rate_per_min=60),
        Route("/api/v1/fares", s.FARE_URL),
        Route("/api/v1/wallet", s.FARE_URL),
        Route("/api/v1/driver/earnings", s.FARE_URL),
        Route("/api/v1/rides", s.TRIP_URL),
        Route("/api/v1/driver", s.TRIP_URL),
        Route("/api/v1/notifications", s.NOTIFICATION_URL),
    ]
    return sorted(routes, key=lambda r: len(r.prefix), reverse=True)


def match(routes: list[Route], path: str) -> Route | None:
    for r in routes:
        if path == r.prefix or path.startswith(r.prefix + "/"):
            return r
    return None
