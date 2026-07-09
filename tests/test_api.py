import pytest

from app.lichess import LichessRateLimited, LichessUserNotFound


def _make_fake_fetch_games(games: list[dict]):
    async def fake_fetch_games(username: str, *, max_games: int = 20, since: int | None = None):
        for g in games:
            yield g

    return fake_fetch_games


def _make_raising_fetch_games(exc: Exception):
    async def fake_fetch_games(username: str, *, max_games: int = 20, since: int | None = None):
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
    }
    # Solution must never leak in the list response.
    assert "solution_uci" not in puzzle
    assert "solution_san" not in puzzle


@pytest.mark.asyncio
async def test_threshold_filtering_is_query_time_not_refetch(client, monkeypatch, peremil_games):
    call_count = 0
    fake = _make_fake_fetch_games(peremil_games)

    async def counting_fake_fetch_games(username, *, max_games=20, since=None):
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
