from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.database_url)
async_session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with async_session() as session:
        yield session


def utcnow() -> datetime:
    """Naive UTC "now" — the storage convention for every datetime column.

    SQLite silently drops tzinfo on read, so comparisons must stay naive on
    both sides regardless of backend.
    """
    return datetime.now(UTC).replace(tzinfo=None)
