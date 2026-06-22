# tests/test_api.py
"""
Integration tests for the EPANET simulation service.

Run with:
    pytest tests/ -v

Requires the service to have a test .gpkg file available.
Set TEST_GPKG_FILE env var to your filename, e.g.:
    TEST_GPKG_FILE=duwas_network_clean.gpkg pytest tests/ -v
"""

import os
import asyncio
import pytest
from httpx import AsyncClient, ASGITransport

# set test env before importing the app
os.environ.setdefault("API_KEYS",       "test-key-123")
os.environ.setdefault("GPKG_DIR",       "/data/gpkg")
os.environ.setdefault("DATABASE_URL",   "sqlite+aiosqlite:////tmp/test_epanet.db")

from app.main import app  # noqa: E402

TEST_KEY      = "test-key-123"
TEST_GPKG     = os.getenv("TEST_GPKG_FILE", "duwas_network_clean.gpkg")
AUTH_HEADERS  = {"X-API-Key": TEST_KEY}


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ── auth tests ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_api_key_rejected(client):
    r = await client.get("/files")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_wrong_api_key_rejected(client):
    r = await client.get("/files", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


# ── health ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ── files ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_files(client):
    r = await client.get("/files", headers=AUTH_HEADERS)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ── simulation lifecycle ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_simulation_missing_file(client):
    r = await client.post("/simulate", headers=AUTH_HEADERS, json={
        "gpkg_filename": "does_not_exist.gpkg",
        "name": "Should fail",
    })
    # 202 queued, but will transition to FAILED
    assert r.status_code in (202, 404)


@pytest.mark.asyncio
async def test_simulate_not_found(client):
    r = await client.get("/simulate/99999", headers=AUTH_HEADERS)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_simulations(client):
    r = await client.get("/simulate", headers=AUTH_HEADERS)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_list_simulations_filter_status(client):
    r = await client.get("/simulate?status=DONE", headers=AUTH_HEADERS)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_delete_nonexistent(client):
    r = await client.delete("/simulate/99999", headers=AUTH_HEADERS)
    assert r.status_code == 404


# ── full simulation (skipped if no gpkg file present) ─────────────────────────

@pytest.mark.asyncio
async def test_full_simulation_lifecycle(client):
    """
    End-to-end test: submit → poll until DONE → fetch results → alerts → GeoJSON.
    Skipped if TEST_GPKG_FILE is not present on disk.
    """
    gpkg_dir = os.getenv("GPKG_DIR", "/data/gpkg")
    if not os.path.isfile(os.path.join(gpkg_dir, TEST_GPKG)):
        pytest.skip(f"Test gpkg not found: {TEST_GPKG}")

    # submit
    r = await client.post("/simulate", headers=AUTH_HEADERS, json={
        "gpkg_filename": TEST_GPKG,
        "name":          "Test run",
        "duration_hrs":  1,
        "time_step_min": 60,
        "base_demand":   0.001,
    })
    assert r.status_code == 202
    scenario_id = r.json()["id"]

    # poll until done or failed (timeout after 120 s)
    import asyncio as aio
    for _ in range(40):
        await aio.sleep(3)
        r = await client.get(f"/simulate/{scenario_id}", headers=AUTH_HEADERS)
        data = r.json()
        if data["status"] in ("DONE", "FAILED"):
            break

    assert data["status"] == "DONE", f"Simulation failed: {data.get('error_message')}"
    assert data["summary"] is not None

    # node results at hour 0
    r = await client.get(f"/simulate/{scenario_id}/nodes?time_step=0", headers=AUTH_HEADERS)
    assert r.status_code == 200
    nodes = r.json()
    assert len(nodes) > 0
    assert "pressure" in nodes[0]

    # pipe results at hour 0
    r = await client.get(f"/simulate/{scenario_id}/pipes?time_step=0", headers=AUTH_HEADERS)
    assert r.status_code == 200
    assert len(r.json()) > 0

    # alerts
    r = await client.get(f"/simulate/{scenario_id}/alerts?time_step=0", headers=AUTH_HEADERS)
    assert r.status_code == 200
    alerts = r.json()
    assert "low_pressure_nodes" in alerts
    assert "high_velocity_pipes" in alerts

    # GeoJSON
    r = await client.get(f"/simulate/{scenario_id}/geojson/nodes?time_step=0", headers=AUTH_HEADERS)
    assert r.status_code == 200
    gj = r.json()
    assert gj["type"] == "FeatureCollection"
    assert len(gj["features"]) > 0

    # cleanup
    r = await client.delete(f"/simulate/{scenario_id}", headers=AUTH_HEADERS)
    assert r.status_code == 204
