import os
import pickle
import random
import hashlib
import math
from typing import cast

import osmnx as ox
from shapely.geometry import Point
from tqdm import tqdm

from context import PipelineContext
from pipeline_types import SnappingStageResult
from utils import graphml, services as serv, delta_g
from osmnx.distance import nearest_nodes
import numpy as np

# Computes the haversine distance in meters between two points (lat1, lon1) and (lat2, lon2)
def _haversine_m(lat1, lon1, lat2, lon2):
    """Compute great-circle distance between two coordinates in meters.

    Inputs:
    - lat1, lon1: first coordinate.
    - lat2, lon2: second coordinate.

    Outputs:
    - float: distance in meters.
    """
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

# When caching routing/impedance results, the keys used to retrieve the values are the coordinates of the node in question, rounded to 6 decimals.
def _coord_key(coord):
    """Normalize coordinate keys for cache/index lookups.

    Inputs:
    - coord: `(lat, lon)` coordinate tuple.

    Outputs:
    - tuple: rounded `(lat, lon)` key at 6-decimal precision.
    """
    return (round(coord[0], 6), round(coord[1], 6))


# Since POI can be geometries and each POI must be snapped to a node of the graph to compute routing, in this function we create a collection of candidate
# snap points for a geometry, i.e. a list of vertices for that geometry. Then, the snapped point of the graph will be selected based on the closest to the origin of the routing.
def _extract_geom_vertices(geom):
    """Extract representative vertex coordinates from a geometry object.

    Inputs:
    - geom: shapely geometry (point, line, polygon, multiparts, collection).

    Outputs:
    - list of `(lat, lon)` coordinates used as snap candidates.
    """
    gtype = geom.geom_type
    if gtype == "Point":
        return [(geom.y, geom.x)]
    if gtype in ("LineString", "LinearRing"):
        return [(lat, lon) for lon, lat in geom.coords]
    if gtype == "MultiLineString":
        out = []
        for line in geom.geoms:
            out.extend([(lat, lon) for lon, lat in line.coords])
        return out
    if gtype == "Polygon":
        out = [(lat, lon) for lon, lat in geom.exterior.coords]
        for ring in geom.interiors:
            out.extend([(lat, lon) for lon, lat in ring.coords])
        return out
    if gtype == "MultiPolygon":
        out = []
        for poly in geom.geoms:
            out.extend([(lat, lon) for lon, lat in poly.exterior.coords])
            for ring in poly.interiors:
                out.extend([(lat, lon) for lon, lat in ring.coords])
        return out
    if gtype == "GeometryCollection":
        out = []
        for sub in geom.geoms:
            out.extend(_extract_geom_vertices(sub))
        return out
    rep = geom.representative_point()
    return [(rep.y, rep.x)]

# In a single pass, all coordinates are snapped to the closest node of the graph 
def _snap_coords_batch(graph, coords):
    """Snap many coordinates to nearest graph nodes in one vectorized call.

    Inputs:
    - graph: network graph used for nearest-node queries.
    - coords: list of source coordinates `(lat, lon)`.

    Outputs:
    - dict: `coord_key -> (snapped_coord, snap_distance_m)`.
    """
    if not coords:
        return {}
    unique = {}
    ordered = []
    for coord in coords:
        key = _coord_key(coord)
        if key not in unique:
            unique[key] = (coord[0], coord[1])
            ordered.append(key)
    lons = [unique[k][1] for k in ordered]
    lats = [unique[k][0] for k in ordered]
    nodes = nearest_nodes(graph, lons, lats)

    if isinstance(nodes, np.ndarray):
        nodes = nodes.tolist()
    elif isinstance(nodes, tuple):
        nodes = list(nodes)
    elif not isinstance(nodes, list):
        nodes = [nodes]
    out = {}
    for idx, key in enumerate(ordered):
        node_id = nodes[idx]
        node_data = graph.nodes[node_id]
        snapped = (node_data["y"], node_data["x"])
        src = unique[key]
        dist_m = _haversine_m(src[0], src[1], snapped[0], snapped[1])
        out[key] = (snapped, dist_m)
    return out

