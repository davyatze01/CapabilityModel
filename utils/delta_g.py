from utils import graphml, decay, get_impedance, services as serv
import math
import os
import csv
import json
import hashlib
from collections import OrderedDict
from typing import Hashable, TypeGuard, cast
import networkx as nx
from shapely.geometry.base import BaseGeometry
import logging
import plotting.walkability as walkability
from core.config import PipelineConfig
logger = logging.getLogger(__name__)

# Shared with graphml's walk CSR wscore placeholder -- see WALK_EDGE_DEFAULT_SCORE's
# docstring and the comment in accessibility_non_bus_from_snap_map's w_i assignment.
DEFAULT_WALK_SCORE = graphml.WALK_EDGE_DEFAULT_SCORE

# Kept because main.py uses this in-memory geometry cache while building snap maps.
POI_GEOM_CACHE_FOLDER = "poi_geom_cache"
os.makedirs(POI_GEOM_CACHE_FOLDER, exist_ok=True)

_POI_GEOM_CACHE = {}  # key: (feature, value) -> list geometries
_MODE_GRAPH_CACHE = {}  # key: network_type -> graph
_MODE_LENGTHS_CACHE = {}  # key: (origin, network_type, radius_key) -> dict node->distance

# When True, edge `geometry` is dropped from mode graphs right after load to slash
# per-worker RAM. Only safe in worker processes: the walkability signature is
# overridden there and the edge-walkability index is loaded from disk, so geometry
# is never read again. The parent process (which builds that index) must keep it.
_STRIP_GRAPH_GEOMETRY = False
_MODE_NODE_KDTREE = {}  # key: network_type -> (cKDTree over projected (x,y) meters, node_id ndarray)


class _BoundedMemo(OrderedDict):
    """Insertion-ordered dict capped at `maxsize`, evicting oldest entries (FIFO).

    `_COORD_NODE_MEMO`/`_NODE_IDX_MEMO` below are process-lifetime caches (only
    `_MODE_LENGTHS_CACHE` is cleared per origin in `reset_origin_caches`), keyed by
    rounded POI coordinates. A single Paris origin can pull in over a million distinct
    POI-coordinate lookups, so an unbounded plain dict grows without limit across a
    worker's life -- a contributor to the OOM kills of 2026-07-15/16. A fixed cap keeps
    it bounded, in line with this repo's fail-fast/bounded-resource preference.

    CRITICAL: eviction MUST be O(1). The first version subclassed `dict` and evicted
    via `next(iter(self))` + `del`. On a dict where the front entry is repeatedly
    deleted and new keys appended, the internal table fills with tombstones at the
    front, so `iter()` scans past all of them to reach the first live key -- making
    each eviction O(n). With the cap far below one origin's working set the cache was
    permanently full (100% evict-on-insert), and this O(n) scan measured as 262s of a
    413s / 4-origin profile (63% of total runtime, 2026-07-16). `OrderedDict.popitem(
    last=False)` pops the oldest in O(1) with no tombstone scan, eliminating that cost.
    """

    def __init__(self, maxsize: int):
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key, value):
        if key in self:
            super().__setitem__(key, value)
            return
        if len(self) >= self._maxsize:
            self.popitem(last=False)  # O(1) FIFO eviction, no tombstone scan
        super().__setitem__(key, value)


# Memory-vs-hit-rate knob. Now that eviction is O(1) (see _BoundedMemo) a cache miss
# just costs one cheap KD-tree query (profiled at ~1.9s self / 5.6M calls even near the
# thrash regime) instead of the old O(n) cliff, so this can stay modest to protect
# memory. At ~196 bytes/entry (tracemalloc, 2026-07-16) 1M entries is ~196MB/cache, so
# ~0.4GB for both caches per worker -- small enough to run ~16-24 concurrent workers
# under the 45G cap alongside the ~1.8GB/origin transient working set (the real memory
# driver, bounded separately via non_bus_max_workers). Raise only if a fresh profile
# shows _snap_node_idx re-climbing due to cross-origin misses at this cap.
_MEMO_CACHE_MAXSIZE = 1_000_000
_COORD_NODE_MEMO = _BoundedMemo(_MEMO_CACHE_MAXSIZE)  # key: (network_type, round(lat,6), round(lon,6)) -> nearest node id
_NODE_IDX_MEMO = _BoundedMemo(_MEMO_CACHE_MAXSIZE)  # key: (network_type, round(lat,6), round(lon,6)) -> full-graph CSR node index
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


