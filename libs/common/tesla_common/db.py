from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def make_engine(db_path: str, *, immediate: bool) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", connect_args={"timeout": 5})

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _record):
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return engine


class Database:
    def __init__(self, db_path: str):
        self.rw_engine = make_engine(db_path, immediate=True)
        self.ro_engine = make_engine(db_path, immediate=False)
        self.rw = async_sessionmaker(self.rw_engine, expire_on_commit=False)
        self.ro = async_sessionmaker(self.ro_engine, expire_on_commit=False)

    async def ro_session(self) -> AsyncIterator[AsyncSession]:
        async with self.ro() as s:
            yield s

    async def dispose(self) -> None:
        await self.rw_engine.dispose()
        await self.ro_engine.dispose()
