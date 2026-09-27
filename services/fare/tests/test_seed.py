"""6.7.6: demo wallets for the cast, with Identity's fixed ids; safe to run on every start."""
from sqlalchemy import select, update

from app.models import Wallet, WalletTransaction
from app.seed import WALLETS, seed
from conftest import JASHIM, NUSRAT, RAFIQ

SHIRIN = "44444444-4444-4444-8444-444444444444"


async def balances(db) -> dict:
    async with db.ro() as s:
        return dict((await s.execute(select(Wallet.user_id, Wallet.balance_poysha))).all())


async def test_the_plans_balances(db):
    assert await seed(db) == ["Nusrat", "Rafiq", "Shirin", "Jashim"]
    assert await balances(db) == {NUSRAT: 50000, RAFIQ: 50000, SHIRIN: 20000, JASHIM: 0}


async def test_starting_money_is_a_top_up(db):
    await seed(db)
    async with db.ro() as s:
        rows = (await s.execute(select(WalletTransaction.user_id, WalletTransaction.kind,
                                       WalletTransaction.amount_poysha))).all()
    assert sorted(rows) == sorted([(NUSRAT, "TOPUP", 50000), (RAFIQ, "TOPUP", 50000), (SHIRIN, "TOPUP", 20000)])


async def test_second_run_adds_nothing_and_touches_nothing(db):
    await seed(db)
    async with db.rw.begin() as s:  # Nusrat has ridden since the first start
        await s.execute(update(Wallet).where(Wallet.user_id == NUSRAT).values(balance_poysha=42800))
    assert await seed(db) == []
    assert (await balances(db))[NUSRAT] == 42800


def test_ids_are_identitys():
    # Identity's seed (services/identity/app/seed.py) uses these exact ids (plan 3.6 step 7).
    assert {w[0] for w in WALLETS} == {JASHIM, NUSRAT, RAFIQ, SHIRIN}
