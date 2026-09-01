import os
import sys
import time
import threading
from typing import Callable, TypeVar
import geopandas as gpd
import pandas as pd
import osmnx as ox
from osmnx import settings as ox_settings
import osmium
import osmium.geom
import osmium.area
import osmium.index
import shapely.wkb as wkblib
from shapely.prepared import prep

T = TypeVar("T")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

# Our buffer polygon (~3500 km²) exceeds osmnx's default max_query_area_size (2500 km²), but
# even its own automatic quadrat-cut split at that default is too coarse for this Overpass
# server to answer each piece within timeout -- shrink the quadrat size so osmnx splits the
# query into many smaller sub-requests and merges them itself (built-in, tested, and already
# dedups by OSM element id -- see osmnx.utils_geo._consolidate_subdivide_geometry).
ox_settings.max_query_area_size = 100_000_000  # ~10km x 10km quadrats
# osmnx pings <endpoint>/status before every request to self-throttle by server load. That
# status check itself is timing out in this environment and its own exception handler has a
# bug (references an unset `response` var), turning a plain timeout into an opaque
# UnboundLocalError on every single request regardless of query size. Skip it entirely --
# _fetch_with_retries already covers transient failures (including a real 429) with bounded
# retries/backoff, so we don't need osmnx's adaptive pause.
ox_settings.overpass_rate_limit = False
# overpass.openstreetmap.fr and other public mirrors either block osmnx's repeated requests
# (whitelist-only) or 502/timeout outright -- overpass-api.de itself is flaky but the only one
# that has actually returned real data this session (fountains, waterways both succeeded on
# it), so stay on it and lean on per-tag checkpointing + retries instead of mirror-hopping.

BOUNDARY_GPKG = "shapefile_base/mgp_boundary_expanded.gpkg"
BUFFER_LAYER = "expanded_buffer_15km"
POI_POINT_SHP = "Paris/POI_point2.shp"
POI_LINE_SHP = "Paris/POI_line2.shp"
POI_POLYGON_SHP = "Paris/POI_polygon2.shp"


class _ElapsedTimer:
    def __init__(self, label: str) -> None:
        self._label = label
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._tick, daemon=True)

    def _tick(self) -> None:
        start = time.monotonic()
        while not self._stop.wait(timeout=1.0):
            elapsed = int(time.monotonic() - start)
            print(f"\r[{self._label}] {elapsed}s elapsed...", end="", flush=True)
        elapsed = time.monotonic() - start
        print(f"\r[{self._label}] done in {elapsed:.1f}s" + " " * 20, flush=True)

    def __enter__(self) -> "_ElapsedTimer":
        print(f"[{self._label}] starting...", flush=True)
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._stop.set()
        self._thread.join()


def _fetch_with_retries(label: str, fetch_fn: Callable[[], T], max_attempts: int = 3, retry_delay_s: int = 30) -> T:
    for attempt in range(1, max_attempts + 1):
        try:
            return fetch_fn()
        except Exception as exc:
            if attempt == max_attempts:
                raise
            print(f"[{label}] attempt {attempt}/{max_attempts} failed ({exc!r}); "
                  f"retrying in {retry_delay_s}s...", flush=True)
            time.sleep(retry_delay_s)
    raise AssertionError("unreachable: loop always returns or raises on its last attempt")


def load_buffer_polygon():
    buffer_gdf = gpd.read_file(BOUNDARY_GPKG, layer=BUFFER_LAYER)
    return buffer_gdf.geometry.iloc[0]


def fetch_fountains(polygon):
    def _do():
        with _ElapsedTimer("Overpass: fountains"):
            return ox.features_from_polygon(polygon, tags={"amenity": "fountain"})
    gdf = _fetch_with_retries("fountains", _do)
    gdf = gdf[gdf.geometry.geom_type == "Point"].copy()
    gdf["TYPEQU"] = "BI05"
    return gdf[["TYPEQU", "geometry"]]


def append_points(new_points):
    existing = gpd.read_file(POI_POINT_SHP)
    combined = gpd.GeoDataFrame(
        pd.concat([existing, new_points.to_crs(existing.crs)], ignore_index=True),
        crs=existing.crs,
    )
    combined.to_file(POI_POINT_SHP, driver="ESRI Shapefile")
    print(f"Appended {len(new_points)} fountain points to {POI_POINT_SHP} (total {len(combined)})")


WATERWAY_TYPEQU = {
    "river": "BI07",
    "stream": "BI08",
    "canal": "BI09",
}


