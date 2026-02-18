from tqdm import tqdm
from utils import graphml, route, decay, get_impedance
import math
import os
import pickle
from typing import Hashable, TypeGuard, cast
import networkx as nx
import osmnx as ox
import time
import numpy as np
import re
import hashlib
import json
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

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


def _normalize_node_id(node):
    # Convert numpy scalars returned by nearest_nodes to plain hashable ids.
    try:
        return node.item()
    except Exception:
        return node


def _entrance_point_for_geom(geom, snap_graph=None, search_radius_m=50, poi_name=None, poi_lat=None, poi_lon=None) -> Point:
    if geom is None:
        raise ValueError(
            f"POI geometry is None; cannot infer point. name={poi_name} lat={poi_lat} lon={poi_lon}"
        )
    if geom.geom_type == "Point":
        return geom

    # Polygon fallback: evaluate all boundary vertices and keep the closest
    # vertex->nearest-node pair.
    if snap_graph is not None and geom.geom_type in {"Polygon", "MultiPolygon"}:
        try:
            vertices = []
            if geom.geom_type == "Polygon":
                rings = [geom.exterior] + list(geom.interiors)
                for ring in rings:
                    vertices.extend(list(ring.coords))
            else:
                for poly in geom.geoms:
                    rings = [poly.exterior] + list(poly.interiors)
                    for ring in rings:
                        vertices.extend(list(ring.coords))

            if vertices:
                # remove duplicates while preserving order
                seen = set()
                unique_vertices = []
                for x, y in vertices:
                    key = (round(x, 9), round(y, 9))
                    if key in seen:
                        continue
                    seen.add(key)
                    unique_vertices.append((x, y))

                xs = [x for x, _ in unique_vertices]
                ys = [y for _, y in unique_vertices]
                node_ids = ox.distance.nearest_nodes(snap_graph, xs, ys)
                try:
                    node_ids = list(node_ids)
                except TypeError:
                    node_ids = [node_ids]

                best_node = None
                best_dist = None
                for (vx, vy), node_id in zip(unique_vertices, node_ids):
                    node = snap_graph.nodes[node_id]
                    node_lat = node["y"]
                    node_lon = node["x"]
                    d = _haversine_m(vy, vx, node_lat, node_lon)
                    if best_dist is None or d < best_dist:
                        best_dist = d
                        best_node = (node_lon, node_lat)

                if best_node is not None:
                    return Point(best_node[0], best_node[1])
        except Exception:
            pass

    # Fallback: centroid
    try:
        return cast(Point, geom.centroid)
    except Exception as e:
        raise ValueError(
            f"Cannot compute centroid ({geom.geom_type}): {e}. name={poi_name} lat={poi_lat} lon={poi_lon}"
        )


def _extract_geom_and_name(item):
    if isinstance(item, tuple) and len(item) == 2:
        return item[0], item[1]
    return item, None


def _is_geometry(value) -> TypeGuard[BaseGeometry]:
    return isinstance(value, BaseGeometry)


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
            origin_nodes = ox.distance.nearest_nodes(mode_graph, [origin[1]], [origin[0]])
            try:
                origin_nodes = list(origin_nodes)
            except TypeError:
                origin_nodes = [origin_nodes]
            if not origin_nodes:
                raise RuntimeError("nearest_nodes returned no origin node")
            origin_node = cast(Hashable, _normalize_node_id(origin_nodes[0]))
            
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