# This utility function normalizes snap entries to this format:
# A list of pairs contiaining the source node and then the second element is a list of snapped points.
# Each snapped point is a pair as well containing the coordinates of the snapped node and the distance in meters from the source coordinate.
def _normalize_cached_snap_entries(cached):
    """Normalize old/new cache formats into a unified snap-entry structure.

    Inputs:
    - cached: object loaded from a snap cache file.

    Outputs:
    - normalized list or None when payload is not a compatible list format.
    """
    if not isinstance(cached, list):
        return None
    normalized = []
    for item in cached:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        coord_raw = item[0]
        if not isinstance(coord_raw, (list, tuple)) or len(coord_raw) < 2:
            continue
        coord = (float(coord_raw[0]), float(coord_raw[1]))
        candidates = []
        second = item[1]
        if (
            isinstance(second, list)
            and second
            and isinstance(second[0], (list, tuple))
            and len(second[0]) >= 2
            and isinstance(second[0][0], (list, tuple))
        ):
            for cand in second:
                if not isinstance(cand, (list, tuple)) or len(cand) < 2:
                    continue
                snapped = cand[0]
                if not isinstance(snapped, (list, tuple)) or len(snapped) < 2:
                    continue
                try:
                    snap_dist_m = float(cand[1])
                except Exception:
                    snap_dist_m = _haversine_m(coord[0], coord[1], snapped[0], snapped[1])
                candidates.append(((snapped[0], snapped[1]), snap_dist_m))
        else:
            snapped = second
            if not isinstance(snapped, (list, tuple)) or len(snapped) < 2:
                continue
            if len(item) >= 3 and isinstance(item[2], (int, float)):
                snap_dist_m = float(item[2])
            else:
                snap_dist_m = _haversine_m(coord[0], coord[1], snapped[0], snapped[1])
            candidates.append(((snapped[0], snapped[1]), snap_dist_m))
        if not candidates:
            continue
        deduped = {}
        for snapped_coord, snap_dist_m in candidates:
            key = _coord_key(snapped_coord)
            prev = deduped.get(key)
            if prev is None or snap_dist_m < prev[1]:
                deduped[key] = (snapped_coord, float(snap_dist_m))
        normalized.append((coord, list(deduped.values())))
    return normalized


# For each specified PoiQuery it returns the corresponding geometries. This is done only if the snapped list for the poi does not exist.
# Otherwise, we will always refer to the snapped point and avoid dealing with the geometry, reducing the complexity of the code
def _get_query_geometries(query):
    """Load and cache POI geometries for one query key.

    Inputs:
    - query: POI query configuration object.

    Outputs:
    - list of geometries with optional names.
    """
    feature, value, tags = delta_g._resolve_query(query.poi_type, None, query.tags)
    cache_key = (feature, value)
    if cache_key in delta_g._POI_GEOM_CACHE:
        return delta_g._POI_GEOM_CACHE[cache_key]
    if tags:
        poi = graphml.get_poi(tags=tags)
    else:
        poi = graphml.get_poi(feature, value)
    geometries = graphml.get_poi_geometries(poi)
    delta_g._POI_GEOM_CACHE[cache_key] = geometries
    return geometries

# To avoid long file names, an hash is computed for each poi_key and this function returns the path where a particular poi_key is saved
def _poi_snap_cache_path(cache_dir, poi_key, cache_ns):
    """Build deterministic cache filename for one POI key and namespace.

    Inputs:
    - cache_dir: directory where snap caches are stored.
    - poi_key: normalized key of POI query.
    - cache_ns: namespace token (for mode separation).

    Outputs:
    - str: cache file path.
    """
    os.makedirs(cache_dir, exist_ok=True)
    key_repr = f"{cache_ns}|{repr(poi_key)}"
    key_hash = hashlib.sha1(key_repr.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, f"{key_hash}.pkl")

# Loads the poi_snap_cache, i.e. for each poi -> the closest node of the graph. If the POI is a geometry then there's one closest node for each vertex
def _load_poi_snap_cache(cache_dir, poi_key, cache_ns):
    """Load snap cache payload for one POI key when available.

    Inputs:
    - cache_dir: snap cache directory.
    - poi_key: POI query key.
    - cache_ns: mode/cache namespace.

    Outputs:
    - cached payload object or None when file is missing/invalid.
    """
    path = _poi_snap_cache_path(cache_dir, poi_key, cache_ns)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None

