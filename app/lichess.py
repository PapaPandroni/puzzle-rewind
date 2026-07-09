import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import settings

USER_AGENT = "puzzle-rewind/0.1 (hobby project)"

STANDARD_PERF_TYPES = "ultraBullet,bullet,blitz,rapid,classical,correspondence"


class LichessUserNotFound(Exception):
    def __init__(self, username: str):
        self.username = username
        super().__init__(f"lichess user not found: {username}")


class LichessRateLimited(Exception):
    pass


def _build_client() -> httpx.AsyncClient:
    # Factored out so tests can monkeypatch in a client backed by httpx.MockTransport.
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0))


async def fetch_games(
    username: str,
    *,
    max_games: int = settings.max_games_mvp,
    since: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream a player's analyzed standard games from the Lichess export API.

    Yields one parsed game dict per NDJSON line. Games missing an "analysis"
    field despite analysed=true are skipped defensively (§5.3).
    """
    url = f"{settings.lichess_base}/api/games/user/{username}"
    params: dict[str, Any] = {
        "max": max_games,
        "analysed": "true",
        "evals": "true",
        "moves": "true",
        "perfType": STANDARD_PERF_TYPES,
    }
    if since is not None:
        params["since"] = since

    headers = {"Accept": "application/x-ndjson", "User-Agent": USER_AGENT}
    if settings.lichess_token:
        headers["Authorization"] = f"Bearer {settings.lichess_token}"

    async with _build_client() as client:
        async with client.stream("GET", url, params=params, headers=headers) as response:
            if response.status_code == 404:
                raise LichessUserNotFound(username)
            if response.status_code == 429:
                raise LichessRateLimited()
            response.raise_for_status()

            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                game = json.loads(line)
                if game.get("variant") != "standard":
                    continue
                if not game.get("analysis"):
                    continue
                yield game
