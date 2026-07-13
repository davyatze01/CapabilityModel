from utils import graphml, decay, get_impedance, services as serv
import math
import os
import json
import hashlib
from typing import Hashable, TypeGuard, cast
import networkx as nx
from shapely.geometry.base import BaseGeometry
import logging
import walkability
from config import PipelineConfig
logger = logging.getLogger(__name__)

# Kept because main.py uses this in-memory geometry cache while building snap maps.
POI_GEOM_CACHE_FOLDER = "poi_geom_cache"
os.makedirs(POI_GEOM_CACHE_FOLDER, exist_ok=True)

_POI_GEOM_CACHE = {}  # key: (feature, value) -> list geometries
_MODE_GRAPH_CACHE = {}  # key: network_type -> graph
_MODE_LENGTHS_CACHE = {}  # key: (origin, network_type, radius_key) -> dict node->distance
_MODE_PRED_CACHE = {}  # key: (origin, network_type, radius_key) -> dict node->predecessor list (walk only)

# When True, edge `geometry` is dropped from mode graphs right after load to slash
# per-worker RAM. Only safe in worker processes: the walkability signature is
# overridden there and the edge-walkability index is loaded from disk, so geometry
# is never read again. The parent process (which builds that index) must keep it.
_STRIP_GRAPH_GEOMETRY = False
_MODE_NODE_KDTREE = {}  # key: network_type -> (cKDTree over projected (x,y) meters, node_id ndarray)
_COORD_NODE_MEMO = {}  # key: (network_type, round(lat,6), round(lon,6)) -> nearest node id
_NODE_IDX_MEMO = {}  # key: (network_type, round(lat,6), round(lon,6)) -> full-graph CSR node index
_WALK_EDGE_SCORES_CACHE: dict[str, dict[tuple[int, int, int], float]] = {}
_WALK_GRAPH_SIG_BY_OBJID: dict[int, str] = {}
_WALK_GRAPH_SIG_OVERRIDE: str | None = None
_EMPTY_SOURCE_COORDS_LOGGED: set[str] = set()



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


def _get_mode_graph(network_type, cfg: PipelineConfig | None = None):
    """Get the full unsimplified mode graph from in-memory cache or disk.

    Inputs:
    - network_type: mode key.
    - cfg: optional PipelineConfig. If not provided, creates a new one.

    Outputs:
    - graph object for the requested mode.
    """
    if network_type not in _MODE_GRAPH_CACHE:
        graph = graphml.get_mode_graph(network_type, cfg)
        if _STRIP_GRAPH_GEOMETRY:
            _strip_edge_geometry(graph)
        _MODE_GRAPH_CACHE[network_type] = graph
    return _MODE_GRAPH_CACHE[network_type]


def _strip_edge_geometry(graph) -> None:
    """Drop edge `geometry` from a routing graph to free per-worker RAM.

    Routing needs only `length`; walkability path scoring in workers uses the
    pre-built edge-score index, not geometry. Mutates the graph in place.
    """
    for _u, _v, data in graph.edges(data=True):
        if "geometry" in data:
            data["geometry"] = None
            del data["geometry"]


def reset_origin_caches() -> None:
    """Clear per-origin shortest-path caches between origin nodes.

    `_MODE_LENGTHS_CACHE`/`_MODE_PRED_CACHE` only ever need the origin currently
    being processed (all POI queries for one origin share the same key). Clearing
    them per origin caps a worker's memory at one origin's data instead of letting
    it grow without bound across every origin the worker handles.
    """
    _MODE_LENGTHS_CACHE.clear()
    _MODE_PRED_CACHE.clear()


def _reconstruct_path(pred, origin_node, target_node):
    """Rebuild origin->target node path from a Dijkstra predecessor map.

    Returns None when the target is unreachable. Predecessor maps are far smaller
    than caching every node's full path, so we keep the map and rebuild only the
    handful of POI-target paths actually needed.
    """
    if pred is None or target_node not in pred:
        return None
    path = [target_node]
    node = target_node
    while node != origin_node:
        preds = pred.get(node)
        if not preds:
            return None
        node = preds[0]
        path.append(node)
    path.reverse()
    return path


