# app/core/exceptions.py
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


class GpkgNotFoundError(HTTPException):
    def __init__(self, filename: str):
        super().__init__(
            status_code=404,
            detail=f"GeoPackage file '{filename}' not found in the configured directory.",
        )


class SimulationNotFoundError(HTTPException):
    def __init__(self, scenario_id: int):
        super().__init__(
            status_code=404,
            detail=f"Simulation scenario {scenario_id} does not exist.",
        )


class SimulationStillRunningError(HTTPException):
    def __init__(self, scenario_id: int):
        super().__init__(
            status_code=409,
            detail=f"Scenario {scenario_id} is still running. Try again later.",
        )


class InvalidGpkgError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status_code=422, detail=f"Invalid GeoPackage: {detail}")
