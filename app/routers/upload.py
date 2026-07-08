# app/routers/upload.py
"""
POST /files/upload — accept a DMA GeoPackage (or zip of shapefiles)
and place it in the shared GPKG_DIR so it is immediately available
for simulation.

Accepted formats
----------------
  .gpkg          — single multi-layer GeoPackage (preferred)
  .zip           — zip containing either:
                     • a single .gpkg  → extracted as-is
                     • shapefiles      → converted to a multi-layer .gpkg
                       via geopandas (requires the bundled env)

Validation
----------
After extraction / conversion the file is inspected for the two
mandatory DMA layers (dma, waterpipes). Missing layers are reported
but do not block the upload — the UI warns the user instead.

Security
--------
Files are written to GPKG_DIR only. The filename is sanitised: only
alphanumerics, hyphens, underscores and dots are kept; directory
traversal sequences are stripped. Maximum upload size is enforced by
the `MAX_UPLOAD_MB` setting (default 200 MB).
"""

import io
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/files", tags=["files"])

_MAX_UPLOAD_MB     = 200
_MANDATORY_LAYERS  = {"dma", "waterpipes"}
_EXPECTED_LAYERS   = {"dma", "waterpipes", "watersources", "storagefacility", "valves", "bulk_meters"}
_SAFE_NAME_RE      = re.compile(r"[^\w\-.]")
_SHP_LAYER_MAP     = {
    "waterpipes":     ["waterpipes", "pipes", "water_pipes", "pipe"],
    "watersources":   ["watersources", "sources", "water_sources", "boreholes"],
    "storagefacility":["storagefacility", "tanks", "storage", "reservoirs"],
    "valves":         ["valves", "valve"],
    "bulk_meters":    ["bulk_meters", "meters", "bulkmeters"],
    "dma":            ["dma", "dma_boundary", "boundary", "zone"],
}


class UploadResult(BaseModel):
    filename:          str
    size_kb:           float
    layers:            List[str]
    missing_layers:    List[str]
    has_dma_structure: bool
    message:           str


def _safe_filename(name: str) -> str:
    base = os.path.basename(name)
    safe = _SAFE_NAME_RE.sub("_", base)
    # Collapse multiple underscores
    safe = re.sub(r"_+", "_", safe).strip("_.")
    if not safe.endswith(".gpkg"):
        safe += ".gpkg"
    return safe


def _gpkg_layers(path: str) -> List[str]:
    try:
        conn = sqlite3.connect(path)
        rows = conn.execute("SELECT table_name FROM gpkg_contents").fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def _convert_shapefiles_to_gpkg(shp_dir: str, out_path: str) -> List[str]:
    """
    Read any shapefiles found in `shp_dir` and write them as layers
    into a single .gpkg at `out_path`.
    Returns the list of layer names written.
    """
    try:
        import geopandas as gpd  # bundled in the EPyT-Flow venv
    except ImportError:
        raise HTTPException(
            status_code=422,
            detail="geopandas is required to convert shapefiles — upload a .gpkg instead.",
        )

    written: List[str] = []
    # Collect all .shp files
    shp_files = []
    for root, _dirs, files in os.walk(shp_dir):
        for f in files:
            if f.lower().endswith(".shp"):
                shp_files.append(os.path.join(root, f))

    if not shp_files:
        raise HTTPException(status_code=422, detail="No shapefiles found inside the zip.")

    mode = "w"
    for shp_path in shp_files:
        stem = os.path.splitext(os.path.basename(shp_path))[0].lower()
        # Map to canonical layer name
        layer_name = stem
        for canonical, aliases in _SHP_LAYER_MAP.items():
            if stem in aliases or any(a in stem for a in aliases):
                layer_name = canonical
                break
        try:
            gdf = gpd.read_file(shp_path, on_invalid="ignore")
            if len(gdf) == 0:
                continue
            gdf.to_file(out_path, layer=layer_name, driver="GPKG")
            written.append(layer_name)
            mode = "a"
            logger.info("Converted %s → layer '%s' (%d rows)", shp_path, layer_name, len(gdf))
        except Exception as exc:
            logger.warning("Skipped %s: %s", shp_path, exc)

    return written


