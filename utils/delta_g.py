from utils import graphml, decay, get_impedance, services as serv
import math
import os
import json
import hashlib
from typing import Hashable, TypeGuard, cast
import networkx as nx
import osmnx as ox
from shapely.geometry.base import BaseGeometry


# Kept because main.py uses this in-memory geometry cache while building snap maps.
POI_GEOM_CACHE_FOLDER = "poi_geom_cache"
os.makedirs(POI_GEOM_CACHE_FOLDER, exist_ok=True)

_G_CACHE = None
_POI_GEOM_CACHE = {}  # key: (feature, value) -> list geometries
_MODE_GRAPH_CACHE = {}  # key: network_type -> graph
_MODE_LENGTHS_CACHE = {}  # key: (origin, network_type, radius_key) -> dict node->distance


def _normalize_node_id(node):
    """Normalize external node scalar types into plain Python values.

    Inputs:
    - node: node id possibly wrapped in numpy/pandas scalar type.

    Outputs:
    - normalized node id value.
    """
    try:
        return node.item()
    except Exception:
        return node


def _extract_geom_and_name(item):
    """Unpack geometry/name tuple or pass-through geometry item.

    Inputs:
    - item: geometry or `(geometry, name)` pair.

    Outputs:
    - tuple `(geometry, name_or_none)`.
    """
    if isinstance(item, tuple) and len(item) == 2:
        return item[0], item[1]
    return item, None


def _is_geometry(value) -> TypeGuard[BaseGeometry]:
    """Check whether a value is a shapely geometry instance.

    Inputs:
    - value: object to inspect.

    Outputs:
    - bool (TypeGuard): True when value is a geometry.
    """
    return isinstance(value, BaseGeometry)


def _resolve_feature(poi_type, feature):
    """Resolve fallback OSM feature/value pair for a POI type.

    Inputs:
    - poi_type: POI type key.
    - feature: optional explicit feature key.

    Outputs:
    - tuple `(feature_key, feature_value)` used for OSM POI query.
    """
    if feature is None:
        if poi_type in {"healthcare"}:
            return poi_type, True
        return "amenity", poi_type
    return feature, poi_type


