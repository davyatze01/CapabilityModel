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
        raise ValueError("Shapefile vuoto")

    if gdf.crs is None:
        raise ValueError("CRS mancante")

    # Conversione a WGS84 richiesta da osmnx
    if gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    geometry: BaseGeometry = gdf.union_all()


    if isinstance(geometry, (Polygon, MultiPolygon)):

        polygon = cast(Polygon | MultiPolygon, geometry)

    else:
        raise TypeError(
            f"Geometria non supportata: {type(geometry)}"
        )

    G = ox.graph_from_polygon(
        polygon,
        network_type=network_type
    )

    return shp_name,G


def feature_from_shapefile(shp_name : str, query_tags: dict):
    cfg = PipelineConfig()

    gdf = gpd.read_file(f"shapefile_base/{shp_name}")

    if gdf.empty:
        raise ValueError("Shapefile vuoto")
    
    if gdf.crs is None:
        raise ValueError("CRS mancante")
    
    if gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    geometry: BaseGeometry = gdf.union_all()

    if not isinstance(geometry, (Polygon, MultiPolygon)):
        raise TypeError(f"Geometria non suppertata: {type(geometry)}")
    
    if cfg.poi_from_shp:
        # Caricamento poi da shapefile
        return poi_from_shp(query_tags)
    else:
        return ox.features_from_polygon(geometry, query_tags)
    

def poi_from_shp(query_tags: dict):
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

    mask = pd.Series(True, index=pois.index)

    for key, value in query_tags.items():
        if key not in pois.columns:
            return gpd.GeoDataFrame(geometry=[], crs=pois.crs)

        if value is True:
            mask = mask & pois[key].notna()
        elif isinstance(value, list):
            mask = mask & pois[key].astype(str).isin([str(v) for v in value])
        else:
            mask = mask & (pois[key].astype(str) == str(value))

    return pois.loc[mask].copy()

