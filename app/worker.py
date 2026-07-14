"""Background Stockfish worker (§14.1): a single asyncio task in the app lifespan.

Scheduling is round-robin per *game*, not FIFO per job: each cycle services one
game from the pending job with the lowest progress (tie-break: oldest id). A
fresh search has progress 0, so it starts showing progress within seconds and
catches up, then concurrent jobs alternate game-by-game — without this, a
second search sat on a contextless "waiting" banner until the first job
finished entirely. Trade-offs, accepted: concurrent jobs share the single
engine's speed, and a stream of brand-new jobs briefly starves an older
half-done job while they catch up (bounded by the daily fuse). Commits are per
game, so puzzles appear incrementally and a crash loses at most one game. No
Celery, no Redis — one process.

Budget fuses live here and only here: checked before each game, never at job
creation. A tripped fuse fails the job with a machine-readable error; the
remaining games stay unprocessed, so the next search simply re-queues them —
self-healing, no scheduler. Under round-robin a per-player trip fails only
that player's job; other jobs keep being serviced.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime

import chess.engine
from sqlalchemy import false, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis import extract_puzzles_for_color, find_blunder_plies_for_color
from app.config import settings
from app.engine import analyse_game, engine_handle, refine_plies
from app.models import Game, Job, Puzzle

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    # Naive UTC, same convention as app/routers/puzzles.py.
    return datetime.now(UTC).replace(tzinfo=None)


def _utc_midnight() -> datetime:
    return _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


async def reset_stale_jobs(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Re-queue jobs left 'running' by a container restart mid-job."""
    async with sessionmaker() as db:
        await db.execute(update(Job).where(Job.status == "running").values(status="queued"))
        await db.commit()


async def games_analyzed_today(db: AsyncSession, player_id: int | None = None) -> int:
    """Fuse accounting: engine-analyzed games since UTC midnight (§14.1).

    Counting Game.analyzed_at rather than summing job progress keeps the
    accounting exact across restarts and attributes midnight-spanning jobs
    to the day each game was actually analyzed.
    """
    query = select(func.count()).select_from(Game).where(Game.analyzed_at >= _utc_midnight())
    if player_id is not None:
        query = query.where(Game.player_id == player_id)
    return await db.scalar(query) or 0


async def analyse_and_extract(
    moves_san_str: str, color: str
) -> tuple[list[dict], list[dict]]:
    """Two-pass analysis (§14.1): returns (merged_entries, puzzle_dicts).

    Cheap sweep detects candidate blunders; only those plies are re-analyzed at
    the higher refine budget (firming up the solution move — the one
    user-noticeable weakness of a 0.1s sweep). Detection re-runs on the merged
    entries, keeping only cheap-pass candidates that still cross the threshold:
    a ply that only *becomes* a blunder post-merge has no refined solution
    move, so it is deliberately ignored rather than cascading another pass.
    """
    moves_san = moves_san_str.split()
    entries = await analyse_game(moves_san)
    candidates = find_blunder_plies_for_color(
        entries, moves_san, color, settings.min_win_drop_stored
    )
    if not candidates:
        return entries, []
    flagged = [ply for ply, _ in candidates]
    merged = await refine_plies(moves_san, entries, flagged)
    candidate_set = set(flagged)
    puzzles = [
        p
        for p in extract_puzzles_for_color(
            merged, moves_san_str, color, settings.min_win_drop_stored
        )
        if p["ply"] in candidate_set
    ]
    return merged, puzzles


async def _fail_job(db: AsyncSession, job: Job, error: str) -> None:
    job.status = "failed"
    job.error = error
    await db.commit()


async def pick_job(db: AsyncSession) -> Job | None:
    """The pending job to service next (round-robin, see module docstring).

    Several jobs may sit in "running" at once; one game at a time still holds
    globally. Single uvicorn process on a single Railway replica: no claim
    race. Horizontal scaling would need SELECT ... FOR UPDATE SKIP LOCKED.
    """
    return await db.scalar(
        select(Job)
        .where(Job.status.in_(("queued", "running")))
        .order_by(Job.progress.asc(), Job.id.asc())
        .limit(1)
    )


