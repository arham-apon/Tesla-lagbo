from fastapi import APIRouter, Depends, Request, status

from tesla_common.auth import Principal
from tesla_common.timeutil import utcnow

from ..deps import auth
from ..fleet import record_ping
from ..schemas import LocationPing

router = APIRouter(tags=["driver"])


@router.post("/driver/location", status_code=status.HTTP_202_ACCEPTED)
async def location(body: LocationPing, request: Request, p: Principal = Depends(auth.role("DRIVER"))):
    zone = await record_ping(request.app.state.redis, request.app.state.dist, p.user_id, body.lat, body.lng,
                             utcnow().isoformat(timespec="microseconds") + "Z")
    return {"zone": zone}
