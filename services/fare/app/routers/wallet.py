from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from tesla_common.auth import Principal

from ..deps import auth, db
from ..models import Wallet, WalletTransaction
from ..schemas import BalanceOut, TopUpIn, WalletOut

router = APIRouter(prefix="/wallet", tags=["wallet"])


@router.get("", response_model=WalletOut)
async def my_wallet(limit: int = Query(default=20, ge=1, le=100),
                    p: Principal = Depends(auth.role("PASSENGER", "DRIVER"))):
    """Balance and the latest transactions, newest first. No wallet yet -> balance 0."""
    async with db.ro() as s:
        balance = await s.scalar(select(Wallet.balance_poysha).where(Wallet.user_id == p.user_id))
        txns = (await s.execute(select(WalletTransaction).where(WalletTransaction.user_id == p.user_id)
                                .order_by(WalletTransaction.created_at.desc()).limit(limit))).scalars().all()
    return WalletOut(balance_poysha=balance or 0, transactions=txns)


@router.post("/topup", response_model=BalanceOut)
async def top_up(body: TopUpIn, p: Principal = Depends(auth.role("PASSENGER"))):
    """Simulated TeslaPay: adds money. Creates the wallet on first top-up (the plan's upsert)."""
    async with db.rw.begin() as s:
        stmt = insert(Wallet).values(user_id=p.user_id, balance_poysha=body.amount_poysha)
        stmt = stmt.on_conflict_do_update(index_elements=[Wallet.user_id],
                                          set_={"balance_poysha": Wallet.balance_poysha + stmt.excluded.balance_poysha})
        await s.execute(stmt)
        s.add(WalletTransaction(user_id=p.user_id, kind="TOPUP", amount_poysha=body.amount_poysha))
        await s.flush()
        balance = await s.scalar(select(Wallet.balance_poysha).where(Wallet.user_id == p.user_id))
    return BalanceOut(balance_poysha=balance)