@router.post("/upload", response_model=UploadResult, status_code=201)
async def upload_gpkg(
    file: UploadFile = File(..., description="DMA GeoPackage (.gpkg) or zip of shapefiles (.zip)"),
    _: str = Depends(require_api_key),
):
    """
    Upload a DMA dataset and register it for simulation.

    Accepts:
    - A `.gpkg` with layers: `dma`, `waterpipes`, and optionally
      `watersources`, `storagefacility`, `valves`, `bulk_meters`.
    - A `.zip` containing either a `.gpkg` or a set of shapefiles
      (one per asset type). Shapefiles are auto-converted.

    The file is immediately available via `GET /files` and
    `POST /dma/{filename}/simulate` after a successful upload.
    """
    settings = get_settings()
    os.makedirs(settings.gpkg_dir, exist_ok=True)

    # ── size guard ────────────────────────────────────────────────────────────
    content = await file.read()
    size_mb = len(content) / (1024 * 1024)
    if size_mb > _MAX_UPLOAD_MB:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size_mb:.1f} MB). Maximum is {_MAX_UPLOAD_MB} MB.",
        )

    original_name = file.filename or "upload"
    ext = os.path.splitext(original_name)[1].lower()

    if ext not in (".gpkg", ".zip"):
        raise HTTPException(
            status_code=422,
            detail="Only .gpkg and .zip files are accepted.",
        )

    with tempfile.TemporaryDirectory(prefix="dma_upload_") as tmpdir:

        if ext == ".gpkg":
            # ── direct GeoPackage ─────────────────────────────────────────────
            tmp_gpkg = os.path.join(tmpdir, "upload.gpkg")
            with open(tmp_gpkg, "wb") as fh:
                fh.write(content)
            out_name = _safe_filename(original_name)

        elif ext == ".zip":
            # ── zip: unpack, then detect .gpkg or shapefiles ──────────────────
            zip_dir = os.path.join(tmpdir, "unzipped")
            os.makedirs(zip_dir)
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as zf:
                    # Safety: reject zip slip paths
                    for info in zf.infolist():
                        if ".." in info.filename or info.filename.startswith("/"):
                            raise HTTPException(422, "Zip contains unsafe paths.")
                    zf.extractall(zip_dir)
            except zipfile.BadZipFile:
                raise HTTPException(422, "The uploaded file is not a valid zip archive.")

            # Look for a .gpkg inside the zip first
            gpkg_in_zip = []
            for root, _dirs, files in os.walk(zip_dir):
                for f in files:
                    if f.lower().endswith(".gpkg"):
                        gpkg_in_zip.append(os.path.join(root, f))

            if gpkg_in_zip:
                tmp_gpkg = gpkg_in_zip[0]
                stem = os.path.splitext(os.path.basename(gpkg_in_zip[0]))[0]
                out_name = _safe_filename(stem + ".gpkg")
            else:
                # Convert shapefiles
                stem = os.path.splitext(os.path.basename(original_name))[0]
                out_name = _safe_filename(stem + ".gpkg")
                tmp_gpkg = os.path.join(tmpdir, "converted.gpkg")
                _convert_shapefiles_to_gpkg(zip_dir, tmp_gpkg)

        # ── validate ──────────────────────────────────────────────────────────
        layers = _gpkg_layers(tmp_gpkg)
        if not layers:
            raise HTTPException(422, "The GeoPackage appears empty or corrupt.")

        missing = sorted(_MANDATORY_LAYERS - set(layers))
        has_structure = len(missing) == 0

        # ── persist ───────────────────────────────────────────────────────────
        dest = os.path.join(settings.gpkg_dir, out_name)
        if os.path.exists(dest):
            # Overwrite: move old aside
            backup = dest + ".bak"
            shutil.move(dest, backup)
            logger.info("Backed up existing %s → %s", out_name, backup + ".bak")

        shutil.copy2(tmp_gpkg, dest)
        size_kb = round(os.path.getsize(dest) / 1024, 1)

        msg = (
            f"Uploaded and registered as '{out_name}'."
            if has_structure else
            f"Uploaded as '{out_name}' but mandatory layers are missing: {missing}. "
            "Simulation will fail until these layers are present."
        )
        logger.info("Upload OK: %s  layers=%s  missing=%s", out_name, layers, missing)

        return UploadResult(
            filename          = out_name,
            size_kb           = size_kb,
            layers            = layers,
            missing_layers    = missing,
            has_dma_structure = has_structure,
            message           = msg,
        )