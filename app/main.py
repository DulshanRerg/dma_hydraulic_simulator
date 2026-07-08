# app/main.py
"""
EPANET Hydraulic Simulation Service — FastAPI entry point.

Start with:
    uvicorn app.main:app --host 0.0.0.0 --port 8080
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.database import init_db
from app.routers import dma, files, simulation, upload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise the database on startup."""
    logger.info("Initialising database …")
    await init_db()
    logger.info("Database ready.")
    yield
    logger.info("Shutting down.")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title       = settings.service_name,
        version     = settings.service_version,
        description = (
            "DUWASA DMA Hydraulic Simulation Service.\n\n"
            "Reads multi-layer `.gpkg` files (pipes, sources, tanks, valves, DMA boundary) "
            "from a shared volume, builds EPANET models with automatic topology repair, "
            "runs 24-hour Extended Period Simulations via **EPyT-Flow / EPANET**, and "
            "exposes pressure, flow, NRW, and leakage-risk results through a REST API "
            "secured by API-key authentication.\n\n"
            "All endpoints require the `X-API-Key` header.\n\n"
            "Start a simulation: `POST /dma/{filename}/simulate`"
        ),
        lifespan    = lifespan,
        docs_url    = "/docs",
        redoc_url   = "/redoc",
    )

    # ── CORS (restrict origins for production) ────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins     = ["*"],
        allow_credentials = True,
        allow_methods     = ["*"],
        allow_headers     = ["*"],
    )

    # ── routers ───────────────────────────────────────────────────────────────
    app.include_router(files.router)
    app.include_router(upload.router)
    app.include_router(dma.router)
    app.include_router(simulation.router)

    # ── health check (no auth required) ───────────────────────────────────────
    @app.get("/health", tags=["health"], include_in_schema=False)
    async def health():
        return JSONResponse({"status": "ok", "service": settings.service_name})

    return app


app = create_app()