def _nearest_mode_node(network_type, lat, lon, cfg: PipelineConfig | None = None):
    """Snap one coordinate to the nearest full-graph node id, memoized.

    Inputs:
    - network_type: mode key.
    - lat, lon: coordinate to snap.
    - cfg: optional PipelineConfig.

    Outputs:
    - nearest node id for the coordinate.
    """
    memo_key = (network_type, round(float(lat), 6), round(float(lon), 6))
    cached = _COORD_NODE_MEMO.get(memo_key)
    if cached is not None:
        return cached
    bundle = graphml.get_mode_csr(network_type, cfg)
    tree = bundle["tree"]
    node_ids = bundle["node_ids"]
    snap_indices = bundle["snap_indices"]
    m_per_deg_lon, m_per_deg_lat = bundle["scale"]
    _, idx = tree.query([float(lon) * m_per_deg_lon, float(lat) * m_per_deg_lat])
    node_id = cast(Hashable, _normalize_node_id(node_ids[int(snap_indices[int(idx)])]))
    _COORD_NODE_MEMO[memo_key] = node_id
    return node_id


def _snap_node_idx(network_type, lat, lon, cfg: PipelineConfig | None = None):
    """Snap a coordinate to the nearest full-graph node, returning its CSR index.

    Memoized per rounded coordinate. The CSR bundle's KD-tree is built over the full
    (unsimplified) graph node coordinates, so snapping is as precise as it was before
    simplification was ever introduced.
    """
    memo_key = (network_type, round(float(lat), 6), round(float(lon), 6))
    cached = _NODE_IDX_MEMO.get(memo_key)
    if cached is not None:
        return cached
    bundle = graphml.get_mode_csr(network_type, cfg)
    tree = bundle["tree"]
    snap_indices = bundle["snap_indices"]
    m_per_deg_lon, m_per_deg_lat = bundle["scale"]
    _, idx = tree.query([float(lon) * m_per_deg_lon, float(lat) * m_per_deg_lat])
    i = int(snap_indices[int(idx)])
    _NODE_IDX_MEMO[memo_key] = i
    return i


def snap_origin_nodes_by_mode(coords_by_id, cfg: PipelineConfig | None = None, modes=("walk", "bike", "drive")):
    """Batch-snap origin coordinates to each mode's full-graph node index, once.

    Intended to run in the parent so workers reuse the result instead of snapping
    lazily. One vectorized KD-tree query per mode; warms `_NODE_IDX_MEMO`.

    Inputs:
    - coords_by_id: mapping origin id -> `(lat, lon)`.

    Outputs:
    - dict origin id -> {mode: node_index}.
    """
    import numpy as np

    ids = list(coords_by_id.keys())
    result: dict = {nid: {} for nid in ids}
    if not ids:
        return result
    for mode in modes:
        bundle = graphml.get_mode_csr(mode, cfg)
        tree = bundle["tree"]
        snap_indices = bundle["snap_indices"]
        m_per_deg_lon, m_per_deg_lat = bundle["scale"]
        query_xy = np.array(
            [
                [float(coords_by_id[nid][1]) * m_per_deg_lon, float(coords_by_id[nid][0]) * m_per_deg_lat]
                for nid in ids
            ],
            dtype=float,
        )
        _, idxs = tree.query(query_xy)
        for nid, idx in zip(ids, np.atleast_1d(idxs)):
            i = int(snap_indices[int(idx)])
            result[nid][mode] = i
            lat, lon = coords_by_id[nid]
            _NODE_IDX_MEMO[(mode, round(float(lat), 6), round(float(lon), 6))] = i
    return result


