from __future__ import annotations

import math
from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from shapely.geometry import Point, Polygon

from plotting.plot_shapefile import find_value_column, load_graph, pick_graphml_file


DEFAULT_GRAPH_MODE = "bike"
DEFAULT_EXPERIMENTS_DIR = Path("experiments")
DEFAULT_OUTPUT_DIR = Path("outputs/shapefiles")

# Table name for the capability-grid layer in the exported GeoPackage. When a
# colleague drags the whole .gpkg into QGIS (rather than opening our generated
# project), QGIS's own "select layers to add" dialog re-sorts the candidate
# layers by name before adding them -- our gpkg_contents insertion order (which
# controls plain GDAL/OGR enumeration) turned out not to matter for that
# dialog. A name that alphabetically sorts after every "service_*" view name
# is the only thing that reliably keeps the capability grid on top once dragged
# in, since each newly-added layer stacks above the ones already added.
GRID_TABLE_NAME = "zz_capability_grid"

# One isobands table per capability, one dissolved region per ELECTRE class band
# (Very Low .. Very High) in each. Named to sort after GRID_TABLE_NAME so a bare
# drag-in stacks them consistently relative to the capability grid, and split per
# capability so all three can be loaded side by side in the project and toggled
# independently instead of only ever showing whichever single field the grid
# happens to be colored by.
ISOBANDS_TABLE_NAMES = {
    "nutrition": "zzz_capability_isobands_nutrition",
    "care": "zzz_capability_isobands_care",
    "restorativeness": "zzz_capability_isobands_restorativeness",
}

# Point layers of named places overlaid on the maps, the way Google Maps labels
# an area. Split into two tables -- comuni and quartieri -- so each can carry its
# own STATIC label style (font/size/color, no data-defined expressions). The
# qgis2web / OpenLayers exporter ignores expression-based styling, so a single
# layer with data-defined size/color exports as plain text; two statically-styled
# layers survive the export. Named to sort after every other layer so a bare
# drag-in of the .gpkg stacks the labels on top of the fills and bands.
PLACE_COMUNI_TABLE_NAME = "zzzz_place_comuni"
PLACE_QUARTIERI_TABLE_NAME = "zzzz_place_quartieri"

# (kind value in the labels GeoDataFrame -> destination table name)
PLACE_LABEL_TABLES = {
    "comune": PLACE_COMUNI_TABLE_NAME,
    "quartiere": PLACE_QUARTIERI_TABLE_NAME,
}


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


def _prepare_geodataframe(frame: pd.DataFrame, graph) -> tuple[gpd.GeoDataFrame, str]:
    """Build a GeoDataFrame for one experiment from CSV rows plus graph geometry.

    `graph` may be an nx.Graph or a zero-arg callable returning one: exports
    whose CSVs carry lon/lat never touch the graph, so a callable lets callers
    defer the (slow, multi-GB) GraphML load until a CSV actually needs it.
    """
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
        if callable(graph):
            graph = graph()
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


