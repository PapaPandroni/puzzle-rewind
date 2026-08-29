import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from app import lichess
from app.backfill import backfill_moves_san
from app.lichess import LichessRateLimited, fetch_games_by_ids
from app.models import Game, Player, Puzzle

# 20 plies, so a puzzle at ply 12 has both a predecessor and a valid replay.
MOVES = (
    "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7 Re1 b5 Bb3 d6 c3 O-O h3 Nb8 d4 Nbd7"
)


def _ndjson_transport(
    games: list[dict], status_code: int = 200, seen: list[httpx.Request] | None = None
) -> httpx.MockTransport:
    body = "\n".join(json.dumps(g) for g in games).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status_code, content=body)

    return httpx.MockTransport(handler)


def _patch_client(monkeypatch, transport: httpx.MockTransport):
    def _build_client(timeout: float = 30.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=timeout)

    monkeypatch.setattr(lichess, "_build_client", _build_client)


# --- fetch_games_by_ids --------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_games_by_ids_posts_ids_and_parses_stream(monkeypatch):
    seen: list[httpx.Request] = []
    _patch_client(monkeypatch, _ndjson_transport([{"id": "aaaa1111", "moves": MOVES}], seen=seen))

    got = [g async for g in fetch_games_by_ids(["aaaa1111", "bbbb2222"])]

    assert [(g["id"], g["moves"]) for g in got] == [("aaaa1111", MOVES)]
    # The ids travel as a comma-separated POST body, not as query params — a GET
    # with 300 ids in the URL is what this endpoint exists to avoid.
    request = seen[0]
    assert request.method == "POST"
    assert request.content == b"aaaa1111,bbbb2222"
    assert request.url.path == "/api/games/export/_ids"
    assert request.url.params["moves"] == "true"


@pytest.mark.asyncio
async def test_fetch_games_by_ids_empty_list_makes_no_request(monkeypatch):
    seen: list[httpx.Request] = []
    _patch_client(monkeypatch, _ndjson_transport([{"id": "aaaa1111", "moves": MOVES}], seen=seen))

    assert [g async for g in fetch_games_by_ids([])] == []
    assert seen == []


@pytest.mark.asyncio
async def test_fetch_games_by_ids_omits_unknown_ids_rather_than_erroring(monkeypatch):
    # Lichess simply leaves an unknown/deleted/private game out of the stream —
    # that absence is how the backfill learns a row is unrecoverable.
    _patch_client(monkeypatch, _ndjson_transport([{"id": "aaaa1111", "moves": MOVES}]))

    got = [g async for g in fetch_games_by_ids(["aaaa1111", "deleted0"])]

    assert [g["id"] for g in got] == ["aaaa1111"]


@pytest.mark.asyncio
async def test_fetch_games_by_ids_429_raises_rate_limited(monkeypatch):
    _patch_client(monkeypatch, _ndjson_transport([], status_code=429))

    with pytest.raises(LichessRateLimited):
        [g async for g in fetch_games_by_ids(["aaaa1111"])]


@pytest.mark.asyncio
async def test_fetch_games_by_ids_rejects_oversized_batch(monkeypatch):
    # Chunking is the caller's job; silently truncating would drop rows.
    with pytest.raises(ValueError, match="at most 300"):
        [g async for g in fetch_games_by_ids([f"id{i:06d}" for i in range(301)])]


# --- backfill_moves_san --------------------------------------------------------


async def _seed(sessionmaker, games: list[tuple[str, str | None]], *, with_puzzle: str | None = None):
    now = datetime.now(UTC).replace(tzinfo=None)
    async with sessionmaker() as session:
        player = Player(username="oldrows")
        session.add(player)
        await session.flush()
        for lichess_id, moves_san in games:
            game = Game(
                lichess_id=lichess_id,
                player_id=player.id,
                player_color="white",
                player_rating=1500,
                opponent_name="opp",
                opponent_rating=1500,
                speed="blitz",
                played_at=now,
                raw_analysis_processed=True,
                moves_san=moves_san,
            )
            session.add(game)
            await session.flush()
            if lichess_id == with_puzzle:
                session.add(
                    Puzzle(
                        game_id=game.id,
                        ply=12,
                        fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
                        side_to_move="white",
                        solution_uci="e2e4",
                        solution_san="e4",
                        played_uci="d2d4",
                        played_san="d4",
                        variation_san="e4 e5",
                        win_drop=30.0,
                        eval_before_cp=200,
                        eval_after_cp=-150,
                    )
                )
        await session.commit()


@pytest.mark.asyncio
async def test_backfill_fills_null_rows_and_leaves_populated_ones_alone(
    db_sessionmaker, monkeypatch
):
    await _seed(db_sessionmaker, [("nullrow01", None), ("hasmoves1", "e4 e5")])
    _patch_client(
        monkeypatch,
        _ndjson_transport([{"id": "nullrow01", "moves": MOVES}]),
    )

    report = await backfill_moves_san(db_sessionmaker, pause_s=0)

    assert report.candidates == 1  # the populated row was never a candidate
    assert report.filled == 1
    assert report.unrecoverable == []
    async with db_sessionmaker() as session:
        rows = {g.lichess_id: g.moves_san for g in await session.scalars(select(Game))}
    assert rows == {"nullrow01": MOVES, "hasmoves1": "e4 e5"}


@pytest.mark.asyncio
async def test_backfill_reports_games_lichess_will_not_serve(db_sessionmaker, monkeypatch):
    await _seed(db_sessionmaker, [("nullrow01", None), ("deletedrw", None)])
    _patch_client(monkeypatch, _ndjson_transport([{"id": "nullrow01", "moves": MOVES}]))

    report = await backfill_moves_san(db_sessionmaker, pause_s=0)

    assert report.filled == 1
    assert report.unrecoverable == ["deletedrw"]
    # Left NULL on purpose — _derive_last_move logs it as `no_movelist` from here on.
    async with db_sessionmaker() as session:
        game = await session.scalar(
            select(Game).where(Game.lichess_id == "deletedrw")
        )
        assert game.moves_san is None


@pytest.mark.asyncio
async def test_backfill_dry_run_writes_nothing_but_counts_puzzle_impact(
    db_sessionmaker, monkeypatch
):
    await _seed(db_sessionmaker, [("nullrow01", None), ("nullrow02", None)], with_puzzle="nullrow01")
    _patch_client(monkeypatch, _ndjson_transport([{"id": "nullrow01", "moves": MOVES}]))

    report = await backfill_moves_san(db_sessionmaker, dry_run=True, pause_s=0)

    assert (report.candidates, report.with_puzzles, report.filled) == (2, 1, 0)
    async with db_sessionmaker() as session:
        moves = await session.scalar(
            select(Game.moves_san).where(Game.lichess_id == "nullrow01")
        )
        assert moves is None


@pytest.mark.asyncio
async def test_backfill_is_idempotent(db_sessionmaker, monkeypatch):
    await _seed(db_sessionmaker, [("nullrow01", None)])
    _patch_client(monkeypatch, _ndjson_transport([{"id": "nullrow01", "moves": MOVES}]))

    first = await backfill_moves_san(db_sessionmaker, pause_s=0)
    second = await backfill_moves_san(db_sessionmaker, pause_s=0)

    assert first.filled == 1
    # Nothing left to do: a re-run after a crash picks up only what remains.
    assert (second.candidates, second.filled) == (0, 0)
