from tqdm import tqdm
from utils import graphml, route, decay, get_impedance
import math
import os
import pickle
import networkx as nx
import osmnx as ox
import time
import numpy as np
import re
import hashlib
import json

# Configurazione base per la cache su disco
CACHE_VERSION = 1
CACHE_FOLDER = "rra_cache"
os.makedirs(CACHE_FOLDER, exist_ok=True)
POI_GEOM_CACHE_FOLDER = "poi_geom_cache"
os.makedirs(POI_GEOM_CACHE_FOLDER, exist_ok=True)

# Variabili globali per mantenere i dati in memoria (RAM) ed evitare caricamenti ripetuti
_G_CACHE = None
_POI_GEOM_CACHE = {}  # chiave: (feature, value) -> list geom
_POI_POINTS_CACHE = {}  # chiave: (feature, value) -> list of Point geometries
_MODE_GRAPH_CACHE = {}  # chiave: network_type -> graph
_MODE_LENGTHS_CACHE = {}  # chiave: (origin_lat, origin_lon, network_type, radius_key) -> dict node->distance


def _entrance_point_for_geom(geom, search_radius_m=50, poi_name=None, poi_lat=None, poi_lon=None):
    if geom is None:
        raise ValueError(
            f"POI geometry is None; cannot infer point. name={poi_name} lat={poi_lat} lon={poi_lon}"
        )
    if geom.geom_type == "Point":
        return geom
    # Fast path: use centroid only
    try:
        return geom.centroid
    except Exception as e:
        raise ValueError(
            f"Cannot compute centroid ({geom.geom_type}): {e}. name={poi_name} lat={poi_lat} lon={poi_lon}"
        )


def _extract_geom_and_name(item):
    if isinstance(item, tuple) and len(item) == 2:
        return item[0], item[1]
    return item, None


def _poi_geom_cache_path(feature, value):
    # Build a stable filename for geometry cache on disk.
    key = f"{feature}|{value}"
    key_hash = hashlib.sha1(key.encode("utf-8")).hexdigest()
    safe_value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value))
    return os.path.join(POI_GEOM_CACHE_FOLDER, f"{feature}_{safe_value}_{key_hash}.pkl")


def _load_poi_geometries_from_disk(feature, value):
    path = _poi_geom_cache_path(feature, value)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _save_poi_geometries_to_disk(feature, value, geometries):
    path = _poi_geom_cache_path(feature, value)
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        pickle.dump(geometries, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp_path, path)


def _resolve_feature(poi_type, feature):
    # Determina la categoria corretta per OpenStreetMap (es. 'amenity' o 'healthcare')
    if feature is None:
        if poi_type in {"healthcare"}:
            return poi_type, True
        return "amenity", poi_type
    return feature, poi_type


