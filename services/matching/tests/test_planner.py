"""4.4 / 4.8.3: the pool planner, on the real distance table (B=Banani, G1/G2=Gulshan 1/2, M=Mohakhali)."""
import pytest
from pydantic import ValidationError

from tesla_common.errors import DomainError

from app.geo import DistanceTable
from app.planner import _route, plan_for_pool, rank_pools
from app.schemas import NEW_RIDE, EvaluateIn, OpenPool, Stop

MAX = 140


def pool(stops: list[tuple[str, str, str]], remaining: int, pickup: str = "BANANI", pool_id: str = "bullet",
         version: int = 1) -> OpenPool:
    return OpenPool(pool_id=pool_id, driver_id="jashim", pickup_zone=pickup, remaining_seats=remaining,
                    version=version, stops=[Stop(ride_id=r, kind=k, zone=z) for r, k, z in stops])


def request(dropoff: str, seats: int = 1, pickup: str = "BANANI", pools=()) -> EvaluateIn:
    return EvaluateIn(pickup_zone=pickup, dropoff_zone=dropoff, seats=seats, open_pools=list(pools))


def order(option) -> list[tuple[str, str, str]]:
    return [(s.kind, s.ride_id, s.zone) for s in option.plan]


NUSRAT_POOL = pool([("nusrat", "PICKUP", "BANANI"), ("nusrat", "DROPOFF", "MOHAKHALI")], remaining=2)
AFTER_RAFIQ = pool([("nusrat", "PICKUP", "BANANI"), ("rafiq", "PICKUP", "BANANI"),
                    ("rafiq", "DROPOFF", "GULSHAN_1"), ("nusrat", "DROPOFF", "MOHAKHALI")], remaining=1, version=2)


# ---- the plan's worked example -------------------------------------------------------------------------------

async def test_rafiq_joins_nusrat_gulshan1_first(dist):
    option = plan_for_pool(dist, NUSRAT_POOL, request("GULSHAN_1"), MAX)
    assert order(option) == [("PICKUP", "nusrat", "BANANI"), ("PICKUP", NEW_RIDE, "BANANI"),
                             ("DROPOFF", NEW_RIDE, "GULSHAN_1"), ("DROPOFF", "nusrat", "MOHAKHALI")]
    assert option.total_route_m == 4000          # B->G1 2000 + G1->M 2000
    assert option.added_route_m == 500           # Nusrat alone was 3500
    assert option.max_detour_pct == 114          # Nusrat 4000/3500 = 114 %, Rafiq 2000/2000 = 100 %
    assert (option.pool_id, option.version) == ("bullet", 1)  # Trip needs the version for its atomic join


async def test_gulshan1_last_would_break_rafiqs_limit(dist):
    nusrat_drop = Stop(ride_id="nusrat", kind="DROPOFF", zone="MOHAKHALI")
    rafiq_drop = Stop(ride_id=NEW_RIDE, kind="DROPOFF", zone="GULSHAN_1")
    total, reached = _route(dist, "BANANI", [nusrat_drop, rafiq_drop])
    assert (total, reached) == (5500, {"nusrat": 3500, NEW_RIDE: 5500})
    assert reached[NEW_RIDE] * 100 > 2000 * MAX  # 275 % > 140 %: that order is never offered


async def test_shirin_two_seats_rejected_when_one_left(dist):
    assert plan_for_pool(dist, AFTER_RAFIQ, request("GULSHAN_2", seats=2), MAX) is None


async def test_different_pickup_zone_rejected(dist):
    assert plan_for_pool(dist, NUSRAT_POOL, request("MOHAKHALI", pickup="GULSHAN_1"), MAX) is None
    # A case the detour rule alone would let through (newcomer 18000/12900 = 139 %, Nusrat 100 %):
    # only the same-pickup-zone rule (plan decision A4) keeps a Gulshan 2 rider out of a Banani pool.
    assert plan_for_pool(dist, NUSRAT_POOL, request("UTTARA", pickup="GULSHAN_2"), MAX) is None


# ---- more cases -----------------------------------------------------------------------------------------------

async def test_third_rider_fits_first(dist):
    # Karim B->G2 into Bullet after Rafiq joined. Only G2 first works:
    #   [G2,G1,M]: 1000, 2700, 4700 -> Karim 100 %, Rafiq 2700/2000 = 135 %, Nusrat 4700/3500 = 134 %  OK
    #   [G1,G2,M]: Karim 3700/1000 = 370 %  no;  [G1,M,G2]: Karim 6400/1000 = 640 %  no
    option = plan_for_pool(dist, AFTER_RAFIQ, request("GULSHAN_2"), MAX)
    assert [z for k, _, z in order(option) if k == "DROPOFF"] == ["GULSHAN_2", "GULSHAN_1", "MOHAKHALI"]
    assert (option.total_route_m, option.added_route_m, option.max_detour_pct) == (4700, 700, 135)


