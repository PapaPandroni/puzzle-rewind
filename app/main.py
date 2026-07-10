from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import inspect

from app.database import engine
from app.rate_limit import limiter
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


app = FastAPI(title="Puzzle Rewind", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

CANONICAL_HOST = "www.puzzle-rewind.eu"


@app.middleware("http")
async def redirect_to_canonical_host(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    host = request.headers.get("host", "").split(":")[0].lower()
    if host.endswith(".railway.app") or host == "puzzle-rewind.eu":
        url = request.url.replace(scheme="https", netloc=CANONICAL_HOST)
        return RedirectResponse(str(url), status_code=301)
    return await call_next(request)


app.add_middleware(SlowAPIMiddleware)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


app.include_router(puzzles_router)

# Mounted last so "/api/*" and "/healthz" above take precedence over static files.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
