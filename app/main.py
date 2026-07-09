from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect

from app.database import engine
from app.routers.puzzles import router as puzzles_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast and loud on a missing schema instead of a raw 500 on first
    # request — the easiest local-dev trip-up is starting the server before
    # running `alembic upgrade head`.
    async with engine.connect() as conn:
        table_names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
    if "players" not in table_names:
        raise RuntimeError(
            "Database schema is not initialized. Run `uv run alembic upgrade head` "
            "before starting the server."
        )
    yield


app = FastAPI(title="Puzzle Rewind", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


app.include_router(puzzles_router)

# Mounted last so "/api/*" and "/healthz" above take precedence over static files.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
