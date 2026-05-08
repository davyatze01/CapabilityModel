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
        print(f"[POI] OSMnx city-universe download: keys={len(query_tags)}")
        return ox.features_from_polygon(geometry, query_tags)
    

def poi_from_shp(query_tags: dict):
    import csv
    import json
    from pathlib import Path

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

    poi_types = set()
    config_csv_path = Path(__file__).resolve().parents[1] / "config" / "poi_types.csv"

    with config_csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            poi_type = (row.get("poi_type") or "").strip()
            tags_raw = row.get("tags") or ""

            if not poi_type or not tags_raw:
                continue

            tags = json.loads(tags_raw)
            matches = True
            for key, value in tags.items():
                if key not in query_tags:
                    matches = False
                    break

                query_value = query_tags[key]
                if query_value is True:
                    continue
                if isinstance(query_value, list):
                    if str(value) not in {str(v) for v in query_value}:
                        matches = False
                        break
                elif str(query_value) != str(value):
                    matches = False
                    break

            if matches:
                poi_types.add(poi_type)

    if not poi_types:
        return gpd.GeoDataFrame(geometry=[], crs=pois.crs)

    mask = pois["poi_type"].astype(str).isin(poi_types)
    return pois.loc[mask].copy()
