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


def _build_client(timeout: float = 30.0) -> httpx.AsyncClient:
    # Factored out so tests can monkeypatch in a client backed by httpx.MockTransport.
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout))


async def fetch_games(
    username: str,
    *,
    max_games: int = settings.max_games_mvp,
    since: int | None = None,
    until: int | None = None,
    timeout: float = 30.0,
    analysed: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """Stream a player's standard games from the Lichess export API.

    Yields one parsed game dict per NDJSON line. With `analysed=True` only
    server-analyzed games are requested, and games missing an "analysis" field
    anyway are skipped defensively (§5.3). With `analysed=False` the filter is
    *omitted* — Lichess treats an explicit analysed=false as "only unanalyzed
    games", but the Phase 3 sync wants all of them (evals still attached where
    they exist). `since`/`until` are epoch milliseconds; period backfills pass
    a longer `timeout` because hundreds of games stream for tens of seconds
    (§13.2).
    """
    url = f"{settings.lichess_base}/api/games/user/{username}"
    params: dict[str, Any] = {
        "max": max_games,
        "evals": "true",
        "moves": "true",
        "perfType": STANDARD_PERF_TYPES,
    }
    if analysed:
        params["analysed"] = "true"
    if since is not None:
        params["since"] = since
    if until is not None:
        params["until"] = until

    headers = {"Accept": "application/x-ndjson", "User-Agent": USER_AGENT}
    if settings.lichess_token:
        headers["Authorization"] = f"Bearer {settings.lichess_token}"

    async with _build_client(timeout) as client:
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
                if analysed and not game.get("analysis"):
                    continue
                yield game
