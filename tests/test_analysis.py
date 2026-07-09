import chess
import pytest

from app.analysis import (
    build_puzzle,
    determine_player_color,
    find_blunder_plies,
    move_delivers_checkmate,
    preset_for_rating,
    replay_to_ply,
    win_percent_for_color,
    win_percent_white,
)


# --- win% formula spot-checks (§12) ---------------------------------------


def test_win_percent_even_position_is_fifty():
    assert win_percent_white({"eval": 0}) == pytest.approx(50.0)


def test_win_percent_plus_300cp_is_roughly_75():
    assert win_percent_white({"eval": 300}) == pytest.approx(75.1, abs=0.2)


def test_win_percent_mate_for_white_is_100():
    assert win_percent_white({"mate": 3}) == 100.0


def test_win_percent_mate_for_black_is_0():
    assert win_percent_white({"mate": -2}) == 0.0


def test_win_percent_clamps_extreme_cp():
    # +5000cp should behave identically to the +1000cp clamp ceiling.
    assert win_percent_white({"eval": 5000}) == win_percent_white({"eval": 1000})


def test_win_percent_for_color_mirrors_for_black():
    assert win_percent_for_color(70.0, "white") == 70.0
    assert win_percent_for_color(70.0, "black") == pytest.approx(30.0)


# --- player color matching --------------------------------------------------


def test_determine_player_color_white(peremil_games):
    game = peremil_games[0]
    assert determine_player_color(game, "peremil") == "white"
    assert determine_player_color(game, "Peremil") == "white"  # case-insensitive


def test_determine_player_color_no_match(peremil_games):
    assert determine_player_color(peremil_games[0], "someone-else") is None


# --- analysis index alignment (§5.2, §12) -----------------------------------
# Confirmed correct against a real-world Lichess NDJSON consumer during planning,
# but this is the mandatory regression guard: for a fixture ply Lichess itself
# judged "Blunder", the move our replay finds at that ply must be the exact move
# Lichess judged, and our own detector must flag the same ply.


def test_blunder_ply_alignment_matches_lichess_judgment(peremil_games):
    game = peremil_games[2]  # id "Ci4M4EHH", peremil is white
    ply = 34
    entry = game["analysis"][ply]
    assert entry["judgment"]["name"] == "Blunder"

    moves_san = game["moves"].split()
    board = replay_to_ply(moves_san, ply)
    played_san = moves_san[ply]
    # The move must be legal in the reconstructed position (validates FEN/replay).
    assert board.parse_san(played_san) is not None

    blunders = dict(find_blunder_plies(game, "peremil", min_win_drop=30))
    assert ply in blunders
    assert blunders[ply] == pytest.approx(38.5, abs=0.5)


def test_blunder_detection_agrees_with_second_lichess_judged_ply(peremil_games):
    game = peremil_games[4]  # id "PTTJ9d8q"
    ply = 62
    assert game["analysis"][ply]["judgment"]["name"] == "Blunder"

    blunders = dict(find_blunder_plies(game, "peremil", min_win_drop=30))
    assert ply in blunders


# --- filters (§6.2) ----------------------------------------------------------


def test_ply_below_skip_threshold_excluded(peremil_games):
    # PLY_SKIP=10 excludes opening blunders regardless of size; use the loosest
    # stored threshold to make sure the exclusion is from ply, not win_drop.
    game = peremil_games[0]
    blunders = find_blunder_plies(game, "peremil", min_win_drop=1)
    assert all(ply >= 10 for ply, _ in blunders)


def test_only_moves_by_target_player_are_considered(peremil_games):
    game = peremil_games[0]  # peremil is white -> even plies only
    blunders = find_blunder_plies(game, "peremil", min_win_drop=1)
    assert all(ply % 2 == 0 for ply, _ in blunders)


def test_black_player_blunders_detected_on_odd_plies(halilegebaylam_games):
    # Find a fixture game where the target is black and verify odd-ply detection.
    black_games = [
        g
        for g in halilegebaylam_games
        if determine_player_color(g, "halilegebaylam") == "black"
    ]
    assert black_games, "expected at least one fixture game with halilegebaylam as black"
    game = black_games[0]
    blunders = find_blunder_plies(game, "halilegebaylam", min_win_drop=1)
    assert all(ply % 2 == 1 for ply, _ in blunders)


def test_already_lost_positions_excluded(peremil_games):
    # Manufacture a game where the mover's win% before the move is deep in "already
    # lost" territory (<20%) and confirm it's filtered even though the drop is huge.
    game = {
        "players": {
            "white": {"user": {"id": "peremil"}, "rating": 1600},
            "black": {"user": {"id": "opponent"}, "rating": 1600},
        },
        "moves": " ".join(["e4"] * 11 + ["Qxe4"]),
        "analysis": [{"eval": 0}] * 10 + [{"eval": -900}, {"eval": -1000, "best": "d2d4"}],
    }
    blunders = find_blunder_plies(game, "peremil", min_win_drop=1)
    assert blunders == []


def test_move_without_best_field_excluded(peremil_games):
    game = {
        "players": {
            "white": {"user": {"id": "peremil"}, "rating": 1600},
            "black": {"user": {"id": "opponent"}, "rating": 1600},
        },
        "moves": " ".join(["e4"] * 11),
        "analysis": [{"eval": 0}] * 10 + [{"eval": -500}],  # no "best" -> not a judged move
    }
    blunders = find_blunder_plies(game, "peremil", min_win_drop=1)
    assert blunders == []


# --- FEN reconstruction / puzzle building (§6.4) -----------------------------


def test_fen_reconstruction_is_legal_and_solution_is_legal(peremil_games):
    game = peremil_games[2]
    ply = 34
    puzzle = build_puzzle(game, ply, win_drop=38.5)

    board = chess.Board(puzzle["fen"])  # raises if FEN is malformed
    solution_move = chess.Move.from_uci(puzzle["solution_uci"])
    assert solution_move in board.legal_moves
    assert puzzle["played_san"] == "Rf2"
    assert puzzle["solution_san"] != puzzle["played_san"]


def test_preset_for_rating_bands():
    assert preset_for_rating(1000) == "beginner"
    assert preset_for_rating(1199) == "beginner"
    assert preset_for_rating(1200) == "intermediate"
    assert preset_for_rating(1799) == "intermediate"
    assert preset_for_rating(1800) == "advanced"
    assert preset_for_rating(2199) == "advanced"
    assert preset_for_rating(2200) == "expert"
    assert preset_for_rating(2600) == "expert"


# --- checkmate-move check (§6.5 correction) ----------------------------------


def test_move_delivers_checkmate_true_for_mating_move():
    # Fool's mate position: black to move, Qh4# is mate.
    board = chess.Board()
    for san in ["f3", "e5", "g4"]:
        board.push_san(san)
    assert move_delivers_checkmate(board, "d8h4")
    # Board must be unchanged (popped) after the check.
    assert board.fullmove_number == 2
    assert board.turn == chess.BLACK


def test_move_delivers_checkmate_false_for_non_mating_move():
    board = chess.Board()
    assert not move_delivers_checkmate(board, "e2e4")


def test_move_delivers_checkmate_false_for_illegal_move():
    board = chess.Board()
    assert not move_delivers_checkmate(board, "e2e5")