# Save the poi_snap_cache, i.e. for each poi -> the closest node of the graph. If the POI is a geometry then there's one closest node for each vertex
def _save_poi_snap_cache(cache_dir, poi_key, payload, cache_ns):
    """Persist normalized snap payload atomically for one POI key.

    Inputs:
    - cache_dir: snap cache directory.
    - poi_key: POI query key.
    - payload: normalized snap payload.
    - cache_ns: mode/cache namespace.

    Outputs:
    - None. Writes cache file to disk.
    """
    path = _poi_snap_cache_path(cache_dir, poi_key, cache_ns)
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp_path, path)

# This function builds the poi_snap_map. For each poi type, we have a list of snapped POIs. If the POI is a geometry or so, then that POI is represented with a list
# containing the snapped node for each vertex. The selection of the node will be dependent to the origin of the routing.
def _build_poi_snap_map(graph, query_by_key, enable_progress, cache_dir, cache_ns, max_pois=None, seed=42):
    """Build POI-to-snap-candidates map, reusing cache when possible.

    Inputs:
    - graph: target transport graph for snapping.
    - query_by_key: mapping of query key to POI query definition.
    - enable_progress: whether to show progress bars.
    - cache_dir: snap cache directory.
    - cache_ns: namespace to isolate cache by mode.
    - max_pois: optional debug cap per query.
    - seed: random seed used for debug sampling.

    Outputs:
    - dict: POI key -> list of source coords and candidate snapped coords.
    """
    poi_snap_map = {}
    rng = random.Random(seed)

    point_coords = []
    geom_vertex_map = {}
    pending_coords = {}

    for poi_key, query in query_by_key.items():
        cached = _load_poi_snap_cache(cache_dir, poi_key, cache_ns)
        normalized = _normalize_cached_snap_entries(cached)
        # Empty cached payloads can come from transient POI download failures;
        # treat them as stale so we retry POI extraction/snapping.
        if normalized is not None and len(normalized) > 0:
            poi_snap_map[poi_key] = normalized
            continue

        geometries = _get_query_geometries(query)
        coords = []
        geom_vertices_list = []
        for item in geometries:
            geom, _poi_name = delta_g._extract_geom_and_name(item)
            if not delta_g._is_geometry(geom):
                continue
            try:
                if geom.geom_type == "Point":
                    g_point = cast(Point, geom)
                    coord = (float(g_point.y), float(g_point.x))
                    geom_vertices = None
                else:
                    geom_vertices = _extract_geom_vertices(geom)
                    if not geom_vertices:
                        continue
                    # For non-point geometries, use vertex-derived candidates only.
                    coord = geom_vertices[0]
            except Exception:
                continue
            coords.append(coord)
            geom_vertices_list.append(geom_vertices)
        if max_pois is not None and len(coords) > max_pois:
            idx = rng.sample(range(len(coords)), max_pois)
            coords = [coords[i] for i in idx]
            geom_vertices_list = [geom_vertices_list[i] for i in idx]
        pending_coords[poi_key] = coords
        for idx, coord in enumerate(coords):
            coord_k = _coord_key(coord)
            if geom_vertices_list and idx < len(geom_vertices_list):
                vertices = geom_vertices_list[idx]
                if vertices:
                    geom_vertex_map[(poi_key, coord_k)] = vertices
                    point_coords.extend(vertices)
                    continue
            point_coords.append(coord)

    snap_pbar = tqdm(total=len(point_coords), desc=f"Snapping stage: snap POIs ({cache_ns})", mininterval=0) if enable_progress else None
    snapped_points = _snap_coords_batch(graph, point_coords)

    for poi_key, coords in pending_coords.items():
        snapped_list = []
        for coord in coords:
            coord_k = _coord_key(coord)
            geom_vertices = geom_vertex_map.get((poi_key, coord_k))
            if geom_vertices:
                candidates = []
                for vertex in geom_vertices:
                    snap_info = snapped_points.get(_coord_key(vertex))
                    if snap_info is None:
                        continue
                    candidates.append((tuple(snap_info[0]), float(snap_info[1])))
                if not candidates:
                    best = snapped_points.get(coord_k)
                    if best is not None:
                        candidates = [(tuple(best[0]), float(best[1]))]
            else:
                best = snapped_points.get(coord_k)
                if best is not None:
                    candidates = [(tuple(best[0]), float(best[1]))]
                else:
                    candidates = [(coord, 0.0)]
            deduped = {}
            for snapped_coord, snap_dist_m in candidates:
                k = _coord_key(snapped_coord)
                prev = deduped.get(k)
                if prev is None or snap_dist_m < prev[1]:
                    deduped[k] = (snapped_coord, snap_dist_m)
            snapped_list.append((coord, list(deduped.values())))
        poi_snap_map[poi_key] = snapped_list
        _save_poi_snap_cache(cache_dir, poi_key, snapped_list, cache_ns)

    if snap_pbar:
        snap_pbar.update(len(point_coords))
        snap_pbar.close()
    for poi_key in query_by_key:
        poi_snap_map.setdefault(poi_key, [])
    return poi_snap_map

