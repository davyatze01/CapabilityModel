"""Rebuild the GeoPackage and QGIS project for an existing run without rerunning the pipeline.

Usage:
    python rebuild_qgis_output.py                  # Cagliari_Shapefile (default)
    python rebuild_qgis_output.py public_strike    # Cagliari_Shapefile_public_strike
"""
from __future__ import annotations

import sys
from pathlib import Path

from config import PipelineConfig
from generate_experiment_shapefiles import generate_combined_experiment_gpkg
from pipeline_runner import _build_qgis_project, _resolve_qgis_executable


def rebuild(slug_suffix: str = "") -> None:
    cfg = PipelineConfig(study_city="cagliari")
    if slug_suffix:
        cfg.name_shapefile = f"Cagliari Shapefile_{slug_suffix}.shp"
        cfg.__post_init__()

    artifact_slug = cfg.artifact_slug
    print(f"[Rebuild] artifact_slug = {artifact_slug}")

    experiments_dir = Path("experiments")
    # Pick the single most-recently-modified CSV per capability type, mirroring
    # what the pipeline does: each run writes exactly one CSV per capability.
    capability_names = ["care", "nutrition", "restorativeness"]
    csv_paths = []
    for cap in capability_names:
        candidates = [
            p for p in experiments_dir.glob(f"{artifact_slug}_capability_{cap}*.csv")
            if p.stat().st_size > 0
        ]
        if not candidates:
            print(f"[Rebuild] No CSV found for capability '{cap}' — skipping.")
            continue
        csv_paths.append(max(candidates, key=lambda p: p.stat().st_mtime))

    if not csv_paths:
        print(f"[Rebuild] No experiment CSVs found for slug '{artifact_slug}' in {experiments_dir}/")
        sys.exit(1)
    print(f"[Rebuild] Using {len(csv_paths)} CSVs (latest per capability):")
    for p in csv_paths:
        print(f"  {p.name}")

    gpkg_path = Path("outputs") / "gpkg" / artifact_slug / f"{artifact_slug}.gpkg"
    print(f"[Rebuild] Writing GeoPackage: {gpkg_path}")
    gpkg_path = generate_combined_experiment_gpkg(
        csv_paths,
        output_path=gpkg_path,
        grid_enabled=cfg.qgis_grid_enabled,
        grid_cell_size_m=cfg.qgis_grid_cell_size_m,
        grid_capability_field=cfg.qgis_autostyle_field,
        grid_max_cells=cfg.qgis_grid_max_cells,
        grid_fill_hull=cfg.qgis_grid_fill_hull,
        grid_hull_buffer_m=cfg.qgis_grid_hull_buffer_m,
        grid_hull_ratio=cfg.qgis_grid_hull_ratio,
    )
    print(f"[Rebuild] GeoPackage written: {gpkg_path} ({gpkg_path.stat().st_size / 1024:.0f} KB)")

    qgis_exe = _resolve_qgis_executable(cfg)
    if qgis_exe is None:
        print("[Rebuild] QGIS executable not found — skipping project generation.")
        print("[Rebuild] Set qgis_bin_path in PipelineConfig or QGIS_BIN_PATH env var.")
        return

    print(f"[Rebuild] Building QGIS project with embedded styles (QGIS: {qgis_exe})")
    project_path = _build_qgis_project(cfg, gpkg_path, qgis_exe)
    if project_path:
        print(f"[Rebuild] QGIS project written: {project_path}")
    else:
        print("[Rebuild] QGIS project generation failed.")


if __name__ == "__main__":
    suffix = sys.argv[1] if len(sys.argv) > 1 else ""
    rebuild(suffix)
