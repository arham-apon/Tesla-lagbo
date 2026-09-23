"""4.3 / 4.8.2: DistanceTable: overrides, symmetry, unknown zones, computed distances, nearest zone."""
import itertools

import pytest

from tesla_common.errors import DomainError

from app.geo import haversine_m


def test_haversine_one_degree_of_latitude():
    assert haversine_m(23.0, 90.0, 24.0, 90.0) == pytest.approx(111_195, abs=1)


async def test_banani_mohakhali_is_3500(dist):  # the plan's named check
    assert dist.get("BANANI", "MOHAKHALI") == 3500


@pytest.mark.parametrize("a, b, metres", [("BANANI", "MOHAKHALI", 3500), ("BANANI", "GULSHAN_1", 2000),
                                          ("GULSHAN_1", "MOHAKHALI", 2000)])
async def test_overrides_are_symmetric(dist, a, b, metres):
    assert dist.get(a, b) == dist.get(b, a) == metres


async def test_every_pair_symmetric_and_positive(dist):
    for a, b in itertools.combinations(dist.zones, 2):
        d = dist.get(a, b)
        assert d == dist.get(b, a)
        assert d >= 1000 and d % 100 == 0  # never 0 between different zones (the planner divides by it)


async def test_same_zone_is_zero(dist):
    assert dist.get("UTTARA", "UTTARA") == 0


async def test_computed_distance_is_road_factor_rounded(dist):
    straight = haversine_m(*dist.zones["FARMGATE"], *dist.zones["DHANMONDI"])
    assert dist.get("FARMGATE", "DHANMONDI") == round(straight * 1.3, -2) == 2500


@pytest.mark.parametrize("a, b", [("BANANI", "MOTIJHEEL"), ("MOTIJHEEL", "BANANI"), ("banani", "MOHAKHALI")])
async def test_unknown_zone_is_422(dist, a, b):
    with pytest.raises(DomainError) as exc:
        dist.get(a, b)
    assert (exc.value.code, exc.value.status) == ("UNKNOWN_ZONE", 422)


async def test_nearest_zone(dist):
    assert all(dist.nearest_zone(*dist.zones[code]) == code for code in dist.zones)
    assert dist.nearest_zone(23.7940, 90.4040) == "BANANI"        # a few hundred metres from Banani's centre
    assert dist.nearest_zone(23.7790, 90.4120) == "GULSHAN_1"


async def test_loader_reads_all_zones_and_overrides(dist):
    assert len(dist.zones) == 9 and len(dist.overrides) == 6
