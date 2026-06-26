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

        # Include all capability_* columns so a single CSV carrying all capabilities
        # is passed through intact rather than reduced to one column.
        cap_cols = [col for col in gdf.columns if str(col).startswith("capability_") and col != value_column]
        keep = ["join_key", "node_id", "lon", "lat", "geometry", value_column] + cap_cols
        slim = gdf[[col for col in keep if col in gdf.columns]].copy()
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
    grid_fill_hull: bool = True,
    grid_hull_buffer_m: float = 0.0,
    grid_hull_ratio: float = 0.3,
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
            fill_hull=bool(grid_fill_hull),
            hull_buffer_m=float(grid_hull_buffer_m),
            hull_ratio=float(grid_hull_ratio),
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

            # Write a flat CSV indexed by hex_id so QGIS attributes and the
            # tabular output share the same primary key.
            if "hex_id" in grid_layer.columns:
                hex_csv_path = output_path.with_name(f"{output_path.stem}_hex.csv")
                csv_cols = ["hex_id"] + (
                    ["node_id"] if "node_id" in grid_layer.columns else []
                ) + [c for c in grid_layer.columns if c.startswith("grid_mean_")]
                grid_layer[csv_cols].to_csv(hex_csv_path, index=False)

    return output_path


def _build_capability_grid(
    points_gdf: gpd.GeoDataFrame,
    capability_field: str,
    cell_size_m: float,
    max_cells: int,
    fill_hull: bool = True,
    hull_buffer_m: float = 0.0,
    hull_ratio: float = 0.3,
) -> gpd.GeoDataFrame | None:
    """Build a regular hex grid in the same coordinate system as the sampling grid.

    Uses the identical approximate-Cartesian projection as _hex_grid_sample_nodes
    (xs = lon × m_per_deg_lon, ys = lat × 111320) so that the display grid and the
    sampling grid share the same scaling and column-offset pattern.

    Cell selection (when fill_hull=True, the default): keep every cell whose centroid lies
    inside the convex hull of the sampled nodes, as well as any cell with a node within
    hex_radius_m. This fills the interior holes that the legacy "node within hex_radius only"
    rule left in sparse parts of the network, and yields a clean hull outline so the study
    area's shape/perimeter is recognizable instead of a ragged bounding-box-style grid. Measured
    cells (a node within hex_radius_m) are colored by that node; hull-fill cells carry NO value
    (null capability columns) and exist only to make the area solid. A `has_data` flag (1 =
    measured, 0 = hull-fill) lets QGIS style the two differently. Set fill_hull=False to restore
    the legacy node-hugging behavior. hull_buffer_m optionally grows the hull (in metres).

    hull_ratio selects the boundary shape: >= 1.0 = convex hull; ~0.3 = concave hull that follows
    a real concave outline (coastlines/bays); lower gets tighter/jaggier. Falls back to the convex
    hull if concave_hull is unavailable or fails.
    """
    from scipy.spatial import cKDTree

    capability_columns = [column for column in points_gdf.columns if str(column).startswith("capability_")]
    if not capability_columns:
        return None
    if capability_field not in capability_columns:
        capability_field = capability_columns[0]

    _id_col = "node_id" if "node_id" in points_gdf.columns else None
    _extra = [_id_col] if _id_col else []
    work = points_gdf[["geometry"] + _extra + capability_columns].copy()
    for column in capability_columns:
        work[column] = pd.to_numeric(work[column], errors="coerce")
    work = work.dropna(subset=["geometry"])
    if work.empty:
        return None

    # ── Same approximate-Cartesian projection as _hex_grid_sample_nodes ──────
    M_LAT = 111320.0
    lats = work.geometry.y.to_numpy(dtype=float)
    lons = work.geometry.x.to_numpy(dtype=float)
    finite_mask = np.isfinite(lats) & np.isfinite(lons)
    if not finite_mask.any():
        return None

    # Try to reuse the exact grid parameters written by _hex_grid_sample_nodes
    # so both grids share an identical tiling (same bbox, same m_per_deg_lon).
    import json as _json
    _params_path = Path("outputs") / "grid_params.json"
    _loaded_params: dict | None = None
    try:
        with open(_params_path, encoding="utf-8") as _f:
            _p = _json.load(_f)
        if abs(float(_p["cell_size_m"]) - float(cell_size_m)) < 0.01:
            _loaded_params = _p
    except (FileNotFoundError, KeyError, ValueError):
        pass

    if _loaded_params is not None:
        m_per_deg_lon = float(_loaded_params["m_per_deg_lon"])
        min_x = float(_loaded_params["min_x"])
        max_x = float(_loaded_params["max_x"])
        min_y = float(_loaded_params["min_y"])
        max_y = float(_loaded_params["max_y"])
    else:
        mean_lat_rad = math.radians(float(np.mean(lats[finite_mask])))
        m_per_deg_lon = M_LAT * math.cos(mean_lat_rad)
        _xs_all = lons[finite_mask] * m_per_deg_lon
        _ys_all = lats[finite_mask] * M_LAT
        min_x, max_x = float(_xs_all.min()), float(_xs_all.max())
        min_y, max_y = float(_ys_all.min()), float(_ys_all.max())

    xs = lons * m_per_deg_lon
    ys = lats * M_LAT
    finite_mask &= np.isfinite(xs) & np.isfinite(ys)
    work = work.loc[finite_mask].copy().reset_index(drop=True)
    xs = xs[finite_mask]
    ys = ys[finite_mask]
    if work.empty:
        return None

    cap_arr = work[capability_columns].to_numpy(dtype=float)
    node_ids = work[_id_col].to_numpy() if _id_col else None

    # ── Hex geometry parameters ───────────────────────────────────────────────
    hex_radius_m = float(cell_size_m) / math.cos(math.pi / 6.0)
    hex_width_m  = 2.0 * hex_radius_m
    hex_height_m = math.sqrt(3.0) * hex_radius_m
    x_step = 1.5 * hex_radius_m
    y_step = hex_height_m

    if not np.isfinite([min_x, min_y, max_x, max_y]).all():
        return None

    # ── Build grid centroids (identical loop to _hex_grid_sample_nodes) ───────
    # Store (cx, cy, col_idx, row_idx) so each cell gets a stable unique hex_id.
    centroids: list[tuple[float, float, int, int]] = []
    col_idx = 0
    cx = min_x - hex_radius_m
    while cx <= max_x + hex_radius_m:
        y_offset = 0.0 if col_idx % 2 == 0 else y_step / 2.0
        cy = min_y - y_step + y_offset
        row_idx = 0
        while cy <= max_y + y_step:
            centroids.append((cx, cy, col_idx, row_idx))
            if len(centroids) > max_cells:
                raise ValueError(
                    f"Grid would exceed {max_cells:,} cells. "
                    "Increase qgis_grid_cell_size_m or clean coordinate outliers."
                )
            cy += y_step
            row_idx += 1
        cx += x_step
        col_idx += 1

    if not centroids:
        return None

    # ── KDTree: assign each display cell its nearest computed node ────────────
    node_xy = np.column_stack([xs, ys])
    tree = cKDTree(node_xy)
    centroid_xy = np.array([(cx, cy) for cx, cy, _, _ in centroids], dtype=float)
    dists, idxs = tree.query(centroid_xy)

    # ── Decide which cells to keep ────────────────────────────────────────────
    # near_mask = cells with a real node within hex_radius_m (the legacy criterion).
    # When fill_hull is on, also keep cells whose centroid is inside the convex hull of the
    # sampled nodes, so interior holes are filled and the perimeter follows the study area.
    near_mask = dists <= hex_radius_m
    keep_mask = near_mask
    if fill_hull and node_xy.shape[0] >= 3:
        import shapely
        from shapely.geometry import MultiPoint

        mp = MultiPoint([(float(x), float(y)) for x, y in node_xy])
        # hull_ratio controls how tightly the boundary follows the nodes:
        #   >= 1.0  -> convex hull (bridges across bays/coastline)
        #   ~0.3    -> concave hull that follows the real, possibly concave study-area outline
        #   -> 0    -> very tight, can get jagged
        # allow_holes=False keeps the area solid (interior gaps are still filled). Any failure
        # or an unexpected (non-polygonal) result falls back to the convex hull.
        hull = None
        if hull_ratio < 1.0 and hasattr(shapely, "concave_hull"):
            try:
                hull = shapely.concave_hull(mp, ratio=float(max(0.0, hull_ratio)), allow_holes=False)
            except Exception:
                hull = None
        if hull is None or hull.is_empty or hull.geom_type not in ("Polygon", "MultiPolygon"):
            hull = mp.convex_hull
        if hull_buffer_m and hull_buffer_m > 0:
            hull = hull.buffer(float(hull_buffer_m))
        if not hull.is_empty and hull.geom_type in ("Polygon", "MultiPolygon"):
            inside_hull = np.asarray(
                shapely.contains_xy(hull, centroid_xy[:, 0], centroid_xy[:, 1]), dtype=bool
            )
            keep_mask = near_mask | inside_hull

    # ── Build hex geometries in lon/lat by inverting the projection ───────────
    # Each vertex (vx, vy) in approx-Cartesian maps back to
    #   lon = vx / m_per_deg_lon,  lat = vy / M_LAT
    # hex_id = "H{col:04d}_{row:04d}" — unique per cell, stable across rebuilds
    # as long as grid_params.json (bbox + cell_size) does not change.
    rename_map = {col: f"grid_mean_{col.replace('capability_', '')}" for col in capability_columns}
    cells: list[dict[str, object]] = []
    for cell_idx, ((cx, cy, c_i, r_i), node_idx) in enumerate(zip(centroids, idxs)):
        if not keep_mask[cell_idx]:
            continue
        hex_id = f"H{c_i:04d}_{r_i:04d}"
        verts = []
        for k in range(6):
            # Flat-top hexagon (vertices at 0,60,...,300 deg) to match the
            # flat-top centroid lattice (x_step=1.5R, columns offset by y_step/2).
            # Drawing pointy-top here made cells wider than the column spacing and
            # they overlapped.
            angle = k * math.pi / 3.0
            vx = cx + hex_radius_m * math.cos(angle)
            vy = cy + hex_radius_m * math.sin(angle)
            verts.append((vx / m_per_deg_lon, vy / M_LAT))
        cell: dict[str, object] = {"hex_id": hex_id, "geometry": Polygon(verts)}
        # 1 = a real node sits within hex_radius_m (measured cell); 0 = hull-fill cell (an
        # interior hole or boundary cell) that exists only to make the study area's shape
        # solid. Hull-fill cells carry NO capability value — they are geometry-only.
        is_real = bool(near_mask[cell_idx])
        cell["has_data"] = int(is_real)
        if node_ids is not None:
            cell["node_id"] = str(node_ids[node_idx]) if is_real else None
        for j, col in enumerate(capability_columns):
            if is_real:
                v = cap_arr[node_idx, j]
                cell[rename_map[col]] = float(v) if np.isfinite(v) else None
            else:
                cell[rename_map[col]] = None
        cells.append(cell)

    if not cells:
        return None

    grid_gdf = gpd.GeoDataFrame(cells, geometry="geometry", crs="EPSG:4326")
    grid_gdf["cell_size_m"]   = float(cell_size_m)
    grid_gdf["hex_radius_m"]  = float(hex_radius_m)
    grid_gdf["hex_width_m"]   = float(hex_width_m)
    grid_gdf["hex_height_m"]  = float(hex_height_m)
    return grid_gdf


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
