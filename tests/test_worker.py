"""Worker tests with a faked engine — no Stockfish binary needed.

Job-level tests patch `app.worker.analyse_and_extract` (the orchestration
seam); the two-pass logic itself is tested directly with patched
`app.worker.analyse_game` / `app.worker.refine_plies` (the names imported
into the worker module, per the project's monkeypatch convention).
"""

import json
from datetime import datetime, timedelta

import chess.engine
import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Game, Job, Player, Puzzle
from app.worker import (
    _utcnow,
    analyse_and_extract,
    games_analyzed_today,
    reset_stale_jobs,
    service_one_game,
)

# A legal game whose ply 12 (7. Qe3??) hangs the queen — same sequence the
# engine tests use, giving build_puzzle a real position to replay.
MOVES = "e4 e5 Nf3 Nc6 Bc4 Bc5 Nc3 Nf6 d3 d6 Qe2 Bg4 Qe3"
BLUNDER_PLY = 12

NOW = datetime(2026, 7, 13, 12, 0, 0)


def _fake_puzzle(ply: int = BLUNDER_PLY) -> dict:
    return {
        "ply": ply,
        "fen": "fake-fen",
        "side_to_move": "white",
        "solution_uci": "h2h3",
        "solution_san": "h3",
        "played_uci": "e2e3",
        "played_san": "Qe3",
        "variation_san": ["h3", "Bh5"],
        "win_drop": 41.0,
        "eval_before_cp": 0,
        "eval_after_cp": -600,
    }


async def _run_until_idle(sessionmaker) -> None:
    while await service_one_game(sessionmaker):
        pass


async def _seed(
    db, n_unprocessed: int, *, username: str = "engineuser", color: str = "white"
) -> Player:
    player = Player(username=username)
    db.add(player)
    await db.flush()
    for i in range(n_unprocessed):
        db.add(
            Game(
                lichess_id=f"{username[:6]}{i:04d}",
                player_id=player.id,
                player_color=color,
                player_rating=1500,
                opponent_name="opp",
                opponent_rating=1500,
                speed="blitz",
                played_at=NOW - timedelta(days=i),
                raw_analysis_processed=False,
                eval_source="stockfish",
                moves_san=MOVES,
            )
        )
    await db.commit()
    return player


async def _queue_job(
    db, player: Player, total: int, period_start: datetime | None = None
) -> Job:
    job = Job(
        player_id=player.id,
        status="queued",
        total=total,
        period_start=period_start,
        created_at=_utcnow(),
    )
    db.add(job)
    await db.commit()
    return job


def _patch_extract(monkeypatch, side_effects):
    """Patch analyse_and_extract; side_effects yields results or raises."""
    calls = []

    async def fake(moves_san_str, color):
        calls.append((moves_san_str, color))
        effect = side_effects[min(len(calls) - 1, len(side_effects) - 1)]
        if isinstance(effect, Exception):
            raise effect
        return effect

    monkeypatch.setattr("app.worker.analyse_and_extract", fake)
    return calls


async def test_single_job_happy_path(db_sessionmaker, monkeypatch):
    entries = [{"eval": 0}]
    _patch_extract(monkeypatch, [(entries, [_fake_puzzle()])])

    async with db_sessionmaker() as db:
        player = await _seed(db, 3)
        job = await _queue_job(db, player, total=3)

    await _run_until_idle(db_sessionmaker)

    async with db_sessionmaker() as db:
        job = await db.get(Job, job.id)
        assert job.status == "done"
        assert job.progress == 3
        assert job.error is None
        games = (await db.scalars(select(Game))).all()
        assert all(g.raw_analysis_processed for g in games)
        assert all(g.analyzed_at is not None for g in games)
        assert all(g.analysis_json == json.dumps(entries) for g in games)
        puzzles = (await db.scalars(select(Puzzle))).all()
        assert len(puzzles) == 3  # one per game
        assert {p.game_id for p in puzzles} == {g.id for g in games}
        assert puzzles[0].variation_san == "h3 Bh5"
        assert puzzles[0].kind == "blunder"


