import pytest


@pytest.mark.asyncio
async def test_railway_host_redirects_to_canonical(client):
    resp = await client.get("/", headers={"Host": "puzzle-rewind-production.up.railway.app"})
    assert resp.status_code == 301
    assert resp.headers["location"] == "https://www.puzzle-rewind.eu/"


@pytest.mark.asyncio
async def test_apex_host_redirects_to_canonical(client):
    resp = await client.get("/", headers={"Host": "puzzle-rewind.eu"})
    assert resp.status_code == 301
    assert resp.headers["location"] == "https://www.puzzle-rewind.eu/"


@pytest.mark.asyncio
async def test_healthz_is_exempt_from_redirect(client):
    resp = await client.get("/healthz", headers={"Host": "puzzle-rewind-production.up.railway.app"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_default_test_host_is_not_redirected(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
