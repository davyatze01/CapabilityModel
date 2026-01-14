import osmnx as ox
import os
import geopandas as gpd
import pandas as pd


def get_graph():

    # Nomi file
    PLACE_NAME = "Cagliari, Sardinia, Italy"
    NAME_FILE = "graph/cagliari.graphml"

    # Se esiste lo carico altrimenti creo e salvo

    graph = None

    try:
        graph = ox.io.load_graphml(NAME_FILE)
        print(f"Grafo caricato correttamente da: {NAME_FILE}")
    except Exception as e:
        print("Grafo non presente, scarico il grafo di Cagliari..")
        graph = ox.graph_from_place(PLACE_NAME)
        ox.io.save_graphml(graph,filepath = NAME_FILE)
        print("Grafo scaricato e salvato correttamente!")

    return graph

def get_poi(feature=None,value=None):

    # Nomi file
    PLACE_NAME = "Cagliari, Sardinia, Italy"

    os.makedirs("poi", exist_ok=True)

    if feature is None or value is None:
        poi_files = [
            os.path.join("poi", name)
            for name in os.listdir("poi")
            if name.lower().endswith(".geojson")
        ]

        if poi_files:
            frames = []
            for path in poi_files:
                try:
                    frames.append(gpd.read_file(path))
                except Exception:
                    continue

            if frames:
                poi = gpd.GeoDataFrame(
                    pd.concat(frames, ignore_index=True),
                    crs=frames[0].crs
                )
                print(f"POI caricati correttamente da {len(frames)} file in poi/")
                return poi

        # Fallback: scarica tutti gli amenity se non ci sono file locali
        feature = "amenity"
        value = True

    NAME_FILE = f"poi/{feature}_{value}.geojson"

    try:
        # Provo a caricare i POI gia salvati
        poi = gpd.read_file(NAME_FILE)
        print(f"POI caricati correttamente da: {NAME_FILE}")
    except Exception:
        # Scarico POI da OSM
        print("Scarico POI da OSM...")

        poi = ox.features_from_place(
            PLACE_NAME,
            {feature: value}
        )

        # Salvo tutti i POI
        poi.to_file(NAME_FILE, driver="GeoJSON")
        print("POI scaricati e salvati correttamente!")

    return poi

def print_poi(poi, print_start, print_end):
    if print_start == None or print_start < 0:
        print_start = 0
    
    if print_end == None or print_end > len(poi):
        print_end = len(poi)

    subset = poi.iloc[print_start:print_end]

    for i, row in subset.iterrows():
        print(f"{i+1}: {row.get('name', 'Senza nome')}")


def get_poi_names(poi):
    return [
        name for name in poi["name"].dropna().astype(str).values
    ]

def get_poi_geom(poi):
    return [
        geometry for geometry in poi["geometry"].dropna().astype(str).values
    ]

def get_poi_geometries(poi):
    return [
        geometry for geometry in poi["geometry"].dropna().values
    ]

def get_poi_amenity_types(poi):
    if "amenity" not in poi.columns:
        return []
    return sorted(poi["amenity"].dropna().astype(str).unique().tolist())

from shapely import wkt

def geocode_from_geometry_str(geometry_str):
    """
    Converte una geometry WKT (stringa) in (lat, lon)
    """
    geom = wkt.loads(geometry_str)

    if geom.geom_type != "Point":
        raise ValueError(f"Geometry type non supportato: {geom.geom_type}")

    lon, lat = geom.x, geom.y
    return lat, lon

