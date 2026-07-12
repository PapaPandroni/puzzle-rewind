"""Engine-module tests — require a local Stockfish binary (brew install stockfish).

Marked `engine` and skipped when the binary is absent so CI/offline runs stay
green. Movetimes are tiny (0.05 s): every asserted signal (a hung queen, a
mate in one) is visible at depth 1, so the tests stay fast and deterministic.
"""

import copy
import shutil

import chess
import pytest

from app.analysis import (
    extract_puzzles_for_color,
    find_blunder_plies_for_color,
    replay_to_ply,
    variation_board,
)
from app.config import settings
from app.engine import analyse_game, engine_handle, refine_plies

pytestmark = [
    pytest.mark.engine,
    pytest.mark.skipif(
        shutil.which(settings.stockfish_path) is None,
        reason="stockfish binary not found",
    ),
]

MOVETIME = 0.05

# A quiet opening past PLY_SKIP (10), then White hangs the queen: 7. Qe3?? runs
# into Bxe3 (the c5-bishop takes it) — bishop for queen, an unmissable blunder
# at ply 12.
HANGING_QUEEN_GAME = [
    "e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "Nc3", "Nf6",
    "d3", "d6", "Qe2", "Bg4", "Qe3",
]
BLUNDER_PLY = 12

# Fool's mate: Black delivers mate on ply 3.
FOOLS_MATE = ["f3", "e5", "g4", "Qh4#"]


@pytest.fixture(autouse=True, scope="module")
async def _quit_engine_after_module():
    yield
    await engine_handle.quit()


async def test_sweep_shape_and_extraction_reuse():
    """The 'extraction code reused unchanged' proof: a full sweep feeds
    find_blunder_plies_for_color/build_puzzle exactly like Lichess data."""
    entries = await analyse_game(HANGING_QUEEN_GAME, movetime=MOVETIME)

    assert len(entries) == len(HANGING_QUEEN_GAME)
    for ply, entry in enumerate(entries):
        assert ("eval" in entry) != ("mate" in entry), f"ply {ply}: exactly one score key"
        board = replay_to_ply(HANGING_QUEEN_GAME, ply)
        assert chess.Move.from_uci(entry["best"]) in board.legal_moves, f"ply {ply}"
        line = entry["variation"].split()
        assert line, f"ply {ply}: variation present"
        assert len(line) <= settings.engine_variation_max_plies
        assert variation_board(board.fen(), line, len(line)) is not None, f"ply {ply}"

    blunders = dict(
        find_blunder_plies_for_color(entries, HANGING_QUEEN_GAME, "white", 25)
    )
    assert BLUNDER_PLY in blunders

    puzzles = extract_puzzles_for_color(
        entries, " ".join(HANGING_QUEEN_GAME), "white", 25
    )
    puzzle = next(p for p in puzzles if p["ply"] == BLUNDER_PLY)
    board = replay_to_ply(HANGING_QUEEN_GAME, BLUNDER_PLY)
    assert puzzle["fen"] == board.fen()
    assert puzzle["side_to_move"] == "white"
    assert puzzle["played_san"] == "Qe3"
    assert chess.Move.from_uci(puzzle["solution_uci"]) in board.legal_moves


async def test_pov_sign_matches_lichess_convention():
    # After 1. f3 e5 2. g4 White is lost (Qh4# looms): the White-POV score for
    # entry[2] must be negative — the classic sign bug this guards against
    # would report it positive.
    entries = await analyse_game(FOOLS_MATE[:3], movetime=MOVETIME)
    after_g4 = entries[2]
    assert after_g4.get("mate", 0) < 0 or after_g4.get("eval", 0) < -200


async def test_terminal_mate_synthesized_not_engine_scored():
    entries = await analyse_game(FOOLS_MATE, movetime=MOVETIME)
    assert len(entries) == 4
    # Position before Qh4# is engine-analyzed (finding the mate as best)...
    assert entries[3]["best"] == "d8h4"
    # ...but the mated final position is synthesized: Black delivered mate, so
    # White-POV mate is negative. A naive engine "mate 0" here would score
    # Black's own mating move as a 100% win-drop blunder.
    assert entries[3]["mate"] == -1
    win_drop_for_black = dict(
        find_blunder_plies_for_color(entries, FOOLS_MATE, "black", 10)
    )
    assert 3 not in win_drop_for_black


async def test_refine_plies_merges_without_mutating_input():
    entries = await analyse_game(HANGING_QUEEN_GAME, movetime=MOVETIME)
    snapshot = copy.deepcopy(entries)

    merged = await refine_plies(
        HANGING_QUEEN_GAME, entries, [BLUNDER_PLY], movetime=MOVETIME
    )

    assert entries == snapshot, "refine_plies must not mutate its input"
    assert len(merged) == len(entries)
    refined = merged[BLUNDER_PLY]
    assert ("eval" in refined) != ("mate" in refined)
    board = replay_to_ply(HANGING_QUEEN_GAME, BLUNDER_PLY)
    assert chess.Move.from_uci(refined["best"]) in board.legal_moves
    # The hung queen is still a blunder at any depth.
    blunders = dict(
        find_blunder_plies_for_color(merged, HANGING_QUEEN_GAME, "white", 25)
    )
    assert BLUNDER_PLY in blunders


async def test_empty_game_returns_no_entries():
    assert await analyse_game([], movetime=MOVETIME) == []


async def test_unparseable_san_raises_value_error():
    with pytest.raises(ValueError):
        await analyse_game(["e4", "not-a-move"], movetime=MOVETIME)