def _tags_cache_key(tags):
    """Build deterministic cache key token from tags dictionary.

    Inputs:
    - tags: tags dictionary used for OSM query.

    Outputs:
    - str cache key token.
    """
    tags_json = json.dumps(tags, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    key_hash = hashlib.sha1(tags_json.encode("utf-8")).hexdigest()
    return f"tags_{key_hash}"


def _resolve_query(poi_type, feature, tags):
    """Resolve POI query representation from poi_type/feature/tags inputs.

    Inputs:
    - poi_type: POI type key.
    - feature: optional feature key.
    - tags: optional tags dictionary.

    Outputs:
    - tuple `(feature_key, value_or_cache_key, tags_or_none)`.
    """
    if tags:
        return "tags", _tags_cache_key(tags), tags
    feature, value = _resolve_feature(poi_type, feature)
    return feature, value, None


def _haversine_m(lat1, lon1, lat2, lon2):
    """Compute great-circle distance between two coordinates in meters.

    Inputs:
    - lat1, lon1: first coordinate.
    - lat2, lon2: second coordinate.

    Outputs:
    - float distance in meters.
    """
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _select_best_snap_for_origin(origin, source_coord, candidates):
    """Choose best candidate snap for an origin using origin distance tie-break.

    Inputs:
    - origin: origin coordinate `(lat, lon)`.
    - source_coord: source POI coordinate `(lat, lon)`.
    - candidates: candidate snaps with snap-distance metadata.

    Outputs:
    - selected snapped coordinate `(lat, lon)`.
    """
    best = None
    for cand in candidates or []:
        if not isinstance(cand, (list, tuple)) or len(cand) < 2:
            continue
        cand_coord = tuple(cand[0])
        cand_snap_dist = float(cand[1])
        origin_dist = _haversine_m(origin[0], origin[1], cand_coord[0], cand_coord[1])
        score = (origin_dist, cand_snap_dist)
        if best is None or score < best[0]:
            best = (score, cand_coord)
    if best is not None:
        return best[1]
    return source_coord


def _get_mode_graph(network_type):
    """Get mode-specific graph from in-memory cache or disk loader.

    Inputs:
    - network_type: mode key.

    Outputs:
    - graph object for the requested mode.
    """
    if network_type not in _MODE_GRAPH_CACHE:
        _MODE_GRAPH_CACHE[network_type] = graphml.get_mode_graph(network_type)
    return _MODE_GRAPH_CACHE[network_type]


def _get_mode_lengths(grafo, origin, network_type, radius_m, origin_node=None):
    """Get/calculate shortest-path lengths from one origin on a mode graph.

    Inputs:
    - grafo: base graph reference (kept for compatibility).
    - origin: origin coordinate `(lat, lon)`.
    - network_type: mode key.
    - radius_m: optional routing radius.
    - origin_node: optional pre-snapped origin node id.

    Outputs:
    - dict mapping reachable node id -> path length in meters.
    """
    radius_key = "all" if radius_m is None else f"r{int(radius_m)}"
    if origin_node is None:
        origin_key = (round(origin[0], 6), round(origin[1], 6))
        key = (origin_key[0], origin_key[1], network_type, radius_key)
    else:
        key = (int(origin_node), network_type, radius_key)

    if key in _MODE_LENGTHS_CACHE:
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
    lengths = nx.single_source_dijkstra_path_length(mode_graph, origin_node, weight="length")
    _MODE_LENGTHS_CACHE[key] = lengths
    return lengths


def build_rra(decay_walk, decay_bike, decay_drive, decay_bus):
    """Build per-POI RRA list by combining modal decay lists element-wise.

    Inputs:
    - decay_walk, decay_bike, decay_drive, decay_bus: modal decay arrays.

    Outputs:
    - list[float]: merged RRA values for valid entries.
    """
    rra = []
    for i in range(len(decay_walk)):
        dw = decay_walk[i]
        db = decay_bike[i]
        dd = decay_drive[i]
        d_bus = decay_bus[i]
        if None not in (dw, db, dd, d_bus):
            rra.append(decay.calculate_rra(dw, db, dd, d_bus))
    return rra


def accessibility_from_rra(RRA, poi_type=None, contribution_constant=None):
    """Aggregate RRA values into one accessibility score for a POI type.

    Inputs:
    - RRA: list of RRA values.
    - poi_type: optional POI type key (for configured contribution constant lookup).
    - contribution_constant: optional explicit contribution constant.

    Outputs:
    - float accessibility score.
    """
    rra_desc = sorted(RRA, reverse=True)

    def c_from_target(target: float) -> float:
        y_target = 0.9
        if target <= 0:
            raise ValueError(f"contribution_constant must be > 0, got {target}")
        return round(math.log(1.0 - y_target) / target, 2)

    def g(x: int, c: float) -> float:
        return 1.0 - math.exp(c * x)

    def deltag(x: int, x2: int, c: float) -> float:
        return g(x, c) - g(x2, c)

    if contribution_constant is None:
        if poi_type is not None:
            contribution_constant = serv.get_contribution_constant(poi_type)
        else:
            contribution_constant = 2.0
    c = c_from_target(target=float(contribution_constant))

    out = 0.0
    for i, element in enumerate(rra_desc):
        if i == 0:
            out += element * g(i + 1, c)
        else:
            out += element * deltag(i + 1, i, c)
    return out


def accessibility_non_bus_from_snap_map(poi_type, origine, poi_snap_info_by_mode, feature=None, radius_m=None, tags=None):
    """Compute non-bus decay ingredients for one origin/POI type from snap maps.

    Inputs:
    - poi_type: POI type key.
    - origine: origin coordinate `(lat, lon)`.
    - poi_snap_info_by_mode: snapped candidate map grouped by mode.
    - feature: optional explicit OSM feature key.
    - radius_m: optional routing radius.
    - tags: optional tags filter used for query identity.

    Outputs:
    - dict with cache metadata and non-bus modal decay arrays.
    """
    _feature, _value, _tags = _resolve_query(poi_type, feature, tags)

    mode_infos = poi_snap_info_by_mode or {}
    source_keys_seen = set()
    source_coords = []
    for mode in ("walk", "bike", "drive"):
        info = mode_infos.get(mode) or {}
        for src_key in info.keys():
            if src_key in source_keys_seen:
                continue
            source_keys_seen.add(src_key)
            source_coords.append((float(src_key[0]), float(src_key[1])))

    decay_constant = float(serv.get_decay_constant(poi_type))
    beta = math.log(2) / decay_constant
    if not source_coords:
        return {
            "source_coords": [],
            "beta": beta,
            "decay_walk": [],
            "decay_bike": [],
            "decay_drive": [],
        }

    global _G_CACHE
    if _G_CACHE is None:
        _G_CACHE = graphml.get_graph()
    grafo = _G_CACHE

    mode_distances = {}
    for mode in ("walk", "bike", "drive"):
        info = mode_infos.get(mode) or {}
        chosen_coords = []
        for src in source_coords:
            src_key = (round(src[0], 6), round(src[1], 6))
            chosen_coords.append(_select_best_snap_for_origin(origine, src, info.get(src_key)))
        try:
            mode_graph = _get_mode_graph(mode)
            all_x = [origine[1]] + [coord[1] for coord in chosen_coords]
            all_y = [origine[0]] + [coord[0] for coord in chosen_coords]
            all_nodes = ox.distance.nearest_nodes(mode_graph, all_x, all_y)
            try:
                all_nodes = list(all_nodes)
            except TypeError:
                all_nodes = [all_nodes]
            if not all_nodes:
                mode_distances[mode] = [None] * len(source_coords)
                continue
            origin_node = cast(Hashable, _normalize_node_id(all_nodes[0]))
            poi_nodes = [cast(Hashable, _normalize_node_id(n)) for n in all_nodes[1:]]
            lengths = _get_mode_lengths(grafo, origine, mode, radius_m, origin_node=origin_node)
            mode_distances[mode] = [lengths.get(node) for node in poi_nodes]
        except Exception:
            mode_distances[mode] = [None] * len(source_coords)

    decay_walk = []
    decay_bike = []
    decay_drive = []
    for i in range(len(source_coords)):
        dw = db = dd = None
        for mode in ("walk", "bike", "drive"):
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
        "source_coords": source_coords,
        "beta": beta,
        "decay_walk": decay_walk,
        "decay_bike": decay_bike,
        "decay_drive": decay_drive,
    }


def merge_rra_and_accessibility(decay_walk, decay_bike, decay_drive, decay_bus, poi_type=None, contribution_constant=None):
    """Compute both RRA list and final accessibility from modal decays.

    Inputs:
    - decay_walk, decay_bike, decay_drive, decay_bus: modal decay arrays.
    - poi_type: optional POI type key.
    - contribution_constant: optional explicit contribution constant.

    Outputs:
    - tuple `(rra_list, accessibility_value)`.
    """
    rra = build_rra(decay_walk, decay_bike, decay_drive, decay_bus)
    return rra, accessibility_from_rra(rra, poi_type=poi_type, contribution_constant=contribution_constant)