async def test_games_processed_newest_first(db_sessionmaker, monkeypatch):
    _patch_extract(monkeypatch, [([{"eval": 0}], [])])

    async with db_sessionmaker() as db:
        player = await _seed(db, 3)  # game i is i days old
        job = await _queue_job(db, player, total=1)  # only the first gets done

    await _run_until_idle(db_sessionmaker)

    async with db_sessionmaker() as db:
        processed = (
            await db.scalars(select(Game).where(Game.raw_analysis_processed))
        ).all()
        assert [g.lichess_id for g in processed] == ["engine0000"]  # the newest


async def test_worker_skips_out_of_period_games(db_sessionmaker, monkeypatch):
    _patch_extract(monkeypatch, [([{"eval": 0}], [])])

    async with db_sessionmaker() as db:
        player = await _seed(db, 3)  # game i is i days old
        # Scope covers the day-0 and day-1 games only.
        job = await _queue_job(
            db, player, total=2, period_start=NOW - timedelta(days=1, hours=12)
        )

    await _run_until_idle(db_sessionmaker)

    async with db_sessionmaker() as db:
        job = await db.get(Job, job.id)
        assert job.status == "done"
        assert job.progress == 2
        out_of_scope = await db.scalar(
            select(Game).where(Game.lichess_id == "engine0002")
        )
        assert out_of_scope.raw_analysis_processed is False  # never touched


async def test_worker_marks_done_when_in_period_set_exhausted(
    db_sessionmaker, monkeypatch
):
    _patch_extract(monkeypatch, [([{"eval": 0}], [])])

    async with db_sessionmaker() as db:
        player = await _seed(db, 3)
        # total overshoots the in-scope set (2 games): the worker must finish
        # via the game-is-None branch, not stall or leak out of scope.
        job = await _queue_job(
            db, player, total=3, period_start=NOW - timedelta(days=1, hours=12)
        )

    await _run_until_idle(db_sessionmaker)

    async with db_sessionmaker() as db:
        job = await db.get(Job, job.id)
        assert job.status == "done"
        assert job.progress == 2
        out_of_scope = await db.scalar(
            select(Game).where(Game.lichess_id == "engine0002")
        )
        assert out_of_scope.raw_analysis_processed is False


async def test_midjob_crash_keeps_committed_games(db_sessionmaker, monkeypatch):
    _patch_extract(
        monkeypatch,
        [
            ([{"eval": 0}], [_fake_puzzle()]),
            ([{"eval": 0}], [_fake_puzzle()]),
            RuntimeError("boom"),
        ],
    )

    async with db_sessionmaker() as db:
        player = await _seed(db, 3)
        job = await _queue_job(db, player, total=3)

    await _run_until_idle(db_sessionmaker)

    async with db_sessionmaker() as db:
        job = await db.get(Job, job.id)
        assert job.status == "failed"
        assert job.error == "internal_error"
        assert job.progress == 2  # per-game commits survived the crash
        assert len((await db.scalars(select(Puzzle))).all()) == 2


async def test_engine_failure_fails_job_and_leaves_games_unprocessed(
    db_sessionmaker, monkeypatch
):
    _patch_extract(
        monkeypatch,
        [
            ([{"eval": 0}], []),
            chess.engine.EngineTerminatedError("engine died"),
        ],
    )

    async with db_sessionmaker() as db:
        player = await _seed(db, 2)
        job = await _queue_job(db, player, total=2)

    await _run_until_idle(db_sessionmaker)

    async with db_sessionmaker() as db:
        job = await db.get(Job, job.id)
        assert job.status == "failed"
        assert job.error == "engine_error"
        assert job.progress == 1
        unprocessed = (
            await db.scalars(select(Game).where(~Game.raw_analysis_processed))
        ).all()
        assert len(unprocessed) == 1  # stays queued for the next search's job


