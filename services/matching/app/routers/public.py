from fastapi import APIRouter, Depends, Request

from ..deps import auth
from ..schemas import ZoneOut

# No login needed (the gateway marks /zones public), but it must still come through the gateway.
router = APIRouter(tags=["public"], dependencies=[Depends(auth.require_internal())])


@router.get("/zones", response_model=list[ZoneOut])
async def zones(request: Request):
    return request.app.state.zones
