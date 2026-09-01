"""Rebuild the per-scenario capability/service grid GeoPackage for one or more
scenarios, from already-computed capability CSVs -- no pipeline rerun needed.

Targets scenarios/_capability_grids/<key>/Cagliari_<key>.gpkg, the exact file
the colleague opens directly (not profile_comparison.qgz). Re-registers the
service_*/capability_* views and their embedded default styles.
"""
from pathlib import Path

from core.config import PipelineConfig
from core.pipeline_runner import _build_qgis_project, _resolve_qgis_executable
from exports.generate_experiment_shapefiles import generate_combined_experiment_gpkg

SCENARIO_KEYS = ["underservice_is_mirrionis", "public_strike"]

def rebuild(key: str) -> None:
    cfg = PipelineConfig(study_city="cagliari")
    experiments_dir = Path("experiments")
    candidates = [
        p for p in experiments_dir.glob(f"Cagliari_{key}_capability_*.csv")
        if p.stat().st_size > 0
    ]
    if not candidates:
        print(f"[Rebuild] No experiment CSVs found for '{key}'")
        return
    latest_csv = max(candidates, key=lambda p: p.stat().st_mtime)
    print(f"[Rebuild:{key}] Using {latest_csv.name}")

    gpkg_path = Path("scenarios") / "_capability_grids" / key / f"Cagliari_{key}.gpkg"
    gpkg_path = generate_combined_experiment_gpkg(
        [latest_csv],
        output_path=gpkg_path,
        grid_enabled=cfg.qgis_grid_enabled,
        grid_cell_size_m=cfg.hexagon_radius,
        grid_capability_field=cfg.default_capability,
        grid_max_cells=cfg.qgis_grid_max_cells,
        grid_fill_hull=cfg.qgis_grid_fill_hull,
        grid_hull_buffer_m=cfg.qgis_grid_hull_buffer_m,
        grid_hull_ratio=cfg.qgis_grid_hull_ratio,
        grid_params_path=Path(cfg.poi_export_dir) / "grid_params.json",
        grid_exclude_water=cfg.qgis_grid_exclude_water,
        grid_water_cache_path=Path(cfg.poi_export_dir) / "water_mask.gpkg",
        place_labels_enabled=cfg.qgis_place_labels_enabled,
        place_labels_cache_path=Path(cfg.poi_export_dir) / "place_labels.gpkg",
    )
    print(f"[Rebuild:{key}] Wrote {gpkg_path}")

    qgis_exe = _resolve_qgis_executable(cfg)
    if qgis_exe is None:
        print(f"[Rebuild:{key}] QGIS executable not found — skipping style embedding.")
        return
    project_path = _build_qgis_project(cfg, gpkg_path, qgis_exe)
    if project_path:
        print(f"[Rebuild:{key}] Styles embedded via {project_path}")
    else:
        print(f"[Rebuild:{key}] Style embedding failed.")

if __name__ == "__main__":
    for k in SCENARIO_KEYS:
        rebuild(k)
