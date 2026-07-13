"""Coverage-window sync tests (§13.2): forward fill vs backward fill on
`_sync_player_games`, exercised directly against an in-memory DB with a
call-capturing fake Lichess client."""

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import Game, Player
from app.routers.puzzles import _sync_player_games, _to_epoch_ms

REPO_ROOT = Path(__file__).parent.parent


def _game_dict(lichess_id: str, played_at: datetime) -> dict:
    return {
        "id": lichess_id,
        "variant": "standard",
        "speed": "blitz",
        "createdAt": _to_epoch_ms(played_at),
        "players": {
            "white": {"user": {"id": "syncuser", "name": "syncuser"}, "rating": 1500},
            "black": {"user": {"id": "opp", "name": "opp"}, "rating": 1500},
        },
        "moves": "e4 e5",
        "analysis": [{"eval": 0}, {"eval": 0}],
    }


def _game_row(player: Player, lichess_id: str, played_at: datetime) -> Game:
    return Game(
        lichess_id=lichess_id,
        player_id=player.id,
        player_color="white",
        player_rating=1500,
        opponent_name="opp",
        opponent_rating=1500,
        speed="blitz",
        played_at=played_at,
        raw_analysis_processed=True,
    )


def _capturing_fetch(batches: list[list[dict]]):
    """Fake fetch_games yielding one batch per call; records each call's kwargs."""
    calls: list[dict] = []

    async def fake(username, *, max_games=20, since=None, until=None, timeout=30.0, analysed=True):
        calls.append({"max_games": max_games, "since": since, "until": until, "timeout": timeout})
        batch = batches[len(calls) - 1] if len(calls) <= len(batches) else []
        for g in batch:
            yield g

    return fake, calls


async def _seed_player(db, games_at: list[tuple[str, datetime]]) -> Player:
    player = Player(username="syncuser", last_fetched_at=None)
    db.add(player)
    await db.flush()
    for lichess_id, played_at in games_at:
        db.add(_game_row(player, lichess_id, played_at))
    await db.commit()
    return player


NOW = datetime(2026, 7, 9, 12, 0, 0)


@pytest.mark.asyncio
async def test_forward_only_sync_never_sends_until(db_sessionmaker, monkeypatch):
    fake, calls = _capturing_fetch([[_game_dict("newgame001", NOW)]])
    monkeypatch.setattr("app.routers.puzzles.fetch_games", fake)

    async with db_sessionmaker() as db:
        player = await _seed_player(db, [("oldgame001", NOW - timedelta(days=3))])
        await _sync_player_games(db, player, "syncuser")

        assert len(calls) == 1
        assert calls[0]["until"] is None
        assert calls[0]["since"] == _to_epoch_ms(NOW - timedelta(days=3))
        # last20 flow must not touch the coverage window.
        assert player.history_fetched_until is None
        assert player.last_fetched_at is not None

        # Every persisted game carries its raw movelist since Phase 3.
        stored = await db.scalar(select(Game).where(Game.lichess_id == "newgame001"))
        assert stored.moves_san == "e4 e5"
        assert stored.eval_source == "lichess"


@pytest.mark.asyncio
async def test_forward_cap_hit_paginates_until_reconnected(db_sessionmaker, monkeypatch):
    # 20 games fill page 1; one older game hides below them, above the stored
    # anchor — the continuation page must fetch it so the pool stays contiguous.
    anchor_at = NOW - timedelta(days=30)
    page1 = [_game_dict(f"fwdcap{i:04d}", NOW - timedelta(hours=i)) for i in range(20)]
    page2 = [_game_dict("fwddeep001", NOW - timedelta(days=2))]
    fake, calls = _capturing_fetch([page1, page2])
    monkeypatch.setattr("app.routers.puzzles.fetch_games", fake)

    async with db_sessionmaker() as db:
        player = await _seed_player(db, [("oldgame001", anchor_at)])
        await _sync_player_games(db, player, "syncuser")

        assert len(calls) == 2
        assert calls[0]["max_games"] == 20
        assert calls[0]["until"] is None
        # Continuation: same since, bounded above by the oldest game received,
        # at the period page size and timeout.
        assert calls[1]["since"] == _to_epoch_ms(anchor_at)
        assert calls[1]["until"] == _to_epoch_ms(NOW - timedelta(hours=19))
        assert calls[1]["max_games"] == 300
        assert calls[1]["timeout"] == 60.0
        # Second page came in under its cap → gap closed, no honesty fallback.
        assert player.history_fetched_until is None
        ids = set((await db.scalars(select(Game.lichess_id))).all())
        assert "fwddeep001" in ids
        assert len(ids) == 22  # anchor + 20 + 1, nothing lost