def _tags_cache_key(tags):
    # Stable key for tag dicts (OSMnx-style), used in caches and filenames.
    tags_json = json.dumps(tags, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    key_hash = hashlib.sha1(tags_json.encode("utf-8")).hexdigest()
    return f"tags_{key_hash}"


def _resolve_query(poi_type, feature, tags):
    # Resolve query to a (feature, value) cache key and normalized tags.
    if tags:
        return "tags", _tags_cache_key(tags), tags
    feature, value = _resolve_feature(poi_type, feature)
    return feature, value, None


def _haversine_m(lat1, lon1, lat2, lon2):
    # Calcola la distanza in metri tra due coordinate geografiche
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

def _get_mode_graph(network_type):
    # Recupera il grafo stradale specifico per il mezzo di trasporto (piedi, bici, auto)
    # Se esiste già in cache lo restituisce, altrimenti lo calcola.
    if network_type not in _MODE_GRAPH_CACHE:
        _MODE_GRAPH_CACHE[network_type] = graphml.get_mode_graph(network_type)
    return _MODE_GRAPH_CACHE[network_type]

def precompute_distances(origin, radius_m=None):
    # Calcola le distanze stradali
    # (Dijkstra) per walk/bike/drive UNA volta sola per il nodo di origine.
    # I risultati vengono poi riutilizzati per tutti i tipi di POI (ristoranti, bar, ecc.)
    global _G_CACHE
    if _G_CACHE is None:
        _G_CACHE = graphml.get_graph()
    grafo = _G_CACHE
    
    network_cache = {}
    
    for mode in ["walk", "bike", "drive"]:
        try:
            mode_graph = _get_mode_graph(mode)
            origin_node = ox.distance.nearest_nodes(mode_graph, origin[1], origin[0])
            
            # Calcola la distanza da 'origin_node' verso TUTTI gli altri nodi del grafo
            lengths = nx.single_source_dijkstra_path_length(
                mode_graph, origin_node, weight="length"
            )
            network_cache[mode] = {"graph": mode_graph, "lengths": lengths}
        except Exception as e:
            # Se un grafo non è disponibile (es. errore di rete o grafo disconnesso), proseguiamo senza
            network_cache[mode] = {"graph": None, "lengths": {}}
            
    return network_cache

def preload_all_pois(poi_list):
    # Scarica preventivamente tutti i Punti di Interesse (POI) richiesti.
    # È fondamentale chiamarla nel main prima di avviare i processi paralleli
    # per evitare che ogni worker cerchi di scaricare dati simultaneamente.
    global _POI_GEOM_CACHE
    print("Pre-loading POI geometries...")
    for item in poi_list:
        if isinstance(item, str):
            poi_type = item
            feature = None
            tags = None
        else:
            poi_type = item.poi_type
            feature = getattr(item, "feature", None)
            tags = getattr(item, "tags", None)
        feature, value, tags = _resolve_query(poi_type, feature, tags)
        cache_key = (feature, value)
        if cache_key not in _POI_GEOM_CACHE:
            try:
                if tags:
                    print(f"  Fetching POI: {poi_type} (tags={tags})...")
                    poi = graphml.get_poi(tags=tags)
                else:
                    print(f"  Fetching POI: {poi_type} ({feature}={value})...")
                    poi = graphml.get_poi(feature, value)
                geometries = graphml.get_poi_geometries(poi)
                _POI_GEOM_CACHE[cache_key] = geometries
                _save_poi_geometries_to_disk(feature, value, geometries)
            except Exception as e:
                print(f"  Error fetching {poi_type}: {e}")
                raise
    print("POI pre-loading complete.")
    return _POI_GEOM_CACHE


def _get_mode_lengths(grafo, origin, network_type, radius_m):
    # cache delle distanze da origine per evitare Dijkstra ripetuti
    origin_key = (round(origin[0], 6), round(origin[1], 6))
    radius_key = "all" if radius_m is None else f"r{int(radius_m)}"
    key = (origin_key[0], origin_key[1], network_type, radius_key)

    if key in _MODE_LENGTHS_CACHE:
        # ritorna distanze già calcolate per questa origine/modo
        return _MODE_LENGTHS_CACHE[key]

    mode_graph = _get_mode_graph(network_type)
    origin_node = ox.distance.nearest_nodes(mode_graph, origin[1], origin[0])
    lengths = nx.single_source_dijkstra_path_length(
        mode_graph,
        origin_node,
        weight="length"
    )
    # salva distanze per riuso futuro
    _MODE_LENGTHS_CACHE[key] = lengths
    return lengths


def _cache_file_path(poi_type, origine, feature, radius_m):
    # genera il path del cache file RRA per questo POI/origine
    lat_key = f"{origine[0]:.6f}"
    lon_key = f"{origine[1]:.6f}"
    radius_key = "all" if radius_m is None else f"r{int(radius_m)}"
    return os.path.join(
        CACHE_FOLDER,
        f"RRA_v{CACHE_VERSION}_{feature}_{poi_type}_{radius_key}_{lat_key}_{lon_key}.pkl"
    )


def build_rra(decay_walk, decay_bike, decay_drive, decay_bus):
    # combina decay per POI in RRA
    RRA = []
    for i in range(len(decay_walk)):
        dw = decay_walk[i]
        db = decay_bike[i]
        dd = decay_drive[i]
        d_bus = decay_bus[i]
        if None not in (dw, db, dd, d_bus):
            rra_poi = decay.calculate_rra(dw, db, dd, d_bus)
            RRA.append(rra_poi)
    return RRA


def accessibility_from_rra(RRA):
    # calcola il valore di accessibilità da una lista di RRA
    RRA_desc = sorted(RRA, reverse=True)
    
    def c_from_target(n_target: int, y_target: float) -> float:
        return round(math.log(1.0 - y_target) / n_target, 2)
    def g(x: int, c: float) -> float:
        return 1.0 - math.exp(c * x)
    def deltag(x: int, x2: int, c: float) -> float:
        return g(x, c) - g(x2, c)

    accessibility_value = 0
    c = c_from_target(n_target=2, y_target=0.6)
    
    for i, element in enumerate(RRA_desc):
        if i == 0: accessibility_value += (element * g(i + 1, c))
        else: accessibility_value += (element * deltag(i + 1, i, c))

    return accessibility_value


def accessibility_non_bus(poi_type, origine, feature=None, radius_m=None, tags=None):
    feature, value, tags = _resolve_query(poi_type, feature, tags)
    cache_file = _cache_file_path(poi_type, origine, feature, radius_m)

    if os.path.exists(cache_file):
        # cache hit: evita ogni calcolo e ritorna il valore
        RRA = load_rra(cache_file)
        return {
            "cache_hit": True,
            "cache_file": cache_file,
            "accessibility_value": accessibility_from_rra(RRA),
        }

    global _G_CACHE, _POI_GEOM_CACHE, _POI_POINTS_CACHE

    if _G_CACHE is None:
        # carica il grafo base una sola volta
        _G_CACHE = graphml.get_graph()
    grafo = _G_CACHE

    cache_key = (feature, value)
    if cache_key not in _POI_GEOM_CACHE:
        # try disk cache before hitting OSM / geopandas
        cached_geom = _load_poi_geometries_from_disk(feature, value)
        if cached_geom is not None:
            _POI_GEOM_CACHE[cache_key] = cached_geom
        else:
            # carica geometrie POI una sola volta per tipo
            if tags:
                poi = graphml.get_poi(tags=tags)
            else:
                poi = graphml.get_poi(feature, value)
            geometries = graphml.get_poi_geometries(poi)
            _POI_GEOM_CACHE[cache_key] = geometries
            _save_poi_geometries_to_disk(feature, value, geometries)

    poi_geometry = _POI_GEOM_CACHE[cache_key]

    points_cache_key = (feature, value)
    if radius_m is None and points_cache_key in _POI_POINTS_CACHE:
        poi_points = _POI_POINTS_CACHE[points_cache_key]
    else:
        poi_points = []
        for item in poi_geometry:
            geom, poi_name = _extract_geom_and_name(item)
            if geom is None:
                continue
            poi_lat = None
            poi_lon = None
            try:
                if geom.geom_type == "Point":
                    poi_lat = geom.y
                    poi_lon = geom.x
                else:
                    c = geom.centroid
                    poi_lat = c.y
                    poi_lon = c.x
            except Exception:
                pass
            geom = _entrance_point_for_geom(geom, poi_name=poi_name, poi_lat=poi_lat, poi_lon=poi_lon)
            poi_points.append(geom)
        if radius_m is None:
            _POI_POINTS_CACHE[points_cache_key] = poi_points

    if radius_m is not None:
        # filtra POI per raggio per ridurre il lavoro
        poi_points = [
            geom for geom in poi_points
            if _haversine_m(origine[0], origine[1], geom.y, geom.x) <= radius_m
        ]

    beta = math.log(2) / 20.0  # parametro di decay

    xs = [geom.x for geom in poi_points]
    ys = [geom.y for geom in poi_points]

    mode_distances = {}
    for mode in ["walk", "bike", "drive"]:
        # trova nodi POI e distanze shortest-path cached per ogni modo
        mode_graph = _get_mode_graph(mode)
        poi_nodes = ox.distance.nearest_nodes(mode_graph, xs, ys)
        try:
            poi_nodes = list(poi_nodes)
        except TypeError:
            poi_nodes = [poi_nodes]

        lengths = _get_mode_lengths(grafo, origine, mode, radius_m)
        mode_distances[mode] = [lengths.get(node) for node in poi_nodes]

    decay_walk = []
    decay_bike = []
    decay_drive = []

    for i, geom in enumerate(poi_points):
        # calcola decay walk/bike/drive per ciascun POI
        decay_walk_val = decay_bike_val = decay_drive_val = None
        for mode in ["walk", "bike", "drive"]:
            dist_m = mode_distances[mode][i]
            if dist_m is None:
                continue
            imp = get_impedance.impedance_base(dist_m / 1000.0, mode)
            d = decay.distance_decay(beta, imp)
            if mode == "walk":
                decay_walk_val = d
            elif mode == "bike":
                decay_bike_val = d
            elif mode == "drive":
                decay_drive_val = d

        decay_walk.append(decay_walk_val)
        decay_bike.append(decay_bike_val)
        decay_drive.append(decay_drive_val)

    return {
        "cache_hit": False,
        "cache_file": cache_file,
        "poi_points": poi_points,
        "beta": beta,
        "decay_walk": decay_walk,
        "decay_bike": decay_bike,
        "decay_drive": decay_drive,
    }


def get_poi_points(poi_type, origine, feature=None, radius_m=None, tags=None, progress=None):
    feature, value, tags = _resolve_query(poi_type, feature, tags)

    global _POI_GEOM_CACHE, _POI_POINTS_CACHE
    cache_key = (feature, value)
    if cache_key not in _POI_GEOM_CACHE:
        cached_geom = _load_poi_geometries_from_disk(feature, value)
        if cached_geom is not None:
            _POI_GEOM_CACHE[cache_key] = cached_geom
        else:
            if tags:
                poi = graphml.get_poi(tags=tags)
            else:
                poi = graphml.get_poi(feature, value)
            geometries = graphml.get_poi_geometries(poi)
            _POI_GEOM_CACHE[cache_key] = geometries
            _save_poi_geometries_to_disk(feature, value, geometries)

    poi_geometry = _POI_GEOM_CACHE[cache_key]

    points_cache_key = (feature, value)
    if radius_m is None and points_cache_key in _POI_POINTS_CACHE:
        poi_points = _POI_POINTS_CACHE[points_cache_key]
    else:
        poi_points = []
        for item in poi_geometry:
            geom, poi_name = _extract_geom_and_name(item)
            if geom is None:
                continue
            poi_lat = None
            poi_lon = None
            try:
                if geom.geom_type == "Point":
                    poi_lat = geom.y
                    poi_lon = geom.x
                else:
                    c = geom.centroid
                    poi_lat = c.y
                    poi_lon = c.x
            except Exception:
                pass
            geom = _entrance_point_for_geom(geom, poi_name=poi_name, poi_lat=poi_lat, poi_lon=poi_lon)
            poi_points.append(geom)
            if progress is not None:
                progress.update(1)
        if radius_m is None:
            _POI_POINTS_CACHE[points_cache_key] = poi_points

    if radius_m is not None:
        poi_points = [
            geom for geom in poi_points
            if _haversine_m(origine[0], origine[1], geom.y, geom.x) <= radius_m
        ]

    return poi_points


def accessibility_bus(origine, poi_points, beta, grafo):
    decay_bus = []
    for geom in poi_points:
        destinazione = (geom.y, geom.x)
        try:
            _, _, imp_bus = route.get_route(
                grafo,
                "bus",
                origine,
                destinazione,
                impedance_flag=True,
                ax=None,
                distance_only=True
            )
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            imp_bus = None
        except Exception:
            imp_bus = None

        if imp_bus:
            decay_bus.append(decay.distance_decay(beta, imp_bus))
        else:
            decay_bus.append(0.0)
    return decay_bus


def merge_rra_and_accessibility(decay_walk, decay_bike, decay_drive, decay_bus):
    RRA = build_rra(decay_walk, decay_bike, decay_drive, decay_bus)
    return RRA, accessibility_from_rra(RRA)


def accessibility(poi_type, origine, feature=None, radius_m=None, tags=None):
    feature, value, tags = _resolve_query(poi_type, feature, tags)
    cache_file = _cache_file_path(poi_type, origine, feature, radius_m)

    if os.path.exists(cache_file):
        RRA = load_rra(cache_file)
        return accessibility_from_rra(RRA)

    data = accessibility_non_bus(poi_type, origine, feature, radius_m, tags)
    poi_points = data["poi_points"]
    beta = data["beta"]
    decay_walk = data["decay_walk"]
    decay_bike = data["decay_bike"]
    decay_drive = data["decay_drive"]
    if _G_CACHE is None:
        _G_CACHE = graphml.get_graph()
    grafo = _G_CACHE

    decay_bus = accessibility_bus(origine, poi_points, beta, grafo)
    RRA, accessibility_value = merge_rra_and_accessibility(
        decay_walk, decay_bike, decay_drive, decay_bus
    )
    save_rra(cache_file, RRA)
    return accessibility_value


def save_rra(path, RRA):
    # Salva i dati usando un file temporaneo per evitare corruzione in caso di crash
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        pickle.dump(RRA, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp_path, path)

def load_rra(path):
    with open(path, "rb") as f:
        return pickle.load(f)