async def service_one_game(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    """Process one game of the least-progressed pending job.

    Returns False when no pending job exists (the worker is idle).
    """
    async with sessionmaker() as db:
        job = await pick_job(db)
        if job is None:
            return False
        job_id = job.id
        if job.status == "queued":
            job.status = "running"  # committed together with the outcome below
        try:
            if await games_analyzed_today(db) >= settings.max_engine_games_per_day:
                await _fail_job(db, job, "daily_budget_reached")
                return True
            if (
                await games_analyzed_today(db, job.player_id)
                >= settings.max_engine_games_per_day_per_player
            ):
                await _fail_job(db, job, "player_budget_reached")
                return True

            game = None
            if job.progress < job.total:
                game_query = (
                    select(Game)
                    .where(Game.player_id == job.player_id)
                    .where(Game.raw_analysis_processed == false())
                    .order_by(Game.played_at.desc())
                    .limit(1)
                )
                if job.period_start is not None:
                    # Scoped job (§14.1): only games the search that queued
                    # it can display. NULL scope = whole pool.
                    game_query = game_query.where(Game.played_at >= job.period_start)
                game = await db.scalar(game_query)
            if game is None:
                job.status = "done"
                await db.commit()
                return True

            try:
                merged, puzzles = await analyse_and_extract(
                    game.moves_san or "", game.player_color
                )
            except ValueError:
                # Unparseable movelist: mark it processed with no puzzles so
                # it can't clog the queue, and keep going.
                logger.warning(
                    "worker: unparseable moves in game %s; skipped", game.lichess_id
                )
                merged, puzzles = None, []

            for p in puzzles:
                db.add(
                    Puzzle(
                        game_id=game.id,
                        ply=p["ply"],
                        fen=p["fen"],
                        side_to_move=p["side_to_move"],
                        solution_uci=p["solution_uci"],
                        solution_san=p["solution_san"],
                        played_uci=p["played_uci"],
                        played_san=p["played_san"],
                        variation_san=" ".join(p["variation_san"]),
                        win_drop=p["win_drop"],
                        eval_before_cp=p["eval_before_cp"],
                        eval_after_cp=p["eval_after_cp"],
                    )
                )
            # Flag + puzzles flip in the same commit, so a crash either loses
            # this one game entirely (retried by the next job) or lands it
            # completely — the (game_id, ply) unique constraint can never be
            # violated by re-processing. The final game also flips the job to
            # done in the same commit, so jobs never linger running-complete.
            game.raw_analysis_processed = True
            game.analyzed_at = _utcnow()
            if merged is not None:
                game.analysis_json = json.dumps(merged)
            job.progress += 1
            if job.progress >= job.total:
                job.status = "done"
            await db.commit()
        except asyncio.CancelledError:
            raise  # lifespan shutdown; startup stale-reset re-queues this job
        except Exception as exc:
            # FileNotFoundError is the missing-binary spawn failure — engine-
            # classified even though popen_uci raises it as a plain OSError.
            engine_failure = isinstance(
                exc,
                (chess.engine.EngineTerminatedError, chess.engine.EngineError, FileNotFoundError),
            )
            if engine_failure:
                logger.error("worker: engine failure in job %s: %s", job_id, exc)
                try:
                    await engine_handle.quit()  # drop the broken process; next use respawns
                except Exception:
                    # The handle already forgot the process; failing to quit a
                    # dead engine must not skip failing the job below.
                    logger.warning("worker: quitting broken engine failed", exc_info=True)
            else:
                logger.exception("worker: job %s crashed", job_id)
            await db.rollback()
            job = await db.get(Job, job_id)
            if job is not None:
                await _fail_job(db, job, "engine_error" if engine_failure else "internal_error")
        return True


async def worker_loop(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Single-flight worker; must never die silently."""
    while True:
        try:
            if await service_one_game(sessionmaker):
                continue  # more work may be pending — keep going immediately
            await engine_handle.quit_if_idle(settings.engine_idle_quit_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("worker: loop iteration failed")
        await asyncio.sleep(settings.worker_poll_seconds)
