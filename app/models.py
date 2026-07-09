from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

__all__ = ["Base", "Player", "Game", "Puzzle"]


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
