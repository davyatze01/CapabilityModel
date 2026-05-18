from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely.geometry import Point

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
) -> Path:
    """Generate one GeoPackage containing all selected experiment CSVs."""
    graph_dir = Path(graph_dir)

    selected_graph = Path(graph_path) if graph_path is not None else pick_graphml_file(graph_dir, preferred_mode)
    graph = load_graph(selected_graph)

    selected_csv_paths = [Path(path) for path in csv_paths]
    combined = _prepare_combined_geodataframe(selected_csv_paths, graph)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_file(output_path, driver="GPKG")
    return output_path


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
