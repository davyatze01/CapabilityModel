from typing import cast
import geopandas as gpd
import pandas as pd
import osmnx as ox
import os

from shapely.geometry import Polygon, MultiPolygon
from shapely.geometry.base import BaseGeometry
from core.config import PipelineConfig


_UNUSED_POI_TYPES_WARNED: set[tuple[str, tuple[str, ...]]] = set()

# Module-level cache: keyed by the tuple of shapefile paths so the cache is
# invalidated automatically if cfg.poi_shapefile_paths changes between runs.
_POI_SHP_CACHE: dict[tuple[str, ...], gpd.GeoDataFrame] = {}

# Boundary shapefile + its precomputed union polygon (read/computed once per shp_name).
_BOUNDARY_SHP_CACHE: dict[str, gpd.GeoDataFrame] = {}
_BOUNDARY_UNION_CACHE: dict[str, BaseGeometry] = {}


def graph_from_shapefile(
    shp_name: str,
    network_type: str = "drive"
):
    gdf = gpd.read_file(f"shapefile_base/{shp_name}")

    if gdf.empty:
        raise ValueError("Shapefile is empty")

    if gdf.crs is None:
        raise ValueError("Missing CRS")

    # Conversione a WGS84 richiesta da osmnx
    if gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    geometry: BaseGeometry = gdf.unary_union


    if isinstance(geometry, (Polygon, MultiPolygon)):
        polygon = cast(Polygon | MultiPolygon, geometry)
        # Fix occasional invalid rings/self-intersections before OSMnx usage.
        if not polygon.is_valid:
            polygon = cast(Polygon | MultiPolygon, polygon.buffer(0))

    else:
        raise TypeError(f"Unsupported geometry type: {type(geometry)}")

    G = ox.graph_from_polygon(
        polygon,
        network_type=network_type,
        simplify=False,
        retain_all=True,
    )

    return shp_name,G


def feature_from_shapefile(shp_name : str, query_tags: dict, poi_type: str | None = None):
    cfg = PipelineConfig()

    if shp_name not in _BOUNDARY_SHP_CACHE:
        raw = gpd.read_file(f"shapefile_base/{shp_name}")
        if raw.empty:
            raise ValueError("Shapefile is empty")
        if raw.crs is None:
            raise ValueError("Missing CRS")
        if raw.crs.to_epsg() != 4326:
            raw = raw.to_crs(epsg=4326)
        _BOUNDARY_SHP_CACHE[shp_name] = raw

    if shp_name not in _BOUNDARY_UNION_CACHE:
        _BOUNDARY_UNION_CACHE[shp_name] = _BOUNDARY_SHP_CACHE[shp_name].unary_union

    geometry: BaseGeometry = _BOUNDARY_UNION_CACHE[shp_name]

    if not isinstance(geometry, (Polygon, MultiPolygon)):
        raise TypeError(f"Unsupported geometry type: {type(geometry)}")
    
    if cfg.poi_from_shp:
        # Caricamento poi da shapefile
        return poi_from_shp(poi_type=poi_type)
    else:
        print(f"[POI] OSMnx city-universe download: keys={len(query_tags)}")
        return ox.features_from_polygon(geometry, query_tags)
    

def poi_from_shp(poi_type: str | None = None):
    from utils import services as serv
    cfg = PipelineConfig()
    paths = list(cfg.poi_shapefile_paths)
    cache_key = tuple(paths)

    if cache_key not in _POI_SHP_CACHE:
        frames = []
        for path in paths:
            if not os.path.exists(path):
                continue
            gdf = gpd.read_file(path)
            if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
                gdf = gdf.to_crs(epsg=4326)
            frames.append(gdf)

        if not frames:
            _POI_SHP_CACHE[cache_key] = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        else:
            merged = gpd.GeoDataFrame(
                pd.concat(frames, ignore_index=True),
                crs=frames[0].crs,
            )
            # Normalise poi_type column name once on load.
            if "poi_type" not in merged.columns and cfg.poi_label_field in merged.columns:
                merged = merged.rename(columns={cfg.poi_label_field: "poi_type"})

            # Shapefiles (esp. Paris' MGP layers) carry many attribute columns that
            # nothing downstream reads — graphml.get_poi_geometries only pulls
            # SOURCE_KEY_COLUMNS + geometry, and poi_exports._compose_address reads the
            # same address-ish subset of them. This cache is held for the entire process
            # lifetime and gets .copy()'d in full for every poi_type query below, so on a
            # 290k+ POI city (Paris) the unused columns are pure multiplied-up memory
            # cost. Drop them once, right after load, instead of paying for them on
            # every subsequent filter/copy.
            from utils.poi_identity import SOURCE_KEY_COLUMNS
            keep_cols = ["geometry", "poi_type"] + [
                c for c in SOURCE_KEY_COLUMNS if c in merged.columns
            ]
            merged = merged[[c for c in keep_cols if c in merged.columns]].copy()
            _POI_SHP_CACHE[cache_key] = merged

    pois = _POI_SHP_CACHE[cache_key]
    if pois.empty:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    if "poi_type" not in pois.columns:
        return gpd.GeoDataFrame(geometry=[], crs=pois.crs)

    labels_by_poi_type: dict[str, set[str]] = {}
    configured_labels = set()
    for q in serv.unique_query_keys():
        labels = {str(label).strip() for label in q.labels if str(label).strip()}
        labels_by_poi_type[str(q.poi_type)] = labels
        configured_labels.update(labels)

    shp_poi_types = set(
        pois["poi_type"]
        .dropna()
        .astype(str)
        .unique()
    )

    missing_poi_types = shp_poi_types - configured_labels
    missing_poi_types.discard("therapeutic_wellness")
    if missing_poi_types:
        missing_key = (str(cfg.name_shapefile), tuple(sorted(missing_poi_types)))
        if missing_key not in _UNUSED_POI_TYPES_WARNED:
            _UNUSED_POI_TYPES_WARNED.add(missing_key)
            print(
                "[POI] The following poi_types were found in the shapefile but are currently unused "
                f"for config/poi_types.csv: {', '.join(sorted(missing_poi_types))}",
                flush=True,
            )

    if poi_type is None:
        return pois.copy()

    query_poi_types = labels_by_poi_type.get(str(poi_type), set())
    if not query_poi_types:
        if str(poi_type) != "therapeutic_wellness":
            print(
                f"[POI] No shapefile labels configured for poi_type='{poi_type}'. Returning empty result.",
                flush=True,
            )
        return gpd.GeoDataFrame(geometry=[], crs=pois.crs)

    mask = pois["poi_type"].astype(str).isin(query_poi_types)
    out = pois.loc[mask].copy()
    if out.empty:
        print(
            f"[POI] No shapefile POIs matched poi_type='{poi_type}' "
            f"labels={sorted(query_poi_types)}. Returning empty result.",
            flush=True,
        )
    return out


def clear_poi_shp_cache() -> None:
    """Release the cached merged POI shapefile(s) and boundary geometry.

    Callers that only need one pass over the shapefile POIs (e.g. poi_exports'
    _collect_poi_records, which extracts everything it needs into plain dicts) should
    call this once done. On a large shapefile source (Paris' MGP layers, 290k+ POIs)
    this cache is otherwise held for the rest of the process's lifetime, adding up on
    top of whatever the caller itself is holding.
    """
    _POI_SHP_CACHE.clear()
    _BOUNDARY_SHP_CACHE.clear()
    _BOUNDARY_UNION_CACHE.clear()
