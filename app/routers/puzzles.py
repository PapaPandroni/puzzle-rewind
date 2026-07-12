import random
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import chess
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.analysis import (
    determine_player_color,
    extract_puzzles,
    move_delivers_checkmate,
    mover_moves_in_line,
    preset_for_rating,
    variation_board,
    variation_move_uci,
)
from app.config import settings
from app.database import get_db
from app.lichess import LichessRateLimited, LichessUserNotFound, fetch_games
from app.models import Game, Player, Puzzle
from app.rate_limit import limiter
from app.schemas import AttemptRequest, AttemptResponse, PuzzleSetResponse, PuzzleSummary

router = APIRouter()

UsernamePath = Annotated[str, Path(pattern=r"^[a-zA-Z0-9_-]{2,30}$")]
Preset = Literal["auto", "beginner", "intermediate", "advanced", "expert", "custom"]
Period = Literal["last20", "day", "week", "month", "year", "all"]

_PERIOD_LENGTHS: dict[str, timedelta] = {
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}


def _utcnow() -> datetime:
    # Stored naive (UTC by convention) — SQLite silently drops tzinfo on read, so
    # comparisons must stay naive on both sides regardless of backend.
    return datetime.now(UTC).replace(tzinfo=None)


def _period_start(period: Period) -> datetime | None:
    """Start of the requested window; None for last20 (whole accumulated pool)."""
    if period == "last20":
        return None
    if period == "all":
        return datetime(1970, 1, 1)
    return _utcnow() - _PERIOD_LENGTHS[period]


def _period_cap(period: Period) -> int:
    if period in ("year", "all"):
        return settings.max_games_period_long
    return settings.max_games_period_short


def _game_url(game: Game, ply: int) -> str:
    suffix = "/black" if game.player_color == "black" else ""
    return f"https://lichess.org/{game.lichess_id}{suffix}#{ply}"


def _to_epoch_ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=UTC).timestamp() * 1000)


async def _persist_game(db: AsyncSession, player: Player, username: str, game: dict) -> bool:
    """Store one exported game + its puzzle candidates; False if skipped or known."""
    existing = await db.scalar(select(Game).where(Game.lichess_id == game["id"]))
    if existing is not None:
        return False

    color = determine_player_color(game, username)
    if color is None:
        return False

    opponent_color = "black" if color == "white" else "white"
    # Games vs the Lichess AI (or other non-human opponents) omit "rating"
    # entirely instead of using a sentinel value — skip, since Game.opponent_rating
    # is non-nullable and these aren't meaningful puzzle sources anyway.
    if "rating" not in game["players"][opponent_color]:
        return False

    game_row = Game(
        lichess_id=game["id"],
        player_id=player.id,
        player_color=color,
        player_rating=game["players"][color]["rating"],
        opponent_name=game["players"][opponent_color].get("user", {}).get("name", "?"),
        opponent_rating=game["players"][opponent_color]["rating"],
        speed=game["speed"],
        played_at=datetime.fromtimestamp(game["createdAt"] / 1000, tz=UTC).replace(tzinfo=None),
        raw_analysis_processed=True,
        eval_source="lichess",
        moves_san=game.get("moves") or None,
    )
    db.add(game_row)
    await db.flush()  # assign game_row.id

    for puzzle_data in extract_puzzles(game, username, settings.min_win_drop_stored):
        db.add(
            Puzzle(
                game_id=game_row.id,
                ply=puzzle_data["ply"],
                fen=puzzle_data["fen"],
                side_to_move=puzzle_data["side_to_move"],
                solution_uci=puzzle_data["solution_uci"],
                solution_san=puzzle_data["solution_san"],
                played_uci=puzzle_data["played_uci"],
                played_san=puzzle_data["played_san"],
                variation_san=" ".join(puzzle_data["variation_san"]),
                win_drop=puzzle_data["win_drop"],
                eval_before_cp=puzzle_data["eval_before_cp"],
                eval_after_cp=puzzle_data["eval_after_cp"],
            )
        )
    return True


