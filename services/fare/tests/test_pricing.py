"""6.2 / 6.4 / 6.7.2: the fare formula. The plan's three rows are the PRD's "pooled fares calculate correctly"."""
import pytest

from app.models import Tariff
from app.pricing import Breakdown, compute

V1 = Tariff(id=1, base_poysha=3000, per_km_poysha=1500, pool_discount_pct=20)


# ---- the plan's table (6.2) -------------------------------------------------------------------------------------

def test_nusrat_pooled_banani_to_mohakhali():
    assert compute(V1, 3500, 1, pooled=True) == Breakdown(3000, 5250, 1050, 7200)  # 72.00 taka


def test_rafiq_pooled_banani_to_gulshan_1():
    assert compute(V1, 2000, 1, pooled=True) == Breakdown(3000, 3000, 600, 5400)  # 54.00 taka


def test_nusrat_alone():
    assert compute(V1, 3500, 1, pooled=False) == Breakdown(3000, 5250, 0, 8250)  # 82.50 taka


# ---- the rules around it ----------------------------------------------------------------------------------------

def test_two_seats_pay_twice():
    assert compute(V1, 3500, 2, pooled=True) == Breakdown(6000, 10500, 2100, 14400)


@pytest.mark.parametrize("seats", range(1, 7))
@pytest.mark.parametrize("pooled", [True, False])
def test_total_is_always_exactly_its_parts(seats, pooled):
    # What the fares table's ck_fare_arithmetic demands (6.4), for every seat count.
    for d in range(100, 30001, 100):
        b = compute(V1, d, seats, pooled)
        assert b.total_poysha == b.base_poysha + b.distance_charge_poysha - b.pool_discount_poysha


def test_pooled_is_never_dearer_and_never_below_the_base():
    for d in range(100, 30001, 100):
        solo, pooled = compute(V1, d, 1, False), compute(V1, d, 1, True)
        assert V1.base_poysha <= pooled.total_poysha <= solo.total_poysha


def test_discount_is_only_on_the_distance_part():
    b = compute(V1, 3500, 1, pooled=True)
    assert b.pool_discount_poysha == b.distance_charge_poysha * 20 // 100  # not 20 % of the 30-taka base


def test_rounding_down_never_overcharges_with_a_future_tariff():
    """Tariff v1 never needs rounding (6.2). With 17 taka/km it does: 1234 m -> 2097.8 poysha -> 2097."""
    t = Tariff(id=2, base_poysha=3000, per_km_poysha=1700, pool_discount_pct=15)
    b = compute(t, 1234, 1, pooled=True)
    assert b.distance_charge_poysha == 2097                  # rounded down, never up
    assert b.pool_discount_poysha == 314                     # 2097 * 15 / 100 = 314.55 -> 314
    assert b.total_poysha == 3000 + 2097 - 314


def test_money_is_whole_poysha():
    b = compute(Tariff(id=3, base_poysha=2999, per_km_poysha=1333, pool_discount_pct=33), 777, 3, True)
    assert all(isinstance(v, int) for v in (b.base_poysha, b.distance_charge_poysha, b.pool_discount_poysha,
                                            b.total_poysha))


def test_no_discount_tariff_and_full_discount_tariff():
    assert compute(Tariff(id=4, base_poysha=3000, per_km_poysha=1500, pool_discount_pct=0), 3500, 1, True) == \
           Breakdown(3000, 5250, 0, 8250)
    assert compute(Tariff(id=5, base_poysha=3000, per_km_poysha=1500, pool_discount_pct=100), 3500, 1, True) == \
           Breakdown(3000, 5250, 5250, 3000)  # pooled rides pay only the base
