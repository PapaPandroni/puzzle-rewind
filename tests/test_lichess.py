from pathlib import Path

import httpx
import pytest

from app import lichess

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _ndjson_transport(
    body: bytes, status_code: int = 200, captured_headers: dict | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured_headers is not None:
            captured_headers.update(request.headers)
        return httpx.Response(status_code, content=body)

    return httpx.MockTransport(handler)


def _patch_client(monkeypatch, transport: httpx.MockTransport, seen_timeouts: list | None = None):
    def _build_client(timeout: float = 30.0) -> httpx.AsyncClient:
        if seen_timeouts is not None:
            seen_timeouts.append(timeout)
        return httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(timeout))

    monkeypatch.setattr(lichess, "_build_client", _build_client)


@pytest.mark.asyncio
async def test_fetch_games_parses_ndjson_lines(monkeypatch):
    body = (FIXTURES_DIR / "games_peremil.ndjson").read_bytes()
    _patch_client(monkeypatch, _ndjson_transport(body))

    games = [g async for g in lichess.fetch_games("peremil")]

    assert len(games) == 5
    assert all(g["variant"] == "standard" for g in games)
    assert all("analysis" in g for g in games)
    assert games[0]["id"] == "5A0GVdKN"


@pytest.mark.asyncio
async def test_fetch_games_skips_empty_lines(monkeypatch):
    body = b'{"id":"a","variant":"standard","analysis":[{"eval":0}]}\n\n\n{"id":"b","variant":"standard","analysis":[{"eval":0}]}\n'
    _patch_client(monkeypatch, _ndjson_transport(body))

    games = [g async for g in lichess.fetch_games("someone")]

    assert [g["id"] for g in games] == ["a", "b"]


@pytest.mark.asyncio
async def test_fetch_games_skips_non_standard_variant(monkeypatch):
    body = (
        b'{"id":"std","variant":"standard","analysis":[{"eval":0}]}\n'
        b'{"id":"chess960","variant":"chess960","analysis":[{"eval":0}]}\n'
    )
    _patch_client(monkeypatch, _ndjson_transport(body))

    games = [g async for g in lichess.fetch_games("someone")]

    assert [g["id"] for g in games] == ["std"]


@pytest.mark.asyncio
async def test_fetch_games_skips_missing_analysis(monkeypatch):
    body = (
        b'{"id":"analyzed","variant":"standard","analysis":[{"eval":0}]}\n'
        b'{"id":"unanalyzed","variant":"standard"}\n'
    )
    _patch_client(monkeypatch, _ndjson_transport(body))

    games = [g async for g in lichess.fetch_games("someone")]

    assert [g["id"] for g in games] == ["analyzed"]


@pytest.mark.asyncio
async def test_fetch_games_404_raises_user_not_found(monkeypatch):
    _patch_client(monkeypatch, _ndjson_transport(b"", status_code=404))

    with pytest.raises(lichess.LichessUserNotFound):
        async for _ in lichess.fetch_games("ghost-user-does-not-exist"):
            pass


@pytest.mark.asyncio
async def test_fetch_games_429_raises_rate_limited(monkeypatch):
    _patch_client(monkeypatch, _ndjson_transport(b"", status_code=429))

    with pytest.raises(lichess.LichessRateLimited):
        async for _ in lichess.fetch_games("someone"):
            pass


@pytest.mark.asyncio
async def test_fetch_games_empty_stream_yields_nothing(monkeypatch):
    _patch_client(monkeypatch, _ndjson_transport(b""))

    games = [g async for g in lichess.fetch_games("no-analyzed-games-user")]

    assert games == []


@pytest.mark.asyncio
async def test_fetch_games_serializes_since_until_and_timeout(monkeypatch):
    captured_urls: list[httpx.URL] = []
    seen_timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(request.url)
        return httpx.Response(200, content=b"")

    _patch_client(monkeypatch, httpx.MockTransport(handler), seen_timeouts=seen_timeouts)

    async for _ in lichess.fetch_games(
        "someone", max_games=300, since=1000, until=2000, timeout=60.0
    ):
        pass

    params = dict(captured_urls[0].params)
    assert params["since"] == "1000"
    assert params["until"] == "2000"
    assert params["max"] == "300"
    assert seen_timeouts == [60.0]


@pytest.mark.asyncio
async def test_fetch_games_omits_since_until_by_default(monkeypatch):
    captured_urls: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(request.url)
        return httpx.Response(200, content=b"")

    _patch_client(monkeypatch, httpx.MockTransport(handler))

    async for _ in lichess.fetch_games("someone"):
        pass

    params = dict(captured_urls[0].params)
    assert "since" not in params
    assert "until" not in params


@pytest.mark.asyncio
async def test_fetch_games_no_auth_header_by_default(monkeypatch):
    captured: dict = {}
    _patch_client(monkeypatch, _ndjson_transport(b"", captured_headers=captured))

    async for _ in lichess.fetch_games("someone"):
        pass

    assert "authorization" not in captured


@pytest.mark.asyncio
async def test_fetch_games_sends_bearer_token_when_configured(monkeypatch):
    monkeypatch.setattr(lichess.settings, "lichess_token", "test-token")
    captured: dict = {}
    _patch_client(monkeypatch, _ndjson_transport(b"", captured_headers=captured))

    async for _ in lichess.fetch_games("someone"):
        pass

    assert captured["authorization"] == "Bearer test-token"


def _param_capturing_transport(body: bytes, captured_params: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_fetch_games_analysed_false_omits_param_and_yields_unanalyzed(monkeypatch):
    # analysed=False must OMIT the query param (an explicit analysed=false
    # would ask Lichess for *only unanalyzed* games — §14 wants all of them)
    # and pass analysis-less games through instead of skipping them.
    body = (
        b'{"id":"analyzed","variant":"standard","analysis":[{"eval":0}]}\n'
        b'{"id":"unanalyzed","variant":"standard"}\n'
    )
    captured_params: dict = {}
    _patch_client(monkeypatch, _param_capturing_transport(body, captured_params))

    games = [g async for g in lichess.fetch_games("someone", analysed=False)]

    assert [g["id"] for g in games] == ["analyzed", "unanalyzed"]
    assert "analysed" not in captured_params
    assert captured_params["evals"] == "true"  # analyzed games still arrive with evals


@pytest.mark.asyncio
async def test_fetch_games_default_still_requests_analysed_only(monkeypatch):
    body = b'{"id":"analyzed","variant":"standard","analysis":[{"eval":0}]}\n'
    captured_params: dict = {}
    _patch_client(monkeypatch, _param_capturing_transport(body, captured_params))

    [g async for g in lichess.fetch_games("someone")]

    assert captured_params["analysed"] == "true"