# The snap map is then converted to a dictionary for faster lookup.
def _snap_map_to_info(poi_snap_map):
    """Convert snap-map payload into fast lookup structure by POI/source coord.

    Inputs:
    - poi_snap_map: normalized snap map payload.

    Outputs:
    - dict: POI key -> `{source_coord_key: [(snapped_coord, dist_m), ...]}`.
    """
    poi_snap_info_by_type = {}
    for poi_key, snapped_list in poi_snap_map.items():
        info = {}
        for item in snapped_list:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            coord = item[0]
            candidates = item[1]
            if not isinstance(candidates, list):
                continue
            normalized_candidates = []
            for cand in candidates:
                if not isinstance(cand, (list, tuple)) or len(cand) < 2:
                    continue
                snapped = tuple(cand[0])
                snap_dist_m = float(cand[1])
                normalized_candidates.append((snapped, snap_dist_m))
            if normalized_candidates:
                info[_coord_key(coord)] = normalized_candidates
        poi_snap_info_by_type[poi_key] = info
    return poi_snap_info_by_type

# This function does the step of selecting the best snap candidate.
# When the poi is a geometry, the closest graph node depends to the source. Ideally, the closest vertex to the source will be where you'll access to the poi.
# So distance is computed between all candidate snap pois and origin and only one is chosen.
def _select_best_snap_candidate_for_origin(origin, coord, snap_info):
    """Choose best snapped candidate for an origin/source pair.

    Inputs:
    - origin: origin coordinate `(lat, lon)`.
    - coord: original source coordinate `(lat, lon)`.
    - snap_info: candidate snapped points with snap distances.

    Outputs:
    - tuple: `(selected_snapped_coord, selected_snap_distance_m)`.
    """
    if snap_info and isinstance(snap_info, list):
        best_cand = None
        for cand in snap_info:
            if not isinstance(cand, (list, tuple)) or len(cand) < 2:
                continue
            cand_coord = tuple(cand[0])
            cand_snap_dist_m = float(cand[1])
            origin_dist_m = _haversine_m(origin[0], origin[1], cand_coord[0], cand_coord[1])
            score = (origin_dist_m, cand_snap_dist_m)
            if best_cand is None or score < best_cand[0]:
                best_cand = (score, cand_coord, cand_snap_dist_m)
        if best_cand is not None:
            return best_cand[1], best_cand[2]
    elif snap_info and isinstance(snap_info, (list, tuple)) and len(snap_info) >= 2:
        return tuple(snap_info[0]), float(snap_info[1])
    return coord, 0.0

