# tests/test_network.py
"""
Tests for the network-exploration / sub-network-selection endpoints.

Run with:
    pytest tests/test_network.py -v

Requires the service to have a test .gpkg file available, same as
tests/test_api.py. Set TEST_GPKG_FILE if it isn't duwas_network.gpkg.
"""

import asyncio
import os

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("API_KEYS",     "test-key-123")
os.environ.setdefault("GPKG_DIR",     "/data/gpkg")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/test_epanet.db")

from app.main import app  # noqa: E402

TEST_KEY     = "test-key-123"
TEST_GPKG    = os.getenv("TEST_GPKG_FILE", "duwas_network.gpkg")
AUTH_HEADERS = {"X-API-Key": TEST_KEY}


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _gpkg_present() -> bool:
    return os.path.isfile(os.path.join(os.getenv("GPKG_DIR", "/data/gpkg"), TEST_GPKG))


# ── GET /network/{filename}/pipes ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_pipes_geojson_requires_api_key(client):
    r = await client.get(f"/network/{TEST_GPKG}/pipes")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_pipes_geojson_missing_file(client):
    r = await client.get("/network/does_not_exist.gpkg/pipes", headers=AUTH_HEADERS)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_pipes_geojson_shape(client):
    if not _gpkg_present():
        pytest.skip(f"Test gpkg not found: {TEST_GPKG}")
    r = await client.get(f"/network/{TEST_GPKG}/pipes", headers=AUTH_HEADERS)
    assert r.status_code == 200
    gj = r.json()
    assert gj["type"] == "FeatureCollection"
    assert len(gj["features"]) > 0
    feat = gj["features"][0]
    assert feat["geometry"]["type"] == "LineString"
    assert "fid" in feat["properties"]


# ── POST /network/{filename}/select ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_select_validates_body(client):
    if not _gpkg_present():
        pytest.skip(f"Test gpkg not found: {TEST_GPKG}")
    # selection_type='pipes' but no pipe_ids → 422
    r = await client.post(
        f"/network/{TEST_GPKG}/select", headers=AUTH_HEADERS,
        json={"selection_type": "pipes"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_select_by_pipe_ids(client):
    if not _gpkg_present():
        pytest.skip(f"Test gpkg not found: {TEST_GPKG}")
    pipes = (await client.get(f"/network/{TEST_GPKG}/pipes", headers=AUTH_HEADERS)).json()
    fids = [f["properties"]["fid"] for f in pipes["features"][:30]]

    r = await client.post(
        f"/network/{TEST_GPKG}/select", headers=AUTH_HEADERS,
        json={"selection_type": "pipes", "pipe_ids": fids, "snap_tolerance_m": 2.0},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["matched_pipe_count"] == len(set(fids))
    assert len(body["components"]) >= 1
    comp = body["components"][0]
    assert comp["pipe_count"] > 0
    assert len(comp["nodes"]) == comp["node_count"]
    assert comp["geojson"]["type"] == "FeatureCollection"


@pytest.mark.asyncio
async def test_select_by_point(client):
    if not _gpkg_present():
        pytest.skip(f"Test gpkg not found: {TEST_GPKG}")
    pipes = (await client.get(f"/network/{TEST_GPKG}/pipes", headers=AUTH_HEADERS)).json()
    lon, lat = pipes["features"][0]["geometry"]["coordinates"][0]

    r = await client.post(
        f"/network/{TEST_GPKG}/select", headers=AUTH_HEADERS,
        json={"selection_type": "point", "lat": lat, "lon": lon, "radius_m": 200},
    )
    assert r.status_code == 200
    assert r.json()["matched_pipe_count"] >= 1


@pytest.mark.asyncio
async def test_select_no_match_returns_422(client):
    if not _gpkg_present():
        pytest.skip(f"Test gpkg not found: {TEST_GPKG}")
    r = await client.post(
        f"/network/{TEST_GPKG}/select", headers=AUTH_HEADERS,
        json={"selection_type": "point", "lat": 0.0, "lon": 0.0, "radius_m": 5},
    )
    assert r.status_code == 422


# ── full subset simulation lifecycle ────────────────────────────────────────

@pytest.mark.asyncio
async def test_subset_simulation_lifecycle(client):
    """select a small connected piece → simulate it → confirm results scoped to it."""
    if not _gpkg_present():
        pytest.skip(f"Test gpkg not found: {TEST_GPKG}")

    pipes = (await client.get(f"/network/{TEST_GPKG}/pipes", headers=AUTH_HEADERS)).json()
    fids = [f["properties"]["fid"] for f in pipes["features"][:80]]

    sel = (await client.post(
        f"/network/{TEST_GPKG}/select", headers=AUTH_HEADERS,
        json={"selection_type": "pipes", "pipe_ids": fids, "snap_tolerance_m": 2.0},
    )).json()
    comp = sel["components"][0]
    node = comp["nodes"][0]

    r = await client.post("/simulate", headers=AUTH_HEADERS, json={
        "gpkg_filename":     TEST_GPKG,
        "name":              "Subset test run",
        "duration_hrs":      1,
        "time_step_min":     60,
        "base_demand":       0.001,
        "pipe_ids":          comp["pipe_ids"],
        "reservoir_lat":     node["lat"],
        "reservoir_lon":     node["lon"],
        "snap_tolerance_m":  2.0,
    })
    assert r.status_code == 202
    scenario_id = r.json()["id"]
    assert r.json()["pipe_ids"] == comp["pipe_ids"]

    for _ in range(40):
        await asyncio.sleep(3)
        data = (await client.get(f"/simulate/{scenario_id}", headers=AUTH_HEADERS)).json()
        if data["status"] in ("DONE", "FAILED"):
            break

    assert data["status"] == "DONE", f"Subset simulation failed: {data.get('error_message')}"
    assert data["summary"]["total_pipes"] <= len(comp["pipe_ids"])

    r = await client.delete(f"/simulate/{scenario_id}", headers=AUTH_HEADERS)
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_simulate_pipe_ids_requires_reservoir(client):
    r = await client.post("/simulate", headers=AUTH_HEADERS, json={
        "gpkg_filename": TEST_GPKG,
        "pipe_ids":      [1, 2, 3],
    })
    assert r.status_code == 422
