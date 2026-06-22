from typing import cast
import geopandas as gpd
import pandas as pd
import osmnx as ox
import os

from shapely.geometry import Polygon, MultiPolygon
from shapely.geometry.base import BaseGeometry
from config import PipelineConfig


_UNUSED_POI_TYPES_WARNED: set[tuple[str, tuple[str, ...]]] = set()


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

    gdf = gpd.read_file(f"shapefile_base/{shp_name}")

    if gdf.empty:
        raise ValueError("Shapefile is empty")
    
    if gdf.crs is None:
        raise ValueError("Missing CRS")
    
    if gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    geometry: BaseGeometry = gdf.unary_union

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

    frames = []
    for path in paths:
        if not os.path.exists(path):
            continue

        gdf = gpd.read_file(path)

        if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)

        frames.append(gdf)

    if not frames:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    pois = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True),
        crs=frames[0].crs,
    )

    # Support both poi_type (new) and poiType (legacy) column names
    if "poi_type" not in pois.columns and "poiType" in pois.columns:
        pois = pois.rename(columns={"poiType": "poi_type"})
    if "poi_type" not in pois.columns and "TYPEQU" in pois.columns:
        pois = pois.rename(columns={"TYPEQU": "poi_type"})

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
