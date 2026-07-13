from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
    mover_moves_in_line: int  # how many line moves "Full line" mode requires (≤3)


class JobStatus(BaseModel):
    """Background engine-analysis job (§14.1), inlined in the puzzles response
    so the frontend can render the progress banner without an extra request;
    GET /api/jobs/{id} serves the same shape for polling."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: Literal["queued", "running", "done", "failed"]
    progress: int
    total: int
    error: str | None = None
    # The tripped budget's limit (games/day), set by the jobs endpoint on
    # budget failures — limits are env-tunable, so the frontend must never
    # hardcode them into banner copy.
    daily_limit: int | None = None


class PuzzleSetResponse(BaseModel):
    username: str
    player_ratings_seen: list[int]
    games_scanned: int
    # How many of games_scanned have been analyzed (Lichess or our engine) —
    # the honest denominator for "puzzles from N games" copy while a backlog
    # is still cooking.
    games_analyzed: int
    puzzles: list[PuzzleSummary]
    reason: str | None = None
    job: JobStatus | None = None  # pending engine analysis for this player, if any


class AttemptRequest(BaseModel):
    move_uci: str | None = None
    # "line" activates the multi-move flow (§13.1); "single" preserves the
    # Phase 1 contract exactly, which is why it must be the default.
    mode: Literal["single", "line"] = "single"
    move_index: int = Field(default=0, ge=0, le=8)  # even line index of the attempted move


class AttemptResponse(BaseModel):
    correct: bool
    solution_uci: str
    solution_san: str
    played_san: str
    win_drop: float
    variation_san: list[str]
    opponent_reply_uci: str | None = None
    line_complete: bool = True