@pytest.mark.asyncio
async def test_forward_full_page_without_since_does_not_paginate(db_sessionmaker, monkeypatch):
    # Fresh player: a full first page is just the initial last-N window, not a
    # gap — pagination must not chase the player's whole history.
    page1 = [_game_dict(f"fresh{i:05d}", NOW - timedelta(hours=i)) for i in range(20)]
    fake, calls = _capturing_fetch([page1, []])
    monkeypatch.setattr("app.routers.puzzles.fetch_games", fake)

    async with db_sessionmaker() as db:
        player = await _seed_player(db, [])
        await _sync_player_games(db, player, "syncuser")

        assert len(calls) == 1
        assert player.history_fetched_until is None


@pytest.mark.asyncio
async def test_forward_budget_exhaustion_moves_watermark_forward(db_sessionmaker, monkeypatch):
    # Every page arrives full through the whole budget → a residual hole may
    # remain, so the watermark must move *forward* to the oldest game received,
    # voiding the stale year-old claim (the one exception to never-shrink).
    monkeypatch.setattr("app.routers.puzzles.settings.max_games_mvp", 2)
    monkeypatch.setattr("app.routers.puzzles.settings.max_games_period_short", 2)
    monkeypatch.setattr("app.routers.puzzles.settings.forward_fill_max_pages", 2)

    anchor_at = NOW - timedelta(days=30)
    pages = [
        [_game_dict("fullp1a", NOW), _game_dict("fullp1b", NOW - timedelta(days=1))],
        [_game_dict("fullp2a", NOW - timedelta(days=2)), _game_dict("fullp2b", NOW - timedelta(days=3))],
        [_game_dict("fullp3a", NOW - timedelta(days=4)), _game_dict("fullp3b", NOW - timedelta(days=5))],
    ]
    fake, calls = _capturing_fetch(pages)
    monkeypatch.setattr("app.routers.puzzles.fetch_games", fake)

    async with db_sessionmaker() as db:
        player = await _seed_player(db, [("oldgame001", anchor_at)])
        player.history_fetched_until = NOW - timedelta(days=365)
        await _sync_player_games(db, player, "syncuser")

        assert len(calls) == 3  # page 1 + 2 continuation pages (budget)
        assert player.history_fetched_until == NOW - timedelta(days=5)


@pytest.mark.asyncio
async def test_forward_stalled_progress_breaks_and_falls_back(db_sessionmaker, monkeypatch):
    # A full continuation page whose oldest game equals its own `until` bound
    # can't shrink the window further — the loop must brake, not spin, and the
    # honesty fallback must claim only down to what arrived.
    monkeypatch.setattr("app.routers.puzzles.settings.max_games_mvp", 2)
    monkeypatch.setattr("app.routers.puzzles.settings.max_games_period_short", 2)

    anchor_at = NOW - timedelta(days=30)
    pile_up_at = NOW - timedelta(days=1)
    pages = [
        [_game_dict("stallp1a", NOW), _game_dict("stallp1b", pile_up_at)],
        [_game_dict("stallp2a", pile_up_at), _game_dict("stallp2b", pile_up_at)],
    ]
    fake, calls = _capturing_fetch(pages)
    monkeypatch.setattr("app.routers.puzzles.fetch_games", fake)

    async with db_sessionmaker() as db:
        player = await _seed_player(db, [("oldgame001", anchor_at)])
        await _sync_player_games(db, player, "syncuser")

        assert len(calls) == 2  # would-be third page has until == previous until
        assert player.history_fetched_until == pile_up_at


@pytest.mark.asyncio
async def test_backfill_until_prefers_watermark_over_oldest_stored(db_sessionmaker, monkeypatch):
    # Post-fallback state: watermark above older stored games (possibly-holey
    # region). Backfill must bound at the watermark so it re-scans the hole.
    watermark = NOW - timedelta(days=2)
    fake, calls = _capturing_fetch([[]])
    monkeypatch.setattr("app.routers.puzzles.fetch_games", fake)

    async with db_sessionmaker() as db:
        player = await _seed_player(db, [("oldgame001", NOW - timedelta(days=10))])
        player.history_fetched_until = watermark
        await _sync_player_games(
            db,
            player,
            "syncuser",
            forward=False,
            backfill_start=NOW - timedelta(days=30),
            backfill_cap=300,
        )

        assert calls[0]["until"] == _to_epoch_ms(watermark)
        assert player.history_fetched_until == NOW - timedelta(days=30)


