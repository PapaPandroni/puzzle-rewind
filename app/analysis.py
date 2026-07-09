"""Win-percentage math, blunder detection, and puzzle-position building (DESIGN.md §6)."""

import math
from typing import Any

import chess

CP_CLAMP = 1000
WIN_PCT_K = -0.00368208

# Filter #2 (§6.2): a "blunder" from a position the mover was already losing badly
# isn't an interesting puzzle.
MIN_WIN_PCT_BEFORE_NOT_LOST = 20.0

# Filter #5 (§6.2): opening blunders at low depth make poor puzzles; evals there are noisy.
PLY_SKIP = 10

PRESET_RATING_BANDS = (
    (1200, "beginner"),
    (1800, "intermediate"),
    (2200, "advanced"),
)
DEFAULT_PRESET = "expert"


def win_percent_white(entry: dict[str, Any]) -> float:
    """Win% for White given one analysis[i] entry (has "eval" cp or "mate")."""
    if "mate" in entry:
        return 100.0 if entry["mate"] > 0 else 0.0
    cp = entry.get("eval", 0)
    cp = max(-CP_CLAMP, min(CP_CLAMP, cp))
    return 50 + 50 * (2 / (1 + math.exp(WIN_PCT_K * cp)) - 1)


def win_percent_for_color(win_pct_white: float, color: str) -> float:
    return win_pct_white if color == "white" else 100.0 - win_pct_white


def mover_color(ply: int) -> str:
    """Color of the player who played half-move `ply` (0-indexed)."""
    return "white" if ply % 2 == 0 else "black"


def preset_for_rating(rating: int) -> str:
    for ceiling, preset in PRESET_RATING_BANDS:
        if rating < ceiling:
            return preset
    return DEFAULT_PRESET


def determine_player_color(game: dict[str, Any], username: str) -> str | None:
    """Match `username` (case-insensitive) against the game's white/black player ids."""
    username_lower = username.lower()
    for color in ("white", "black"):
        player_id = game["players"][color].get("user", {}).get("id", "")
        if player_id == username_lower:
            return color
    return None


def replay_to_ply(moves_san: list[str], ply: int) -> chess.Board:
    """Board position just before half-move `ply` (0-indexed) is played."""
    board = chess.Board()
    for san in moves_san[:ply]:
        board.push_san(san)
    return board


def _eval_cp_or_none(entry: dict[str, Any]) -> int | None:
    if "mate" in entry:
        return None
    return entry.get("eval")


def find_blunder_plies(
    game: dict[str, Any], username: str, min_win_drop: float
) -> list[tuple[int, float]]:
    """Return [(ply, win_drop), ...] for half-moves where `username` blundered.

    Applies filters #1, #2, #3, #5 from DESIGN.md §6.2. Caller is responsible for
    filter #4 (dedup across fetches) via the DB's unique (game_id, ply) constraint.
    """
    color = determine_player_color(game, username)
    if color is None:
        return []

    analysis = game.get("analysis", [])
    moves_san = game.get("moves", "").split()

    results = []
    for ply, entry in enumerate(analysis):
        if ply >= len(moves_san):
            continue
        if ply < PLY_SKIP:
            continue
        if mover_color(ply) != color:
            continue
        if "best" not in entry:
            continue

        before_entry = analysis[ply - 1] if ply > 0 else {"eval": 0}
        win_mover_before = win_percent_for_color(win_percent_white(before_entry), color)
        win_mover_after = win_percent_for_color(win_percent_white(entry), color)

        if win_mover_before < MIN_WIN_PCT_BEFORE_NOT_LOST:
            continue

        drop = win_mover_before - win_mover_after
        if drop < min_win_drop:
            continue

        results.append((ply, drop))

    return results


def build_puzzle(game: dict[str, Any], ply: int, win_drop: float) -> dict[str, Any]:
    """Build the puzzle-position fields for a detected blunder at `ply` (§6.4).

    Does not include game-level metadata (game_id, opponent, ratings, speed,
    played_at) — the caller attaches that from the parent `game`/`Game` row.
    """
    analysis = game["analysis"]
    moves_san = game["moves"].split()
    color = mover_color(ply)

    entry = analysis[ply]
    before_entry = analysis[ply - 1] if ply > 0 else {"eval": 0}

    board = replay_to_ply(moves_san, ply)

    best_uci = entry["best"]
    best_move = chess.Move.from_uci(best_uci)
    solution_san = board.san(best_move)

    played_san = moves_san[ply]
    played_move = board.parse_san(played_san)
    played_uci = played_move.uci()

    return {
        "ply": ply,
        "fen": board.fen(),
        "side_to_move": color,
        "solution_uci": best_uci,
        "solution_san": solution_san,
        "played_uci": played_uci,
        "played_san": played_san,
        "variation_san": entry.get("variation", "").split(),
        "win_drop": win_drop,
        "eval_before_cp": _eval_cp_or_none(before_entry),
        "eval_after_cp": _eval_cp_or_none(entry),
    }


def extract_puzzles(
    game: dict[str, Any], username: str, min_win_drop: float
) -> list[dict[str, Any]]:
    """All puzzle candidates for `username` in one game, at threshold `min_win_drop`."""
    return [
        build_puzzle(game, ply, win_drop)
        for ply, win_drop in find_blunder_plies(game, username, min_win_drop)
    ]


def move_delivers_checkmate(board: chess.Board, uci: str) -> bool:
    """Whether pushing `uci` on `board` delivers checkmate (§6.5 correction).

    `Board.is_checkmate()` only reflects the current position, so the candidate
    move must be pushed and popped rather than checked in a single call.
    """
    try:
        move = chess.Move.from_uci(uci)
    except (chess.InvalidMoveError, ValueError):
        return False
    if move not in board.legal_moves:
        return False
    board.push(move)
    try:
        return board.is_checkmate()
    finally:
        board.pop()
