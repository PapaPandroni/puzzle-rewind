from pathlib import Path

import httpx
import pytest

from app import lichess

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _ndjson_transport(body: bytes, status_code: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=body)

    return httpx.MockTransport(handler)


def _patch_client(monkeypatch, transport: httpx.MockTransport):
    def _build_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(30.0))

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
