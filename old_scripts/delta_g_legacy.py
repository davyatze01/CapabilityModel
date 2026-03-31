"""
Legacy delta_g APIs kept for backward compatibility with older scripts.

These functions are not used by the current run_pipeline implementation.
"""

from utils import graphml, decay, get_impedance, delta_g as core
from old_scripts import route
import os
import pickle
from typing import cast, Hashable
import networkx as nx
import osmnx as ox
from shapely.geometry import Point


_POI_POINTS_CACHE = {}


def _poi_geom_cache_path(feature, value):
    return os.path.join(core.POI_GEOM_CACHE_FOLDER, f"{feature}_{value}.pkl")


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


def _geometry_to_point(geom):
    if geom.geom_type == "Point":
        return cast(Point, geom)
    return cast(Point, geom.centroid)


def precompute_distances(origin, radius_m=None):
    if core._G_CACHE is None:
        core._G_CACHE = graphml.get_graph()
    network_cache = {}
    for mode in ["walk", "bike", "drive"]:
        try:
            mode_graph = core._get_mode_graph(mode)
            origin_nodes = ox.distance.nearest_nodes(mode_graph, [origin[1]], [origin[0]])
            try:
                origin_nodes = list(origin_nodes)
            except TypeError:
                origin_nodes = [origin_nodes]
            if not origin_nodes:
                raise RuntimeError("nearest_nodes returned no origin node")
            origin_node = cast(Hashable, core._normalize_node_id(origin_nodes[0]))
            lengths = nx.single_source_dijkstra_path_length(mode_graph, origin_node, weight="length")
            network_cache[mode] = {"graph": mode_graph, "lengths": lengths}
        except Exception:
            network_cache[mode] = {"graph": None, "lengths": {}}
    return network_cache


def preload_all_pois(poi_list):
    print("Pre-loading POI geometries (legacy)...")
    for item in poi_list:
        if isinstance(item, str):
            poi_type = item
            feature = None
            tags = None
        else:
            poi_type = item.poi_type
            feature = getattr(item, "feature", None)
            tags = getattr(item, "tags", None)
        feature, value, tags = core._resolve_query(poi_type, feature, tags)
        cache_key = (feature, value)
        if cache_key not in core._POI_GEOM_CACHE:
            if tags:
                poi = graphml.get_poi(tags=tags)
            else:
                poi = graphml.get_poi(feature, value)
            geometries = graphml.get_poi_geometries(poi)
            core._POI_GEOM_CACHE[cache_key] = geometries
            _save_poi_geometries_to_disk(feature, value, geometries)
    return core._POI_GEOM_CACHE


def get_poi_points(poi_type, origine, feature=None, radius_m=None, tags=None, progress=None):
    feature, value, tags = core._resolve_query(poi_type, feature, tags)
    if core._G_CACHE is None:
        core._G_CACHE = graphml.get_graph()
    cache_key = (feature, value)
    if cache_key not in core._POI_GEOM_CACHE:
        cached_geom = _load_poi_geometries_from_disk(feature, value)
        if cached_geom is not None:
            core._POI_GEOM_CACHE[cache_key] = cached_geom
        else:
            if tags:
                poi = graphml.get_poi(tags=tags)
            else:
                poi = graphml.get_poi(feature, value)
            geometries = graphml.get_poi_geometries(poi)
            core._POI_GEOM_CACHE[cache_key] = geometries
            _save_poi_geometries_to_disk(feature, value, geometries)

    poi_geometry = core._POI_GEOM_CACHE[cache_key]
    points_cache_key = (feature, value)
    if radius_m is None and points_cache_key in _POI_POINTS_CACHE:
        poi_points = cast(list[Point], _POI_POINTS_CACHE[points_cache_key])
    else:
        poi_points = []
        for item in poi_geometry:
            geom, _name = core._extract_geom_and_name(item)
            if not core._is_geometry(geom):
                continue
            poi_points.append(_geometry_to_point(geom))
            if progress is not None:
                progress.update(1)
        if radius_m is None:
            _POI_POINTS_CACHE[points_cache_key] = poi_points

    if radius_m is not None:
        poi_points = [
            geom for geom in poi_points
            if core._haversine_m(origine[0], origine[1], geom.y, geom.x) <= radius_m
        ]
    return poi_points


