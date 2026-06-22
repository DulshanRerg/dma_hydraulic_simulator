# app/core/config.py

from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    service_name: str = "EPANET Hydraulic Service"
    service_version: str = "1.0.0"
    debug: bool = False

    api_keys_raw: str = Field(
        default="change-me-key-1",
        validation_alias="API_KEYS"
    )

    @property
    def api_keys(self) -> List[str]:
        return [
            k.strip()
            for k in self.api_keys_raw.split(",")
            if k.strip()
        ]

    gpkg_dir: str = "./data/gpkg"
    database_url: str = "sqlite+aiosqlite:///./data/db/epanet_service.db"

    default_duration_hrs: int = 24
    default_timestep_min: int = 60
    default_base_demand: float = 0.001
    min_pressure_m: float = 7.0
    max_velocity_ms: float = 3.0


@lru_cache
def get_settings() -> Settings:
    return Settings()