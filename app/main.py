import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import inspect

from app.database import async_session, engine
from app.engine import engine_handle
from app.rate_limit import limiter
from app.routers.puzzles import router as puzzles_router
from app.worker import reset_stale_jobs, worker_loop


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
    # Background Stockfish worker (§14.1) — uses the module-level session
    # factory, never the request-scoped get_db. Note API tests drive the app
    # through httpx.ASGITransport, which skips lifespan: no worker runs there.
    await reset_stale_jobs(async_session)
    worker_task = asyncio.create_task(worker_loop(async_session))
    yield
    worker_task.cancel()
    with suppress(asyncio.CancelledError):
        await worker_task
    await engine_handle.quit()


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