def _prepare_combined_geodataframe(csv_paths: list[Path], graph) -> gpd.GeoDataFrame:
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

        # Include all capability_* and service_* columns so a single CSV carrying
        # every capability and its underlying services is passed through intact
        # rather than reduced to one column.
        extra_cols = [
            col
            for col in gdf.columns
            if (str(col).startswith("capability_") or str(col).startswith("service_"))
            and col != value_column
        ]
        keep = ["join_key", "node_id", "lon", "lat", "geometry", value_column] + extra_cols
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
    grid_params_path: str | Path | None = None,
    grid_exclude_water: bool = True,
    grid_water_cache_path: str | Path | None = None,
    place_labels_enabled: bool = True,
    place_labels_cache_path: str | Path | None = None,
) -> Path:
    """Generate one GeoPackage containing all selected experiment CSVs."""
    graph_dir = Path(graph_dir)

    # Deferred: pipeline CSVs carry lon/lat, so the (slow, multi-GB) GraphML is
    # only loaded if some CSV actually lacks coordinates. This keeps the rebuild
    # path runnable on machines that have no graph/ artifacts at all.
    _graph_memo: list[nx.Graph] = []

    def _lazy_graph() -> nx.Graph:
        if not _graph_memo:
            selected = Path(graph_path) if graph_path is not None else pick_graphml_file(graph_dir, preferred_mode)
            _graph_memo.append(load_graph(selected))
        return _graph_memo[0]

    selected_csv_paths = [Path(path) for path in csv_paths]
    combined = _prepare_combined_geodataframe(selected_csv_paths, _lazy_graph)
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
            grid_params_path=grid_params_path,
            exclude_water=bool(grid_exclude_water),
            water_cache_path=grid_water_cache_path,
        )
        if grid_layer is not None and not grid_layer.empty:
            grid_layer = _prepare_gpkg_layer(grid_layer)
            # Write the grid to its own throwaway single-layer GeoPackage (a GDAL
            # write that never appends, so it can't hit the "NULL pointer" append
            # bug), then fold its table into output_path via plain SQL. This keeps
            # the export to exactly one file instead of sometimes falling back to
            # a second "_grid" sidecar when GDAL's append path is broken.
            staging_grid = output_path.with_name(f"{output_path.stem}_grid_staging{output_path.suffix}")
            if staging_grid.exists():
                staging_grid.unlink()
            _write_gpkg_layer(grid_layer, staging_grid, layer_name=GRID_TABLE_NAME, mode="w")
            try:
                _merge_gpkg_table(staging_grid, output_path, GRID_TABLE_NAME)
            finally:
                staging_grid.unlink()

            try:
                service_views = _create_service_grid_views(output_path)
                if service_views:
                    print(f"[Export] Registered {len(service_views)} per-service views in {output_path.name}", flush=True)
            except Exception as exc:
                print(f"[Export] Failed to register per-service views: {exc}", flush=True)

            # Filled iso-value polygons, one per capability, each dissolving the
            # grid's own hexagon cells into one region per ELECTRE class band
            # (Very Low .. Very High) -- so band boundaries follow hexagon edges
            # instead of a smoothed contour. Built for every capability present
            # in the grid, not just the one it's currently colored by, so all
            # three can be loaded in the project and toggled independently.
            from utils.isolines import compute_capability_isobands_from_cells

            for _capability, _iso_table in ISOBANDS_TABLE_NAMES.items():
                try:
                    iso_value_field = f"grid_mean_{_capability}"
                    if iso_value_field not in grid_layer.columns:
                        print(f"[Export] Skipping iso-bands: {iso_value_field} not in grid.", flush=True)
                        continue

                    isobands = compute_capability_isobands_from_cells(grid_layer, iso_value_field)
                    if isobands is None or isobands.empty:
                        print(f"[Export] No iso-value bands produced for {_capability} (field too sparse).", flush=True)
                        continue

                    isobands = _prepare_gpkg_layer(isobands)
                    staging_iso = output_path.with_name(
                        f"{output_path.stem}_iso_staging_{_capability}{output_path.suffix}"
                    )
                    if staging_iso.exists():
                        staging_iso.unlink()
                    _write_gpkg_layer(isobands, staging_iso, layer_name=_iso_table, mode="w")
                    try:
                        _merge_gpkg_table(staging_iso, output_path, _iso_table)
                    finally:
                        staging_iso.unlink()
                    print(
                        f"[Export] Wrote {len(isobands)} iso-value bands ({_iso_table}) from {iso_value_field}",
                        flush=True,
                    )
                except Exception as exc:
                    print(f"[Export] Failed to build iso-bands for {_capability}: {exc}", flush=True)

            # Write a flat CSV indexed by hex_id so QGIS attributes and the
            # tabular output share the same primary key.
            if "hex_id" in grid_layer.columns:
                hex_csv_path = output_path.with_name(f"{output_path.stem}_hex.csv")
                csv_cols = ["hex_id"] + (
                    ["node_id"] if "node_id" in grid_layer.columns else []
                ) + [c for c in grid_layer.columns if c.startswith("grid_mean_")]
                grid_layer[csv_cols].to_csv(hex_csv_path, index=False)

    # ── Place labels (comuni + quartieri) ─────────────────────────────────────
    # A point layer of named places covering the study-area bbox, overlaid on the
    # capability maps the way Google Maps labels an area. Best-effort: a failed or
    # empty OSM fetch just skips the layer instead of failing the whole export.
    if place_labels_enabled:
        try:
            from utils.place_labels import get_place_labels

            min_lon, min_lat, max_lon, max_lat = (float(v) for v in combined.total_bounds)
            cache_path = (
                Path(place_labels_cache_path)
                if place_labels_cache_path is not None
                else Path("outputs") / "place_labels.gpkg"
            )
            places_gdf = get_place_labels(min_lon, min_lat, max_lon, max_lat, cache_path=cache_path)
            if places_gdf is not None and not places_gdf.empty and "place_kind" in places_gdf.columns:
                # Split into one table per kind so each carries its own static
                # label style (comuni vs quartieri), which is what survives the
                # qgis2web / OpenLayers export (see PLACE_LABEL_TABLES).
                for _kind, _table in PLACE_LABEL_TABLES.items():
                    subset = places_gdf[places_gdf["place_kind"] == _kind]
                    if subset.empty:
                        continue
                    subset = _prepare_gpkg_layer(subset)
                    staging_places = output_path.with_name(
                        f"{output_path.stem}_{_kind}_staging{output_path.suffix}"
                    )
                    if staging_places.exists():
                        staging_places.unlink()
                    _write_gpkg_layer(subset, staging_places, layer_name=_table, mode="w")
                    try:
                        _merge_gpkg_table(staging_places, output_path, _table)
                    finally:
                        staging_places.unlink()
                    print(f"[Export] Wrote {len(subset)} place labels ({_table})", flush=True)
            else:
                print("[Export] No place labels found for the study-area bbox.", flush=True)
        except Exception as exc:
            print(f"[Export] Failed to build place labels: {exc}", flush=True)

    # Done last, and only via a plain sqlite3 DDL edit (not a GDAL/OGR write):
    # doing this in between the GDAL writes above confuses GDAL's own cache of
    # the file's schema and reliably breaks the capability_grid append that
    # follows (observed as GDAL's "NULL pointer" error).
    try:
        _hide_gpkg_layer(output_path, "capability_points")
    except Exception as exc:
        print(f"[Export] Failed to hide capability_points layer: {exc}", flush=True)

    return output_path


