import pytest

from app.lichess import LichessRateLimited, LichessUserNotFound


def _make_fake_fetch_games(games: list[dict]):
    async def fake_fetch_games(
        username: str,
        *,
        max_games: int = 20,
        since: int | None = None,
        until: int | None = None,
        timeout: float = 30.0,
    ):
        for g in games:
            yield g

    return fake_fetch_games


def _make_raising_fetch_games(exc: Exception):
    async def fake_fetch_games(
        username: str,
        *,
        max_games: int = 20,
        since: int | None = None,
        until: int | None = None,
        timeout: float = 30.0,
    ):
        raise exc
        yield  # pragma: no cover - makes this an async generator

    return fake_fetch_games


@pytest.mark.asyncio
async def test_get_puzzles_full_flow(client, monkeypatch, peremil_games):
    monkeypatch.setattr(
        "app.routers.puzzles.fetch_games", _make_fake_fetch_games(peremil_games)
    )

    response = await client.get(
        "/api/players/peremil/puzzles", params={"preset": "custom", "threshold": 10}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "peremil"
    assert body["games_scanned"] == 5
    assert body["reason"] is None
    assert len(body["puzzles"]) > 0
    puzzle = body["puzzles"][0]
    assert set(puzzle.keys()) == {
        "id",
        "fen",
        "side_to_move",
        "game_url",
        "opponent_name",
        "opponent_rating",
        "speed",
        "played_at",
        "win_drop",
        "mover_moves_in_line",
    }
    assert 0 <= puzzle["mover_moves_in_line"] <= 3
    # Solution must never leak in the list response.
    assert "solution_uci" not in puzzle
    assert "solution_san" not in puzzle


@pytest.mark.asyncio
async def test_threshold_filtering_is_query_time_not_refetch(client, monkeypatch, peremil_games):
    call_count = 0
    fake = _make_fake_fetch_games(peremil_games)

    async def counting_fake_fetch_games(username, *, max_games=20, since=None, until=None, timeout=30.0):
        nonlocal call_count
        call_count += 1
        async for g in fake(username, max_games=max_games, since=since):
            yield g

    monkeypatch.setattr("app.routers.puzzles.fetch_games", counting_fake_fetch_games)

    loose = await client.get(
        "/api/players/peremil/puzzles", params={"preset": "custom", "threshold": 10}
    )
    strict = await client.get(
        "/api/players/peremil/puzzles", params={"preset": "custom", "threshold": 35}
    )

    assert loose.status_code == 200
    assert strict.status_code == 200
    assert len(loose.json()["puzzles"]) > len(strict.json()["puzzles"])
    # Second request should be served from cache (fresh TTL) — no second upstream fetch.
    assert call_count == 1


@pytest.mark.asyncio
async def test_attempt_wrong_then_correct_move(client, monkeypatch, peremil_games):
    monkeypatch.setattr(
        "app.routers.puzzles.fetch_games", _make_fake_fetch_games(peremil_games)
    )
    puzzles_resp = await client.get(
        "/api/players/peremil/puzzles", params={"preset": "custom", "threshold": 10}
    )
    puzzle_id = puzzles_resp.json()["puzzles"][0]["id"]

    wrong = await client.post(
        f"/api/puzzles/{puzzle_id}/attempt", json={"move_uci": "a2a3"}
    )
    assert wrong.status_code == 200
    wrong_body = wrong.json()
    assert wrong_body["correct"] is False
    assert wrong_body["solution_uci"]
    assert wrong_body["solution_san"]
    assert wrong_body["played_san"]

    right = await client.post(
        f"/api/puzzles/{puzzle_id}/attempt",
        json={"move_uci": wrong_body["solution_uci"]},
    )
    assert right.status_code == 200
    assert right.json()["correct"] is True


@pytest.mark.asyncio
async def test_attempt_give_up_path(client, monkeypatch, peremil_games):
    monkeypatch.setattr(
        "app.routers.puzzles.fetch_games", _make_fake_fetch_games(peremil_games)
    )
    puzzles_resp = await client.get(
        "/api/players/peremil/puzzles", params={"preset": "custom", "threshold": 10}
    )
    puzzle_id = puzzles_resp.json()["puzzles"][0]["id"]

    resp = await client.post(f"/api/puzzles/{puzzle_id}/attempt", json={"move_uci": None})
    assert resp.status_code == 200
    body = resp.json()
    assert body["correct"] is False
    assert body["solution_uci"]


@pytest.mark.asyncio
async def test_attempt_alternate_checkmate_accepted(db_sessionmaker, client):
    # Fool's-mate-adjacent position where a non-"best" move also delivers mate.
    from app.models import Game, Player, Puzzle

    async with db_sessionmaker() as session:
        player = Player(username="matetest")
        session.add(player)
        await session.flush()
        game = Game(
            lichess_id="matetest01",
            player_id=player.id,
            player_color="black",
            player_rating=1500,
            opponent_name="opp",
            opponent_rating=1500,
            speed="blitz",
            played_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        )
        session.add(game)
        await session.flush()
        puzzle = Puzzle(
            game_id=game.id,
            ply=3,
            fen="rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR b KQkq - 1 3",
            side_to_move="black",
            solution_uci="d8h4",
            solution_san="Qh4#",
            played_uci="d8h4",
            played_san="Qh4#",
            variation_san="",
            win_drop=100.0,
            eval_before_cp=0,
            eval_after_cp=None,
        )
        session.add(puzzle)
        await session.commit()
        puzzle_id = puzzle.id

    resp = await client.post(
        f"/api/puzzles/{puzzle_id}/attempt", json={"move_uci": "d8h4"}
    )
    assert resp.status_code == 200
    assert resp.json()["correct"] is True


@pytest.mark.asyncio
async def test_unknown_username_returns_404(client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.puzzles.fetch_games",
        _make_raising_fetch_games(LichessUserNotFound("ghost")),
    )
    resp = await client.get("/api/players/ghost/puzzles")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "lichess_user_not_found"


@pytest.mark.asyncio
async def test_rate_limited_returns_503(client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.puzzles.fetch_games",
        _make_raising_fetch_games(LichessRateLimited()),
    )
    resp = await client.get("/api/players/someone/puzzles")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "lichess_rate_limited"


@pytest.mark.asyncio
async def test_game_vs_ai_without_opponent_rating_is_skipped_not_crashed(
    client, monkeypatch, peremil_games
):
    # Games vs the Lichess AI have no "rating" key for the AI side (only "aiLevel").
    # Sync must skip these instead of raising KeyError on Game.opponent_rating.
    ai_game = dict(peremil_games[0])
    ai_game["id"] = "vsAIgame01"
    ai_game["players"] = {
        "white": peremil_games[0]["players"]["white"],
        "black": {"aiLevel": 8, "analysis": {"acpl": 66}},
    }
    monkeypatch.setattr(
        "app.routers.puzzles.fetch_games", _make_fake_fetch_games([ai_game] + peremil_games)
    )

    response = await client.get(
        "/api/players/peremil/puzzles", params={"preset": "custom", "threshold": 10}
    )
    assert response.status_code == 200
    # The AI game contributes no puzzles, but the rest of the batch still syncs.
    assert response.json()["games_scanned"] == 5


@pytest.mark.asyncio
async def test_no_analyzed_games_returns_empty_with_reason(client, monkeypatch):
    monkeypatch.setattr("app.routers.puzzles.fetch_games", _make_fake_fetch_games([]))
    resp = await client.get("/api/players/nogames/puzzles")
    assert resp.status_code == 200
    body = resp.json()
    assert body["puzzles"] == []
    assert body["reason"] == "no_analyzed_games"


@pytest.mark.asyncio
async def test_invalid_username_returns_422(client):
    resp = await client.get("/api/players/a/puzzles")  # too short (min 2 chars)
    assert resp.status_code == 422

    resp2 = await client.get("/api/players/bad$name!/puzzles")
    assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_attempt_unknown_puzzle_returns_404(client):
    resp = await client.post("/api/puzzles/99999/attempt", json={"move_uci": "e2e4"})
    assert resp.status_code == 404


# --- line mode (§13.1, Phase 2) ------------------------------------------------
# Synthetic 5-move line (3 mover moves at indices 0, 2, 4): from the puzzle FEN,
# white plays Qb4 / Qc4 / Qd4+ against black's a6 / a5 tempo moves. After
# "Qb4 a6" white also has the alternate mate Qf8# (rank check; g8/g7 covered by
# the queen, h7 blocked by black's own pawn) — used for the mid-line mate test.

LINE_FEN = "7k/p6p/8/8/8/1Q6/8/6RK w - - 0 1"
LINE_SAN = "Qb4 a6 Qc4 a5 Qd4+"


async def _seed_line_puzzle(db_sessionmaker) -> int:
    from datetime import UTC, datetime

    from app.models import Game, Player, Puzzle

    async with db_sessionmaker() as session:
        player = Player(username="linetest")
        session.add(player)
        await session.flush()
        game = Game(
            lichess_id="linetest01",
            player_id=player.id,
            player_color="white",
            player_rating=1500,
            opponent_name="opp",
            opponent_rating=1500,
            speed="blitz",
            played_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(game)
        await session.flush()
        puzzle = Puzzle(
            game_id=game.id,
            ply=20,
            fen=LINE_FEN,
            side_to_move="white",
            solution_uci="b3b4",
            solution_san="Qb4",
            played_uci="b3b1",
            played_san="Qb1",
            variation_san=LINE_SAN,
            win_drop=30.0,
            eval_before_cp=200,
            eval_after_cp=-150,
        )
        session.add(puzzle)
        await session.commit()
        return puzzle.id


@pytest.mark.asyncio
async def test_line_mode_full_line_success(db_sessionmaker, client):
    puzzle_id = await _seed_line_puzzle(db_sessionmaker)

    first = await client.post(
        f"/api/puzzles/{puzzle_id}/attempt",
        json={"move_uci": "b3b4", "mode": "line", "move_index": 0},
    )
    assert first.status_code == 200
    body = first.json()
    assert body["correct"] is True
    assert body["line_complete"] is False
    assert body["opponent_reply_uci"] == "a7a6"
    assert body["solution_san"] == "Qb4"
    assert body["variation_san"] == []  # mid-line: future moves must not leak

    second = await client.post(
        f"/api/puzzles/{puzzle_id}/attempt",
        json={"move_uci": "b4c4", "mode": "line", "move_index": 2},
    )
    body = second.json()
    assert body["correct"] is True
    assert body["line_complete"] is False
    assert body["opponent_reply_uci"] == "a6a5"
    assert body["solution_san"] == "Qc4"
    assert body["variation_san"] == []

    third = await client.post(
        f"/api/puzzles/{puzzle_id}/attempt",
        json={"move_uci": "c4d4", "mode": "line", "move_index": 4},
    )
    body = third.json()
    assert body["correct"] is True
    assert body["line_complete"] is True
    assert body["opponent_reply_uci"] is None
    assert body["variation_san"] == LINE_SAN.split()


@pytest.mark.asyncio
async def test_line_mode_wrong_move_mid_line_reveals_full_line(db_sessionmaker, client):
    puzzle_id = await _seed_line_puzzle(db_sessionmaker)
    resp = await client.post(
        f"/api/puzzles/{puzzle_id}/attempt",
        json={"move_uci": "b4b5", "mode": "line", "move_index": 2},
    )
    body = resp.json()
    assert body["correct"] is False
    assert body["line_complete"] is True
    # The reveal names the move expected at *this* index, not the line's first move.
    assert body["solution_san"] == "Qc4"
    assert body["solution_uci"] == "b4c4"
    assert body["variation_san"] == LINE_SAN.split()


@pytest.mark.asyncio
async def test_line_mode_give_up_mid_line(db_sessionmaker, client):
    puzzle_id = await _seed_line_puzzle(db_sessionmaker)
    resp = await client.post(
        f"/api/puzzles/{puzzle_id}/attempt",
        json={"move_uci": None, "mode": "line", "move_index": 2},
    )
    body = resp.json()
    assert body["correct"] is False
    assert body["line_complete"] is True
    assert body["variation_san"] == LINE_SAN.split()


@pytest.mark.asyncio
async def test_line_mode_alternate_mate_mid_line_completes(db_sessionmaker, client):
    puzzle_id = await _seed_line_puzzle(db_sessionmaker)
    resp = await client.post(
        f"/api/puzzles/{puzzle_id}/attempt",
        json={"move_uci": "b4f8", "mode": "line", "move_index": 2},  # Qf8#, not the line move
    )
    body = resp.json()
    assert body["correct"] is True
    assert body["line_complete"] is True  # stored line no longer applies after a divergent mate
    assert body["opponent_reply_uci"] is None


@pytest.mark.asyncio
async def test_line_mode_invalid_move_index_rejected(db_sessionmaker, client):
    puzzle_id = await _seed_line_puzzle(db_sessionmaker)

    past_end = await client.post(
        f"/api/puzzles/{puzzle_id}/attempt",
        json={"move_uci": "c4d4", "mode": "line", "move_index": 6},  # only indices 0/2/4 exist
    )
    assert past_end.status_code == 422
    assert past_end.json()["detail"] == "invalid_move_index"

    odd_index = await client.post(
        f"/api/puzzles/{puzzle_id}/attempt",
        json={"move_uci": "a7a6", "mode": "line", "move_index": 1},  # opponent move, not attemptable
    )
    assert odd_index.status_code == 422

    single_mode_positional = await client.post(
        f"/api/puzzles/{puzzle_id}/attempt",
        json={"move_uci": "b4c4", "move_index": 2},  # move_index > 0 requires line mode
    )
    assert single_mode_positional.status_code == 422

    beyond_schema_cap = await client.post(
        f"/api/puzzles/{puzzle_id}/attempt",
        json={"move_uci": "c4d4", "mode": "line", "move_index": 9},  # le=8 schema bound
    )
    assert beyond_schema_cap.status_code == 422


# --- time periods (§13.2, Phase 2) ---------------------------------------------


def _at_days_ago(games: list[dict], assignments: list[tuple[str, int]]) -> list[dict]:
    """Copies of fixture games re-stamped with fresh ids and createdAt N days ago."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    out = []
    for game, (new_id, days_ago) in zip(games, assignments):
        out.append(
            {**game, "id": new_id, "createdAt": int((now - timedelta(days=days_ago)).timestamp() * 1000)}
        )
    return out


@pytest.mark.asyncio
async def test_period_filters_games_by_played_at(client, monkeypatch, peremil_games):
    games = _at_days_ago(
        peremil_games,
        [("recent0001", 5), ("recent0002", 10), ("old0000001", 100), ("old0000002", 200), ("old0000003", 300)],
    )
    monkeypatch.setattr("app.routers.puzzles.fetch_games", _make_fake_fetch_games(games))

    month = await client.get(
        "/api/players/peremil/puzzles",
        params={"preset": "custom", "threshold": 10, "period": "month"},
    )
    assert month.status_code == 200
    assert month.json()["games_scanned"] == 2  # only the two recent games

    year = await client.get(
        "/api/players/peremil/puzzles",
        params={"preset": "custom", "threshold": 10, "period": "year"},
    )
    assert year.json()["games_scanned"] == 5

    default = await client.get(
        "/api/players/peremil/puzzles", params={"preset": "custom", "threshold": 10}
    )
    # last20 (the default) serves the whole accumulated pool, exactly as Phase 1.
    assert default.json()["games_scanned"] == 5


@pytest.mark.asyncio
async def test_period_with_no_games_in_window_returns_reason(client, monkeypatch, peremil_games):
    games = _at_days_ago(peremil_games[:2], [("old0000001", 100), ("old0000002", 200)])
    monkeypatch.setattr("app.routers.puzzles.fetch_games", _make_fake_fetch_games(games))

    resp = await client.get(
        "/api/players/peremil/puzzles",
        params={"preset": "custom", "threshold": 10, "period": "day"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["puzzles"] == []
    assert body["reason"] == "no_games_in_period"


@pytest.mark.asyncio
async def test_period_backfill_fetches_once_then_serves_from_coverage(
    client, monkeypatch, peremil_games
):
    games = _at_days_ago(peremil_games[:2], [("recent0001", 5), ("recent0002", 10)])
    calls: list[dict] = []

    async def counting_fake(username, *, max_games=20, since=None, until=None, timeout=30.0):
        calls.append({"max_games": max_games, "since": since, "until": until, "timeout": timeout})
        for g in games:
            yield g

    monkeypatch.setattr("app.routers.puzzles.fetch_games", counting_fake)

    # Initial default search: forward fill only.
    await client.get("/api/players/peremil/puzzles", params={"preset": "custom", "threshold": 10})
    assert len(calls) == 1
    assert calls[0]["until"] is None

    # Year request on a fresh TTL: no forward fetch, one backward fill bounded
    # by the oldest stored game, at the long-period cap and timeout.
    year1 = await client.get(
        "/api/players/peremil/puzzles",
        params={"preset": "custom", "threshold": 10, "period": "year"},
    )
    assert year1.status_code == 200
    assert len(calls) == 2
    assert calls[1]["until"] is not None
    assert calls[1]["max_games"] == 500
    assert calls[1]["timeout"] == 60.0

    # Second year request: coverage now reaches back a year — no upstream fetch.
    await client.get(
        "/api/players/peremil/puzzles",
        params={"preset": "custom", "threshold": 10, "period": "year"},
    )
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_invalid_period_returns_422(client):
    resp = await client.get("/api/players/peremil/puzzles", params={"period": "decade"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_single_move_requests_unchanged_regression(db_sessionmaker, client):
    # Phase 1 clients send only move_uci; every pre-existing field must behave
    # exactly as before, with the new fields at their inert defaults.
    puzzle_id = await _seed_line_puzzle(db_sessionmaker)
    resp = await client.post(f"/api/puzzles/{puzzle_id}/attempt", json={"move_uci": "b3b4"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["correct"] is True
    assert body["solution_uci"] == "b3b4"
    assert body["solution_san"] == "Qb4"
    assert body["played_san"] == "Qb1"
    assert body["variation_san"] == LINE_SAN.split()  # full line, exactly as Phase 1
    assert body["opponent_reply_uci"] is None
    assert body["line_complete"] is True
