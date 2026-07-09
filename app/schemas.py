from datetime import datetime

from pydantic import BaseModel


class PuzzleSummary(BaseModel):
    id: int
    fen: str
    side_to_move: str
    game_url: str
    opponent_name: str
    opponent_rating: int
    speed: str
    played_at: datetime
    win_drop: float


class PuzzleSetResponse(BaseModel):
    username: str
    player_ratings_seen: list[int]
    games_scanned: int
    puzzles: list[PuzzleSummary]
    reason: str | None = None


class AttemptRequest(BaseModel):
    move_uci: str | None = None


class AttemptResponse(BaseModel):
    correct: bool
    solution_uci: str
    solution_san: str
    played_san: str
    win_drop: float
    variation_san: list[str]
    opponent_reply_uci: str | None = None  # phase 2
