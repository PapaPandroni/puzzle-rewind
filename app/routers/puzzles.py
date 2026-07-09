import random
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import chess
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.analysis import determine_player_color, extract_puzzles, move_delivers_checkmate, preset_for_rating
from app.config import settings
from app.database import get_db
from app.lichess import LichessRateLimited, LichessUserNotFound, fetch_games
from app.models import Game, Player, Puzzle
from app.rate_limit import limiter
from app.schemas import AttemptRequest, AttemptResponse, PuzzleSetResponse, PuzzleSummary

router = APIRouter()

UsernamePath = Annotated[str, Path(pattern=r"^[a-zA-Z0-9_-]{2,30}$")]
Preset = Literal["auto", "beginner", "intermediate", "advanced", "expert", "custom"]


def _utcnow() -> datetime:
    # Stored naive (UTC by convention) — SQLite silently drops tzinfo on read, so
    # comparisons must stay naive on both sides regardless of backend.
    return datetime.now(UTC).replace(tzinfo=None)


def _game_url(game: Game, ply: int) -> str:
    suffix = "/black" if game.player_color == "black" else ""
    return f"https://lichess.org/{game.lichess_id}{suffix}#{ply}"


async def _sync_player_games(db: AsyncSession, player: Player, username: str) -> str | None:
    """Fetch new games from Lichess and persist Game/Puzzle rows (§7 cache flow).

    Returns a "reason" string for empty results (no analyzed games / user has none),
    or None on success. Raises LichessUserNotFound / LichessRateLimited upstream.
    """
    since_ms: int | None = None
    latest = await db.scalar(
        select(Game.played_at).where(Game.player_id == player.id).order_by(Game.played_at.desc())
    )
    if latest is not None:
        since_ms = int(latest.replace(tzinfo=UTC).timestamp() * 1000)

    fetched_any = False
    async for game in fetch_games(username, max_games=settings.max_games_mvp, since=since_ms):
        fetched_any = True
        lichess_id = game["id"]

        existing = await db.scalar(select(Game).where(Game.lichess_id == lichess_id))
        if existing is not None:
            continue

        color = determine_player_color(game, username)
        if color is None:
            continue

        opponent_color = "black" if color == "white" else "white"
        # Games vs the Lichess AI (or other non-human opponents) omit "rating"
        # entirely instead of using a sentinel value — skip, since Game.opponent_rating
        # is non-nullable and these aren't meaningful puzzle sources anyway.
        if "rating" not in game["players"][opponent_color]:
            continue

        game_row = Game(
            lichess_id=lichess_id,
            player_id=player.id,
            player_color=color,
            player_rating=game["players"][color]["rating"],
            opponent_name=game["players"][opponent_color].get("user", {}).get("name", "?"),
            opponent_rating=game["players"][opponent_color]["rating"],
            speed=game["speed"],
            played_at=datetime.fromtimestamp(game["createdAt"] / 1000, tz=UTC).replace(tzinfo=None),
            raw_analysis_processed=True,
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

    player.last_fetched_at = _utcnow()
    await db.commit()

    if not fetched_any and since_ms is None:
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

    reason: str | None = None
    if not cache_is_fresh:
        try:
            reason = await _sync_player_games(db, player, username)
        except LichessUserNotFound:
            raise HTTPException(status_code=404, detail="lichess_user_not_found") from None
        except LichessRateLimited:
            raise HTTPException(status_code=503, detail="lichess_rate_limited") from None

    games_result = await db.scalars(
        select(Game).where(Game.player_id == player.id).options(selectinload(Game.puzzles))
    )
    games = games_result.all()

    if not games:
        return PuzzleSetResponse(
            username=username,
            player_ratings_seen=[],
            games_scanned=0,
            puzzles=[],
            reason=reason or "no_analyzed_games",
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
@limiter.limit("60/minute")
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

    correct = False
    if body.move_uci is not None:
        if body.move_uci == puzzle.solution_uci:
            correct = True
        else:
            board = chess.Board(puzzle.fen)
            correct = move_delivers_checkmate(board, body.move_uci)

    return AttemptResponse(
        correct=correct,
        solution_uci=puzzle.solution_uci,
        solution_san=puzzle.solution_san,
        played_san=puzzle.played_san,
        win_drop=puzzle.win_drop,
        variation_san=puzzle.variation_san.split() if puzzle.variation_san else [],
        opponent_reply_uci=None,
    )
