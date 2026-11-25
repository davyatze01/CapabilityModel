"""Rebuild pois_used.gpkg from the current POI source -- no routing rerun needed.

generate_poi_exports() writes pois_used.gpkg from _collect_poi_records() alone (reads
the configured POI source + services.csv/poi_types.csv queries); the snap/non_bus/acc
args it also accepts are only for the optional hexagon-shard/accessibility exports, so
passing none of them skips straight to the gpkg write. Useful after switching a city's
POI source (e.g. Paris's shapefile cutover), which leaves the old pois_used.gpkg's
source_keys stale/unmatched against the new source.
"""
import os
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

STUDY_CITY = "paris"
# Must be set before any project import: core.config reads it at import time, and
# utils.graphml builds its own PipelineConfig() from it when loading POIs -- without it,
# the POIs come from the default city while the output path follows STUDY_CITY.
os.environ["CAP_STUDY_CITY"] = STUDY_CITY

from core.config import PipelineConfig
from core.context import build_context
from exports.poi_exports import generate_poi_exports

# Fail if fewer than this share of the written POIs lie within the study area's origin
# nodes' bounding box (padded by ~25 km for POIs reachable from the edge).
MIN_SHARE_INSIDE = 0.95
BBOX_PAD_DEG = 0.25


def _check_pois_match_study_area(gpkg_path: str, ctx) -> None:
    lons = [d["x"] for _, d in ctx.nodes_with_coords]
    lats = [d["y"] for _, d in ctx.nodes_with_coords]
    lo_x, hi_x = min(lons) - BBOX_PAD_DEG, max(lons) + BBOX_PAD_DEG
    lo_y, hi_y = min(lats) - BBOX_PAD_DEG, max(lats) + BBOX_PAD_DEG
    con = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT lon, lat FROM pois_used").fetchall()
    finally:
        con.close()
    inside = sum(lo_x <= x <= hi_x and lo_y <= y <= hi_y for x, y in rows)
    share = inside / len(rows) if rows else 0.0
    print(f"[Rebuild] {inside}/{len(rows)} POIs ({share:.1%}) inside the {STUDY_CITY} study area", flush=True)
    if share < MIN_SHARE_INSIDE:
        raise SystemExit(
            f"[Rebuild] ERROR: only {share:.1%} of POIs in {gpkg_path} lie in the {STUDY_CITY} "
            f"study area -- the export is for the wrong city; do not use it."
        )


def rebuild() -> None:
    cfg = PipelineConfig(study_city=STUDY_CITY)
    ctx = build_context(cfg)
    result = generate_poi_exports(ctx)
    print(f"[Rebuild] Wrote {result['poi_geopackage_path']}")
    _check_pois_match_study_area(result["poi_geopackage_path"], ctx)


if __name__ == "__main__":
    rebuild()