# This function does the snapping process for all Origin-Destination pairs. When the destination is a point, that is taken.
# Instead, if there are multiple candidates the _select_best_snap_candidate above is called.
# At the end the result is a set of snapped_pois based on the OD pairs.
def _build_selected_routing_destinations(nodes_with_coords, poi_bus_snap_info_by_type, enable_progress):
    """Build reduced bus destination set by selecting best candidates per origin.

    Inputs:
    - nodes_with_coords: origin nodes with x/y coordinates.
    - poi_bus_snap_info_by_type: bus snap candidates for each POI key.
    - enable_progress: whether to display progress while evaluating OD pairs.

    Outputs:
    - list: sorted set of selected destination coordinates for routing.
    """
    single_candidate_selected = set()
    multi_candidate_items = []

    for snap_info_for_key in poi_bus_snap_info_by_type.values():
        for coord_k, snap_info in snap_info_for_key.items():
            coord = (float(coord_k[0]), float(coord_k[1]))
            if isinstance(snap_info, list) and len(snap_info) > 1:
                multi_candidate_items.append((coord, snap_info))
                continue
            snapped_coord, _ = _select_best_snap_candidate_for_origin(coord, coord, snap_info)
            single_candidate_selected.add(snapped_coord)

    total_pairs = len(multi_candidate_items) * len(nodes_with_coords)
    pbar = tqdm(total=total_pairs, desc="Snapping stage: select bus D", mininterval=1) if (enable_progress and total_pairs > 0) else None
    last_refresh = 0.0
    selected = set()
    try:
        selected.update(single_candidate_selected)
        for _, data in nodes_with_coords:
            origin = (data["y"], data["x"])
            for coord, snap_info in multi_candidate_items:
                snapped_coord, _ = _select_best_snap_candidate_for_origin(origin, coord, snap_info)
                selected.add(snapped_coord)
                if pbar:
                    pbar.update(1)
                    now = __import__("time").time()
                    if now - last_refresh >= 1.0:
                        pbar.refresh()
                        last_refresh = now
    finally:
        if pbar:
            pbar.refresh()
            pbar.close()
    return sorted(selected)


def run_snapping_stage(ctx: PipelineContext) -> SnappingStageResult:
    """Run snapping stage for walk/bike/drive and derive bus snap candidates.

    Inputs:
    - ctx: pipeline context with config, graphs, and debug/progress settings.

    Outputs:
    - SnappingStageResult: query map, bus/mode snap info, and shared mode graphs.
    """
    # Creates the query keys to get the POIs information
    cfg = ctx.config
    unique_queries = serv.unique_query_keys()
    query_by_key = {serv.query_key(q): q for q in unique_queries}

    # For each mode, gets the corresponding graph
    shared_mode_graphs = {
        "walk": graphml.get_mode_graph("walk"),
        "bike": graphml.get_mode_graph("bike"),
        "drive": graphml.get_mode_graph("drive"),
    }

    # Snap POIs for walk, bike and drive networks.
    # Bus will reuse walk snaps to avoid duplicate work/caches.
    poi_mode_snap_info_by_type = {}
    for mode, mode_graph in shared_mode_graphs.items():
        mode_snap_map = _build_poi_snap_map(
            mode_graph,
            query_by_key,
            cfg.enable_progress,
            cfg.poi_snap_cache_dir,
            cache_ns=f"{cfg.city_slug}|mode_{mode}",
            max_pois=cfg.debug_max_pois,
            seed=cfg.seed,
        )
        mode_snap_info = _snap_map_to_info(mode_snap_map)
        for poi_key, info in mode_snap_info.items():
            poi_mode_snap_info_by_type.setdefault(poi_key, {})[mode] = info

    # Set default empty values to fields
    for poi_key in query_by_key:
        poi_mode_snap_info_by_type.setdefault(poi_key, {}).setdefault("walk", {})
        poi_mode_snap_info_by_type[poi_key].setdefault("bike", {})
        poi_mode_snap_info_by_type[poi_key].setdefault("drive", {})

    # Reuse walk snaps as bus snaps
    poi_bus_snap_info_by_type = {
        poi_key: mode_infos["walk"]
        for poi_key, mode_infos in poi_mode_snap_info_by_type.items()
    }

    return SnappingStageResult(
        query_by_key=query_by_key,
        poi_bus_snap_info_by_type=poi_bus_snap_info_by_type,
        poi_mode_snap_info_by_type=poi_mode_snap_info_by_type,
        shared_mode_graphs=shared_mode_graphs,
    )


# Alias for compatibility with old scripts
build_selected_routing_destinations = _build_selected_routing_destinations
select_best_snap_candidate_for_origin = _select_best_snap_candidate_for_origin
coord_key = _coord_key
