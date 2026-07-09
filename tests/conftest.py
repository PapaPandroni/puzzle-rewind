import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.rate_limit import limiter

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    limiter.reset()
    yield


def load_ndjson_fixture(name: str) -> list[dict]:
    path = FIXTURES_DIR / name
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.fixture
def peremil_games() -> list[dict]:
    return load_ndjson_fixture("games_peremil.ndjson")


@pytest.fixture
def halilegebaylam_games() -> list[dict]:
    return load_ndjson_fixture("games_halilegebaylam.ndjson")


@pytest.fixture
def onebestagon_games() -> list[dict]:
    return load_ndjson_fixture("games_onebestagon.ndjson")


@pytest_asyncio.fixture
async def db_sessionmaker():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    yield sessionmaker

    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_sessionmaker):
    async def override_get_db():
        async with db_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