async def _stream_and_persist(
    db: AsyncSession,
    player: Player,
    username: str,
    *,
    max_games: int,
    since: int | None,
    until: int | None,
    timeout: float = 30.0,
) -> tuple[int, datetime | None]:
    """Fetch one export page and persist it: (games received, oldest createdAt seen).

    `oldest_received` is a min over the batch rather than "last yielded" so it
    doesn't depend on Lichess's newest-first stream order.
    """
    received = 0
    oldest_received: datetime | None = None
    async for game in fetch_games(
        username, max_games=max_games, since=since, until=until, timeout=timeout
    ):
        received += 1
        played_at = datetime.fromtimestamp(game["createdAt"] / 1000, tz=UTC).replace(tzinfo=None)
        if oldest_received is None or played_at < oldest_received:
            oldest_received = played_at
        await _persist_game(db, player, username, game)
    return received, oldest_received


async def _sync_player_games(
    db: AsyncSession,
    player: Player,
    username: str,
    *,
    forward: bool = True,
    backfill_start: datetime | None = None,
    backfill_cap: int = 0,
) -> str | None:
    """Fetch games from Lichess and persist Game/Puzzle rows (§7 cache flow, §13.2).

    Two independent directions: `forward` tops up from the newest stored game
    (TTL-gated by the caller) and `backfill_start` extends the coverage window
    backwards to that point, fetching at most `backfill_cap` games between it
    and the coverage bottom. `lichess_id` uniqueness makes overlap harmless.

    Contiguity invariant: stored games form one gap-free range from the coverage
    bottom (`history_fetched_until` if set, else the oldest stored game) up to
    the newest stored game. The forward branch paginates on a full page to keep
    the top edge gap-free; the backfill branch only ever extends the bottom edge.

    Returns a "reason" string for empty results (no analyzed games / user has
    none), or None. Raises LichessUserNotFound / LichessRateLimited upstream.
    """
    fetched_any = False
    since_ms: int | None = None

    if forward:
        latest = await db.scalar(
            select(Game.played_at).where(Game.player_id == player.id).order_by(Game.played_at.desc())
        )
        if latest is not None:
            since_ms = _to_epoch_ms(latest)

        received, oldest_received = await _stream_and_persist(
            db, player, username, max_games=settings.max_games_mvp, since=since_ms, until=None
        )
        fetched_any = received > 0
        page_full = received >= settings.max_games_mvp

        # A full page with a `since` bound means games may be hiding between the
        # newest stored game and the oldest just received. Page downward until
        # reconnected with stored history — otherwise a hole opens that neither
        # this branch (always `since = newest stored`) nor the backfill (bounded
        # by the coverage bottom) would ever revisit. Without `since` (fresh
        # player) a full page is just the initial last-N window — no gap to chase.
        pages_left = settings.forward_fill_max_pages
        until_ms: int | None = None
        while since_ms is not None and page_full and pages_left > 0 and oldest_received is not None:
            next_until_ms = _to_epoch_ms(oldest_received)
            if until_ms is not None and next_until_ms >= until_ms:
                break  # no strict progress (timestamp pile-up) — bail to the fallback
            until_ms = next_until_ms
            pages_left -= 1
            received, oldest_received = await _stream_and_persist(
                db,
                player,
                username,
                max_games=settings.max_games_period_short,
                since=since_ms,
                until=until_ms,
                timeout=settings.period_fetch_timeout_seconds,
            )
            page_full = received >= settings.max_games_period_short

        if since_ms is not None and page_full and oldest_received is not None:
            # Pagination stopped (budget/stall) while pages were still arriving
            # full: a residual hole may remain below what arrived, so any earlier
            # coverage claim is void. The honest "contiguous back to" point is the
            # oldest game of this sync — deliberately moving the watermark
            # *forward*, the one exception to the backfill branch's never-shrink
            # rule. The next period search re-backfills through the hole (dedup
            # makes the overlap cheap) and heals it.
            player.history_fetched_until = oldest_received

        player.last_fetched_at = _utcnow()

    if backfill_start is not None:
        oldest = await db.scalar(
            select(Game.played_at).where(Game.player_id == player.id).order_by(Game.played_at.asc())
        )
        # Bound by the watermark, not merely the oldest stored game: after a
        # forward-fill fallback the watermark sits *above* older stored games
        # (a possibly-holey region), and the backfill must re-scan through it
        # rather than skip it. A watermark above the oldest stored game can also
        # arise hole-free (a sparse initial window reaching below a later period
        # claim); the schema can't tell the two apart, so we accept a bounded,
        # deduped overlap refetch there in exchange for guaranteed healing.
        backfill_until = player.history_fetched_until or oldest
        received, oldest_received = await _stream_and_persist(
            db,
            player,
            username,
            max_games=backfill_cap,
            since=_to_epoch_ms(backfill_start),
            until=_to_epoch_ms(backfill_until) if backfill_until is not None else None,
            timeout=settings.period_fetch_timeout_seconds,
        )

        if received >= backfill_cap and oldest_received is not None:
            # Cap hit: only claim coverage down to what actually arrived, so a
            # later request honestly refetches the remaining gap (§13.2).
            new_until = oldest_received
        else:
            new_until = backfill_start
        # Coverage only ever extends backwards — never shrink an earlier claim.
        if player.history_fetched_until is None or new_until < player.history_fetched_until:
            player.history_fetched_until = new_until

    await db.commit()

    if forward and not fetched_any and since_ms is None:
        return "no_analyzed_games"
    return None