def _haversine_m_np(lat1, lon1, lat2, lon2):
    """Array version of _haversine_m: a scalar point vs an array (broadcasts), or two
    same-shape arrays compared elementwise -- verified bit-identical to the old
    scalar-origin-only version for that existing use case."""
    import numpy as np

    r = 6371000.0
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2.0) ** 2
    return 2.0 * r * np.arctan2(np.sqrt(a), np.sqrt(1.0 - a))


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


def build_snap_compact(poi_mode_snap_info_by_type):
    """Collapse the nested per-mode snap dict into compact, refcount-immune arrays.

    The routing stage previously handed every worker
    `poi_mode_snap_info_by_type` = {poi_key: {mode: {src_key: {source_coord, candidates}}}}
    -- ~10M small Python objects (~1.4 GB live from a 186 MB pickle), duplicated three
    times across walk/bike/drive even though the three modes carry *identical* src_keys,
    coords and candidates. Forked workers then fault the whole thing private via refcount
    writes as they traverse it, so each of N workers ends up with a near-private 1.4 GB
    copy. This rebuilds it, once in the parent, as a per-poi_key bundle of numpy arrays
    plus one src_key list:

      src_keys:      bytes  (N,) 'S' dtype    fixed-width ASCII, mode-independent
      source_coords: float64 (N, 2)
      cand_coords:   float64 (M, 2)           all candidates, concatenated
      cand_dists:    float64 (M,)             matching snap distances
      cand_ptr:      int64  (N + 1,)          CSR offsets; src i -> [ptr[i]:ptr[i+1]]

    Traversing a numpy array does not touch per-element Python refcounts, so the numeric
    bulk stays genuinely shared copy-on-write across all forked workers (~186 MB total
    instead of N x 1.4 GB), and worker respawns (maxtasksperchild) re-share it for free.
    Order matches the old dedup exactly: src_keys are taken in `walk`'s insertion order
    (identical across modes), skipping any src with an invalid source_coord -- so
    `accessibility_non_bus_from_snap_map`'s source_items line up positionally as before.
    """
    import numpy as np

    compact: dict = {}
    for poi_key, mode_infos in (poi_mode_snap_info_by_type or {}).items():
        info = mode_infos.get("walk")
        if info is None:
            info = next(iter(mode_infos.values()), {}) if mode_infos else {}
        src_keys: list = []
        coords: list = []
        cand_coords: list = []
        cand_dists: list = []
        cand_ptr: list = [0]
        for src_key, src_info in info.items():
            if isinstance(src_info, dict):
                source_coord = src_info.get("source_coord")
                candidates = src_info.get("candidates")
            else:
                source_coord = src_key
                candidates = None
            if not isinstance(source_coord, (list, tuple)) or len(source_coord) < 2:
                continue
            src_keys.append(src_key)
            coords.append((float(source_coord[0]), float(source_coord[1])))
            m = 0
            if isinstance(candidates, list):
                for cand in candidates:
                    if not isinstance(cand, (list, tuple)) or len(cand) < 2:
                        continue
                    cc = cand[0]
                    cand_coords.append((float(cc[0]), float(cc[1])))
                    cand_dists.append(float(cand[1]))
                    m += 1
            cand_ptr.append(cand_ptr[-1] + m)
        compact[poi_key] = {
            # Fixed-width ASCII bytes array (numpy auto-sizes the width), so the src_keys
            # are refcount-immune too -- the whole bundle is now numpy and fork shares it
            # genuinely COW across every worker. Decoded back to str per kept POI at
            # read time (a per-origin transient, not the static floor).
            "src_keys": np.asarray(src_keys, dtype="S"),
            "source_coords": np.asarray(coords, dtype=np.float64).reshape(-1, 2),
            "cand_coords": np.asarray(cand_coords, dtype=np.float64).reshape(-1, 2),
            "cand_dists": np.asarray(cand_dists, dtype=np.float64),
            "cand_ptr": np.asarray(cand_ptr, dtype=np.int64),
        }
    return compact