def snap_coords_to_mode_nodes(network_type, coords, cfg: PipelineConfig | None = None):
    """Snap `(lat, lon)` coordinates to their nearest mode-graph node coordinates.

    Uses the CSR bundle's KD-tree (built over the FULL graph's node coordinates), so the
    snapped result is identical to `nearest_nodes(full_graph, ...)` but never materializes
    the multi-GB NetworkX graph — only the few-MB CSR bundle. Lets the snapping stage drop
    the full-graph dependency for POIs the same way origin snapping already did.

    Inputs:
    - network_type: mode string ("walk", "bike", "drive").
    - coords: sequence of `(lat, lon)` pairs.

    Outputs:
    - list aligned to `coords` of snapped `(lat, lon)` tuples (EPSG:4326).
    """
    import numpy as np

    coords = list(coords)
    if not coords:
        return []
    bundle = graphml.get_mode_csr(network_type, cfg)
    tree = bundle["tree"]
    snap_indices = bundle["snap_indices"]
    snap_x = bundle["snap_x"]
    snap_y = bundle["snap_y"]
    m_per_deg_lon, m_per_deg_lat = bundle["scale"]
    query_xy = np.array(
        [[float(lon) * m_per_deg_lon, float(lat) * m_per_deg_lat] for (lat, lon) in coords],
        dtype=float,
    )
    _, idxs = tree.query(query_xy)
    out = []
    for idx in np.atleast_1d(idxs):
        real_i = int(snap_indices[int(idx)])
        out.append((float(snap_y[real_i]), float(snap_x[real_i])))
    return out


def _reconstruct_path_idx(predecessors, origin_idx, target_idx):
    """Rebuild an origin->target node-index path from a scipy predecessor array.

    Returns None when the target is unreachable (predecessor -9999) from the source.
    """
    if target_idx == origin_idx:
        return [origin_idx]
    path = [target_idx]
    cur = target_idx
    for _ in range(len(predecessors) + 1):
        p = int(predecessors[cur])
        if p < 0:
            return None
        path.append(p)
        if p == origin_idx:
            path.reverse()
            return path
        cur = p
    return None


def _csr_path_walkability(bundle, path_idx):
    """Length-weighted walkability over a node-index path via the CSR + wscore arrays.

    Mirrors `walkability.compute_path_walkability_from_edges` but reads each segment's
    length and (min-length-edge) walkability score straight from the aligned CSR
    arrays, so no NetworkX graph is needed in the worker. Returns None when the path
    has no scorable length.
    """
    import numpy as np

    wscore = bundle.get("wscore")
    if wscore is None:
        return None
    indptr = bundle["indptr"]
    indices = bundle["indices"]
    length = bundle["length"]
    wsum = 0.0
    lsum = 0.0
    for a, b in zip(path_idx[:-1], path_idx[1:]):
        s = int(indptr[a])
        e = int(indptr[a + 1])
        row = indices[s:e]
        j = int(np.searchsorted(row, b))
        if j >= row.size or int(row[j]) != b:
            continue  # segment not in CSR (should not happen on a real path)
        l_e = float(length[s + j])
        if l_e <= 0:
            continue
        wsum += l_e * float(wscore[s + j])
        lsum += l_e
    if lsum <= 0:
        return None
    return wsum / lsum


