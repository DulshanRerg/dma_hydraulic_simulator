# app/routers/files.py
"""
GET /files
----------
Lists all .gpkg files available in the shared volume so the main system
can discover which network files are ready for simulation.
"""

import os
from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.config import get_settings
from app.services.network_builder import list_gpkg_files

router = APIRouter(prefix="/files", tags=["files"])


class GpkgFileInfo(BaseModel):
    filename:  str
    size_kb:   float
    layer:     str = "unknown"


@router.get("", response_model=List[GpkgFileInfo])
def list_files(_: str = Depends(require_api_key)):
    """
    Return all .gpkg files available in the shared volume.

    The main system can call this to discover which files are available
    before submitting a simulation.
    """
    settings = get_settings()
    files    = list_gpkg_files()
    result   = []

    for fname in files:
        path    = os.path.join(settings.gpkg_dir, fname)
        size_kb = round(os.path.getsize(path) / 1024, 1)

        # try to read the layer name from gpkg_contents
        layer = "unknown"
        try:
            import sqlite3
            conn = sqlite3.connect(path)
            row  = conn.execute(
                "SELECT table_name FROM gpkg_contents LIMIT 1"
            ).fetchone()
            if row:
                layer = row[0]
            conn.close()
        except Exception:
            pass

        result.append(GpkgFileInfo(filename=fname, size_kb=size_kb, layer=layer))

    return result