def _get_mode_lengths(grafo, origin, network_type, radius_m, origin_node=None):
    # cache delle distanze da origine per evitare Dijkstra ripetuti
    radius_key = "all" if radius_m is None else f"r{int(radius_m)}"
    if origin_node is None:
        origin_key = (round(origin[0], 6), round(origin[1], 6))
        key = (origin_key[0], origin_key[1], network_type, radius_key)
    else:
        key = (int(origin_node), network_type, radius_key)

    if key in _MODE_LENGTHS_CACHE:
        # ritorna distanze già calcolate per questa origine/modo
        return _MODE_LENGTHS_CACHE[key]

    mode_graph = _get_mode_graph(network_type)
    if origin_node is None:
        origin_nodes = ox.distance.nearest_nodes(mode_graph, [origin[1]], [origin[0]])
        try:
            origin_nodes = list(origin_nodes)
        except TypeError:
            origin_nodes = [origin_nodes]
        if not origin_nodes:
            raise RuntimeError("nearest_nodes returned no origin node")
        origin_node = cast(Hashable, _normalize_node_id(origin_nodes[0]))
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
        poi_points = cast(list[Point], _POI_POINTS_CACHE[points_cache_key])
    else:
        poi_points: list[Point] = []
        for item in poi_geometry:
            geom, poi_name = _extract_geom_and_name(item)
            if not _is_geometry(geom):
                continue
            poi_lat = None
            poi_lon = None
            try:
                if geom.geom_type == "Point":
                    geom_point = cast(Point, geom)
                    poi_lat = geom_point.y
                    poi_lon = geom_point.x
                else:
                    c = geom.centroid
                    poi_lat = c.y
                    poi_lon = c.x
            except Exception:
                pass
            geom = _entrance_point_for_geom(
                geom,
                snap_graph=grafo,
                poi_name=poi_name,
                poi_lat=poi_lat,
                poi_lon=poi_lon,
            )
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

    if not poi_points:
        return {
            "cache_hit": False,
            "cache_file": cache_file,
            "poi_points": [],
            "beta": beta,
            "decay_walk": [],
            "decay_bike": [],
            "decay_drive": [],
        }

    xs = [geom.x for geom in poi_points]
    ys = [geom.y for geom in poi_points]

    mode_distances = {}
    for mode in ["walk", "bike", "drive"]:
        # trova nodi POI e distanze shortest-path cached per ogni modo
        try:
            mode_graph = _get_mode_graph(mode)
            # single pass: snap origin + tutti i POI insieme
            all_x = [origine[1]]
            all_y = [origine[0]]
            all_x.extend(xs)
            all_y.extend(ys)
            all_nodes = ox.distance.nearest_nodes(mode_graph, all_x, all_y)
            try:
                all_nodes = list(all_nodes)
            except TypeError:
                all_nodes = [all_nodes]

            if not all_nodes:
                mode_distances[mode] = [None] * len(poi_points)
                continue
            origin_node = cast(Hashable, _normalize_node_id(all_nodes[0]))
            poi_nodes = [cast(Hashable, _normalize_node_id(n)) for n in all_nodes[1:]]

            lengths = _get_mode_lengths(grafo, origine, mode, radius_m, origin_node=origin_node)
            mode_distances[mode] = [lengths.get(node) for node in poi_nodes]
        except Exception:
            # fail soft for this mode; caller will skip None distances
            mode_distances[mode] = [None] * len(poi_points)

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

    global _G_CACHE, _POI_GEOM_CACHE, _POI_POINTS_CACHE
    if _G_CACHE is None:
        _G_CACHE = graphml.get_graph()
    grafo = _G_CACHE
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
        poi_points = cast(list[Point], _POI_POINTS_CACHE[points_cache_key])
    else:
        poi_points: list[Point] = []
        for item in poi_geometry:
            geom, poi_name = _extract_geom_and_name(item)
            if not _is_geometry(geom):
                continue
            poi_lat = None
            poi_lon = None
            try:
                if geom.geom_type == "Point":
                    geom_point = cast(Point, geom)
                    poi_lat = geom_point.y
                    poi_lon = geom_point.x
                else:
                    c = geom.centroid
                    poi_lat = c.y
                    poi_lon = c.x
            except Exception:
                pass
            geom = _entrance_point_for_geom(
                geom,
                snap_graph=grafo,
                poi_name=poi_name,
                poi_lat=poi_lat,
                poi_lon=poi_lon,
            )
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
    global _G_CACHE
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
