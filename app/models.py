from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

__all__ = ["Base", "Player", "Game", "Puzzle", "Job"]


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(30), unique=True, index=True)  # lowercase lichess id
    last_fetched_at: Mapped[datetime | None]
    # Oldest point in time fully fetched back to (§13.2 coverage window); None
    # means only the initial last-20 window exists. Naive UTC like the rest.
    history_fetched_until: Mapped[datetime | None]

    games: Mapped[list["Game"]] = relationship(back_populates="player")


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True)
    lichess_id: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    player_color: Mapped[str] = mapped_column(String(5))  # "white" | "black"
    player_rating: Mapped[int]
    opponent_name: Mapped[str] = mapped_column(String(30))
    opponent_rating: Mapped[int]
    speed: Mapped[str] = mapped_column(String(20))
    played_at: Mapped[datetime]
    raw_analysis_processed: Mapped[bool] = mapped_column(default=False)
    # Eval source hierarchy (§14.3): Lichess analysis when it exists (free,
    # instant), otherwise the background Stockfish worker.
    eval_source: Mapped[str] = mapped_column(
        String(10), default="lichess", server_default="lichess"
    )  # "lichess" | "stockfish"
    # Full SAN movelist, stored for every game since Phase 3 (NULL on older
    # rows). The engine path needs it; keeping it uniformly means future
    # features never depend on whether Lichess had analyzed a game.
    moves_san: Mapped[str | None] = mapped_column(Text)
    # Merged engine analysis (Lichess-shaped JSON), engine-sourced games only —
    # so future features (brilliance, alternate solutions) reuse the paid-for
    # CPU work instead of re-analyzing. Lichess analysis is re-fetchable free.
    analysis_json: Mapped[str | None] = mapped_column(Text)
    # When the worker finished this game; drives the daily-fuse accounting
    # (games analyzed since UTC midnight). Naive UTC like the rest.
    analyzed_at: Mapped[datetime | None]

    player: Mapped["Player"] = relationship(back_populates="games")
    puzzles: Mapped[list["Puzzle"]] = relationship(back_populates="game")


class Puzzle(Base):
    __tablename__ = "puzzles"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    ply: Mapped[int]  # half-move index of the blunder
    fen: Mapped[str] = mapped_column(String(100))
    side_to_move: Mapped[str] = mapped_column(String(5))
    solution_uci: Mapped[str] = mapped_column(String(6))
    solution_san: Mapped[str] = mapped_column(String(10))
    played_uci: Mapped[str] = mapped_column(String(6))
    played_san: Mapped[str] = mapped_column(String(10))
    variation_san: Mapped[str] = mapped_column(Text)  # space-separated SAN line
    win_drop: Mapped[float]  # win% drop of the actual played move
    eval_before_cp: Mapped[int | None]  # null if mate score
    eval_after_cp: Mapped[int | None]
    kind: Mapped[str] = mapped_column(String(10), default="blunder")  # "blunder" | "brilliant" (phase 3)

    game: Mapped["Game"] = relationship(back_populates="puzzles")

    __table_args__ = (UniqueConstraint("game_id", "ply"),)


class Job(Base):
    """One background Stockfish analysis job per player (§14.1).

    No params blob: the work set is defined by DB state — the player's
    unprocessed games (raw_analysis_processed=False) within `period_start`
    (NULL = whole pool), newest first, up to `total`. Puzzles are always
    stored at min_win_drop_stored, so the user's threshold never matters at
    analysis time.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    # Start of the searched window this job serves; NULL = whole accumulated
    # pool (period "last20", and jobs created before this column existed).
    # Naive UTC like the rest.
    period_start: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(
        String(10), default="queued", index=True
    )  # queued | running | done | failed
    progress: Mapped[int] = mapped_column(default=0)  # games analyzed so far
    total: Mapped[int] = mapped_column(default=0)  # games this job will analyze
    error: Mapped[str | None] = mapped_column(Text)  # machine-readable, e.g. "daily_budget_reached"
    created_at: Mapped[datetime]