def _get_mode_lengths_and_paths(origin, network_type, radius_m, origin_idx=None, cfg: PipelineConfig | None = None, cutoff_m=None):
    """Single-source shortest distances on the mode's CSR graph from one origin.

    Inputs:
    - origin: origin coordinate `(lat, lon)`.
    - network_type: mode key.
    - radius_m: optional routing radius (cache-key only).
    - origin_idx: optional pre-snapped origin node index.
    - cfg: optional PipelineConfig.
    - cutoff_m: optional distance cutoff (meters) passed to scipy as `limit`; nodes
      beyond it come back as `inf`.

    Outputs:
    - tuple `(dist, pred, origin_idx)` where `dist` is a numpy array node_index ->
      distance in meters (`inf` when unreachable/beyond cutoff), and `pred` is the
      scipy predecessor array for path reconstruction (walk only; `None` otherwise).
      Cached per origin index until `reset_origin_caches`.
    """
    import numpy as np
    from scipy.sparse.csgraph import dijkstra

    bundle = graphml.get_mode_csr(network_type, cfg)
    if origin_idx is None:
        origin_idx = _snap_node_idx(network_type, origin[0], origin[1], cfg)

    radius_key = "all" if radius_m is None else f"r{int(radius_m)}"
    cutoff_key = "all" if cutoff_m is None else f"c{int(cutoff_m)}"
    key = (int(origin_idx), network_type, radius_key, cutoff_key)
    if key in _MODE_LENGTHS_CACHE and key in _MODE_PRED_CACHE:
        return (_MODE_LENGTHS_CACHE[key], _MODE_PRED_CACHE[key], origin_idx)

    limit = np.inf if cutoff_m is None else float(cutoff_m)
    want_pred = network_type == "walk"
    out = dijkstra(
        bundle["mat"],
        directed=True,
        indices=int(origin_idx),
        limit=limit,
        return_predecessors=want_pred,
    )
    if want_pred:
        dist, pred = out
    else:
        dist = out
        pred = None
    _MODE_LENGTHS_CACHE[key] = dist
    _MODE_PRED_CACHE[key] = pred
    return (dist, pred, origin_idx)


def build_rra(decay_walk, decay_bike, decay_drive, decay_bus, decay_subway=None):
    """Build per-POI RRA list by combining modal decay lists element-wise.

    Inputs:
    - decay_walk, decay_bike, decay_drive, decay_bus: modal decay arrays.
    - decay_subway: optional extra-modality (subway) decay array. When None, subway is not
      part of the RRA (mode count m=4, unchanged); when provided it adds a 5th mode.

    Outputs:
    - list[float]: merged RRA values for valid entries.
    """
    has_subway = decay_subway is not None
    rra = []
    lengths = [len(decay_walk), len(decay_bike), len(decay_drive), len(decay_bus)]
    if has_subway:
        lengths.append(len(decay_subway))
    n = max(lengths)
    for i in range(n):
        # Mode lists may differ in length (e.g. callers that only supply walk
        # decays and leave bike/drive/bus empty). Treat a missing entry as 0.0,
        # which is a no-op in calculate_rra's 1 - prod(1 - decay) formula.
        dw = decay_walk[i] if i < len(decay_walk) else 0.0
        db = decay_bike[i] if i < len(decay_bike) else 0.0
        dd = decay_drive[i] if i < len(decay_drive) else 0.0
        d_bus = decay_bus[i] if i < len(decay_bus) else 0.0
        d_subway = (decay_subway[i] if i < len(decay_subway) else 0.0) if has_subway else None
        if None not in (dw, db, dd, d_bus):
            rra.append(decay.calculate_rra(dw, db, dd, d_bus, d_subway))
    return rra


def accessibility_from_rra(RRA, poi_type=None, contribution_coefficient=None):
    """Aggregate per-POI RRA values into one accessibility score for a POI type.

    Implements A^i_k(x) = sum_j A^i_k(x, y_j) * Delta g_k(j): per-POI RRAs are
    sorted descending and weighted by the marginal saturation increments Delta g.
    This is the type-level aggregation only; a single POI's accessibility is its
    RRA (computed upstream) and must not be passed through here.

    Inputs:
    - RRA: list of per-POI RRA values.
    - poi_type: optional POI type key (for configured contribution coefficient lookup).
    - contribution_coefficient: optional explicit contribution coefficient.

    Outputs:
    - float accessibility score.
    """
    rra_desc = sorted(RRA, reverse=True)

    def c_from_target(target: float) -> float:
        y_target = 0.9
        if target <= 0:
            raise ValueError(f"contribution_coefficient must be > 0, got {target}")
        return round(math.log(1.0 - y_target) / target, 2)

    def g(x: int, c: float) -> float:
        return 1.0 - math.exp(c * x)

    def deltag(x: int, x2: int, c: float) -> float:
        return g(x, c) - g(x2, c)

    if contribution_coefficient is None:
        if poi_type is not None:
            contribution_coefficient = serv.get_contribution_coefficient(poi_type)
        else:
            contribution_coefficient = 2.0
    c = c_from_target(target=float(contribution_coefficient))

    out = 0.0
    for i, element in enumerate(rra_desc):
        if i == 0:
            out += element * g(i + 1, c)
        else:
            out += element * deltag(i + 1, i, c)
    return out