def _merge_gpkg_table(src_gpkg: Path, dest_gpkg: Path, table_name: str) -> None:
    """Copy one vector table from src_gpkg into dest_gpkg via plain SQLite.

    GDAL's own "append a second layer" path (`to_file(..., mode="a")`) is
    unreliable in some environments -- both the pyogrio and fiona engines can
    fail identically with a "NULL pointer error" -- which used to force a
    second, sidecar .gpkg file just to hold the grid layer. Writing the grid to
    its own throwaway single-layer GeoPackage (a GDAL write that always
    succeeds, since it never appends) and then copying its table straight into
    the main file with SQL avoids GDAL's append path entirely, so the caller
    always ends up with exactly one output file.

    A GeoPackage geometry column is a BLOB with a small header wrapping
    standard WKB, so a raw `CREATE TABLE ... AS SELECT *` byte-copies valid
    geometries as-is. Both files share the same CRS (EPSG:4326), so the srs_id
    referenced by src's gpkg_geometry_columns row already exists in dest.

    The connection is opened on src (not dest) and dest is ATTACHed, not the
    other way around: dest was already written by GDAL earlier in this same
    process, and a fresh sqlite3 connection rooted on a file GDAL just touched
    fails to see tables in a freshly-ATTACHed second file ("no such table"),
    even though that file is on disk and intact -- some GDAL builds leave
    process-wide SQLite state (e.g. shared-cache mode) that only manifests when
    that file is the *primary* connection. Rooting the connection on src (the
    one nothing has opened yet) and writing into the ATTACHed dest avoids it.
    """
    import sqlite3

    with sqlite3.connect(src_gpkg) as conn:
        conn.execute("ATTACH DATABASE ? AS dest", (str(dest_gpkg),))
        try:
            conn.execute(f'DROP TABLE IF EXISTS dest."{table_name}"')
            conn.execute(f'CREATE TABLE dest."{table_name}" AS SELECT * FROM "{table_name}"')
            conn.execute("DELETE FROM dest.gpkg_contents WHERE table_name = ?", (table_name,))
            conn.execute(
                "INSERT INTO dest.gpkg_contents SELECT * FROM gpkg_contents WHERE table_name = ?",
                (table_name,),
            )
            conn.execute("DELETE FROM dest.gpkg_geometry_columns WHERE table_name = ?", (table_name,))
            conn.execute(
                "INSERT INTO dest.gpkg_geometry_columns SELECT * FROM gpkg_geometry_columns WHERE table_name = ?",
                (table_name,),
            )
            conn.commit()
        finally:
            conn.execute("DETACH DATABASE dest")


