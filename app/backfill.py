"""One-off repair for Game.moves_san.

Games persisted between Phase 1 (2026-07-09) and the migration that added the
column (5de8f26ee5b7, 2026-07-13) have moves_san NULL, and nothing repairs
them: `_persist_game` returns early on a lichess_id it already knows rather
than filling in what is missing. The visible symptom is a puzzle with no
opponent's-move intro — no "Opponent played X.", no replay button, no
animation — because `_derive_last_move` has no movelist to replay and degrades
to `last_move: null` (logged there as `no_movelist`).

This is a data repair, not a code change: the deployed app already re-derives
last_move on every request, so repaired rows start showing their intro
immediately, with no deploy involved.

Run it locally against production. Note that Railway's own DATABASE_URL points
at `postgres.railway.internal`, which resolves only inside Railway's network —
from a laptop you need the public proxy URL (`railway variables`, look for
DATABASE_PUBLIC_URL). So `railway run` is *not* the invocation; this is:

    DATABASE_URL="postgresql://...proxy.rlwy.net:PORT/railway" \
        uv run python -m app.backfill --dry-run   # counts, no writes
    DATABASE_URL="..." uv run python -m app.backfill

If asyncpg rejects the connection over the proxy, append `?ssl=require` to the
URL. Safe to run against a live app: it only writes `moves_san`, a column the
request path reads and never writes. Safe to interrupt and re-run too — it only
ever writes a movelist onto a row that has none, and commits per batch, so a
second run picks up exactly what is left.
"""

import argparse
import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import async_session
from app.lichess import MAX_EXPORT_IDS, LichessRateLimited, fetch_games_by_ids
from app.models import Game, Puzzle

logger = logging.getLogger(__name__)

# Politeness pause between bulk requests. At 300 ids each, even the full ~4600-row
# backfill is ~16 requests, so this costs seconds in total.
BATCH_PAUSE_SECONDS = 2.0
# One retry pause when Lichess pushes back; the batch is then re-requested. Nothing
# is lost by waiting — completed batches are already committed.
RATE_LIMIT_BACKOFF_SECONDS = 65.0


@dataclass
class BackfillReport:
    candidates: int = 0  # rows with moves_san IS NULL
    with_puzzles: int = 0  # ...of which actually serve puzzles today
    filled: int = 0
    unrecoverable: list[str] = field(default_factory=list)  # lichess_ids Lichess won't serve


async def _survey(session) -> tuple[list[tuple[int, str]], int]:
    """Rows still missing a movelist, plus how many of them have puzzles.

    The puzzle count is reported but deliberately *not* used to narrow the work:
    a NULL row that is still unprocessed needs its movelist for the Stockfish
    worker (app/worker.py reads `game.moves_san or ""`), not only for the intro.
    """
    rows = (
        await session.execute(
            select(Game.id, Game.lichess_id).where(Game.moves_san.is_(None)).order_by(Game.id)
        )
    ).all()
    with_puzzles = await session.scalar(
        select(func.count(func.distinct(Game.id)))
        .select_from(Game)
        .join(Puzzle, Puzzle.game_id == Game.id)
        .where(Game.moves_san.is_(None))
    )
    return [(r.id, r.lichess_id) for r in rows], with_puzzles or 0


async def backfill_moves_san(
    sessionmaker: async_sessionmaker,
    *,
    dry_run: bool = False,
    pause_s: float = BATCH_PAUSE_SECONDS,
    backoff_s: float = RATE_LIMIT_BACKOFF_SECONDS,
) -> BackfillReport:
    async with sessionmaker() as session:
        candidates, with_puzzles = await _survey(session)

    report = BackfillReport(candidates=len(candidates), with_puzzles=with_puzzles)
    logger.info(
        "%d games missing a movelist (%d of them serving puzzles today)",
        report.candidates,
        report.with_puzzles,
    )
    if dry_run or not candidates:
        return report

    batches = [
        candidates[i : i + MAX_EXPORT_IDS] for i in range(0, len(candidates), MAX_EXPORT_IDS)
    ]
    for n, batch in enumerate(batches, start=1):
        ids = [lichess_id for _, lichess_id in batch]
        moves_by_id: dict[str, str] = {}
        for attempt in (1, 2):
            try:
                async for game in fetch_games_by_ids(ids):
                    if game.get("moves"):
                        moves_by_id[game["id"]] = game["moves"]
                break
            except LichessRateLimited:
                if attempt == 2:
                    raise
                logger.warning("rate limited on batch %d/%d — waiting %.0fs", n, len(batches), backoff_s)
                moves_by_id.clear()  # a partial stream must not be mistaken for a full one
                await asyncio.sleep(backoff_s)

        async with sessionmaker() as session:
            games = await session.scalars(
                select(Game).where(Game.id.in_([game_id for game_id, _ in batch]))
            )
            for game in games:
                moves = moves_by_id.get(game.lichess_id)
                if moves:
                    game.moves_san = moves
                    report.filled += 1
                else:
                    report.unrecoverable.append(game.lichess_id)
            await session.commit()

        logger.info(
            "batch %d/%d: filled %d, unrecoverable %d (running totals: %d / %d)",
            n,
            len(batches),
            len(moves_by_id),
            len(batch) - len(moves_by_id),
            report.filled,
            len(report.unrecoverable),
        )
        if n < len(batches):
            await asyncio.sleep(pause_s)

    return report


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = await backfill_moves_san(async_session, dry_run=args.dry_run)

    if args.dry_run:
        logger.info("dry run — nothing written")
        return
    logger.info("done: filled %d of %d", report.filled, report.candidates)
    if report.unrecoverable:
        # Left in place on purpose: their puzzles stay solvable, just without the
        # intro, and _derive_last_move now logs each one as `no_movelist`.
        logger.warning(
            "%d games Lichess would not serve (deleted/private): %s",
            len(report.unrecoverable),
            ", ".join(report.unrecoverable[:20])
            + (" ..." if len(report.unrecoverable) > 20 else ""),
        )


if __name__ == "__main__":
    asyncio.run(_main())