def _best_snap_candidate_coord(origin, coord, cand_coords, cand_dists):
    """Origin-dependent best snapped coordinate among a src's candidates.

    Bit-identical to snapping_stage._select_best_snap_candidate_for_origin's list branch
    (min by (haversine-to-origin, snap_distance); falls back to the raw coord when there
    are no candidates), but reads the candidates straight from the compact CSR arrays so
    no per-src Python dict/list has to exist in the worker.
    """
    best = None
    for k in range(cand_coords.shape[0]):
        cand_coord = (float(cand_coords[k, 0]), float(cand_coords[k, 1]))
        origin_dist_m = _haversine_m(origin[0], origin[1], cand_coord[0], cand_coord[1])
        score = (origin_dist_m, float(cand_dists[k]))
        if best is None or score < best[0]:
            best = (score, cand_coord)
    if best is not None:
        return best[1]
    return (float(coord[0]), float(coord[1]))


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

    `_MODE_LENGTHS_CACHE` only ever needs the origin currently being processed (all
    POI queries for one origin share the same key). Clearing it per origin caps a
    worker's memory at one origin's data instead of letting it grow without bound
    across every origin the worker handles.
    """
    _MODE_LENGTHS_CACHE.clear()


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
      distance in meters (`inf` when unreachable/beyond cutoff). `pred` is always
      `None` -- predecessor arrays (for path reconstruction) are not computed. They
      used to feed per-path walkability averaging (see the `w_i` comment in
      `accessibility_non_bus_from_snap_map`), which is currently disabled because
      every edge's walkability score is a flat placeholder constant (see
      `graphml._build_mode_csr_streaming`'s `wscore`), making path-dependent
      averaging a no-op that nonetheless cost ~94% of a Paris origin's routing time
      (profiled 2026-07-15: 523 of 555s/origin in path reconstruction + averaging).
      Re-enable `return_predecessors=True` here if real per-edge walkability data is
      ever wired into `wscore`.
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
    if key in _MODE_LENGTHS_CACHE:
        return (_MODE_LENGTHS_CACHE[key], None, origin_idx)

    limit = np.inf if cutoff_m is None else float(cutoff_m)
    dist = dijkstra(
        bundle["mat"],
        directed=True,
        indices=int(origin_idx),
        limit=limit,
        return_predecessors=False,
    )
    _MODE_LENGTHS_CACHE[key] = dist
    return (dist, None, origin_idx)


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


_DEST_COL_MAP: dict[tuple[float, float], int] | None = None


def _dest_col_map(config: PipelineConfig):
    """Lazily build {rounded (lat, lon) -> impedance-matrix column}, shared by bus and subway.

    Bus and subway route against the same snapped destination set, so a single column
    addresses both matrices -- only the values behind it differ. The check below turns that
    shared assumption into a loud failure if a city ever routes metro against a different
    destination set.

    Cached per worker process: the map is ~140k entries and every origin needs it. Rounding
    matches accessibility_stage's old _BUS_DEST_COORD_TO_COL exactly -- a POI must land on
    the same column on the write side as the read side, or its transit impedance silently
    comes from the wrong stop.
    """
    global _DEST_COL_MAP
    if _DEST_COL_MAP is not None:
        return _DEST_COL_MAP

    def _load(dest_id_to_col_path, dest_csv_path):
        with open(dest_id_to_col_path, encoding="utf-8") as f:
            id_to_col = {str(k): int(v) for k, v in json.load(f).items()}
        out: dict[tuple[float, float], int] = {}
        with open(dest_csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                col = id_to_col.get(row["id"])
                if col is not None:
                    out[(round(float(row["lat"]), 6), round(float(row["lon"]), 6))] = int(col)
        return out

    bus = _load(config.bus_dest_id_to_col_path, config.bus_routing_destinations_input_path)
    if config.enable_subway:
        subway = _load(config.subway_dest_id_to_col_path, config.subway_routing_destinations_input_path)
        if subway != bus:
            raise RuntimeError(
                "Subway routes against a different destination set than bus, so one dest_col "
                "can no longer address both impedance matrices."
            )
    _DEST_COL_MAP = bus
    return _DEST_COL_MAP


def accessibility_non_bus_from_snap_map(config: PipelineConfig , poi_type, origine, snap_compact, feature=None, radius_m=None, tags=None, origin_nodes_by_mode=None):
    """Compute non-bus impedance ingredients for one origin/POI type.

    Inputs:
    - poi_type: POI type key.
    - origine: origin coordinate `(lat, lon)`.
    - snap_compact: this poi_key's compact snap bundle from `build_snap_compact`
      (src_keys list + source_coords / cand_coords / cand_dists / cand_ptr arrays).
    - feature: optional explicit OSM feature key.
    - radius_m: optional routing radius.
    - tags: optional tags filter used for query identity.

    Outputs:
    - dict with per-POI impedance arrays plus the resolved addressing they are read
      through -- `dest_col` (the origin-snapped column addressing BOTH the bus and subway
      matrices, -1 when the POI reaches no stop) and `in_radius` -- all positionally
      aligned with `kept_idx`, which indexes the shared per-poi_type catalog.
    """
    import numpy as np

    _feature, _value, _tags = _resolve_query(poi_type, feature, tags)

    compact = snap_compact or {}
    src_keys_all = compact.get("src_keys")
    source_coords_all = compact.get("source_coords")
    # src_keys_all is a numpy bytes array (or None) -- avoid array truthiness.
    n_all = 0 if src_keys_all is None else len(src_keys_all)

    empty = {
        "kept_idx": np.empty(0, dtype=np.int32),
        "dest_col": np.empty(0, dtype=np.int32),
        "in_radius": np.empty(0, dtype=bool),
        "imp_walk": np.empty(0, dtype=np.float32),
        "imp_bike": np.empty(0, dtype=np.float32),
        "imp_drive": np.empty(0, dtype=np.float32),
    }
    if n_all == 0 or source_coords_all is None or len(source_coords_all) == 0:
        return empty

    # Individual-profile runs narrow the routed non-bus modes (e.g. walk-only for someone
    # with no car/bike). Baseline keeps all three. Disabled modes are never routed, so
    # their impedance arrays stay all-None downstream.
    enabled_modes = tuple(
        m for m in ("walk", "bike", "drive")
        if m in getattr(config, "enabled_non_bus_modes", ("walk", "bike", "drive"))
    )

    # Radius filter, preserving src order. Kept on the scalar _haversine_m (same math as
    # before) so a POI right at the radius boundary lands on the exact same side as the
    # old per-item filter -- vectorised trig could differ by an ULP and flip membership.
    if radius_m is not None:
        kept = [
            i for i in range(n_all)
            if _haversine_m(origine[0], origine[1],
                            float(source_coords_all[i, 0]), float(source_coords_all[i, 1])) <= radius_m
        ]
        kept_idx = np.asarray(kept, dtype=np.intp)
    else:
        kept_idx = np.arange(n_all, dtype=np.intp)

    n = int(kept_idx.shape[0])
    if n == 0:
        return empty

    source_coords = source_coords_all[kept_idx]  # (n, 2), routed coord == source_coord

    # Bound Dijkstra exploration to a generous multiple of the POI radius. Sources are
    # already pre-filtered to haversine <= radius_m, so a detour factor above the urban
    # street-network detour ratio keeps every in-radius POI inside the cutoff.
    cutoff_m = None
    if radius_m is not None:
        cutoff_m = float(radius_m) * float(getattr(config, "non_bus_dijkstra_detour_factor", 1.6))

    # Vectorised over all POIs of this (origin, poi_type). Per mode: one batched KD-tree
    # query snaps every POI to its nearest full-graph node (same tree/metric/snap_indices
    # as _snap_node_idx, so indices are identical), then one fancy-index into the Dijkstra
    # distance array + one elementwise impedance (closed forms ported verbatim from
    # get_impedance.impedance_base, default lambda_walk=0.15). The previous per-item loop
    # (n snaps + 3n scalar calls, all boxed Python objects) dominated runtime and the
    # per-origin transient for dense types (perceived_nature ~ 280k in-radius POIs).
    imp_by_mode: dict[str, np.ndarray] = {
        m: np.full(n, np.nan, dtype=np.float32) for m in ("walk", "bike", "drive")
    }

    for mode in enabled_modes:
        try:
            if origin_nodes_by_mode and mode in origin_nodes_by_mode:
                origin_idx = origin_nodes_by_mode[mode]
            else:
                origin_idx = _snap_node_idx(mode, origine[0], origine[1], config)

            bundle = graphml.get_mode_csr(mode, config)
            tree = bundle["tree"]
            snap_indices = bundle["snap_indices"]
            m_per_deg_lon, m_per_deg_lat = bundle["scale"]
            query_xy = np.column_stack(
                (source_coords[:, 1] * m_per_deg_lon, source_coords[:, 0] * m_per_deg_lat)
            )
            _, knn = tree.query(query_xy)
            poi_idxs = np.asarray(snap_indices)[np.asarray(knn, dtype=np.intp)].astype(np.intp, copy=False)

            dist, _pred, _ = _get_mode_lengths_and_paths(
                origine, mode, radius_m, origin_idx=origin_idx, cfg=config, cutoff_m=cutoff_m
            )
        except (KeyError, ValueError, TypeError, IndexError, MemoryError) as exc:
            logger.warning("Non-bus routing fallback: mode=%s origin=%s reason=%s", mode, origine, exc)
            continue  # leaves imp_by_mode[mode] all-None, matching the old fallback

        # dist is keyed by node index; values are exact full-graph metres, inf beyond the
        # cutoff/unreachable.
        dist_m = np.asarray(dist)[poi_idxs]
        reachable = np.isfinite(dist_m)
        dist_km = dist_m / 1000.0

        # Last-mile snap gaps: dist_km above is snapped-node to snapped-node, not
        # true-coordinate to true-coordinate. Add the real gap at each end -- origin's true
        # position to its own snapped node (one distance, shared by every POI this mode),
        # and each POI's true position to ITS OWN snapped node (per-POI, vectorized).
        origin_gap_km = _haversine_m(
            origine[0], origine[1],
            float(bundle["snap_y"][origin_idx]), float(bundle["snap_x"][origin_idx]),
        ) / 1000.0
        poi_gap_km = _haversine_m_np(
            source_coords[:, 0], source_coords[:, 1],
            bundle["snap_y"][poi_idxs], bundle["snap_x"][poi_idxs],
        ) / 1000.0

        if mode == "walk":
            # Real per-edge walkability isn't wired into the CSR yet -- every edge carries
            # the same placeholder score (WALK_EDGE_DEFAULT_SCORE), so the length-weighted
            # average along ANY path is provably that same constant, making the walkability
            # coefficient a single scalar (this used to cost ~94% of a Paris origin's time
            # via path reconstruction; profiled 523/555s/origin 2026-07-15). Re-derive
            # per-path scores here if real wscore data is ever added.
            coef = 1.0 + 0.15 * ((5.0 - float(DEFAULT_WALK_SCORE)) / 4.0)
            imp = coef * (((dist_km + origin_gap_km + poi_gap_km) / config.speed_walk_kmh) * 60.0)
        elif mode == "bike":
            imp = ((dist_km + origin_gap_km + poi_gap_km) / config.speed_bike_kmh) * 60.0
        else:  # drive
            monetary_cost = config.cost_per_liter / config.distance_for_liter
            # Last-mile gaps are walked (to/from the car), not driven -- speed_walk_kmh,
            # not speed_drive_kmh, for the snap-distance portion only.
            snap_gap_min = ((origin_gap_km + poi_gap_km) / config.speed_walk_kmh) * 60.0
            imp = (
                config.drive_access_time_min
                + (dist_km / config.speed_drive_kmh) * 60.0
                + snap_gap_min
                + config.vot * monetary_cost
            )

        imp_by_mode[mode][reachable] = imp[reachable].astype(np.float32)

    imp_walk = imp_by_mode["walk"]
    imp_bike = imp_by_mode["bike"]
    imp_drive = imp_by_mode["drive"]

    # Origin-dependent best bus-snapped destination coord per POI, read straight from the
    # compact CSR candidate arrays (bit-identical to the old
    # _process_node -> select_best_snap_candidate_for_origin path). Candidates are indexed
    # by the ORIGINAL src position, so use kept_idx, not the filtered position.
    cand_coords = compact.get("cand_coords")
    cand_dists = compact.get("cand_dists")
    cand_ptr = compact.get("cand_ptr")
    poi_coords = np.empty((n, 2), dtype=np.float64)
    for pos, i in enumerate(kept_idx.tolist()):
        sc = (float(source_coords_all[i, 0]), float(source_coords_all[i, 1]))
        if cand_ptr is not None and cand_coords is not None:
            a = int(cand_ptr[i]); b = int(cand_ptr[i + 1])
            poi_coords[pos] = _best_snap_candidate_coord(origine, sc, cand_coords[a:b], cand_dists[a:b])
        else:
            poi_coords[pos] = sc

    # Resolve here what the accessibility stage used to re-derive from these coords on every
    # read: which bus/subway matrix column this POI reads, and whether its SNAPPED position
    # passes the radius test (kept_idx above filtered on the SOURCE position, so the two
    # differ for POIs that snap across the boundary). Storing the resolved integers instead
    # of the coords is what keeps the impedance artifact from shipping duplicated geometry.
    dest_map = _dest_col_map(config)

    if radius_m is not None:
        in_radius = _haversine_m_np(origine[0], origine[1], poi_coords[:, 0], poi_coords[:, 1]) <= radius_m
    else:
        in_radius = np.ones(n, dtype=bool)

    dest_col = np.full(n, -1, dtype=np.int32)
    for pos in range(n):
        coord_key = (round(float(poi_coords[pos, 0]), 6), round(float(poi_coords[pos, 1]), 6))
        dc = dest_map.get(coord_key)
        if dc is not None:
            dest_col[pos] = dc

    return {
        "kept_idx": kept_idx.astype(np.int32),
        "dest_col": dest_col,
        "in_radius": in_radius,
        "imp_walk": imp_walk,
        "imp_bike": imp_bike,
        "imp_drive": imp_drive,
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
