# tests/test_dma.py
"""
Integration tests for the DMA endpoints:
  GET  /dma/{file}/layers
  POST /dma/{file}/simulate
  GET  /dma/{file}/simulate/{id}/leakage
  GET  /dma/{file}/simulate/{id}/nrw
"""

import asyncio
import os

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("API_KEYS",     "test-key-123")
os.environ.setdefault("GPKG_DIR",     "/data/gpkg")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/test_dma.db")

from app.main import app  # noqa: E402

AUTH    = {"X-API-Key": "test-key-123"}
DMA_FILE = os.getenv("TEST_DMA_FILE", "DUWASA.gpkg")


def _dma_present() -> bool:
    return os.path.isfile(
        os.path.join(os.getenv("GPKG_DIR", "/data/gpkg"), DMA_FILE)
    )


@pytest.fixture(scope="session")
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── GET /dma/{file}/layers ─────────────────────────────────────────────────

async def test_layers_auth_required(client):
    r = await client.get(f"/dma/{DMA_FILE}/layers")
    assert r.status_code == 401


async def test_layers_missing_file(client):
    r = await client.get("/dma/does_not_exist.gpkg/layers", headers=AUTH)
    assert r.status_code == 404


async def test_layers_shape(client):
    if not _dma_present():
        pytest.skip(f"DMA gpkg not found: {DMA_FILE}")

    r = await client.get(f"/dma/{DMA_FILE}/layers", headers=AUTH)
    assert r.status_code == 200
    d = r.json()

    assert "dma_name" in d
    assert "dma_bbox" in d
    assert len(d["dma_bbox"]) == 4

    for layer in ("boundary", "pipes", "sources", "tanks", "valves", "bulk_meters"):
        assert layer in d, f"Missing layer: {layer}"
        assert d[layer]["type"] == "FeatureCollection"

    st = d["stats"]
    assert st["pipe_count"]   > 0
    assert st["source_count"] >= 0
    assert st["tank_count"]   >= 0

    # Pipes have required properties
    pipe = d["pipes"]["features"][0]
    assert "diam_mm"  in pipe["properties"]
    assert "hw_c"     in pipe["properties"]
    assert "length_m" in pipe["properties"]

    # Sources have total_head_m (used as EPANET reservoir head)
    for f in d["sources"]["features"]:
        assert "total_head_m" in f["properties"]
        assert f["properties"]["total_head_m"] > 0


# ── POST /dma/{file}/simulate ──────────────────────────────────────────────

async def test_simulate_validation(client):
    if not _dma_present():
        pytest.skip(f"DMA gpkg not found: {DMA_FILE}")

    # leakage_frac > 1.0 → 422
    r = await client.post(
        f"/dma/{DMA_FILE}/simulate",
        headers=AUTH,
        json={"leakage_frac": 1.5},
    )
    assert r.status_code == 422


async def test_simulate_missing_file(client):
    r = await client.post(
        "/dma/missing.gpkg/simulate",
        headers=AUTH,
        json={"name": "test"},
    )
    assert r.status_code == 404


