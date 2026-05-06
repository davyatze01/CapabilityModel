from typing import cast
import geopandas as gpd
import osmnx as ox

from shapely.geometry import Polygon, MultiPolygon
from shapely.geometry.base import BaseGeometry


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
