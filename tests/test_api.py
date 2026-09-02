import httpx
import pytest

from hlsd.api import create_app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HLSD_DATA_DIR", str(tmp_path))
    from hlsd.config import DaemonConfig

    app = create_app(DaemonConfig(data_dir=tmp_path / ".hlsd"))
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        app.router.lifespan_context(app),
    ):
        yield c


async def test_health_and_empty_lists(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert (await client.get("/requests")).json() == []
    assert (await client.get("/sources")).json() == []


async def test_create_scheduled_and_cancel(client):
    r = await client.post(
        "/requests",
        json={"url": "https://x/live.m3u8", "mode": "window", "duration": 60, "start": "in 1m"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["state"] == "scheduled"

    r = await client.get(f"/requests/{body['id']}")
    assert r.json()["state"] == "scheduled"

    r = await client.post(f"/requests/{body['id']}/stop")
    assert r.status_code == 200
    assert r.json()["state"] == "cancelled"


async def test_create_validation_errors(client):
    r = await client.post("/requests", json={"mode": "window"})
    assert r.status_code == 400
    r = await client.post("/requests", json={"url": "https://x/m.m3u8", "mode": "invalid-mode"})
    assert r.status_code == 422


async def test_accepts_fetch_format(client):
    r = await client.post("/requests", json={"curl": 'fetch("https://invalid-hlsd.test/m.m3u8", {"method": "GET"})'})
    assert r.status_code == 201
    assert r.json()["state"] in ("resolving", "active", "failed")


async def test_streams_endpoints(client):
    r = await client.get("/streams")
    assert r.status_code == 200
    assert r.json() == []
    r = await client.get("/streams/nonexistent/playlist.m3u8")
    assert r.status_code == 404


async def test_inspect_unreachable_returns_502(client):
    r = await client.post("/inspect", json={"url": "https://invalid-hlsd.test/x.m3u8"})
    assert r.status_code == 502


async def test_stop_unknown_request(client):
    r = await client.post("/requests/nonexistent/stop")
    assert r.status_code in (404, 409)
