from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routers.puzzles import router as puzzles_router

app = FastAPI(title="Puzzle Rewind")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


app.include_router(puzzles_router)

# Mounted last so "/api/*" and "/healthz" above take precedence over static files.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