async def test_engine_failure_still_fails_job_when_quit_raises(
    db_sessionmaker, monkeypatch
):
    # Quitting an already-dead engine can itself raise; the job must still be
    # failed — otherwise it stays pending and the worker retries it forever,
    # with no daily fuse to stop it (only completed games count against it).
    _patch_extract(monkeypatch, [chess.engine.EngineTerminatedError("engine died")])

    async def broken_quit():
        raise chess.engine.EngineTerminatedError("engine event loop dead")

    monkeypatch.setattr("app.worker.engine_handle.quit", broken_quit)

    async with db_sessionmaker() as db:
        player = await _seed(db, 1)
        job = await _queue_job(db, player, total=1)

    await _run_until_idle(db_sessionmaker)

    async with db_sessionmaker() as db:
        job = await db.get(Job, job.id)
        assert job.status == "failed"
        assert job.error == "engine_error"


async def test_missing_binary_classified_as_engine_error(db_sessionmaker, monkeypatch):
    # popen_uci raises FileNotFoundError (a plain OSError, not an EngineError)
    # when the stockfish binary is absent — still an engine failure to the user.
    _patch_extract(monkeypatch, [FileNotFoundError("stockfish not found")])

    async with db_sessionmaker() as db:
        player = await _seed(db, 1)
        job = await _queue_job(db, player, total=1)

    await _run_until_idle(db_sessionmaker)

    async with db_sessionmaker() as db:
        job = await db.get(Job, job.id)
        assert job.status == "failed"
        assert job.error == "engine_error"


async def test_unparseable_moves_skipped_job_continues(db_sessionmaker, monkeypatch):
    _patch_extract(
        monkeypatch,
        [
            ValueError("illegal san"),
            ([{"eval": 0}], [_fake_puzzle()]),
        ],
    )

    async with db_sessionmaker() as db:
        player = await _seed(db, 2)
        job = await _queue_job(db, player, total=2)

    await _run_until_idle(db_sessionmaker)

    async with db_sessionmaker() as db:
        job = await db.get(Job, job.id)
        assert job.status == "done"
        assert job.progress == 2
        assert len((await db.scalars(select(Puzzle))).all()) == 1
        games = (await db.scalars(select(Game))).all()
        assert all(g.raw_analysis_processed for g in games)


async def test_global_fuse_trips_before_first_game(db_sessionmaker, monkeypatch):
    monkeypatch.setattr(settings, "max_engine_games_per_day", 2)
    _patch_extract(monkeypatch, [([{"eval": 0}], [])])

    async with db_sessionmaker() as db:
        # Another player's games already burned today's budget.
        other = await _seed(db, 2, username="otheruser")
        for g in (await db.scalars(select(Game))).all():
            g.raw_analysis_processed = True
            g.analyzed_at = _utcnow()
        await db.commit()
        player = await _seed(db, 1)
        job = await _queue_job(db, player, total=1)

    await _run_until_idle(db_sessionmaker)

    async with db_sessionmaker() as db:
        job = await db.get(Job, job.id)
        assert job.status == "failed"
        assert job.error == "daily_budget_reached"
        assert job.progress == 0
        untouched = await db.scalar(
            select(Game).where(Game.player_id == job.player_id)
        )
        assert not untouched.raw_analysis_processed


async def test_global_fuse_trips_mid_job(db_sessionmaker, monkeypatch):
    monkeypatch.setattr(settings, "max_engine_games_per_day", 1)
    _patch_extract(monkeypatch, [([{"eval": 0}], [])])

    async with db_sessionmaker() as db:
        player = await _seed(db, 2)
        job = await _queue_job(db, player, total=2)

    await _run_until_idle(db_sessionmaker)

    async with db_sessionmaker() as db:
        job = await db.get(Job, job.id)
        assert job.status == "failed"
        assert job.error == "daily_budget_reached"
        assert job.progress == 1  # first game landed before the fuse tripped


