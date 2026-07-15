# app/core/config.py
from functools import lru_cache
from typing import List
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # service identity
    service_name:    str  = "EPANET-backed Backend Service"
    service_version: str  = "1.0.0"
    debug:           bool = False

    # auth — stored as a comma-separated string, exposed as a list
    api_keys: str = "change-me-key-1"

    @field_validator("api_keys", mode="before")
    @classmethod
    def parse_api_keys(cls, v):
        return v  # kept as raw string; split in property below

    @property
    def api_key_list(self) -> List[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

    # paths
    gpkg_dir:     str = "./data/gpkg"
    # Raw EPANET .inp files uploaded directly (bypassing the GIS→.inp
    # builder pipeline) live here. Same persistence caveat as gpkg_dir:
    # on Render's free tier this is wiped on redeploy/restart.
    inp_dir:      str = "./data/inp"
    database_url: str = "sqlite+aiosqlite:///./data/db/epanet_service.db"
    # EPANET .rpt report files are copied here after each simulation so they
    # survive the temp-dir cleanup and can be viewed/downloaded later.
    reports_dir:  str = "./data/reports"

    # simulation defaults
    default_duration_hrs: int   = 24
    default_timestep_min: int   = 60
    default_base_demand:  float = 0.001
    min_pressure_m:       float = 7.0
    max_velocity_ms:      float = 3.0


@lru_cache
def get_settings() -> Settings:
    return Settings()