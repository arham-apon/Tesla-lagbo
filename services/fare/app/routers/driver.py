from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select

from tesla_common.auth import Principal

from ..deps import auth, db
from ..models import Fare
from ..schemas import EarningsOut

router = APIRouter(prefix="/driver", tags=["driver"])

# A WALLET payment that FAILED was collected in cash by the driver (plan 6.6: Notification tells him to).
_CASH = (Fare.payment_method == "CASH") | ((Fare.payment_method == "WALLET") & (Fare.payment_status == "FAILED"))
_WALLET = (Fare.payment_method == "WALLET") & (Fare.payment_status == "PAID")


@router.get("/earnings", response_model=EarningsOut)
async def earnings(p: Principal = Depends(auth.role("DRIVER"))):
    """Worked out from the ledger (plan 6.5), so it can never disagree with it. Refunded fares don't count."""
    async with db.ro() as s:
        rides, cash, wallet = (await s.execute(
            select(func.count(), func.coalesce(func.sum(case((_CASH, Fare.total_poysha), else_=0)), 0),
                   func.coalesce(func.sum(case((_WALLET, Fare.total_poysha), else_=0)), 0))
            .where(Fare.driver_id == p.user_id, Fare.payment_status != "REFUNDED"))).one()
    return EarningsOut(rides=rides, total_poysha=cash + wallet, cash_poysha=cash, wallet_poysha=wallet)