async def test_player_fuse_is_per_player(db_sessionmaker, monkeypatch):
    monkeypatch.setattr(settings, "max_engine_games_per_day_per_player", 1)
    _patch_extract(monkeypatch, [([{"eval": 0}], [])])

    async with db_sessionmaker() as db:
        # This player already used their share today; global budget is fine.
        player = await _seed(db, 2)
        first = await db.scalar(select(Game).order_by(Game.id))
        first.raw_analysis_processed = True
        first.analyzed_at = _utcnow()
        await db.commit()
        job = await _queue_job(db, player, total=1)

        other = await _seed(db, 1, username="otheruser")
        other_job = await _queue_job(db, other, total=1)

    await _run_until_idle(db_sessionmaker)

    async with db_sessionmaker() as db:
        job = await db.get(Job, job.id)
        assert job.status == "failed"
        assert job.error == "player_budget_reached"
        # The other player's job is unaffected by this player's spent share.
        other_job = await db.get(Job, other_job.id)
        assert other_job.status == "done"


async def test_second_pending_job_for_player_rejected_by_db(db_sessionmaker):
    # The partial unique index enforces at most one queued/running job per
    # player — the invariant _ensure_job's check-then-insert relies on.
    from sqlalchemy.exc import IntegrityError

    async with db_sessionmaker() as db:
        player = await _seed(db, 0)
        await _queue_job(db, player, total=1)
        # Terminal statuses don't count against the index.
        done = Job(player_id=player.id, status="done", total=1, created_at=_utcnow())
        db.add(done)
        await db.commit()

        dup = Job(player_id=player.id, status="queued", total=1, created_at=_utcnow())
        db.add(dup)
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


async def test_reset_stale_jobs_requeues_running(db_sessionmaker):
    async with db_sessionmaker() as db:
        player = await _seed(db, 0)
        job = Job(player_id=player.id, status="running", total=5, created_at=_utcnow())
        done = Job(player_id=player.id, status="done", total=1, created_at=_utcnow())
        db.add_all([job, done])
        await db.commit()

    await reset_stale_jobs(db_sessionmaker)

    async with db_sessionmaker() as db:
        assert (await db.get(Job, job.id)).status == "queued"
        assert (await db.get(Job, done.id)).status == "done"


# --- round-robin scheduling ---------------------------------------------------
# The fake records (moves, color) per call, so giving each player a distinct
# color makes the service order directly observable.


async def test_round_robin_alternates_between_jobs(db_sessionmaker, monkeypatch):
    calls = _patch_extract(monkeypatch, [([{"eval": 0}], [])])

    async with db_sessionmaker() as db:
        a = await _seed(db, 2, username="whiteuser", color="white")
        b = await _seed(db, 2, username="blackuser", color="black")
        job_a = await _queue_job(db, a, total=2)
        job_b = await _queue_job(db, b, total=2)

    for _ in range(4):
        assert await service_one_game(db_sessionmaker)

    # Min-progress with oldest-id tie-break: A, B, A, B — never A-to-completion.
    assert [c[1] for c in calls] == ["white", "black", "white", "black"]

    async with db_sessionmaker() as db:
        assert (await db.get(Job, job_a.id)).status == "done"
        assert (await db.get(Job, job_b.id)).status == "done"


async def test_new_job_catches_up_before_older_job_continues(db_sessionmaker, monkeypatch):
    calls = _patch_extract(monkeypatch, [([{"eval": 0}], [])])

    async with db_sessionmaker() as db:
        a = await _seed(db, 2, username="whiteuser", color="white")
        job_a = await _queue_job(db, a, total=2)

    assert await service_one_game(db_sessionmaker)  # A leads 1-0

    async with db_sessionmaker() as db:
        b = await _seed(db, 2, username="blackuser", color="black")
        await _queue_job(db, b, total=2)

    # The fresh job (progress 0) is served before the older one's next game —
    # this is the "second search shows progress within seconds" property.
    assert await service_one_game(db_sessionmaker)
    assert await service_one_game(db_sessionmaker)
    assert [c[1] for c in calls] == ["white", "black", "white"]

    async with db_sessionmaker() as db:
        assert (await db.get(Job, job_a.id)).status == "done"