def accessibility_non_bus_from_snap_map(config: PipelineConfig , poi_type, origine, poi_snap_info_by_mode, feature=None, radius_m=None, tags=None, origin_nodes_by_mode=None):
    """Compute non-bus impedance ingredients for one origin/POI type from snap maps.

    Inputs:
    - poi_type: POI type key.
    - origine: origin coordinate `(lat, lon)`.
    - poi_snap_info_by_mode: snapped candidate map grouped by mode.
    - feature: optional explicit OSM feature key.
    - radius_m: optional routing radius.
    - tags: optional tags filter used for query identity.

    Outputs:
    - dict with cache metadata and non-bus modal impedance arrays.
    """
    _feature, _value, _tags = _resolve_query(poi_type, feature, tags)

    mode_infos = poi_snap_info_by_mode or {}
    source_keys_seen = set()
    source_items: list[dict[str, object]] = []
    for mode in ("walk", "bike", "drive"):
        info = mode_infos.get(mode) or {}
        for src_key, src_info in info.items():
            if src_key in source_keys_seen:
                continue
            source_keys_seen.add(src_key)
            if isinstance(src_info, dict):
                source_coord = src_info.get("source_coord")
            else:
                source_coord = src_key
            if not isinstance(source_coord, (list, tuple)) or len(source_coord) < 2:
                continue
            source_items.append(
                {
                    "source_key": src_key,
                    "source_coord": (float(source_coord[0]), float(source_coord[1])),
                }
            )

    if radius_m is not None:
        source_items = [
            item for item in source_items
            if _haversine_m(origine[0], origine[1], item["source_coord"][0], item["source_coord"][1]) <= radius_m
        ]

    if not source_items:
        return {
            "source_items": [],
            "source_coords": [],
            "imp_walk": [],
            "imp_bike": [],
            "imp_drive": [],
        }

    # Bound Dijkstra exploration to a generous multiple of the POI radius. Source items
    # are already pre-filtered to haversine <= radius_m, so a detour factor above the
    # urban street-network detour ratio keeps every in-radius POI inside the cutoff.
    cutoff_m = None
    if radius_m is not None:
        cutoff_m = float(radius_m) * float(getattr(config, "non_bus_dijkstra_detour_factor", 1.6))

    mode_distances = {}
    walk_pred = None
    walk_origin_idx = None
    walk_bundle = None
    walk_paths_scores = []
    walk_poi_idxs: list = []
    for mode in ("walk", "bike", "drive"):
        info = mode_infos.get(mode) or {}
        chosen_coords = []
        for item in source_items:
            src = item["source_coord"]
            src_key = item["source_key"]
            chosen_coords.append(_select_best_snap_for_origin(origine, src, info.get(src_key)))
        try:
            if origin_nodes_by_mode and mode in origin_nodes_by_mode:
                origin_idx = origin_nodes_by_mode[mode]
            else:
                origin_idx = _snap_node_idx(mode, origine[0], origine[1], config)
            # Snap each POI precisely to the nearest full-graph node (exact, no chains).
            poi_idxs = [
                _snap_node_idx(mode, coord[0], coord[1], config)
                for coord in chosen_coords
            ]

            dist, pred, _ = _get_mode_lengths_and_paths(
                origine, mode, radius_m, origin_idx=origin_idx, cfg=config, cutoff_m=cutoff_m
            )

            # dist is keyed by node index; values are exact full-graph metres, inf
            # beyond the cutoff/unreachable.
            leg = []
            for pidx in poi_idxs:
                d = dist[pidx]
                leg.append(float(d) if math.isfinite(d) else None)
            mode_distances[mode] = leg

            if mode == "walk":
                walk_pred = pred
                walk_origin_idx = origin_idx
                walk_bundle = graphml.get_mode_csr(mode, config)
                walk_poi_idxs = poi_idxs

        except (KeyError, ValueError, TypeError, IndexError, MemoryError) as exc:
            logger.warning("Non-bus routing fallback: mode=%s origin=%s reason=%s", mode, origine, exc)
            mode_distances[mode] = [None] * len(source_items)

    imp_walk = []
    imp_bike = []
    imp_drive = []

    for i in range(len(source_items)):
        iw = ib = idr = None

        # Walkability score of the origin->POI walk path, read straight from the CSR
        # length/score arrays (no NetworkX graph needed in the worker).
        w_i = None
        if (
            walk_bundle is not None
            and walk_pred is not None
            and i < len(walk_poi_idxs)
        ):
            path_idx = _reconstruct_path_idx(walk_pred, walk_origin_idx, walk_poi_idxs[i])
            if path_idx and len(path_idx) >= 2:
                w_i = _csr_path_walkability(walk_bundle, path_idx)
        walk_paths_scores.append(w_i)

        for mode in ("walk", "bike", "drive"):
            dist_m = mode_distances[mode][i]
            if dist_m is None:
                continue

            dist_km = dist_m / 1000.0
            if mode == "walk":
                imp = get_impedance.impedance_base(
                    dist_km, mode, w_i,
                    vot=config.vot,
                    cost_per_liter=config.cost_per_liter,
                    distance_for_liter=config.distance_for_liter,
                    speed_walk_kmh=config.speed_walk_kmh,
                    speed_bike_kmh=config.speed_bike_kmh,
                    speed_drive_kmh=config.speed_drive_kmh,
                    drive_access_time_min=config.drive_access_time_min,
                )
            else:
                imp = get_impedance.impedance_base(
                    dist_km, mode,
                    vot=config.vot,
                    cost_per_liter=config.cost_per_liter,
                    distance_for_liter=config.distance_for_liter,
                    speed_walk_kmh=config.speed_walk_kmh,
                    speed_bike_kmh=config.speed_bike_kmh,
                    speed_drive_kmh=config.speed_drive_kmh,
                    drive_access_time_min=config.drive_access_time_min,
                )
            
            if imp is None:
                continue
            if mode == "walk":
                iw = imp
            elif mode == "bike":
                ib = imp
            else:
                idr = imp
        imp_walk.append(iw)
        imp_bike.append(ib)
        imp_drive.append(idr)

    return {
        "source_items": source_items,
        "source_coords": [item["source_coord"] for item in source_items],
        "imp_walk": imp_walk,
        "imp_bike": imp_bike,
        "imp_drive": imp_drive,
        "walk_path_scores": walk_paths_scores
    }


def merge_rra_and_accessibility(decay_walk, decay_bike, decay_drive, decay_bus, poi_type=None, contribution_coefficient=None, decay_subway=None):
    """Compute both RRA list and final accessibility from modal decays.

    Inputs:
    - decay_walk, decay_bike, decay_drive, decay_bus: modal decay arrays.
    - poi_type: optional POI type key.
    - contribution_coefficient: optional explicit contribution coefficient.
    - decay_subway: optional extra-modality (subway) decay array; see build_rra.

    Outputs:
    - tuple `(rra_list, accessibility_value)`.
    """
    rra = build_rra(decay_walk, decay_bike, decay_drive, decay_bus, decay_subway)
    return rra, accessibility_from_rra(rra, poi_type=poi_type, contribution_coefficient=contribution_coefficient)
