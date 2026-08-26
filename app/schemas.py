from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LastMove(BaseModel):
    """Opponent's move leading into the puzzle position, derived at response
    time from Game.moves_san (never stored). None on the summary when the
    movelist is missing (pre-Phase-3 rows) — the frontend then skips the
    intro animation and renders the puzzle as before."""

    uci: str
    san: str
    fen_before: str


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
    last_move: LastMove | None = None


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
    """Verdict on one attempt, plus the reveal — but only when the reveal is due.

    A wrong *move* is retryable (the frontend resets the board and lets the user
    try again), so it returns the verdict alone: shipping the answer would leave
    it sitting in the network tab while the user is still solving. The reveal
    fields are populated for a correct attempt and for the give-up path
    (`move_uci is None`), which is the only way to ask for the answer outright.
    """

    correct: bool
    solution_uci: str | None = None
    solution_san: str | None = None
    played_san: str | None = None
    win_drop: float | None = None
    variation_san: list[str] = []
    opponent_reply_uci: str | None = None
    line_complete: bool = True