async def test_existing_riders_keep_their_order(dist):
    option = plan_for_pool(dist, AFTER_RAFIQ, request("GULSHAN_2"), MAX)
    existing = [r for k, r, _ in order(option) if k == "DROPOFF" and r != NEW_RIDE]
    assert existing == ["rafiq", "nusrat"]


async def test_same_destination_costs_nothing_extra(dist):
    option = plan_for_pool(dist, NUSRAT_POOL, request("MOHAKHALI"), MAX)
    assert (option.total_route_m, option.added_route_m, option.max_detour_pct) == (3500, 0, 100)


async def test_far_destination_rejected(dist):
    # [U,M]: Nusrat 26900/3500 = 768 %.  [M,U]: newcomer 18000/12400 = 145 %.  Both over 140 %.
    assert plan_for_pool(dist, NUSRAT_POOL, request("UTTARA"), MAX) is None


async def test_stricter_limit_blocks_rafiq(dist):
    assert plan_for_pool(dist, NUSRAT_POOL, request("GULSHAN_1"), 110) is None  # Nusrat would be at 114 %


async def test_full_pool_rejected(dist):
    full = pool([("a", "PICKUP", "BANANI"), ("a", "DROPOFF", "MOHAKHALI")], remaining=0)
    assert plan_for_pool(dist, full, request("MOHAKHALI"), MAX) is None


def test_exactly_140_percent_is_allowed():
    # Synthetic map: solo A->X = 1000, going via Y makes it 400 + 1000 = 1400 = exactly 140 %.
    zones = {"A": (23.70, 90.30), "X": (23.80, 90.40), "Y": (23.90, 90.50)}
    table = DistanceTable(zones, {("A", "X"): 1000, ("X", "A"): 1000, ("A", "Y"): 400, ("Y", "A"): 400,
                                  ("Y", "X"): 1000, ("X", "Y"): 1000})
    p = pool([("r1", "PICKUP", "A"), ("r1", "DROPOFF", "X")], remaining=2, pickup="A")
    option = plan_for_pool(table, p, request("Y", pickup="A"), 140)
    assert option.max_detour_pct == 140 and option.total_route_m == 1400
    table.overrides[("Y", "X")] = table.overrides[("X", "Y")] = 1001  # 1401 = 140.1 %
    assert plan_for_pool(table, p, request("Y", pickup="A"), 140) is None


# ---- ranking ---------------------------------------------------------------------------------------------------

async def test_rank_least_extra_driving_first(dist):
    to_g1 = pool([("x", "PICKUP", "BANANI"), ("x", "DROPOFF", "GULSHAN_1")], remaining=3, pool_id="to-g1")
    to_m = pool([("y", "PICKUP", "BANANI"), ("y", "DROPOFF", "MOHAKHALI")], remaining=3, pool_id="to-m")
    other_zone = pool([("z", "PICKUP", "UTTARA"), ("z", "DROPOFF", "MIRPUR")], remaining=3, pickup="UTTARA",
                      pool_id="uttara")
    ranked = rank_pools(dist, request("MOHAKHALI", pools=[to_g1, to_m, other_zone]), MAX)
    # to-m: same destination, +0 m.  to-g1: B->G1->M, +2000 m.  uttara: wrong pickup zone.
    assert [(o.pool_id, o.added_route_m) for o in ranked] == [("to-m", 0), ("to-g1", 2000)]


async def test_rank_ties_broken_by_lower_detour(dist):
    a = pool([("a", "PICKUP", "BANANI"), ("a", "DROPOFF", "MOHAKHALI")], remaining=3, pool_id="a")
    b = pool([("b", "PICKUP", "BANANI"), ("b", "DROPOFF", "MOHAKHALI")], remaining=3, pool_id="b")
    ranked = rank_pools(dist, request("MOHAKHALI", pools=[b, a]), MAX)
    assert [o.pool_id for o in ranked] == ["b", "a"]  # equal on both keys: stable, keeps Trip's order


async def test_no_open_pools(dist):
    assert rank_pools(dist, request("GULSHAN_1"), MAX) == []


# ---- bad input -------------------------------------------------------------------------------------------------

def test_same_pickup_and_dropoff_rejected_before_planning():
    # Plan's planner would divide by a 0 m solo distance (ZeroDivisionError -> 500). Now: 422 at the door.
    with pytest.raises(ValidationError):
        request("BANANI", pickup="BANANI", pools=[NUSRAT_POOL])


async def test_unknown_dropoff_zone_is_422(dist):
    with pytest.raises(DomainError) as exc:
        plan_for_pool(dist, NUSRAT_POOL, request("MOTIJHEEL"), MAX)
    assert exc.value.code == "UNKNOWN_ZONE"


async def test_everything_is_whole_numbers(dist):
    option = plan_for_pool(dist, NUSRAT_POOL, request("GULSHAN_1"), MAX)
    assert all(isinstance(v, int) for v in (option.total_route_m, option.added_route_m, option.max_detour_pct))
