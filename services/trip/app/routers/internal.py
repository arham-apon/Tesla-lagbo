from fastapi import APIRouter, Depends
from sqlalchemy import select

from ..deps import auth, db
from ..models import LIVE_POOL_STATUSES, Pool

# Other services only (no user): every route needs the internal token.
router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(auth.require_internal())])


@router.get("/drivers/{driver_id}/live-pool")
async def live_pool(driver_id: str) -> dict:
    """Identity asks this before letting a driver go offline (a live pool means passengers depend on him)."""
    async with db.ro() as s:
        pool_id = await s.scalar(select(Pool.id).where(Pool.driver_id == driver_id,
                                                       Pool.status.in_(LIVE_POOL_STATUSES)))
    return {"pool_id": pool_id}
