from typing import cast
import geopandas as gpd
import pandas as pd
import osmnx as ox
import os

from shapely.geometry import Polygon, MultiPolygon
from shapely.geometry.base import BaseGeometry
from config import PipelineConfig


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

    else:
        raise TypeError(f"Unsupported geometry type: {type(geometry)}")

    G = ox.graph_from_polygon(
        polygon,
        network_type=network_type
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

    geometry: BaseGeometry = gdf.union_all()

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

    paths = [
        "pois_shp/poi_points.shp",
        "pois_shp/poi_lines.shp",
        "pois_shp/poi_polygons.shp",
    ]

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
    if missing_poi_types:
        raise ValueError(
            "poi_type values found in shapefiles but missing from configured labels in config/poi_types.csv: "
            + ", ".join(sorted(missing_poi_types))
        )

    if poi_type is None:
        return pois.copy()

    query_poi_types = labels_by_poi_type.get(str(poi_type), set())
    if not query_poi_types:
        return gpd.GeoDataFrame(geometry=[], crs=pois.crs)

    mask = pois["poi_type"].astype(str).isin(query_poi_types)
    return pois.loc[mask].copy()