def fetch_waterways(polygon):
    def _do():
        with _ElapsedTimer("Overpass: waterways"):
            return ox.features_from_polygon(polygon, tags={"waterway": list(WATERWAY_TYPEQU.keys())})
    gdf = _fetch_with_retries("waterways", _do)
    gdf = gdf[gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy()
    gdf["TYPEQU"] = gdf["waterway"].map(WATERWAY_TYPEQU)
    gdf = gdf[gdf["TYPEQU"].notna()]
    return gdf[["TYPEQU", "geometry"]]


def write_lines(new_lines):
    new_lines.to_file(POI_LINE_SHP, driver="ESRI Shapefile")
    print(f"Wrote {len(new_lines)} waterway lines to {POI_LINE_SHP}")


LEISURE_TYPEQU = {"park": "GI01", "garden": "GI02", "common": "GI03", "playground": "GI04"}
LANDUSE_TYPEQU = {"grass": "GI05", "meadow": "GI06", "forest": "GI09"}
NATURAL_TYPEQU = {"grassland": "GI07", "wood": "GI08", "scrub": "GI10", "heath": "GI11"}
WATER_SUBTAG_TYPEQU = {
    "lake": "BI01",
    "pond": "BI02",
    "basin": "BI03",
    "fishpond": "BI04",
    "reflecting_pool": "BI06",
}


# Ile-de-France's Geofabrik extract is clipped to the region's real (non-rectangular)
# administrative border, not to a bounding box -- anywhere our 15km buffer pokes past that
# border into a neighbouring departement would silently have zero OSM data. The full France
# extract is a strict superset regardless of exactly where that border falls (~4.7GB vs
# ~340MB, so the download and local passes both take longer).
FRANCE_EXTRACT_URL = "https://download.geofabrik.de/europe/france-latest.osm.pbf"


def refresh_regional_extract() -> str:
    """Delete the cached France PBF and re-download the current one.

    Reuses the pipeline's own downloader (resumable, .md5-verified) instead of
    reimplementing it -- see routing.public_transport_routing_stage._download_regional_extract.
    """
    from core.config import PipelineConfig
    from routing.public_transport_routing_stage import _download_regional_extract

    cfg = PipelineConfig(study_city="paris")
    cfg.osm_extract_url = FRANCE_EXTRACT_URL
    cache_dir = os.path.join(cfg.routing_data_dir, "_extracts")
    final_path = os.path.join(cache_dir, os.path.basename(cfg.osm_extract_url))
    if os.path.isfile(final_path):
        os.remove(final_path)
        print(f"[Polygons] Removed cached extract to force refresh: {final_path}", flush=True)
    return _download_regional_extract(cfg)


def _resolve_typequ(tags: dict) -> str | None:
    if tags.get("leisure") in LEISURE_TYPEQU:
        return LEISURE_TYPEQU[tags["leisure"]]
    if tags.get("landuse") in LANDUSE_TYPEQU:
        return LANDUSE_TYPEQU[tags["landuse"]]
    if tags.get("natural") == "water":
        water_subtag = tags.get("water")
        return WATER_SUBTAG_TYPEQU.get(water_subtag) if water_subtag is not None else None
    if tags.get("natural") in NATURAL_TYPEQU:
        return NATURAL_TYPEQU[tags["natural"]]
    return None


class _AreaCollector(osmium.SimpleHandler):
    """Collects assembled OSM areas (closed ways + multipolygon relations) matching
    our leisure/landuse/natural tags and intersecting the buffer polygon."""

    def __init__(self, buffer_polygon):
        super().__init__()
        self._wkbfab = osmium.geom.WKBFactory()
        self._buffer_prepared = prep(buffer_polygon)
        self.records: list[tuple[str, object]] = []

    def area(self, a):
        typequ = _resolve_typequ(dict(a.tags))
        if typequ is None:
            return
        try:
            wkb = self._wkbfab.create_multipolygon(a)
        except RuntimeError:
            return
        geom = wkblib.loads(wkb, hex=True)
        if not geom.is_valid:
            geom = geom.buffer(0)
        if self._buffer_prepared.intersects(geom):
            self.records.append((typequ, geom))


def fetch_polygons_from_pbf(pbf_path: str, buffer_polygon) -> gpd.GeoDataFrame:
    mgr = osmium.area.AreaManager()
    with _ElapsedTimer("PBF first pass (collecting relations)"):
        osmium.apply(pbf_path, mgr.first_pass_handler())

    # Ways in a PBF only store node-id references, not coordinates -- the area assembler
    # needs a location index to resolve way geometry, or it silently builds nothing.
    node_index = osmium.index.create_map("flex_mem")
    location_handler = osmium.NodeLocationsForWays(node_index)
    location_handler.ignore_errors()

    collector = _AreaCollector(buffer_polygon)
    with _ElapsedTimer("PBF second pass (assembling areas)"):
        osmium.apply(pbf_path, location_handler, mgr.second_pass_handler(collector))

    print(f"[Polygons] matched {len(collector.records)} areas", flush=True)
    if not collector.records:
        return gpd.GeoDataFrame(columns=["TYPEQU", "geometry"], geometry="geometry", crs="EPSG:4326")
    typequs, geoms = zip(*collector.records)
    return gpd.GeoDataFrame({"TYPEQU": list(typequs)}, geometry=list(geoms), crs="EPSG:4326")


def write_polygons(new_polygons):
    new_polygons.to_file(POI_POLYGON_SHP, driver="ESRI Shapefile")
    print(f"Wrote {len(new_polygons)} green/water polygons to {POI_POLYGON_SHP}")


def main():
    polygon = load_buffer_polygon()
    # fountains = fetch_fountains(polygon)
    # append_points(fountains)
    # waterways already fetched and written in a prior run — skip to avoid redundant Overpass time
    # waterways = fetch_waterways(polygon)
    # write_lines(waterways)
    pbf_path = refresh_regional_extract()
    polygons = fetch_polygons_from_pbf(pbf_path, polygon)
    write_polygons(polygons)


if __name__ == "__main__":
    main()
