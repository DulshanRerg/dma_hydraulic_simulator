# tests/conftest.py
"""
Shared test setup.

httpx's ASGITransport (used by both test_api.py and test_network.py) does
not trigger FastAPI's lifespan events, so app.main's `init_db()` — which
creates the sim_scenarios / sim_results tables — never runs unless
something calls it explicitly. This fixture does that once per session,
before any test hits an endpoint.
"""

import os

import pytest

os.environ.setdefault("API_KEYS",     "test-key-123")
os.environ.setdefault("GPKG_DIR",     "/data/gpkg")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/test_epanet.db")

from app.core.database import init_db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
async def _init_test_db():
    await init_db()
