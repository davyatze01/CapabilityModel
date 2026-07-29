"""Fetch and cache OSM place labels (comuni + quartieri) for the study area.

Produces a point layer of named places to overlay on the capability maps, the
way Google Maps labels an area: surrounding municipalities/towns (comuni) in
normal case, and neighbourhoods/quarters (quartieri) in UPPERCASE. Two OSM
concepts are pulled from the `place=*` tag:

  - comuni:      place in {city, town, village, hamlet, municipality}
  - quartieri:   place in {suburb, neighbourhood, quarter, borough, city_block}

Everything is reduced to a single representative point per place (polygon
boundaries in OSM are collapsed with representative_point()), and the display
text is baked into a `label` column so a bare drag-in of the GeoPackage renders
the right casing without needing the QGIS project's styling.
"""
from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import osmnx as ox

# OSM place values grouped by how we want to label them. "comune" covers the
# surrounding municipalities/towns; "quartiere" covers named neighbourhoods
# within a city. Ordered from most to least significant within each group so a
# renderer can size them by rank if desired.
COMUNE_PLACES = ["city", "town", "village", "hamlet", "municipality", "isolated_dwelling"]
QUARTIERE_PLACES = ["suburb", "neighbourhood", "quarter", "borough", "city_block"]

PLACE_TAGS: dict[str, object] = {"place": COMUNE_PLACES + QUARTIERE_PLACES}

# Overpass can hang far longer than anything else in this pipeline is willing to
# wait for; fail fast and let the caller fall back to "no labels" rather than
# stall the whole run on a slow/unreachable Overpass server.
_REQUEST_TIMEOUT_S = 60


def _place_kind(place_value: str) -> str | None:
    """Map a raw OSM place=* value to 'comune', 'quartiere', or None (ignore)."""
    value = str(place_value).strip().lower()
    if value in COMUNE_PLACES:
        return "comune"
    if value in QUARTIERE_PLACES:
        return "quartiere"
    return None


def _build_labels_gdf(raw_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Reduce a raw OSM features GeoDataFrame to named place points with labels."""
    if raw_gdf.empty or "place" not in raw_gdf.columns or "name" not in raw_gdf.columns:
        return gpd.GeoDataFrame(
            {"name": [], "place": [], "place_kind": [], "label": []},
            geometry=[],
            crs="EPSG:4326",
        )

    frame = raw_gdf[["name", "place", "geometry"]].copy()
    frame = frame[frame["name"].notna() & frame["geometry"].notna()]
    frame["place_kind"] = frame["place"].map(_place_kind)
    frame = frame[frame["place_kind"].notna()]
    if frame.empty:
        return gpd.GeoDataFrame(
            {"name": [], "place": [], "place_kind": [], "label": []},
            geometry=[],
            crs="EPSG:4326",
        )

    # Collapse every feature (OSM stores some places as boundary polygons) to a
    # single interior point so the layer is uniformly point-labellable.
    frame["geometry"] = frame.geometry.representative_point()

    # Bake the display casing into the data: quartieri all-caps, comuni left in
    # their normal stored case (Google-Maps convention). The QGIS labeling reads
    # this column directly, so a plain drag-in shows the right text too.
    def _label(row: "gpd.GeoSeries") -> str:
        name = str(row["name"]).strip()
        return name.upper() if row["place_kind"] == "quartiere" else name

    frame["label"] = frame.apply(_label, axis=1)
    frame["place"] = frame["place"].astype(str)

    frame = frame.drop_duplicates(subset=["label", "place_kind"])
    return gpd.GeoDataFrame(
        frame[["name", "place", "place_kind", "label", "geometry"]],
        geometry="geometry",
        crs="EPSG:4326",
    )


def get_place_labels(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    cache_path: str | Path,
) -> gpd.GeoDataFrame | None:
    """Return a point GeoDataFrame of comuni/quartieri labels for the bbox, or None.

    Reads/writes a per-city GeoPackage cache at `cache_path` so repeat runs don't
    re-hit Overpass. Returns None (rather than raising) if no cache exists and the
    Overpass fetch fails or times out, so the caller can skip the labels layer
    instead of blocking the pipeline. An empty (but successful) result is cached
    and returned as an empty GeoDataFrame.
    """
    cache_path = Path(cache_path)

    if cache_path.exists():
        try:
            cached = gpd.read_file(cache_path)
            return cached
        except Exception as exc:
            print(f"[Places] Failed to read cached place labels {cache_path}: {exc}", flush=True)

    prev_timeout = ox.settings.requests_timeout
    try:
        ox.settings.requests_timeout = _REQUEST_TIMEOUT_S
        raw_gdf = ox.features_from_bbox(bbox=(max_lat, min_lat, max_lon, min_lon), tags=PLACE_TAGS)
    except Exception as exc:
        print(f"[Places] OSM place-label fetch failed ({exc}); skipping labels layer.", flush=True)
        return None
    finally:
        ox.settings.requests_timeout = prev_timeout

    labels_gdf = _build_labels_gdf(raw_gdf)

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        labels_gdf.to_file(cache_path, driver="GPKG")
    except Exception as exc:
        print(f"[Places] Failed to cache place labels to {cache_path}: {exc}", flush=True)

    return labels_gdf
