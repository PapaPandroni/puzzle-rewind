import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import settings

USER_AGENT = "puzzle-rewind/0.1 (hobby project)"

STANDARD_PERF_TYPES = "ultraBullet,bullet,blitz,rapid,classical,correspondence"

# Lichess caps the bulk games-by-ids export at 300 ids per request. Callers chunk
# to this and pace themselves; keeping the limit here rather than at the call site
# means the etiquette lives with the API wrapper.
MAX_EXPORT_IDS = 300


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


async def fetch_games_by_ids(
    game_ids: list[str], *, timeout: float = 60.0
) -> AsyncIterator[dict[str, Any]]:
    """Stream games addressed by id from the bulk export endpoint.

    Used by the moves_san backfill (app/backfill.py): re-fetching thousands of
    games one at a time would be thousands of requests, where this is one per
    300. Unlike `fetch_games` this yields whatever Lichess returns, variant and
    all — the caller asked for these specific rows and matches the response by
    "id", since Lichess neither guarantees order nor errors on an id it can't
    serve: an unknown, deleted or private game is simply absent from the
    stream, which is how the caller learns it is unrecoverable.

    At most MAX_EXPORT_IDS per call; chunking and pacing are the caller's job.
    """
    if len(game_ids) > MAX_EXPORT_IDS:
        raise ValueError(f"at most {MAX_EXPORT_IDS} ids per request, got {len(game_ids)}")
    if not game_ids:
        return

    url = f"{settings.lichess_base}/api/games/export/_ids"
    params: dict[str, Any] = {"moves": "true", "evals": "false", "clocks": "false"}
    headers = {
        "Accept": "application/x-ndjson",
        "Content-Type": "text/plain",
        "User-Agent": USER_AGENT,
    }
    if settings.lichess_token:
        headers["Authorization"] = f"Bearer {settings.lichess_token}"

    async with _build_client(timeout) as client:
        async with client.stream(
            "POST", url, params=params, headers=headers, content=",".join(game_ids)
        ) as response:
            if response.status_code == 429:
                raise LichessRateLimited()
            response.raise_for_status()

            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                yield json.loads(line)
