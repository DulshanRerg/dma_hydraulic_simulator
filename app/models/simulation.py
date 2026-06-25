# app/models/simulation.py
"""
ORM models for the hydraulic simulation service.

Tables
------
  sim_scenarios  — one row per simulation job (inputs, status, summary)
  sim_results    — per-element results (nodes and pipes, per time step)
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey,
    Integer, JSON, String, Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class SimScenario(Base):
    __tablename__ = "sim_scenarios"

    id:              Mapped[int]            = mapped_column(Integer, primary_key=True, index=True)
    gpkg_filename:   Mapped[str]            = mapped_column(String(256), nullable=False)
    name:            Mapped[str]            = mapped_column(String(128), default="Unnamed")
    description:     Mapped[Optional[str]]  = mapped_column(Text, nullable=True)

    # simulation parameters (stored for reproducibility)
    base_demand:     Mapped[float]          = mapped_column(Float,   default=0.001)
    duration_hrs:    Mapped[int]            = mapped_column(Integer, default=24)
    time_step_min:   Mapped[int]            = mapped_column(Integer, default=60)
    pipe_status:     Mapped[str]            = mapped_column(String(32), default="OPERATIONAL")
    reservoir_head:  Mapped[float]          = mapped_column(Float,   default=50.0)
    # optional JSON array of extra demands: [{"lat": -2.5, "lon": 32.9, "demand_m3s": 0.005}]
    extra_demands:   Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # sub-network simulation (selection workflow): when pipe_ids is set,
    # the worker builds the .inp from exactly these pipe fids instead of
    # the whole network, with reservoir_lat/lon as the user-chosen source.
    pipe_ids:         Mapped[Optional[dict]]  = mapped_column(JSON,  nullable=True)
    reservoir_lat:    Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reservoir_lon:    Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    snap_tolerance_m: Mapped[float]           = mapped_column(Float, default=2.0)

    # lifecycle
    status:          Mapped[str]            = mapped_column(String(20), default="PENDING")
    error_message:   Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    created_at:      Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)
    started_at:      Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at:     Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # summary JSON (written once at completion)
    summary:         Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    results: Mapped[list["SimResult"]] = relationship(
        "SimResult", back_populates="scenario", cascade="all, delete-orphan"
    )


class SimResult(Base):
    __tablename__ = "sim_results"

    id:               Mapped[int]           = mapped_column(Integer, primary_key=True, index=True)
    scenario_id:      Mapped[int]           = mapped_column(
        Integer, ForeignKey("sim_scenarios.id", ondelete="CASCADE"), index=True
    )
    time_step:        Mapped[int]           = mapped_column(Integer, default=0)   # hours
    element_type:     Mapped[str]           = mapped_column(String(8))            # 'node'|'pipe'
    element_id:       Mapped[str]           = mapped_column(String(128))
    lat:              Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lon:              Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # node
    pressure:         Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    head:             Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    demand:           Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    water_age:        Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_low_pressure:  Mapped[bool]           = mapped_column(Boolean, default=False)

    # pipe
    flow_rate:        Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    velocity:         Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    headloss:         Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_high_velocity: Mapped[bool]            = mapped_column(Boolean, default=False)

    scenario: Mapped["SimScenario"] = relationship("SimScenario", back_populates="results")
