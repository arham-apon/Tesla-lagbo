from fastapi import APIRouter, Depends, Query, Request

from ..deps import auth, settings
from ..fleet import nearby_available
from ..planner import rank_pools
from ..schemas import CandidateDriver, EvaluateIn, EvaluateOut

router = APIRouter(prefix="/internal", dependencies=[Depends(auth.require_internal())])


@router.get("/zones/distance")
async def distance(request: Request, from_: str = Query(alias="from"), to: str = Query()):
    return {"distance_m": request.app.state.dist.get(from_, to)}


@router.post("/match/evaluate", response_model=EvaluateOut)
async def evaluate(body: EvaluateIn, request: Request):
    dist = request.app.state.dist
    solo = dist.get(body.pickup_zone, body.dropoff_zone)
    pools = rank_pools(dist, body, settings.POOL_MAX_DETOUR_PCT)
    lat, lng = dist.zones[body.pickup_zone]
    drivers = await nearby_available(request.app.state.redis, lat, lng, settings.MATCH_RADIUS_M, body.max_candidates)
    return EvaluateOut(solo_distance_m=solo, compatible_pools=pools,
                       candidate_drivers=[CandidateDriver(driver_id=d, distance_m=m) for d, m in drivers])
