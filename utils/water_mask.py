"""Fetch and cache OSM water polygons, used to drop hull-fill hexagons that sit
entirely in water (lakes, rivers, open sea) with no real sampled node backing them.

Two sources are combined:
  - Closed water polygons tagged directly in OSM (natural=water, riverbanks, basins).
  - Open sea/bays, which OSM represents only as natural=coastline *lines* (there is no
    polygon for "everything seaward of this line"). These are turned into a polygon by
    polygonizing the coastline against the query bbox, then classifying each resulting
    ring as land or water using the sampled routing-graph nodes as ground truth: a ring
    containing real street nodes is land, one containing none is water.
"""
from __future__ import annotations

from pathlib import Path
from typing import cast

import geopandas as gpd
import numpy as np
import osmnx as ox
import shapely
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union

WATER_TAGS: dict[str, object] = {
    "natural": ["water", "coastline", "bay", "strait"],
    "waterway": ["riverbank", "dock"],
    "landuse": ["basin", "reservoir", "salt_pond", "aquaculture"],
    "leisure": "marina",
    "harbour": "yes",
}

# Overpass can hang far longer than any local computation in this pipeline is
# willing to wait for; fail fast and let the caller fall back to "no water mask"
# rather than stall the whole run on a slow/unreachable Overpass server.
_REQUEST_TIMEOUT_S = 60


def _coastline_water_polygon(
    coast_gdf: gpd.GeoDataFrame,
    bbox_poly: Polygon,
    probe_lons: np.ndarray,
    probe_lats: np.ndarray,
) -> BaseGeometry | None:
    """Turn natural=coastline lines into a water polygon via polygonize + land probing."""
    if coast_gdf.empty or probe_lons.size == 0:
        return None

    lines = []
    for geom in coast_gdf.geometry:
        if geom is None or geom.is_empty:
            continue
        clipped = geom.intersection(bbox_poly)
        if not clipped.is_empty:
            lines.append(clipped)
    if not lines:
        return None

    # Close open coastline segments at the query boundary so polygonize can form rings.
    merged = unary_union(lines + [bbox_poly.boundary])
    rings = list(polygonize(merged))
    if not rings:
        return None

    water_rings = []
    for ring in rings:
        clipped_ring = ring.intersection(bbox_poly)
        if clipped_ring.is_empty or clipped_ring.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        contains_land_node = bool(np.any(shapely.contains_xy(clipped_ring, probe_lons, probe_lats)))
        if not contains_land_node:
            water_rings.append(clipped_ring)

    if not water_rings:
        return None
    return unary_union(water_rings)


def get_water_union(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    cache_path: str | Path,
    probe_lons: np.ndarray | None = None,
    probe_lats: np.ndarray | None = None,
) -> BaseGeometry | None:
    """Return the union of OSM water (incl. open sea) covering the given bbox, or None.

    `probe_lons`/`probe_lats` should be the lon/lat of the real sampled routing nodes;
    they're used only to resolve which side of a coastline is water (a ring with no real
    street node in it). Reads/writes a per-city GeoPackage cache at `cache_path` so repeat
    runs don't re-hit Overpass. Returns None (rather than raising) if no cached file exists
    and the Overpass fetch fails or times out, so callers can skip the water exclusion step
    instead of blocking the pipeline.
    """
    cache_path = Path(cache_path)

    if cache_path.exists():
        try:
            water_gdf = gpd.read_file(cache_path)
            if water_gdf.empty:
                return None
            return water_gdf.unary_union
        except Exception as exc:
            print(f"[Water] Failed to read cached water layer {cache_path}: {exc}", flush=True)

    prev_timeout = ox.settings.requests_timeout
    try:
        ox.settings.requests_timeout = _REQUEST_TIMEOUT_S
        raw_gdf = ox.features_from_bbox(bbox=(max_lat, min_lat, max_lon, min_lon), tags=WATER_TAGS)
    except Exception as exc:
        print(f"[Water] OSM water fetch failed ({exc}); skipping water exclusion.", flush=True)
        return None
    finally:
        ox.settings.requests_timeout = prev_timeout

    if raw_gdf.empty:
        return None

    poly_gdf = raw_gdf[raw_gdf.geometry.type.isin(["Polygon", "MultiPolygon"])]

    coastline_water = None
    if "natural" in raw_gdf.columns:
        coast_gdf = raw_gdf[
            raw_gdf.geometry.type.isin(["LineString", "MultiLineString"]) & (raw_gdf["natural"] == "coastline")
        ]
        if not coast_gdf.empty and probe_lons is not None and probe_lats is not None:
            bbox_poly = box(min_lon, min_lat, max_lon, max_lat)
            try:
                coastline_water = _coastline_water_polygon(coast_gdf, bbox_poly, probe_lons, probe_lats)
            except Exception as exc:
                print(f"[Water] Coastline-to-water resolution failed ({exc}); using tagged water only.", flush=True)
                coastline_water = None

    parts = [g for g in poly_gdf.geometry.tolist() if g is not None and not g.is_empty]
    if coastline_water is not None and not coastline_water.is_empty:
        parts.append(coastline_water)

    if not parts:
        return None

    union = unary_union(parts)
    if union.is_empty:
        return None
    if isinstance(union, (Polygon, MultiPolygon)) and not union.is_valid:
        union = cast(Polygon | MultiPolygon, union.buffer(0))

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        gpd.GeoDataFrame(geometry=[union], crs="EPSG:4326").to_file(cache_path, driver="GPKG")
    except Exception as exc:
        print(f"[Water] Failed to cache water layer to {cache_path}: {exc}", flush=True)

    return union