def _hide_gpkg_layer(gpkg_path: Path, table_name: str) -> None:
    """Unregister a table as a GeoPackage vector layer without touching its data.

    Removes the table from gpkg_contents/gpkg_geometry_columns so QGIS/OGR no
    longer list it as a layer on import, while the table itself (and its rows)
    stays intact -- debug tooling (inspect_hex_pois.py, debug_pipeline.py) reads
    it with plain `sqlite3` SELECTs on node_id/lon/lat, not through OGR, so it
    keeps working unchanged.
    """
    import sqlite3

    with sqlite3.connect(gpkg_path) as conn:
        conn.execute("DELETE FROM gpkg_contents WHERE table_name = ?", (table_name,))
        conn.execute("DELETE FROM gpkg_geometry_columns WHERE table_name = ?", (table_name,))
        conn.commit()


def _create_service_grid_views(gpkg_path: Path) -> list[str]:
    """Expose each grid_mean_* column of the grid table as its own layer.

    A GeoPackage can only carry ONE default style per table, so embedding the
    per-service and per-capability color maps on the grid table leaves them
    invisible on plain import (QGIS applies just the table's default style).
    Registering one SQL view per service (service_<name>) and per capability
    (capability_<name>) in gpkg_contents/gpkg_geometry_columns makes each
    appear as its own layer when the file is imported, and each view can then
    carry its color map as its own default style -- exactly the same treatment
    the iso-band tables already get. Views add no data: they select straight
    from the grid table, filtered to measured cells (has_data = 1) so hull-fill
    cells with NULL values are not drawn.
    """
    import sqlite3

    created: list[str] = []
    with sqlite3.connect(gpkg_path) as conn:
        geom_row = conn.execute(
            "SELECT column_name, geometry_type_name, srs_id, z, m "
            "FROM gpkg_geometry_columns WHERE table_name = ?",
            (GRID_TABLE_NAME,),
        ).fetchone()
        if geom_row is None:
            return created
        geom_col = str(geom_row[0])

        columns = [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{GRID_TABLE_NAME}")')]
        base_columns = [c for c in ("fid", geom_col, "hex_id", "node_id", "has_data") if c in columns]
        where_clause = 'WHERE "has_data" = 1' if "has_data" in columns else ""

        # (view name, value column, description) for both per-service views and
        # per-capability views. Capability views come from the aggregate
        # grid_mean_<capability> columns; service views from grid_mean_service_*.
        view_specs: list[tuple[str, str, str]] = []
        for column in columns:
            if column.startswith("grid_mean_service_"):
                view_specs.append((
                    "service_" + column[len("grid_mean_service_"):],
                    column,
                    "Per-service view of the capability grid",
                ))
        for _capability in ISOBANDS_TABLE_NAMES:
            column = f"grid_mean_{_capability}"
            if column in columns:
                view_specs.append((
                    f"capability_{_capability}",
                    column,
                    "Per-capability view of the capability grid",
                ))

        for view, value_column, description in view_specs:
            select_list = ", ".join(f'"{c}"' for c in base_columns + [value_column])
            conn.execute(f'DROP VIEW IF EXISTS "{view}"')
            conn.execute(
                f'CREATE VIEW "{view}" AS SELECT {select_list} FROM "{GRID_TABLE_NAME}" {where_clause}'
            )
            conn.execute("DELETE FROM gpkg_contents WHERE table_name = ?", (view,))
            conn.execute(
                "INSERT INTO gpkg_contents "
                "(table_name, data_type, identifier, description, min_x, min_y, max_x, max_y, srs_id) "
                "SELECT ?, data_type, ?, ?, "
                "min_x, min_y, max_x, max_y, srs_id "
                "FROM gpkg_contents WHERE table_name = ?",
                (view, view, description, GRID_TABLE_NAME),
            )
            conn.execute("DELETE FROM gpkg_geometry_columns WHERE table_name = ?", (view,))
            conn.execute(
                "INSERT INTO gpkg_geometry_columns "
                "(table_name, column_name, geometry_type_name, srs_id, z, m) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (view, geom_col, geom_row[1], geom_row[2], geom_row[3], geom_row[4]),
            )
            created.append(view)

        # Re-register the grid table last (highest gpkg_contents rowid). QGIS's
        # querySublayers() -- what actually backs the "select layers to add"
        # dialog when a plain .gpkg is dragged in, with no custom project to
        # apply explicit z-order -- returns sublayers in gpkg_contents order,
        # not alphabetically; each newly-added layer stacks above the ones
        # already added, so whichever sublayer is listed last ends up on top.
        # The grid table was registered before any of the service views, so it
        # needs to be moved to the end alongside the alphabet-defeating name
        # (GRID_TABLE_NAME) in case some other GIS tool's import instead goes
        # by name.
        grid_contents_row = conn.execute(
            "SELECT * FROM gpkg_contents WHERE table_name = ?", (GRID_TABLE_NAME,)
        ).fetchone()
        if grid_contents_row is not None:
            columns = [d[0] for d in conn.execute("SELECT * FROM gpkg_contents LIMIT 0").description]
            conn.execute("DELETE FROM gpkg_contents WHERE table_name = ?", (GRID_TABLE_NAME,))
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(f"INSERT INTO gpkg_contents ({', '.join(columns)}) VALUES ({placeholders})", grid_contents_row)

        conn.commit()
    return created


def _build_capability_grid(
    points_gdf: gpd.GeoDataFrame,
    capability_field: str,
    cell_size_m: float,
    max_cells: int,
    fill_hull: bool = True,
    hull_buffer_m: float = 0.0,
    hull_ratio: float = 0.3,
    grid_params_path: str | Path | None = None,
    exclude_water: bool = True,
    water_cache_path: str | Path | None = None,
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

    exclude_water drops hull-fill cells (has_data=0, no real sampled node) that lie entirely
    within OSM water polygons (natural=water, riverbanks, basins/reservoirs). Cells with a real
    node (has_data=1) are always kept, and hull-fill cells only partly over water (e.g. a
    coastline cell) are also kept, since the "fully submerged" test uses cell.within(water).
    """
    from scipy.spatial import cKDTree

    capability_columns = [column for column in points_gdf.columns if str(column).startswith("capability_")]
    if not capability_columns:
        return None
    if capability_field not in capability_columns:
        capability_field = capability_columns[0]

    # Carry the underlying per-service scores through to the grid as well, so each
    # hexagon exposes both its capability values and the individual service values
    # that feed them (grid_mean_service_*).
    service_columns = [column for column in points_gdf.columns if str(column).startswith("service_")]
    value_columns = capability_columns + service_columns

    _id_col = "node_id" if "node_id" in points_gdf.columns else None
    _extra = [_id_col] if _id_col else []
    work = points_gdf[["geometry"] + _extra + value_columns].copy()
    for column in value_columns:
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
    # Prefer the caller-provided (per-city) path; the shared "outputs/grid_params.json"
    # is a legacy fallback for callers that don't pass one — that shared file is
    # overwritten by whichever city ran most recently, so relying on it here would
    # silently draw one city's capability grid using another city's tiling.
    import json as _json
    _params_path = Path(grid_params_path) if grid_params_path is not None else Path("outputs") / "grid_params.json"
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

    cap_arr = work[value_columns].to_numpy(dtype=float)
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

    # ── Fetch OSM water polygons covering the grid bbox (best-effort) ─────────
    # Used below to drop hull-fill cells that lie entirely in water. Cached per city
    # so repeat runs don't re-hit Overpass; falls back to no exclusion on any failure.
    water_union = None
    if exclude_water:
        from utils.water_mask import get_water_union

        try:
            cache_path = (
                Path(water_cache_path) if water_cache_path is not None else Path("outputs") / "water_mask.gpkg"
            )
            water_union = get_water_union(
                min_lon=min_x / m_per_deg_lon,
                min_lat=min_y / M_LAT,
                max_lon=max_x / m_per_deg_lon,
                max_lat=max_y / M_LAT,
                cache_path=cache_path,
                probe_lons=node_xy[:, 0] / m_per_deg_lon,
                probe_lats=node_xy[:, 1] / M_LAT,
            )
        except Exception as exc:
            print(f"[Water] Water exclusion unavailable ({exc}); keeping all hull-fill cells.", flush=True)
            water_union = None

    # ── Build hex geometries in lon/lat by inverting the projection ───────────
    # Each vertex (vx, vy) in approx-Cartesian maps back to
    #   lon = vx / m_per_deg_lon,  lat = vy / M_LAT
    # hex_id = "H{col:04d}_{row:04d}" — unique per cell, stable across rebuilds
    # as long as grid_params.json (bbox + cell_size) does not change.
    # capability_care -> grid_mean_care ; service_food_access -> grid_mean_service_food_access
    rename_map = {col: f"grid_mean_{col.replace('capability_', '')}" for col in value_columns}
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
        for j, col in enumerate(value_columns):
            if is_real:
                v = cap_arr[node_idx, j]
                cell[rename_map[col]] = float(v) if np.isfinite(v) else None
            else:
                cell[rename_map[col]] = None
        cells.append(cell)

    if not cells:
        return None

    # ── Drop hull-fill cells (no real node) that lie entirely within water ────
    # Only candidates with has_data=0 are checked; a cell with a real node is kept
    # regardless. cell.within(water_union) requires the WHOLE hexagon to be submerged,
    # so cells straddling a coastline (partly land) are kept.
    if water_union is not None and not water_union.is_empty:
        import shapely

        fill_idx = [i for i, c in enumerate(cells) if c["has_data"] == 0]
        if fill_idx:
            fill_geoms = np.array([cells[i]["geometry"] for i in fill_idx], dtype=object)
            submerged = shapely.within(fill_geoms, water_union)
            drop_idx = {fill_idx[k] for k, sub in enumerate(submerged) if sub}
            if drop_idx:
                cells = [c for i, c in enumerate(cells) if i not in drop_idx]

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