@pytest.mark.asyncio
async def test_backfill_fetches_between_period_start_and_oldest_stored(
    db_sessionmaker, monkeypatch
):
    period_start = NOW - timedelta(days=365)
    oldest_stored = NOW - timedelta(days=3)
    fake, calls = _capturing_fetch(
        [[_game_dict("backg00001", NOW - timedelta(days=100))]]
    )
    monkeypatch.setattr("app.routers.puzzles.fetch_games", fake)

    async with db_sessionmaker() as db:
        player = await _seed_player(db, [("oldgame001", oldest_stored)])
        await _sync_player_games(
            db, player, "syncuser", forward=False, backfill_start=period_start, backfill_cap=300
        )

        assert len(calls) == 1
        assert calls[0]["since"] == _to_epoch_ms(period_start)
        assert calls[0]["until"] == _to_epoch_ms(oldest_stored)
        assert calls[0]["max_games"] == 300
        assert calls[0]["timeout"] == 60.0
        # Stream ended below the cap → full coverage back to the period start.
        assert player.history_fetched_until == period_start
        stored = await db.scalar(select(func.count()).select_from(Game))
        assert stored == 2


@pytest.mark.asyncio
async def test_backfill_cap_hit_only_claims_coverage_to_oldest_received(
    db_sessionmaker, monkeypatch
):
    period_start = NOW - timedelta(days=365)
    oldest_received = NOW - timedelta(days=40)
    batch = [
        _game_dict("capgame001", NOW - timedelta(days=20)),
        _game_dict("capgame002", NOW - timedelta(days=30)),
        _game_dict("capgame003", oldest_received),
    ]
    fake, _ = _capturing_fetch([batch])
    monkeypatch.setattr("app.routers.puzzles.fetch_games", fake)

    async with db_sessionmaker() as db:
        player = await _seed_player(db, [("oldgame001", NOW - timedelta(days=3))])
        await _sync_player_games(
            db,
            player,
            "syncuser",
            forward=False,
            backfill_start=period_start,
            backfill_cap=3,  # exactly what the fake returns → cap hit
        )

        # Honesty rule: the year was NOT fully fetched; claim only down to
        # the oldest game that actually arrived.
        assert player.history_fetched_until == oldest_received


@pytest.mark.asyncio
async def test_backfill_dedups_overlapping_games(db_sessionmaker, monkeypatch):
    period_start = NOW - timedelta(days=30)
    overlap_at = NOW - timedelta(days=3)
    fake, _ = _capturing_fetch(
        [[_game_dict("oldgame001", overlap_at), _game_dict("backg00001", NOW - timedelta(days=10))]]
    )
    monkeypatch.setattr("app.routers.puzzles.fetch_games", fake)

    async with db_sessionmaker() as db:
        player = await _seed_player(db, [("oldgame001", overlap_at)])
        await _sync_player_games(
            db, player, "syncuser", forward=False, backfill_start=period_start, backfill_cap=300
        )

        stored = await db.scalar(select(func.count()).select_from(Game))
        assert stored == 2  # the overlapping game was not duplicated


@pytest.mark.asyncio
async def test_backfill_never_shrinks_existing_coverage(db_sessionmaker, monkeypatch):
    already_covered_to = NOW - timedelta(days=365)
    fake, _ = _capturing_fetch([[]])
    monkeypatch.setattr("app.routers.puzzles.fetch_games", fake)

    async with db_sessionmaker() as db:
        player = await _seed_player(db, [("oldgame001", NOW - timedelta(days=3))])
        player.history_fetched_until = already_covered_to
        # A shorter-period backfill (e.g. explicit month request racing a TTL
        # refresh) must not move the coverage claim forward again.
        await _sync_player_games(
            db,
            player,
            "syncuser",
            forward=False,
            backfill_start=NOW - timedelta(days=30),
            backfill_cap=300,
        )

        assert player.history_fetched_until == already_covered_to


@pytest.mark.asyncio
async def test_backfill_without_stored_games_omits_until(db_sessionmaker, monkeypatch):
    fake, calls = _capturing_fetch([[], []])
    monkeypatch.setattr("app.routers.puzzles.fetch_games", fake)

    async with db_sessionmaker() as db:
        player = await _seed_player(db, [])
        await _sync_player_games(
            db,
            player,
            "syncuser",
            forward=True,
            backfill_start=NOW - timedelta(days=30),
            backfill_cap=300,
        )

        assert len(calls) == 2  # forward + backfill
        assert calls[1]["until"] is None  # nothing stored to bound the window


def test_migrations_apply_to_fresh_sqlite(tmp_path):
    db_path = tmp_path / "fresh.db"
    env = os.environ | {"DATABASE_URL": f"sqlite+aiosqlite:///{db_path}"}
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        check=True,
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
    )
    assert db_path.exists()
