from dataclasses import dataclass

from .models import Tariff


@dataclass(frozen=True)
class Breakdown:
    base_poysha: int
    distance_charge_poysha: int
    pool_discount_poysha: int
    total_poysha: int


def compute(t: Tariff, distance_m: int, seats: int, pooled: bool) -> Breakdown:
    distance_charge = distance_m * t.per_km_poysha // 1000
    discount = distance_charge * t.pool_discount_pct // 100 if pooled else 0
    return Breakdown(
        base_poysha=t.base_poysha * seats,
        distance_charge_poysha=distance_charge * seats,
        pool_discount_poysha=discount * seats,
        total_poysha=(t.base_poysha + distance_charge - discount) * seats,
    )