def _effective_threshold(preset: Preset, threshold: int | None, game_player_rating: int) -> int:
    if threshold is not None:
        return threshold
    if preset == "auto":
        return settings.thresholds[preset_for_rating(game_player_rating)]
    if preset == "custom":
        # "custom" without an explicit threshold falls back to a sane default.
        return settings.thresholds["intermediate"]
    return settings.thresholds[preset]


@router.get("/api/players/{username}/puzzles", response_model=PuzzleSetResponse)
@limiter.limit("20/minute")
async def get_player_puzzles(
    request: Request,
    response: Response,
    username: UsernamePath,
    db: Annotated[AsyncSession, Depends(get_db)],
    threshold: Annotated[int | None, Query(ge=10, le=40)] = None,
    preset: Preset = "auto",
    period: Period = "last20",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    username = username.lower()

    player = await db.scalar(select(Player).where(Player.username == username))
    if player is None:
        player = Player(username=username)
        db.add(player)
        await db.flush()

    cache_is_fresh = player.last_fetched_at is not None and (
        _utcnow() - player.last_fetched_at < timedelta(seconds=settings.cache_ttl_seconds)
    )

    period_start = _period_start(period)
    # Backward fill is gated on coverage, not TTL: it runs whenever the window
    # reaches further back than anything fetched so far (§13.2).
    needs_backfill = period_start is not None and (
        player.history_fetched_until is None or period_start < player.history_fetched_until
    )

    reason: str | None = None
    if not cache_is_fresh or needs_backfill:
        try:
            reason = await _sync_player_games(
                db,
                player,
                username,
                forward=not cache_is_fresh,
                backfill_start=period_start if needs_backfill else None,
                backfill_cap=_period_cap(period),
            )
        except LichessUserNotFound:
            raise HTTPException(status_code=404, detail="lichess_user_not_found") from None
        except LichessRateLimited:
            raise HTTPException(status_code=503, detail="lichess_rate_limited") from None

    games_query = select(Game).where(Game.player_id == player.id).options(selectinload(Game.puzzles))
    if period_start is not None:
        games_query = games_query.where(Game.played_at >= period_start)
    games_result = await db.scalars(games_query)
    games = games_result.all()

    if not games:
        if reason is None:
            has_any = await db.scalar(
                select(Game.id).where(Game.player_id == player.id).limit(1)
            )
            reason = "no_games_in_period" if has_any is not None else "no_analyzed_games"
        return PuzzleSetResponse(
            username=username,
            player_ratings_seen=[],
            games_scanned=0,
            puzzles=[],
            reason=reason,
        )

    candidates: list[PuzzleSummary] = []
    for game in games:
        eff_threshold = _effective_threshold(preset, threshold, game.player_rating)
        for puzzle in game.puzzles:
            if puzzle.win_drop < eff_threshold:
                continue
            candidates.append(
                PuzzleSummary(
                    id=puzzle.id,
                    fen=puzzle.fen,
                    side_to_move=puzzle.side_to_move,
                    game_url=_game_url(game, puzzle.ply),
                    opponent_name=game.opponent_name,
                    opponent_rating=game.opponent_rating,
                    speed=game.speed,
                    played_at=game.played_at,
                    win_drop=puzzle.win_drop,
                    mover_moves_in_line=mover_moves_in_line(puzzle.variation_san.split()),
                )
            )

    random.Random().shuffle(candidates)
    candidates = candidates[:limit]

    return PuzzleSetResponse(
        username=username,
        player_ratings_seen=sorted({g.player_rating for g in games}),
        games_scanned=len(games),
        puzzles=candidates,
        reason=None,
    )


@router.post("/api/puzzles/{puzzle_id}/attempt", response_model=AttemptResponse)
@limiter.limit("120/minute")  # line mode sends up to 3 attempts per puzzle
async def attempt_puzzle(
    request: Request,
    response: Response,
    puzzle_id: int,
    body: AttemptRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    puzzle = await db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="puzzle_not_found")

    line = puzzle.variation_san.split() if puzzle.variation_san else []
    total_mover_moves = mover_moves_in_line(line)
    line_mode = body.mode == "line"

    if body.move_index == 0:
        board = chess.Board(puzzle.fen)
        # The line's first move should equal `best`, but the stored solution is
        # authoritative (§2.1).
        expected_uci = puzzle.solution_uci
        expected_san = puzzle.solution_san
    else:
        # Mover moves live at even line indices (0, 2, 4); anything else, an
        # index past the required window, or an unreplayable line is a client error.
        if not line_mode or body.move_index % 2 == 1 or body.move_index >= 2 * total_mover_moves:
            raise HTTPException(status_code=422, detail="invalid_move_index")
        board = variation_board(puzzle.fen, line, body.move_index)
        expected_uci = variation_move_uci(puzzle.fen, line, body.move_index)
        if board is None or expected_uci is None:
            raise HTTPException(status_code=422, detail="invalid_move_index")
        expected_san = line[body.move_index]

    correct = False
    alternate_mate = False
    if body.move_uci is not None:
        if body.move_uci == expected_uci:
            correct = True
        elif move_delivers_checkmate(board, body.move_uci):
            # A mate is never wrong (§6.5) — but it diverges from the stored
            # line, so the line ends here even mid-way through.
            correct = True
            alternate_mate = True

    line_complete = True
    opponent_reply_uci = None
    if line_mode and correct and not alternate_mate:
        next_mover_index = body.move_index + 2
        if next_mover_index < 2 * total_mover_moves:
            reply = variation_move_uci(puzzle.fen, line, body.move_index + 1)
            if reply is not None:
                opponent_reply_uci = reply
                line_complete = False

    return AttemptResponse(
        correct=correct,
        solution_uci=expected_uci,
        solution_san=expected_san,
        played_san=puzzle.played_san,
        win_drop=puzzle.win_drop,
        # Mid-line responses omit the line so moves 2-3 aren't spoiled (§2.1);
        # the full line is revealed only once the attempt sequence is over.
        variation_san=line if line_complete else [],
        opponent_reply_uci=opponent_reply_uci,
        line_complete=line_complete,
    )
