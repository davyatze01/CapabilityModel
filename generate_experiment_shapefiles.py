from __future__ import annotations

import math
from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from shapely.geometry import Point, Polygon

from plot_shapefile import find_value_column, load_graph, pick_graphml_file


DEFAULT_GRAPH_MODE = "bike"
DEFAULT_EXPERIMENTS_DIR = Path("experiments")
DEFAULT_OUTPUT_DIR = Path("outputs/shapefiles")


def _read_experiment_csv(csv_path: Path) -> pd.DataFrame:
    """Read one experiment CSV and reject files that are empty or malformed."""
    # We fail early here so downstream logic can assume the file exists and
    # contains at least a minimally valid tabular structure.
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    if csv_path.stat().st_size == 0:
        raise ValueError(f"CSV file is empty: {csv_path}")

    # Pandas handles the actual CSV parsing; the remaining checks make sure we
    # do not continue with recap files, broken exports, or placeholder files.
    frame = pd.read_csv(csv_path)
    if frame.empty:
        raise ValueError(f"CSV file has no rows: {csv_path}")
    if not any(str(column).strip() for column in frame.columns):
        raise ValueError(f"CSV file has no usable columns: {csv_path}")
    return frame


def _build_graph_coordinates(graph: nx.Graph) -> pd.DataFrame:
    """Extract node coordinates from GraphML as a DataFrame keyed by node_id."""
    rows: list[dict[str, object]] = []
    for node_id, attrs in graph.nodes(data=True):
        x = attrs.get("x")
        y = attrs.get("y")
        if x is None or y is None:
            continue

        # Each graph node is flattened into a simple lookup table so we can
        # join CSV rows by `node_id` and recover the geometry coordinates that
        # are stored in the GraphML network.
        rows.append(
            {
                "node_id": str(node_id),
                "lon": float(x),
                "lat": float(y),
            }
        )

    if not rows:
        raise ValueError("The selected graph does not contain node coordinates.")

    return pd.DataFrame(rows)


def _prepare_geodataframe(frame: pd.DataFrame, graph: nx.Graph) -> tuple[gpd.GeoDataFrame, str]:
    """Build a GeoDataFrame for one experiment from CSV rows plus graph geometry."""
    frame = frame.copy()

    # Reuse the same convention as the plotting script: prefer a column named
    # like `capability_*`, otherwise accept a single non-coordinate data column.
    value_column = find_value_column(frame)
    frame[value_column] = pd.to_numeric(frame[value_column], errors="coerce")

    if {"lon", "lat"}.issubset(frame.columns):
        # Some CSV exports may already include explicit coordinates. In that
        # case we can build geometries directly without touching the graph.
        frame["lon"] = pd.to_numeric(frame["lon"], errors="coerce")
        frame["lat"] = pd.to_numeric(frame["lat"], errors="coerce")
        frame = frame.dropna(subset=["lon", "lat", value_column])
    else:
        if "node_id" not in frame.columns:
            raise ValueError(
                "CSV must contain either 'lon'/'lat' columns or a 'node_id' column "
                "that can be joined to the graph."
            )

        # When coordinates are not present in the CSV, we enrich each row by
        # joining against the network nodes loaded from GraphML.
        frame["node_id"] = frame["node_id"].astype(str)
        coordinates = _build_graph_coordinates(graph)
        frame = frame.merge(coordinates, on="node_id", how="left")
        frame = frame.dropna(subset=["lon", "lat", value_column])

    if frame.empty:
        raise ValueError("No rows with valid geometry and experiment values were found.")

    # Shapefiles store geometries explicitly, so we convert every CSV row into a
    # point feature using the longitude/latitude pair.
    geometry = [Point(lon, lat) for lon, lat in zip(frame["lon"], frame["lat"])]
    gdf = gpd.GeoDataFrame(frame, geometry=geometry, crs="EPSG:4326")
    return gdf, value_column