async def test_full_dma_simulation_lifecycle(client):
    """
    Full lifecycle:
      POST simulate → poll DONE → GET leakage report → GET NRW
    """
    if not _dma_present():
        pytest.skip(f"DMA gpkg not found: {DMA_FILE}")

    # 1. Start simulation
    r = await client.post(
        f"/dma/{DMA_FILE}/simulate",
        headers=AUTH,
        json={
            "name":             "pytest DMA run",
            "duration_hrs":     1,
            "time_step_min":    60,
            "base_demand_m3h":  0.011,
            "leakage_frac":     0.20,
        },
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert "id" in body
    sid = body["id"]

    # Topology repair metadata must be present
    tr = body.get("topology_repair", {})
    assert "original_components" in tr
    assert "connectors_added"    in tr
    assert tr["original_components"] >= 1

    # Connector GeoJSON structure
    cgj = tr.get("connectors_geojson", {})
    assert cgj.get("type") == "FeatureCollection"

    # 2. Poll until DONE
    for _ in range(60):
        await asyncio.sleep(3)
        s = (await client.get(f"/simulate/{sid}", headers=AUTH)).json()
        if s["status"] in ("DONE", "FAILED"):
            break

    assert s["status"] == "DONE", f"Simulation failed: {s.get('error_message')}"

    # Summary sanity checks
    sm = s["summary"]
    assert sm["total_nodes"] > 0
    assert sm["total_pipes"] > 0
    assert sm["pressure_avg_m"] > 0

    # 3. GET leakage report
    r = await client.get(
        f"/dma/{DMA_FILE}/simulate/{sid}/leakage",
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    lr = r.json()

    assert lr["dma_name"]
    assert "nrw" in lr
    nrw = lr["nrw"]
    assert 0 <= nrw["nrw_pct"] <= 100
    assert nrw["ili"] >= 0
    assert nrw["real_loss_m3h"] >= 0

    assert "pressure_zones" in lr
    zones = {z["zone"]: z for z in lr["pressure_zones"]}
    assert set(zones.keys()) == {"low", "normal", "high"}

    assert "pipe_risks_top20" in lr
    risks = lr["pipe_risks_top20"]
    assert len(risks) > 0
    for risk in risks:
        assert 0 <= risk["risk_score"] <= 1
        assert risk["risk_level"] in ("low", "medium", "high", "critical")

    assert "hotspots_geojson" in lr
    assert lr["hotspots_geojson"]["type"] == "FeatureCollection"

    assert "timestep_balance" in lr
    for tb in lr["timestep_balance"]:
        assert "inflow_m3h"  in tb
        assert "demand_m3h"  in tb
        assert "nrw_m3h"     in tb

    # 4. GET NRW endpoint
    r = await client.get(
        f"/dma/{DMA_FILE}/simulate/{sid}/nrw",
        headers=AUTH,
    )
    assert r.status_code == 200
    nrw2 = r.json()
    assert "nrw" in nrw2
    assert nrw2["nrw"]["nrw_pct"] >= 0

    # 5. Simulate result GeoJSON
    r = await client.get(
        f"/simulate/{sid}/geojson/nodes?time_step=0",
        headers=AUTH,
    )
    assert r.status_code == 200
    gj = r.json()
    assert gj["type"] == "FeatureCollection"
    assert len(gj["features"]) > 0
    feat = gj["features"][0]
    assert "pressure" in feat["properties"]

    # 6. Cleanup
    r = await client.delete(f"/simulate/{sid}", headers=AUTH)
    assert r.status_code == 204


# ── topology_repair unit tests (pure Python, no server) ────────────────────

def test_topology_repair_mst():
    """Two isolated pipe segments must be joined by one synthetic connector."""
    from app.services.topology_repair import repair_topology

    # Segment A: horizontal pipe at latitude ~-6.13
    seg_a = [(35.820, -6.130), (35.821, -6.130)]
    # Segment B: isolated segment 500 m east, no shared endpoint
    seg_b = [(35.826, -6.130), (35.827, -6.130)]

    meta = [
        {"diam_mm": 50, "hw_c": 140, "material": "PVC", "length_m": 100},
        {"diam_mm": 50, "hw_c": 140, "material": "PVC", "length_m": 100},
    ]

    result = repair_topology(
        pipe_coords  = [seg_a, seg_b],
        pipe_meta    = meta,
        snap_tol_m   = 2.0,
        node_grid_m  = 2.0,
    )

    assert result.report.original_component_count == 2
    assert result.report.final_component_count    == 1
    assert len(result.report.connectors_added)    == 1
    conn = result.report.connectors_added[0]
    assert conn.length_m > 0
    assert conn.is_synthetic if hasattr(conn, "is_synthetic") else True

    # All edges in result (including the synthetic one) must link valid nodes
    node_ids = set(result.nodes.keys())
    for e in result.edges:
        assert e.node_a in node_ids, f"node_a {e.node_a} not in nodes"
        assert e.node_b in node_ids, f"node_b {e.node_b} not in nodes"


def test_topology_repair_already_connected():
    """A single connected pipe → no connectors needed."""
    from app.services.topology_repair import repair_topology

    coords = [(35.820, -6.130), (35.821, -6.130), (35.822, -6.130)]
    meta   = [{"diam_mm": 100, "hw_c": 130, "material": "DI", "length_m": 200}]

    result = repair_topology(
        pipe_coords=[coords], pipe_meta=meta, snap_tol_m=2.0,
    )
    assert result.report.final_component_count == 1
    assert len(result.report.connectors_added) == 0


def test_topology_repair_utm_roundtrip():
    """WGS84 → UTM37S → WGS84 should recover coordinates within 1 mm."""
    from app.services.topology_repair import _wgs84_to_utm37s, _utm37s_to_wgs84
    import math

    test_points = [
        (35.832, -6.115),   # inside the DMA
        (35.850, -6.140),
        (36.000, -5.900),
    ]
    for lon, lat in test_points:
        x, y = _wgs84_to_utm37s(lon, lat)
        lon2, lat2 = _utm37s_to_wgs84(x, y)
        assert abs(lon2 - lon) < 1e-5, f"lon roundtrip error: {abs(lon2-lon)}"
        assert abs(lat2 - lat) < 1e-5, f"lat roundtrip error: {abs(lat2-lat)}"


def test_leakage_analyse_smoke():
    """Leakage analysis must return a valid report without crashing."""
    from app.services.leakage_report import analyse_leakage
    from app.services.simulation_service import NodeResult, PipeResult, SimulationOutput

    node_results = [
        NodeResult("J0", 0, -6.13, 35.83, 25.0, None, None, None, False),
        NodeResult("J0", 1, -6.13, 35.83, 22.0, None, None, None, False),
        NodeResult("J1", 0, -6.14, 35.84,  3.0, None, None, None, True),
        NodeResult("J1", 1, -6.14, 35.84,  4.0, None, None, None, True),
    ]
    pipe_results = [
        PipeResult("P0", 0, -6.13, 35.83, 0.002, None, None, False),
        PipeResult("P0", 1, -6.13, 35.83, 0.003, None, None, False),
    ]
    summary = {
        "pressure_min_m": 3.0, "pressure_max_m": 25.0, "pressure_avg_m": 14.0,
        "total_nodes": 2, "total_pipes": 1, "time_steps": 2, "duration_hrs": 1,
        "low_pressure_nodes": 1,
    }
    output = SimulationOutput(node_results=node_results, pipe_results=pipe_results, summary=summary)

    report = analyse_leakage(output, scenario_id=99, dma_name="Test DMA")

    assert report.scenario_id == 99
    assert report.nrw.nrw_pct >= 0
    assert report.nrw.ili     >= 0
    assert len(report.pressure_zones) == 3
    assert len(report.pipe_risks)     > 0
    assert report.hotspots["type"]    == "FeatureCollection"
