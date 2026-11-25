"""Generic line-geometry helpers: rebuilding line parts from a POI item, merging connected
line-like ways into components, and sampling points along them at a fixed spacing. Used by
stages/snapping_stage.py to turn any line-like POI's raw OSM vertices into evenly-spaced
access-point candidates, and by utils/graphml.py to split the coastline into point POIs.
"""

import geopandas as gpd
import shapely.ops
from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry


def line_parts_from_item(geom):
    """Return line-like parts (EPSG:4326 LineStrings) of this item's geometry, if any."""
    if isinstance(geom, dict):
        gtype = geom.get("geometry_type")
        if gtype == "MultiLineString":
            parts = geom.get("line_parts") or []
            return [
                LineString([(float(lon), float(lat)) for lat, lon in part])
                for part in parts
                if isinstance(part, list) and len(part) > 1
            ]
        if gtype != "LineString":
            return []
        vertices_raw = geom.get("snap_vertices")
        if not isinstance(vertices_raw, list) or len(vertices_raw) < 2:
            return []
        return [LineString([(float(v[1]), float(v[0])) for v in vertices_raw])]
    if not isinstance(geom, BaseGeometry):
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        return list(geom.geoms)
    return []


def merge_connected_lines(lines: list[BaseGeometry]):
    """Group lines sharing an endpoint into merged LineStrings, in a metric CRS.

    Returns (merged_lines, utm_crs, component_index_per_input):
    - merged_lines: in utm_crs (meters), needed for accurate distance-based sampling.
    - component_index_per_input[i]: index into merged_lines that input line i belongs to.
    """
    if not lines:
        return [], None, []
    series = gpd.GeoSeries(lines, crs="EPSG:4326")
    utm_crs = series.estimate_utm_crs()
    lines_m = list(series.to_crs(utm_crs))

    def endpoint_key(coord):
        return (round(coord[0], 1), round(coord[1], 1))

    parent = list(range(len(lines_m)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    endpoint_to_line: dict[tuple, int] = {}
    for i, line in enumerate(lines_m):
        for coord in (line.coords[0], line.coords[-1]):
            key = endpoint_key(coord)
            if key in endpoint_to_line:
                union(i, endpoint_to_line[key])
            else:
                endpoint_to_line[key] = i

    groups: dict[int, list[int]] = {}
    for i in range(len(lines_m)):
        groups.setdefault(find(i), []).append(i)

    merged: list[BaseGeometry] = []
    component_index_per_input: list[int | None] = [None] * len(lines_m)
    for group in groups.values():
        group_lines = [lines_m[i] for i in group]
        result = shapely.ops.linemerge(group_lines) if len(group_lines) > 1 else group_lines[0]
        if result.geom_type == "LineString":
            merged_idx = len(merged)
            merged.append(result)
            for i in group:
                component_index_per_input[i] = merged_idx
        elif result.geom_type == "MultiLineString":
            # Branching component: assign each input line to whichever sub-line shares an endpoint.
            sub_lines = list(result.geoms)
            start_idx = len(merged)
            merged.extend(sub_lines)
            for i in group:
                line = lines_m[i]
                best = 0
                for si, sub in enumerate(sub_lines):
                    if sub.distance(Point(line.coords[0])) < 1.0 or sub.distance(Point(line.coords[-1])) < 1.0:
                        best = si
                        break
                component_index_per_input[i] = start_idx + best
    return merged, utm_crs, component_index_per_input


def sample_line_every(line: BaseGeometry, spacing_m: float) -> list:
    """Points every spacing_m meters along line (same CRS/units as line)."""
    points = []
    distance = 0.0
    length = line.length
    while distance <= length:
        points.append(line.interpolate(distance))
        distance += spacing_m
    return points
