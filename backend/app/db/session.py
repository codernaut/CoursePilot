"""Database handle: an engine plus write and read-only session factories."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.engine import READ_ONLY_OPTION, make_engine


class Database:
    """Owns the engine. ``session()`` = BEGIN IMMEDIATE, ``read_session()`` = deferred BEGIN."""

    def __init__(self, database_url: str) -> None:
        self.url = database_url
        self.engine: AsyncEngine = make_engine(database_url)
        self._write = async_sessionmaker(self.engine, expire_on_commit=False)
        self._read = async_sessionmaker(
            self.engine.execution_options(**{READ_ONLY_OPTION: True}), expire_on_commit=False
        )

    def session(self) -> AsyncSession:
        return self._write()

    def read_session(self) -> AsyncSession:
        return self._read()

    @asynccontextmanager
    async def session_scope(self) -> AsyncIterator[AsyncSession]:
        async with self._write() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
