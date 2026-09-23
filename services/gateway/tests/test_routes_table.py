"""Step 2.6.2: longest-prefix routing."""
from types import SimpleNamespace

import pytest

from app.routes_table import build_routes, match

URLS = SimpleNamespace(IDENTITY_URL="identity", MATCHING_URL="matching", TRIP_URL="trip",
                       FARE_URL="fare", NOTIFICATION_URL="notification")
ROUTES = build_routes(URLS)


@pytest.mark.parametrize("path, upstream", [
    ("/api/v1/driver/location", "matching"),          # the two cases the plan names
    ("/api/v1/driver/offers", "trip"),
    ("/api/v1/driver/offers/r1/accept", "trip"),
    ("/api/v1/driver/earnings", "fare"),
    ("/api/v1/drivers/me", "identity"),
    ("/api/v1/drivers/me/online", "identity"),
    ("/api/v1/auth/logout", "identity"),
    ("/api/v1/users/me", "identity"),
    ("/api/v1/zones", "matching"),
    ("/api/v1/rides/r1/cancel", "trip"),
    ("/api/v1/fares/r1", "fare"),
    ("/api/v1/wallet", "fare"),
    ("/api/v1/notifications", "notification"),
])
def test_resolves_to_owning_service(path, upstream):
    assert match(ROUTES, path).upstream == upstream


@pytest.mark.parametrize("path", ["/api/v1/ridesXYZ", "/api/v1/driverX", "/api/v1/unknown", "/api/v1", "/rides"])
def test_no_match(path):
    assert match(ROUTES, path) is None


def test_public_and_limits():
    by_prefix = {r.prefix: r for r in ROUTES}
    assert {p for p, r in by_prefix.items() if r.public} == {"/api/v1/auth/register", "/api/v1/auth/login", "/api/v1/zones"}
    assert by_prefix["/api/v1/auth/login"].rate_per_min == 10
    assert by_prefix["/api/v1/driver/location"].rate_per_min == 60
    assert by_prefix["/api/v1/rides"].rate_per_min == 120


def test_login_is_public_but_logout_is_not():
    assert match(ROUTES, "/api/v1/auth/login").public
    assert not match(ROUTES, "/api/v1/auth/logout").public


def test_sorted_longest_first():
    lengths = [len(r.prefix) for r in ROUTES]
    assert lengths == sorted(lengths, reverse=True)