def _safe_shapefile_columns(gdf: gpd.GeoDataFrame, value_column: str) -> gpd.GeoDataFrame:
    """Rename columns to fit shapefile field limits while keeping the main value."""
    rename_map: dict[str, str] = {}
    used: set[str] = set()

    preferred_value_name = "value"
    for column in gdf.columns:
        if column == "geometry":
            continue

        if column == value_column:
            # Keep the actual experiment metric easy to identify in GIS tools.
            candidate = preferred_value_name
        else:
            # ESRI Shapefile field names are limited to 10 characters, so we
            # truncate other column names before writing the file.
            candidate = str(column)[:10] or "field"

        base = candidate
        suffix = 1
        while candidate in used:
            # If two columns would collapse to the same short name, append a
            # numeric suffix while still respecting the 10-character limit.
            trimmed = base[: max(0, 10 - len(str(suffix)))]
            candidate = f"{trimmed}{suffix}"
            suffix += 1

        rename_map[column] = candidate
        used.add(candidate)

    return gdf.rename(columns=rename_map)


def _prepare_combined_geodataframe(csv_paths: list[Path], graph: nx.Graph) -> gpd.GeoDataFrame:
    """Build one GeoDataFrame with one column per experiment metric."""
    combined: gpd.GeoDataFrame | None = None

    for current_csv in csv_paths:
        try:
            frame = _read_experiment_csv(current_csv)
            gdf, value_column = _prepare_geodataframe(frame, graph)
        except ValueError:
            # Recap files or malformed CSVs are ignored in batch mode.
            continue

        if "node_id" in gdf.columns:
            gdf["join_key"] = gdf["node_id"].astype(str)
        else:
            gdf["join_key"] = gdf["lon"].astype(str) + ":" + gdf["lat"].astype(str)

        metric_name = value_column
        if combined is not None and metric_name in combined.columns:
            metric_name = f"{value_column}_{current_csv.stem}"

        slim = gdf[["join_key", "node_id", "lon", "lat", "geometry", value_column]].copy()
        slim = slim.rename(columns={value_column: metric_name})

        if combined is None:
            combined = slim
            continue

        merged = combined.merge(slim, on="join_key", how="outer", suffixes=("", "_new"))
        for column in ["node_id", "lon", "lat", "geometry"]:
            new_column = f"{column}_new"
            if new_column in merged.columns:
                if column in merged.columns:
                    merged[column] = merged[column].combine_first(merged[new_column])
                    merged = merged.drop(columns=[new_column])
                else:
                    merged = merged.rename(columns={new_column: column})

        combined = merged

    if combined is None:
        raise ValueError("No valid experiment CSVs were found for shapefile generation.")

    if "join_key" in combined.columns:
        combined = combined.drop(columns=["join_key"])

    return gpd.GeoDataFrame(combined, geometry="geometry", crs="EPSG:4326")