def accessibility_non_bus(poi_type, origine, feature=None, radius_m=None, tags=None):
    feature, value, tags = core._resolve_query(poi_type, feature, tags)
    cache_file = core._cache_file_path(poi_type, origine, feature, radius_m)
    if os.path.exists(cache_file):
        rra = core.load_rra(cache_file)
        return {
            "cache_hit": True,
            "cache_file": cache_file,
            "accessibility_value": core.accessibility_from_rra(rra, poi_type=poi_type),
        }

    poi_points = get_poi_points(poi_type, origine, feature=feature, radius_m=radius_m, tags=tags)
    decay_constant = float(core.serv.get_decay_constant(poi_type))
    beta = core.math.log(2) / decay_constant
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
        try:
            mode_graph = core._get_mode_graph(mode)
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
            origin_node = cast(Hashable, core._normalize_node_id(all_nodes[0]))
            poi_nodes = [cast(Hashable, core._normalize_node_id(n)) for n in all_nodes[1:]]
            lengths = core._get_mode_lengths(core._G_CACHE, origine, mode, radius_m, origin_node=origin_node)
            mode_distances[mode] = [lengths.get(node) for node in poi_nodes]
        except Exception:
            mode_distances[mode] = [None] * len(poi_points)

    decay_walk = []
    decay_bike = []
    decay_drive = []
    for i in range(len(poi_points)):
        dw = db = dd = None
        for mode in ["walk", "bike", "drive"]:
            dist_m = mode_distances[mode][i]
            if dist_m is None:
                continue
            imp = get_impedance.impedance_base(dist_m / 1000.0, mode)
            d = decay.distance_decay(beta, imp)
            if mode == "walk":
                dw = d
            elif mode == "bike":
                db = d
            else:
                dd = d
        decay_walk.append(dw)
        decay_bike.append(db)
        decay_drive.append(dd)

    return {
        "cache_hit": False,
        "cache_file": cache_file,
        "poi_points": poi_points,
        "beta": beta,
        "decay_walk": decay_walk,
        "decay_bike": decay_bike,
        "decay_drive": decay_drive,
    }


def accessibility_bus(origine, poi_points, beta, grafo):
    decay_bus = []
    for geom in poi_points:
        destinazione = (geom.y, geom.x)
        try:
            _, _, imp_bus = route.get_route(
                grafo, "bus", origine, destinazione, impedance_flag=True, ax=None, distance_only=True
            )
        except Exception:
            imp_bus = None
        if imp_bus:
            decay_bus.append(decay.distance_decay(beta, imp_bus))
        else:
            decay_bus.append(0.0)
    return decay_bus


def accessibility(poi_type, origine, feature=None, radius_m=None, tags=None):
    feature, value, tags = core._resolve_query(poi_type, feature, tags)
    cache_file = core._cache_file_path(poi_type, origine, feature, radius_m)
    if os.path.exists(cache_file):
        rra = core.load_rra(cache_file)
        return core.accessibility_from_rra(rra, poi_type=poi_type)

    data = accessibility_non_bus(poi_type, origine, feature, radius_m, tags)
    poi_points = data["poi_points"]
    beta = data["beta"]
    decay_walk = data["decay_walk"]
    decay_bike = data["decay_bike"]
    decay_drive = data["decay_drive"]
    if core._G_CACHE is None:
        core._G_CACHE = graphml.get_graph()
    decay_bus = accessibility_bus(origine, poi_points, beta, core._G_CACHE)
    rra, accessibility_value = core.merge_rra_and_accessibility(
        decay_walk, decay_bike, decay_drive, decay_bus, poi_type=poi_type
    )
    core.save_rra(cache_file, rra)
    return accessibility_value
