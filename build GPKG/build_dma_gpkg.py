#!/usr/bin/env python3
"""
Merge the raw shapefiles in data/gpkg/ into ONE multi-layer GeoPackage
with the exact layer names + column names that app/services/dma_ingest.py
expects:

    dma             <- DMA.shp
    waterpipes      <- WaterPipes.shp
    watersources    <- WaterSources.shp
    storagefacility <- StorageFacility.shp
    valves          <- Valves.shp
    bulk_meters     <- Bulk_Meter.shp

Why this is needed
-------------------
ingest_dma() opens ONE .gpkg file and does
    SELECT ... FROM dma
    SELECT ... FROM waterpipes
    ... etc
But data/gpkg currently only has *separate* shapefiles (DMA.shp,
WaterPipes.shp, ...) plus a placeholder DUWAS_system.gpkg that has
0 useful rows. There is no single .gpkg with those six layers, so
GET /dma/{filename}/layers and POST /dma/{filename}/simulate fail
with "Missing required layers in GeoPackage" (or 404, since /files
only lists *.gpkg and the only .gpkg present is the empty one).

This script builds that missing file.

Usage
-----
    PYTHONPATH=<path-to-bundled-venv-site-packages> python3 build_dma_gpkg.py \
        --src  /path/to/data/gpkg \
        --out  /path/to/data/gpkg/duwas_dma.gpkg

If --out already exists it is overwritten.
"""

import argparse
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="folder containing the .shp files")
    ap.add_argument("--out", required=True, help="output .gpkg path")
    args = ap.parse_args()

    import geopandas as gpd

    # shapefile -> target layer name in the merged GeoPackage
    LAYER_MAP = {
        "DMA.shp":             "dma",
        "WaterPipes.shp":      "waterpipes",
        "WaterSources.shp":    "watersources",
        "StorageFacility.shp": "storagefacility",
        "Valves.shp":          "valves",
        "Bulk_Meter.shp":      "bulk_meters",
    }

    if os.path.exists(args.out):
        os.remove(args.out)

    for shp_name, layer_name in LAYER_MAP.items():
        shp_path = os.path.join(args.src, shp_name)
        if not os.path.isfile(shp_path):
            print(f"  [SKIP] {shp_name} not found at {shp_path}")
            continue

        # on_invalid='ignore': some shapefiles (WaterPipes, Valves) contain a
        # handful of degenerate / single-point geometries from bad digitising.
        # Without this flag, shapely raises GEOSException and the whole read
        # aborts. With it, the bad rows simply come back with geometry=None,
        # and we drop them below (dma_ingest.py already filters
        # "WHERE geom IS NOT NULL" so this matches the runtime behaviour).
        gdf = gpd.read_file(shp_path, on_invalid="ignore")

        before = len(gdf)
        gdf = gdf[gdf.geometry.notna()].copy()
        dropped = before - len(gdf)

        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        elif gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")

        gdf.to_file(args.out, layer=layer_name, driver="GPKG")

        msg = f"  [OK]   {shp_name:22s} -> layer '{layer_name}' ({len(gdf)} rows"
        if dropped:
            msg += f", {dropped} invalid geometr{'y' if dropped == 1 else 'ies'} dropped"
        msg += ")"
        print(msg)

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()