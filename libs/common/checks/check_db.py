"""Step 1.4.3 — prove that two concurrent `rw` transactions serialize.

Run:  python libs/common/checks/check_db.py

Transaction A takes the write lock (BEGIN IMMEDIATE) and holds it for 1 s.
Transaction B starts 0.1 s later and must WAIT (busy_timeout) until A commits.
Meanwhile a `ro` read must NOT wait, because WAL readers never block on writers.
"""
import asyncio
import os
import tempfile
import time

from sqlalchemy import text

from tesla_common.db import Database


async def main() -> None:
    path = os.path.join(tempfile.mkdtemp(), "check.db")
    db = Database(path)
    async with db.rw.begin() as s:
        await s.execute(text("CREATE TABLE seats (id INTEGER PRIMARY KEY, occupied INTEGER)"))
        await s.execute(text("INSERT INTO seats VALUES (1, 0)"))

    t0 = time.perf_counter()
    log: list[str] = []

    def mark(msg: str) -> None:
        log.append(f"{time.perf_counter() - t0:5.2f}s  {msg}")

    async def writer(name: str, hold: float) -> None:
        async with db.rw.begin() as s:
            # SQLAlchemy sends BEGIN IMMEDIATE lazily, on the first statement.
            occupied = (await s.execute(text("SELECT occupied FROM seats WHERE id=1"))).scalar_one()
            mark(f"{name}: got write lock, read occupied={occupied}")
            await asyncio.sleep(hold)
            await s.execute(text("UPDATE seats SET occupied=:o WHERE id=1"), {"o": occupied + 1})
        mark(f"{name}: committed (read {occupied}, wrote {occupied + 1})")

    async def reader() -> None:
        await asyncio.sleep(0.2)
        async with db.ro() as s:
            v = (await s.execute(text("SELECT occupied FROM seats WHERE id=1"))).scalar_one()
        mark(f"reader: read occupied={v} without waiting")

    async def delayed_writer() -> None:
        await asyncio.sleep(0.1)
        mark("B: asking for write lock")
        await writer("B", 0)

    await asyncio.gather(writer("A", 1.0), delayed_writer(), reader())

    async with db.ro() as s:
        final = (await s.execute(text("SELECT occupied FROM seats WHERE id=1"))).scalar_one()
    await db.dispose()

    print("\n".join(log))
    assert final == 2, f"lost update! final={final}"
    print(f"\nOK: final occupied={final} - B waited for A, no lost update.")


if __name__ == "__main__":
    asyncio.run(main())
