"""Export graph nodes and/or hex-grid centroids used by the pipeline.

Usage:
    python export_graph_nodes.py                       # nodes (study area)
    python export_graph_nodes.py --centroids           # hex centroids only
    python export_graph_nodes.py --both                # both
"""
import math
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import Point

from config import PipelineConfig
from context import _filter_nodes_to_boundary
from utils.graphml import get_mode_graph
from utils.services import get_global_radius_m


def export_nodes(out_path: str | Path | None = None) -> Path:
    cfg = PipelineConfig(study_city="cagliari")
    print(f"[Export] artifact_slug={cfg.artifact_slug}  use_shapefile={cfg.use_shapefile}", flush=True)

    graph = get_mode_graph("walk", cfg)
    nodes_with_coords = [
        (nid, data)
        for nid, data in graph.nodes(data=True)
        if "x" in data and "y" in data
    ]
    print(f"[Export] {len(nodes_with_coords)} nodes in buffered graph", flush=True)

    # Apply the same boundary clip that build_context uses
    if get_global_radius_m(cfg) is not None and not cfg.use_shapefile:
        nodes_with_coords = _filter_nodes_to_boundary(nodes_with_coords, cfg.city_name)
        print(f"[Export] {len(nodes_with_coords)} nodes after boundary filter", flush=True)

    records = [
        {"node_id": str(nid), "geometry": Point(float(data["x"]), float(data["y"]))}
        for nid, data in nodes_with_coords
    ]

    gdf = gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")

    if out_path is None:
        out_path = Path("outputs") / "export" / "graph_nodes.shp"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(out_path, driver="ESRI Shapefile")
    print(f"[Export] Saved: {out_path.resolve()}", flush=True)
    return out_path


def export_hex_centroids(out_path: str | Path | None = None) -> Path:
    """Export the display hex-grid centroids in the same CRS/parameters as _build_capability_grid."""
    cfg = PipelineConfig(study_city="cagliari")

    # Load any capability CSV to determine the bounding box of computed points
    from pathlib import Path as _Path
    import pandas as pd
    experiments_dir = _Path("experiments")
    candidates = sorted(experiments_dir.glob(f"{cfg.artifact_slug}_capability_care*.csv"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No capability CSV found for slug '{cfg.artifact_slug}' in {experiments_dir}/")
    df = pd.read_csv(candidates[0])
    if "lon" not in df.columns or "lat" not in df.columns:
        raise ValueError("CSV must contain 'lon' and 'lat' columns")
    points_gdf = gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df["lon"], df["lat"]),
        crs="EPSG:4326",
    )
    print(f"[Export] {len(points_gdf)} computed points from {candidates[0].name}", flush=True)

    utm_crs = points_gdf.estimate_utm_crs()
    work = points_gdf.to_crs(utm_crs)
    min_x, min_y, max_x, max_y = work.total_bounds

    cell_size_m = float(cfg.qgis_grid_cell_size_m)
    hex_radius_m = cell_size_m / math.cos(math.pi / 6.0)
    hex_height_m = math.sqrt(3.0) * hex_radius_m
    x_step_m = 1.5 * hex_radius_m
    y_step_m = hex_height_m

    centroids = []
    col_idx = 0
    x = min_x - hex_radius_m
    while x <= max_x + hex_radius_m:
        y_offset = 0.0 if col_idx % 2 == 0 else hex_height_m / 2.0
        y = min_y - hex_height_m
        row_idx = 0
        while y <= max_y + hex_height_m:
            center_y = y + y_offset
            centroids.append({"ix": col_idx, "iy": row_idx, "geometry": Point(x, center_y)})
            y += y_step_m
            row_idx += 1
        x += x_step_m
        col_idx += 1

    gdf = gpd.GeoDataFrame(centroids, geometry="geometry", crs=utm_crs).to_crs("EPSG:4326")
    print(f"[Export] {len(gdf)} hex centroids (cell_size={cell_size_m:.0f} m)", flush=True)

    if out_path is None:
        out_path = Path("outputs") / "export" / "hex_centroids.shp"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(out_path, driver="ESRI Shapefile")
    print(f"[Export] Saved: {out_path.resolve()}", flush=True)
    return out_path


if __name__ == "__main__":
    args = set(sys.argv[1:])
    do_nodes = "--centroids" not in args
    do_centroids = "--centroids" in args or "--both" in args

    if do_nodes:
        node_path = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
        export_nodes(node_path or "outputs/export/graph_nodes_study_area.shp")
    if do_centroids:
        export_hex_centroids()