async def test_first_service_marks_job_running_and_last_marks_done(
    db_sessionmaker, monkeypatch
):
    _patch_extract(monkeypatch, [([{"eval": 0}], [])])

    async with db_sessionmaker() as db:
        player = await _seed(db, 2)
        job = await _queue_job(db, player, total=2)

    assert await service_one_game(db_sessionmaker)
    async with db_sessionmaker() as db:
        assert (await db.get(Job, job.id)).status == "running"

    assert await service_one_game(db_sessionmaker)
    async with db_sessionmaker() as db:
        # done lands in the same commit as the final game — no lingering
        # running-complete state between service calls.
        refreshed = await db.get(Job, job.id)
        assert refreshed.status == "done"
        assert refreshed.progress == 2

    assert not await service_one_game(db_sessionmaker)  # idle


async def test_games_analyzed_today_ignores_yesterday(db_sessionmaker):
    async with db_sessionmaker() as db:
        player = await _seed(db, 2)
        games = (await db.scalars(select(Game))).all()
        games[0].analyzed_at = _utcnow()
        games[1].analyzed_at = _utcnow() - timedelta(days=1)
        await db.commit()

        assert await games_analyzed_today(db) == 1
        assert await games_analyzed_today(db, player.id) == 1
        assert await games_analyzed_today(db, player.id + 999) == 0


# --- two-pass orchestration (analyse_and_extract itself) ---------------------


def _entries_flagging_blunder(win_drop_eval: int) -> list[dict]:
    """Sweep-shaped entries for MOVES where only ply 12 carries a big drop."""
    entries: list[dict] = []
    for ply in range(len(MOVES.split())):
        entry: dict = {"eval": 0, "best": "h2h3", "variation": "h3"}
        if ply == BLUNDER_PLY:
            entry = {"eval": win_drop_eval, "best": "h2h3", "variation": "h3"}
        entries.append(entry)
    return entries


async def test_two_pass_refines_solution_and_keeps_blunder(monkeypatch):
    cheap = _entries_flagging_blunder(-600)
    refined = [dict(e) for e in cheap]
    refined[BLUNDER_PLY] = {"eval": -550, "best": "a2a3", "variation": "a3"}
    refine_calls = []

    async def fake_sweep(moves_san):
        return cheap

    async def fake_refine(moves_san, entries, plies):
        refine_calls.append(plies)
        return refined

    monkeypatch.setattr("app.worker.analyse_game", fake_sweep)
    monkeypatch.setattr("app.worker.refine_plies", fake_refine)

    merged, puzzles = await analyse_and_extract(MOVES, "white")

    assert refine_calls == [[BLUNDER_PLY]]  # only the flagged ply re-analyzed
    assert merged == refined
    assert len(puzzles) == 1
    assert puzzles[0]["ply"] == BLUNDER_PLY
    assert puzzles[0]["solution_uci"] == "a2a3"  # the refined solution is stored


async def test_two_pass_drops_ply_refined_below_threshold(monkeypatch):
    cheap = _entries_flagging_blunder(-600)
    refined = [dict(e) for e in cheap]
    refined[BLUNDER_PLY] = {"eval": 0, "best": "h2h3", "variation": "h3"}  # not a blunder after all

    async def fake_sweep(moves_san):
        return cheap

    async def fake_refine(moves_san, entries, plies):
        return refined

    monkeypatch.setattr("app.worker.analyse_game", fake_sweep)
    monkeypatch.setattr("app.worker.refine_plies", fake_refine)

    _, puzzles = await analyse_and_extract(MOVES, "white")
    assert puzzles == []


async def test_no_candidates_skips_refinement(monkeypatch):
    quiet = [{"eval": 0, "best": "h2h3", "variation": "h3"} for _ in MOVES.split()]

    async def fake_sweep(moves_san):
        return quiet

    async def fail_refine(*args, **kwargs):
        raise AssertionError("refine_plies must not run without candidates")

    monkeypatch.setattr("app.worker.analyse_game", fake_sweep)
    monkeypatch.setattr("app.worker.refine_plies", fail_refine)

    merged, puzzles = await analyse_and_extract(MOVES, "white")
    assert merged == quiet
    assert puzzles == []
