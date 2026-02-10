import osmnx as ox
import os
import geopandas as gpd
import pandas as pd
import hashlib
import json

# In-memory caches to avoid repeated disk loads
_GRAPH_CACHE = None
_MODE_GRAPH_CACHE = {}


def get_graph():

    # Nomi file
    PLACE_NAME = "Cagliari, Sardinia, Italy"
    NAME_FILE = "graph/cagliari.graphml"

    # Se esiste lo carico altrimenti creo e salvo

    global _GRAPH_CACHE
    if _GRAPH_CACHE is not None:
        return _GRAPH_CACHE

    graph = None
    try:
        graph = ox.io.load_graphml(NAME_FILE)
    except Exception:
        print("Grafo non presente, scarico il grafo di Cagliari..")
        graph = ox.graph_from_place(PLACE_NAME)
        ox.io.save_graphml(graph,filepath = NAME_FILE)
        print("Grafo scaricato e salvato correttamente!")

    _GRAPH_CACHE = graph
    return graph


def get_mode_graph(network_type):
    # Cache per-mode graphs on disk to avoid repeated Overpass downloads.
    PLACE_NAME = "Cagliari, Sardinia, Italy"
    NAME_FILE = f"graph/cagliari_{network_type}.graphml"

    if network_type in _MODE_GRAPH_CACHE:
        return _MODE_GRAPH_CACHE[network_type]

    graph = None
    try:
        graph = ox.io.load_graphml(NAME_FILE)
    except Exception:
        print(f"Grafo {network_type} non presente, scarico da OSM..")
        graph = ox.graph_from_place(PLACE_NAME, network_type=network_type)
        ox.io.save_graphml(graph, filepath=NAME_FILE)
        print("Grafo scaricato e salvato correttamente!")

    _MODE_GRAPH_CACHE[network_type] = graph
    return graph

def _tags_file_name(tags):
    tags_json = json.dumps(tags, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    key_hash = hashlib.sha1(tags_json.encode("utf-8")).hexdigest()
    return f"tags_{key_hash}.geojson"


def get_poi(feature=None, value=None, tags=None):

    # Nomi file
    PLACE_NAME = "Cagliari, Sardinia, Italy"

    os.makedirs("poi", exist_ok=True)

    if (feature is None or value is None) and not tags:
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
                return poi

        # Fallback: scarica tutti gli amenity se non ci sono file locali
        feature = "amenity"
        value = True

    if tags:
        NAME_FILE = os.path.join("poi", _tags_file_name(tags))
    else:
        NAME_FILE = f"poi/{feature}_{value}.geojson"

    try:
        # Provo a caricare i POI gia salvati
        poi = gpd.read_file(NAME_FILE)
    except Exception:
        # Scarico POI da OSM
        print("Scarico POI da OSM...")

        try:
            if tags:
                poi = ox.features_from_place(
                    PLACE_NAME,
                    tags
                )
            else:
                poi = ox.features_from_place(
                    PLACE_NAME,
                    {feature: value}
                )
        except Exception as e:
            # Nessun POI disponibile per questa query: ritorna GDF vuoto
            print(f"Nessun POI trovato per {feature}={value} tags={tags}: {e}")
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

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
    out = []
    if "geometry" not in poi.columns:
        return out
    names = poi["name"] if "name" in poi.columns else None
    for idx, geometry in poi["geometry"].dropna().items():
        name = None
        if names is not None:
            try:
                name = names.loc[idx]
            except Exception:
                name = None
        out.append((geometry, name))
    return out

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

