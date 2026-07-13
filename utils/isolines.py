"""Isovalue lines and filled iso-value bands for the capability grid.

The capability grid is a field of discrete cells, each carrying a mean capability
score in [0, 1]. Both products here are built the same way:

  1. take the (non-null) cell centroids as scattered (x, y, value) samples,
  2. interpolate them onto a regular raster in a METRIC CRS (so the raster
     resolution is a real distance and the geometry is sensible),
  3. trace the field with contourpy -- either contour *lines* at the class
     boundaries, or filled *bands* between consecutive boundaries,
  4. return a GeoDataFrame in the grid's own CRS.

The levels/edges mirror the ELECTRE class bounds used for the grid symbology in
pipeline_runner (`electre_bounds = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]`).
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, MultiPolygon, Polygon

# Interior ELECTRE class boundaries (for lines) and the class each boundary opens
# into (the class immediately ABOVE the line).
ISO_LEVELS: list[float] = [0.2, 0.4, 0.6, 0.8]
ISO_LABELS: list[str] = ["Low", "Medium", "High", "Very High"]

# Band edges (for filled polygons) and the class each band represents. Five bands
# span [0, 1]; the outermost edges are pushed to +/- infinity when filling so the
# lowest/highest bands capture the tails of the interpolated field.
ISO_BAND_EDGES: list[float] = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
ISO_BAND_LABELS: list[str] = ["Very Low", "Low", "Medium", "High", "Very High"]


def _empty_lines(crs) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"level": [], "class_above": []}, geometry=[], crs=crs)


def _empty_bands(crs) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"level_lo": [], "level_hi": [], "class_name": []}, geometry=[], crs=crs
    )


def _interpolate_field(grid_gdf: gpd.GeoDataFrame, value_field: str, cell_size_m: float, oversample: float):
    """Interpolate the cell centroids onto a regular metric raster.

    Returns ``(gx, gy, zi, metric_crs, src_crs, res)`` or ``None`` when there are
    too few samples. ``zi`` is a masked array (NaN outside the sampled hull).
    """
    from scipy.interpolate import griddata

    if value_field not in grid_gdf.columns:
        raise ValueError(f"value_field {value_field!r} not in grid columns")

    sub = grid_gdf.loc[grid_gdf[value_field].notna(), [value_field, grid_gdf.geometry.name]]
    if len(sub) < 4:
        return None

    src_crs = grid_gdf.crs
    metric_crs = sub.estimate_utm_crs() if (src_crs is not None and src_crs.is_geographic) else src_crs
    sub_m = sub.to_crs(metric_crs) if (metric_crs is not None and metric_crs != src_crs) else sub

    centroids = sub_m.geometry.centroid
    x = centroids.x.to_numpy(dtype=float)
    y = centroids.y.to_numpy(dtype=float)
    z = sub_m[value_field].to_numpy(dtype=float)

    res = max(float(cell_size_m) / max(oversample, 1.0), 1e-6)
    gx = np.arange(x.min(), x.max() + res, res)
    gy = np.arange(y.min(), y.max() + res, res)
    if gx.size < 2 or gy.size < 2:
        return None

    grid_x, grid_y = np.meshgrid(gx, gy)
    # Linear interpolation only (never cubic): cubic overshoots past [0, 1] and
    # would spawn spurious rings. Points outside the sampled hull come back NaN;
    # mask them so nothing is drawn across empty space.
    zi = griddata((x, y), z, (grid_x, grid_y), method="linear")
    zi = np.ma.masked_invalid(zi)
    return gx, gy, zi, metric_crs, src_crs, res


def compute_capability_isolines(
    grid_gdf: gpd.GeoDataFrame,
    value_field: str,
    cell_size_m: float,
    levels: list[float] | None = None,
    labels: list[str] | None = None,
    oversample: float = 2.0,
    min_length_cells: float = 1.5,
) -> gpd.GeoDataFrame:
    """Trace iso-lines of ``value_field`` at ``levels`` from the grid cells."""
    if levels is None:
        levels = ISO_LEVELS
    if labels is None:
        labels = ISO_LABELS if levels is ISO_LEVELS else [f">= {lv:g}" for lv in levels]
    if len(labels) != len(levels):
        raise ValueError("levels and labels must have the same length")

    import contourpy
    from shapely.ops import linemerge

    interp = _interpolate_field(grid_gdf, value_field, cell_size_m, oversample)
    if interp is None:
        return _empty_lines(grid_gdf.crs)
    gx, gy, zi, metric_crs, src_crs, res = interp

    contour = contourpy.contour_generator(x=gx, y=gy, z=zi, line_type=contourpy.LineType.Separate)

    # Close contours that loop back on themselves and drop boundary noise fragments.
    close_tol_sq = (res * 2.0) ** 2
    min_length_m = float(cell_size_m) * float(min_length_cells)

    rows_geom: list[LineString] = []
    rows_level: list[float] = []
    rows_class: list[str] = []
    for level, label in zip(levels, labels):
        segments = [LineString(line) for line in contour.lines(float(level)) if len(line) >= 2]
        if not segments:
            continue
        merged = linemerge(segments) if len(segments) > 1 else segments[0]
        parts = list(merged.geoms) if merged.geom_type == "MultiLineString" else [merged]
        for part in parts:
            coords = list(part.coords)
            if len(coords) < 2:
                continue
            if len(coords) >= 3:
                (x0, y0), (x1, y1) = coords[0], coords[-1]
                if (x0 - x1) ** 2 + (y0 - y1) ** 2 <= close_tol_sq:
                    coords[-1] = coords[0]  # snap shut into a closed ring
            line_geom = LineString(coords)
            if not line_geom.is_ring and line_geom.length < min_length_m:
                continue
            rows_geom.append(line_geom)
            rows_level.append(float(level))
            rows_class.append(label)

    if not rows_geom:
        return _empty_lines(grid_gdf.crs)

    out = gpd.GeoDataFrame(
        {"level": rows_level, "class_above": rows_class}, geometry=rows_geom, crs=metric_crs
    )
    if metric_crs is not None and src_crs is not None and metric_crs != src_crs:
        out = out.to_crs(src_crs)
    return out


def compute_capability_isobands_from_cells(
    grid_gdf: gpd.GeoDataFrame,
    value_field: str,
    edges: list[float] | None = None,
    labels: list[float] | None = None,
) -> gpd.GeoDataFrame:
    """Build filled iso-value polygons of ``value_field`` by dissolving the grid's
    own hexagon cells into one region per ELECTRE class band (Very Low -> Very
    High), instead of interpolating and contouring the field. Every band boundary
    then runs exactly along hexagon edges rather than a smoothed contour line.

    Returns a GeoDataFrame with columns ``level_lo`` / ``level_hi`` (the band's
    value range) and ``class_name`` (the band label), one row per non-empty band,
    in the grid's own CRS. Empty if there are no non-null cells.
    """
    if edges is None:
        edges = ISO_BAND_EDGES
    if labels is None:
        labels = ISO_BAND_LABELS
    if len(labels) != len(edges) - 1:
        raise ValueError("labels must have one fewer entry than edges")

    import shapely
    from shapely.ops import unary_union

    sub = grid_gdf.loc[grid_gdf[value_field].notna(), [value_field, grid_gdf.geometry.name]].copy()
    if sub.empty:
        return _empty_bands(grid_gdf.crs)

    # Snap every hex cell's vertices to a fixed coordinate grid before unioning.
    # The hex grid is built column-by-column with an accumulating x centroid, and
    # each vertex's y-offset comes from math.sin(angle) while the row-to-row step
    # comes from a separate sqrt(3)*R formula -- two nominally-coincident vertices
    # (the diagonal edge shared by adjacent columns) can therefore differ by a few
    # ULPs. unary_union only dissolves an edge when its endpoints are bit-identical,
    # so without this the "unified" band comes out as one polygon per column with
    # a hairline gap at every column seam -- exactly the vertical-strip artifact.
    # 1e-9 degrees (~0.1mm on the ground) is far below any hex edge length, so this
    # only removes floating-point noise; it never nudges the perimeter visibly
    # outside the true hexagon bounds.
    PRECISION_GRID_SIZE = 1e-9
    sub[sub.geometry.name] = shapely.set_precision(sub.geometry.to_numpy(), PRECISION_GRID_SIZE)

    # np.digitize with the interior edges as bin walls: values below edges[1] get
    # band 0, up to values >= edges[-2] getting the last band -- so 0.0 and 1.0
    # both land inside the first/last band rather than falling outside every bin.
    inner_edges = list(edges[1:-1])
    band_idx = np.digitize(sub[value_field].to_numpy(dtype=float), inner_edges, right=False)

    rows_geom = []
    rows_lo: list[float] = []
    rows_hi: list[float] = []
    rows_class: list[str] = []
    for i in range(len(labels)):
        mask = band_idx == i
        if not mask.any():
            continue
        merged = unary_union(sub.geometry.to_numpy()[mask])
        if merged.is_empty:
            continue
        if merged.geom_type == "Polygon":
            merged = MultiPolygon([merged])
        rows_geom.append(merged)
        rows_lo.append(float(edges[i]))
        rows_hi.append(float(edges[i + 1]))
        rows_class.append(labels[i])

    if not rows_geom:
        return _empty_bands(grid_gdf.crs)

    return gpd.GeoDataFrame(
        {"level_lo": rows_lo, "level_hi": rows_hi, "class_name": rows_class},
        geometry=rows_geom,
        crs=grid_gdf.crs,
    )
