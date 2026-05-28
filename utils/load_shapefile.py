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
    # Determine the full path to the shapefile
    if os.path.exists(shp_name):
        shp_path = shp_name
    else:
        shp_path = f"shapefile_base/{shp_name}"
    
    gdf = gpd.read_file(shp_path)

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

    # Determine the full path to the shapefile
    if os.path.exists(shp_name):
        shp_path = shp_name
    else:
        shp_path = f"shapefile_base/{shp_name}"
    
    gdf = gpd.read_file(shp_path)

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

    # Load POI shapefiles from Paris folder
    paths = [
        "Paris/POI_point.shp",
        "Paris/POI_line.shp",
        "Paris/POI_polygon.shp",
    ]

    frames = []
    for path in paths:
        if not os.path.exists(path):
            print(f"[POI] Warning: {path} not found, skipping", flush=True)
            continue

        gdf = gpd.read_file(path)

        if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)

        # Rename TYPEQU to poi_type for consistency
        if 'TYPEQU' in gdf.columns:
            gdf['poi_type'] = gdf['TYPEQU'].astype(str)
        
        frames.append(gdf)

    if not frames:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    pois = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True),
        crs=frames[0].crs,
    )

    if "poi_type" not in pois.columns:
        return gpd.GeoDataFrame(geometry=[], crs=pois.crs)

    # For Paris TYPEQU-based POIs, skip the strict label validation
    # TYPEQU values (A101, BI07, etc.) are different from OSM-based labels
    shp_poi_types = set(
        pois["poi_type"]
        .dropna()
        .astype(str)
        .unique()
    )
    
    # Check if we're using TYPEQU codes (they typically start with A, B, C, D, E, F, G, BI, GI)
    # rather than OSM-style labels (like amenity_pharmacy)
    is_typequ_based = any(
        str(ptype).strip()[0] in ['A', 'B', 'C', 'D', 'E', 'F', 'G'] or 
        str(ptype).strip().startswith(('BI', 'GI'))
        for ptype in shp_poi_types if str(ptype).strip()
    )
    
    if not is_typequ_based:
        # Original validation for OSM-based labels
        labels_by_poi_type: dict[str, set[str]] = {}
        configured_labels = set()
        for q in serv.unique_query_keys():
            labels = {str(label).strip() for label in q.labels if str(label).strip()}
            labels_by_poi_type[str(q.poi_type)] = labels
            configured_labels.update(labels)

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
    else:
        # For TYPEQU-based POIs, return all POIs (filtering will be handled by service configuration)
        print(f"[POI] Loaded {len(pois)} POIs from Paris shapefile with TYPEQU codes: {sorted(list(shp_poi_types))[:10]}...", flush=True)
        return pois.copy()