def _safe_shapefile_columns_wide(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    rename_map: dict[str, str] = {
        "capability_care": "cap_care",
        "capability_restorativeness": "cap_rest",
        "capability_nutrition": "cap_nutri",
    }
    used: set[str] = set()

    for column in gdf.columns:
        if column == "geometry":
            continue

        candidate = rename_map.get(column, str(column)[:10] or "field")
        base = candidate
        suffix = 1
        while candidate in used:
            trimmed = base[: max(0, 10 - len(str(suffix)))]
            candidate = f"{trimmed}{suffix}"
            suffix += 1

        rename_map[column] = candidate
        used.add(candidate)

    return gdf.rename(columns=rename_map)


def _safe_gpkg_columns(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Normalize field names for robust GeoPackage writes across GDAL builds."""
    rename_map: dict[str, str] = {}
    used: set[str] = set()

    for column in gdf.columns:
        if column == "geometry":
            continue

        raw = str(column).strip() or "field"
        cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in raw)
        cleaned = cleaned.strip("_") or "field"
        cleaned = cleaned[:63]

        candidate = cleaned
        suffix = 1
        while candidate in used:
            trimmed = cleaned[: max(1, 63 - len(str(suffix)) - 1)]
            candidate = f"{trimmed}_{suffix}"
            suffix += 1

        rename_map[column] = candidate
        used.add(candidate)

    return gdf.rename(columns=rename_map)


def _prepare_gpkg_layer(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Coerce columns to GIS-friendly dtypes to avoid OGR null-pointer write failures."""
    prepared = _safe_gpkg_columns(gdf.copy())

    # Keep only known-safe scalar dtypes for OGR-backed writers.
    for column in prepared.columns:
        if column == "geometry":
            continue
        series = prepared[column]
        if pd.api.types.is_bool_dtype(series):
            prepared[column] = series.astype("Int8")
        elif pd.api.types.is_datetime64_any_dtype(series):
            prepared[column] = series.astype("datetime64[ns]")
        elif pd.api.types.is_numeric_dtype(series):
            continue
        else:
            prepared[column] = series.astype(object)

    prepared = prepared.dropna(subset=["geometry"])
    if prepared.empty:
        raise ValueError("No valid geometries available for GeoPackage export.")
    return prepared


def _regular_hexagon(center_x: float, center_y: float, radius_m: float) -> Polygon:
    """Build a flat-top regular hexagon from a center and circumradius."""
    vertices = []
    for index in range(6):
        angle = math.radians(60.0 * index)
        vertices.append(
            (
                center_x + radius_m * math.cos(angle),
                center_y + radius_m * math.sin(angle),
            )
        )
    return Polygon(vertices)


def _write_gpkg_layer(
    gdf: gpd.GeoDataFrame,
    output_path: Path,
    layer_name: str,
    mode: str,
) -> None:
    """Write a GPKG layer with a fallback writer engine for portability."""
    kwargs = {
        "driver": "GPKG",
        "layer": layer_name,
        "mode": mode,
    }
    try:
        gdf.to_file(output_path, **kwargs)
        return
    except Exception as first_exc:
        first_message = str(first_exc)
        if "NULL pointer" not in first_message and "null pointer" not in first_message:
            raise

    # Some environments fail with default engine but succeed with fiona.
    gdf.to_file(output_path, engine="fiona", **kwargs)


def generate_combined_experiment_shapefile(
    csv_paths: list[str | Path],
    output_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    graph_dir: str | Path = "graph",
    graph_path: str | Path | None = None,
    preferred_mode: str = DEFAULT_GRAPH_MODE,
) -> Path:
    """Generate one shapefile containing all selected experiment CSVs."""
    output_dir = Path(output_dir)
    graph_dir = Path(graph_dir)

    selected_graph = Path(graph_path) if graph_path is not None else pick_graphml_file(graph_dir, preferred_mode)
    graph = load_graph(selected_graph)

    selected_csv_paths = [Path(path) for path in csv_paths]
    combined = _prepare_combined_geodataframe(selected_csv_paths, graph)
    combined = _safe_shapefile_columns_wide(combined)

    shapefile_path = Path(output_path) if output_path is not None else output_dir / "combined_experiments.shp"
    shapefile_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_file(shapefile_path, driver="ESRI Shapefile")
    return shapefile_path


def generate_combined_experiment_gpkg(
    csv_paths: list[str | Path],
    output_path: str | Path,
    graph_dir: str | Path = "graph",
    graph_path: str | Path | None = None,
    preferred_mode: str = DEFAULT_GRAPH_MODE,
    grid_enabled: bool = False,
    grid_cell_size_m: float = 10.0,
    grid_capability_field: str = "capability_care",
    grid_max_cells: int = 500000,
) -> Path:
    """Generate one GeoPackage containing all selected experiment CSVs."""
    graph_dir = Path(graph_dir)

    selected_graph = Path(graph_path) if graph_path is not None else pick_graphml_file(graph_dir, preferred_mode)
    graph = load_graph(selected_graph)

    selected_csv_paths = [Path(path) for path in csv_paths]
    combined = _prepare_combined_geodataframe(selected_csv_paths, graph)
    combined = _prepare_gpkg_layer(combined)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Always regenerate from a clean GeoPackage to avoid stale/duplicated
    # features when grid parameters (e.g., cell size) change between runs.
    if output_path.exists():
        output_path.unlink()
    _write_gpkg_layer(combined, output_path, layer_name="capability_points", mode="w")

    if grid_enabled and grid_cell_size_m > 0:
        grid_layer = _build_capability_grid(
            points_gdf=combined,
            capability_field=grid_capability_field,
            cell_size_m=float(grid_cell_size_m),
            max_cells=int(grid_max_cells),
        )
        if grid_layer is not None and not grid_layer.empty:
            grid_layer = _prepare_gpkg_layer(grid_layer)
            try:
                _write_gpkg_layer(grid_layer, output_path, layer_name="capability_grid", mode="a")
            except Exception:
                # Keep point-layer export successful even when some GDAL builds
                # fail to append a second (polygon) layer with "NULL pointer".
                sidecar_grid = output_path.with_name(f"{output_path.stem}_grid{output_path.suffix}")
                if sidecar_grid.exists():
                    sidecar_grid.unlink()
                _write_gpkg_layer(grid_layer, sidecar_grid, layer_name="capability_grid", mode="w")
    return output_path


def _build_capability_grid(
    points_gdf: gpd.GeoDataFrame,
    capability_field: str,
    cell_size_m: float,
    max_cells: int,
) -> gpd.GeoDataFrame | None:
    """Build full bbox hex grid and aggregate point capability means per cell."""
    capability_columns = [column for column in points_gdf.columns if str(column).startswith("capability_")]
    if not capability_columns:
        return None
    if capability_field not in capability_columns:
        capability_field = capability_columns[0]

    work = points_gdf[["geometry"] + capability_columns].copy()
    for column in capability_columns:
        work[column] = pd.to_numeric(work[column], errors="coerce")
    work = work.dropna(subset=["geometry"])
    if work.empty:
        return None

    work = gpd.GeoDataFrame(work, geometry="geometry", crs=points_gdf.crs).to_crs("EPSG:3857")
    x_vals = work.geometry.x.to_numpy(dtype=float)
    y_vals = work.geometry.y.to_numpy(dtype=float)
    finite_mask = np.isfinite(x_vals) & np.isfinite(y_vals)
    work = work.loc[finite_mask].copy()
    if work.empty:
        return None

    min_x, min_y, max_x, max_y = work.total_bounds
    if not np.isfinite([min_x, min_y, max_x, max_y]).all():
        return None
    if min_x == max_x or min_y == max_y:
        return None

    # Interpret the configured size as the radius of the inscribed circle of
    # each regular hexagon. Convert it to the circumradius used for geometry.
    circle_radius_m = float(cell_size_m)
    hex_radius_m = circle_radius_m / math.cos(math.pi / 6.0)
    hex_width_m = 2.0 * hex_radius_m
    hex_height_m = math.sqrt(3.0) * hex_radius_m
    x_step_m = 1.5 * hex_radius_m
    y_step_m = hex_height_m

    all_cells: list[dict[str, object]] = []
    col_idx = 0
    x = min_x - hex_radius_m
    while x <= max_x + hex_radius_m:
        y_offset = 0.0 if col_idx % 2 == 0 else hex_height_m / 2.0
        y = min_y - hex_height_m
        row_idx = 0
        while y <= max_y + hex_height_m:
            center_y = y + y_offset
            all_cells.append(
                {
                    "ix": col_idx,
                    "iy": row_idx,
                    "geometry": _regular_hexagon(x, center_y, hex_radius_m),
                }
            )
            if len(all_cells) > max_cells:
                raise ValueError(
                    f"Grid would create more than {max_cells:,} hex cells. "
                    "Increase qgis_grid_cell_size_m or clean coordinate outliers."
                )
            y += y_step_m
            row_idx += 1
        x += x_step_m
        col_idx += 1

    if not all_cells:
        return None

    grid_gdf = gpd.GeoDataFrame(all_cells, geometry="geometry", crs="EPSG:3857")

    points = work.copy().reset_index(drop=True)
    points["point_id"] = np.arange(len(points), dtype=int)
    joined = gpd.sjoin(
        points[["point_id", "geometry"] + capability_columns],
        grid_gdf[["ix", "iy", "geometry"]],
        how="left",
        predicate="within",
    )
    grouped_values = joined.groupby(["ix", "iy"], dropna=False)[capability_columns].mean().reset_index()
    rename_map = {column: f"grid_mean_{column.replace('capability_', '')}" for column in capability_columns}
    grouped_values = grouped_values.rename(columns=rename_map)
    grid_gdf = grid_gdf.merge(grouped_values, on=["ix", "iy"], how="left")
    selected_col = f"grid_mean_{capability_field.replace('capability_', '')}"
    if selected_col in grid_gdf.columns:
        grid_gdf["grid_mean"] = grid_gdf[selected_col]
    else:
        grid_gdf["grid_mean"] = np.nan
    grid_gdf["has_data"] = grid_gdf["grid_mean"].notna().astype(int)
    grid_gdf["capability_field"] = capability_field
    grid_gdf["cell_size_m"] = float(cell_size_m)
    grid_gdf["hex_radius_m"] = float(hex_radius_m)
    grid_gdf["hex_width_m"] = float(hex_width_m)
    grid_gdf["hex_height_m"] = float(hex_height_m)
    return grid_gdf.to_crs("EPSG:4326")


def generate_shapefile_by_csv(
    csv_path: str | Path | None = None,
    experiments_dir: str | Path = DEFAULT_EXPERIMENTS_DIR,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    graph_dir: str | Path = "graph",
    graph_path: str | Path | None = None,
    preferred_mode: str = DEFAULT_GRAPH_MODE,
) -> list[Path]:
    """Generate one shapefile for each valid experiment CSV in the experiments folder."""
    experiments_dir = Path(experiments_dir)
    output_dir = Path(output_dir)
    graph_dir = Path(graph_dir)

    # Load the graph once and reuse it for every CSV. This avoids repeating the
    # GraphML I/O and gives us a stable node-to-coordinate lookup source.
    selected_graph = Path(graph_path) if graph_path is not None else pick_graphml_file(graph_dir, preferred_mode)
    graph = load_graph(selected_graph)

    # The function supports two modes:
    # 1. one explicit CSV path
    # 2. batch processing of every CSV in the experiments folder
    csv_paths = [Path(csv_path)] if csv_path is not None else sorted(experiments_dir.glob("*.csv"))
    output_dir.mkdir(parents=True, exist_ok=True)

    created_paths: list[Path] = []
    for current_csv in csv_paths:
        try:
            # Parse the CSV, recover geometry, and normalize field names before
            # exporting to the shapefile format.
            frame = _read_experiment_csv(current_csv)
            gdf, value_column = _prepare_geodataframe(frame, graph)
            shapefile_ready = _safe_shapefile_columns(gdf, value_column)
        except ValueError:
            # Skip recap files or malformed exports without stopping the full batch.
            continue

        # Each CSV gets its own output folder because a shapefile is actually a
        # small group of files (.shp, .shx, .dbf, etc.), not a single file.
        output_path = output_dir / current_csv.stem / f"{current_csv.stem}.shp"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shapefile_ready.to_file(output_path, driver="ESRI Shapefile")
        created_paths.append(output_path)

    return created_paths
