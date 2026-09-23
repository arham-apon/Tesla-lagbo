from .geo import DistanceTable
from .schemas import NEW_RIDE, EvaluateIn, OpenPool, PoolOption, Stop


def _route(dist: DistanceTable, start: str, drops: list[Stop]) -> tuple[int, dict[str, int]]:
    total, prev, reached = 0, start, {}
    for st in drops:
        total += dist.get(prev, st.zone)
        prev = st.zone
        reached[st.ride_id] = total
    return total, reached


def plan_for_pool(dist: DistanceTable, pool: OpenPool, req: EvaluateIn, max_detour_pct: int) -> PoolOption | None:
    if pool.pickup_zone != req.pickup_zone or pool.remaining_seats < req.seats:
        return None
    pickups = [s for s in pool.stops if s.kind == "PICKUP"]
    drops = [s for s in pool.stops if s.kind == "DROPOFF"]
    solo = {s.ride_id: dist.get(pool.pickup_zone, s.zone) for s in drops}
    solo[NEW_RIDE] = dist.get(req.pickup_zone, req.dropoff_zone)
    current_total, _ = _route(dist, pool.pickup_zone, drops)

    best: PoolOption | None = None
    new_drop = Stop(ride_id=NEW_RIDE, kind="DROPOFF", zone=req.dropoff_zone)
    for i in range(len(drops) + 1):
        order = drops[:i] + [new_drop] + drops[i:]
        total, reached = _route(dist, pool.pickup_zone, order)
        worst = 0
        feasible = True
        for ride_id, in_vehicle in reached.items():
            if in_vehicle * 100 > solo[ride_id] * max_detour_pct:
                feasible = False
                break
            worst = max(worst, in_vehicle * 100 // solo[ride_id])
        if feasible and (best is None or total < best.total_route_m):
            plan = pickups + [Stop(ride_id=NEW_RIDE, kind="PICKUP", zone=req.pickup_zone)] + order
            best = PoolOption(pool_id=pool.pool_id, version=pool.version, total_route_m=total,
                              added_route_m=total - current_total, max_detour_pct=worst, plan=plan)
    return best


def rank_pools(dist: DistanceTable, req: EvaluateIn, max_detour_pct: int) -> list[PoolOption]:
    options = [o for p in req.open_pools if (o := plan_for_pool(dist, p, req, max_detour_pct))]
    return sorted(options, key=lambda o: (o.added_route_m, o.max_detour_pct))
