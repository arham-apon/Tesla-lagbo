"""Demo wallets for the story (plan 6.7 step 6). Idempotent: run on every container start, adds only what's missing.

    python -m app.seed
"""
import asyncio
import logging

from sqlalchemy import select

from tesla_common.db import Database
from tesla_common.logging import configure_logging

from .config import settings
from .deps import db
from .models import Wallet, WalletTransaction

log = logging.getLogger("fare.seed")

# Identity's fixed seed ids (Part 3), so the wallets belong to the right people.
WALLETS = [
    ("22222222-2222-4222-8222-222222222222", "Nusrat", 50000),   # 500 taka
    ("33333333-3333-4333-8333-333333333333", "Rafiq", 50000),
    ("44444444-4444-4444-8444-444444444444", "Shirin", 20000),   # 200 taka
    ("11111111-1111-4111-8111-111111111111", "Jashim", 0),       # the driver earns into his
]


async def seed(database: Database = db) -> list[str]:
    created: list[str] = []
    async with database.rw.begin() as s:
        for user_id, name, balance in WALLETS:
            if await s.scalar(select(Wallet.user_id).where(Wallet.user_id == user_id)):
                continue  # never touch a wallet that exists: it may have been used since
            s.add(Wallet(user_id=user_id, balance_poysha=balance))
            await s.flush()
            if balance:
                # Record the starting money as a top-up, so a balance always equals the sum of its transactions.
                s.add(WalletTransaction(user_id=user_id, kind="TOPUP", amount_poysha=balance))
            created.append(name)
    return created


async def main() -> None:
    configure_logging("fare", settings.LOG_LEVEL)
    created = await seed()
    log.info("seed done: %s", ", ".join(created) if created else "nothing new")
    await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
