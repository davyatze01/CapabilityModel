import osmnx as ox
import os
import math
import warnings
import geopandas as gpd
import pandas as pd
import hashlib
import json
import numpy as np
from shapely.geometry import shape
import threading
import time
import xml.etree.ElementTree as ET
from typing import TypeAlias

from utils.load_shapefile import poi_from_shp

from core.config import PipelineConfig
from utils.load_shapefile import graph_from_shapefile, feature_from_shapefile
from utils.poi_identity import build_poi_source_key

# Placeholder per-edge walkability score used until real OSM-derived walkability
# data is wired into the walk CSR's `wscore` array. Every walk edge currently gets
# this same constant (see `_build_mode_csr_streaming`), and `utils.delta_g` reuses
# it directly as the walk-path score instead of reconstructing and averaging paths
# that are guaranteed to all average out to this same value.
WALK_EDGE_DEFAULT_SCORE: float = 5.0

TagValue: TypeAlias = bool | str | list[str]
TagClause: TypeAlias = dict[str, TagValue]
TagQuery: TypeAlias = TagClause | list[TagClause]

class _ElapsedTimer:
    """Context manager that prints a live elapsed-time ticker on a background thread.

    Usage:
        with _ElapsedTimer("Loading walk graph"):
            graph = ox.io.load_graphml(path)

    Prints:  [Loading walk graph] 1s ...   (updates in-place via \\r)
    On exit: [Loading walk graph] done in 42.3s
    """

    def __init__(self, label: str) -> None:
        self._label = label
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._tick, daemon=True)

    def _tick(self) -> None:
        start = time.monotonic()
        while not self._stop.wait(timeout=1.0):
            elapsed = int(time.monotonic() - start)
            print(f"\r[{self._label}] {elapsed}s elapsed...", end="", flush=True)
        elapsed = time.monotonic() - start
        print(f"\r[{self._label}] done in {elapsed:.1f}s" + " " * 20, flush=True)

    def __enter__(self) -> "_ElapsedTimer":
        print(f"[{self._label}] starting...", flush=True)
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._stop.set()
        self._thread.join()


# In-memory caches to avoid repeated disk loads
_GRAPH_CACHE = None
_MODE_GRAPH_CACHE = {}
_CITY_POI_UNIVERSE_CACHE: dict[str, gpd.GeoDataFrame] = {}
_POI_DOWNLOAD_LOGGED: set[tuple[str, str]] = set()


def clear_mode_graph_cache() -> None:
    """Drop cached full NetworkX mode graphs to release their (multi-GB) RAM.

    Routing workers run off the compact CSR bundles (`_MODE_CSR_CACHE`), not the full
    graphs, so once snapping / CSR-building / walkability warmup are done in the parent
    the full graphs are dead weight. Clearing them before the worker pool forks keeps
    the inherited (copy-on-write) baseline tiny and prevents OOM. The CSR cache is left
    intact. Safe to call repeatedly; graphs are lazily rebuilt on demand if needed.
    """
    global _GRAPH_CACHE
    _MODE_GRAPH_CACHE.clear()
    _GRAPH_CACHE = None


def _get_city_settings() -> tuple[str, str]:
    cfg = PipelineConfig()
    return cfg.city_name, cfg.city_slug


def _get_runtime_settings() -> tuple[str, str, str]:
    cfg = PipelineConfig()
    return cfg.city_name, cfg.city_slug, cfg.artifact_slug


def get_graph():
    """Load or download the base graph and cache it in memory.

    Inputs:
    - none.

    Outputs:
    - graph object representing the default study-area network.
    """

    # Nomi file
    place_name, city_slug = _get_city_settings()
    name_file = f"graph/{city_slug}.graphml"

    # Se esiste lo carico altrimenti creo e salvo

    global _GRAPH_CACHE
    if _GRAPH_CACHE is not None:
        return _GRAPH_CACHE

    graph = None
    try:
        with _ElapsedTimer(f"Graph load {name_file}"):
            graph = ox.io.load_graphml(name_file)
    except Exception:
        print(f"[Graph] Cached graph not found. Downloading graph for {place_name}...", flush=True)
        with _ElapsedTimer(f"Graph download {place_name}"):
            graph = ox.graph_from_place(place_name)
        with _ElapsedTimer(f"Graph save {name_file}"):
            ox.io.save_graphml(graph, filepath=name_file)
        print(f"[Graph] Graph downloaded and saved: {name_file}", flush=True)

    _GRAPH_CACHE = graph
    return graph


def _resolve_mode_graph_path(network_type, cfg: PipelineConfig) -> str:
    """Return the on-disk path of the full (unsimplified) mode graph cache file."""
    if cfg.use_shapefile:
        return f"graph/{cfg.artifact_slug}_{network_type}.graphml"

    city_slug = cfg.city_slug
    from utils.services import get_global_radius_m
    buffer_m = get_global_radius_m(cfg) or 0.0
    simplify_tag = "s" if cfg.osm_autobuild_simplify else "ns"
    retain_tag = "ra" if cfg.osm_autobuild_retain_all else ""
    graph_tag = f"_{simplify_tag}{retain_tag}" if retain_tag else f"_{simplify_tag}"
    if buffer_m > 0:
        return f"graph/{city_slug}_{network_type}_buf{buffer_m:.0f}m{graph_tag}.graphml"
    return f"graph/{city_slug}_{network_type}{graph_tag}.graphml"


# In-memory cache of compact CSR routing bundles, one per mode.
_MODE_CSR_CACHE: dict[str, object] = {}


def _xml_local_name(tag: str) -> str:
    """Return an XML tag name without its namespace."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse_float(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_int(value: str | None, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in meters. Local copy of utils.delta_g's version --
    graphml.py can't import delta_g (delta_g already imports graphml)."""
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _build_mode_csr_streaming(
    network_type: str,
    full_path: str,
    csr_path: str,
    cfg: PipelineConfig | None = None,
) -> dict[str, object]:
    """Build a CSR bundle from GraphML without materializing a NetworkX graph.

    The normal CSR artifact is tiny compared with the full OSMnx NetworkX graph, but
    the old first-run path loaded the full graph and then built large Python
    intermediates. This parser keeps only node coordinates and the shortest directed
    edge for each `(u, v)` pair, which is sufficient for length-minimizing Dijkstra.
    """
    from scipy.sparse import csr_matrix

    key_to_attr: dict[str, str] = {}
    nodes: list[tuple[int, float, float]] = []
    node_idx: dict[int, int] = {}
    best_edges: dict[tuple[int, int], float] = {}

    context = ET.iterparse(full_path, events=("start", "end"))
    graph_elem = None
    for event, elem in context:
        name = _xml_local_name(elem.tag)
        if event == "start":
            if name == "graph":
                graph_elem = elem
            continue

        if name == "key":
            key_id = elem.attrib.get("id")
            attr_name = elem.attrib.get("attr.name")
            if key_id and attr_name:
                key_to_attr[key_id] = attr_name
            elem.clear()
            continue

        if name == "node":
            node_id = _parse_int(elem.attrib.get("id"))
            x = y = None
            for child in elem:
                if _xml_local_name(child.tag) != "data":
                    continue
                attr = key_to_attr.get(child.attrib.get("key", ""))
                if attr == "x":
                    x = _parse_float(child.text, default=float("nan"))
                elif attr == "y":
                    y = _parse_float(child.text, default=float("nan"))
            if x is not None and y is not None and math.isfinite(x) and math.isfinite(y):
                node_idx[node_id] = len(nodes)
                nodes.append((node_id, x, y))
            elem.clear()
            if graph_elem is not None:
                try:
                    graph_elem.remove(elem)
                except ValueError:
                    pass
            continue

        if name == "edge":
            source_id = _parse_int(elem.attrib.get("source"))
            target_id = _parse_int(elem.attrib.get("target"))
            length_val = 0.0
            for child in elem:
                if _xml_local_name(child.tag) != "data":
                    continue
                attr = key_to_attr.get(child.attrib.get("key", ""))
                if attr == "length":
                    length_val = _parse_float(child.text, default=0.0)
                    break
            u_idx = node_idx.get(source_id)
            v_idx = node_idx.get(target_id)
            if u_idx is not None and v_idx is not None and math.isfinite(length_val):
                pair = (u_idx, v_idx)
                previous = best_edges.get(pair)
                if previous is None or length_val < previous:
                    best_edges[pair] = length_val
            elem.clear()
            if graph_elem is not None:
                try:
                    graph_elem.remove(elem)
                except ValueError:
                    pass

    real_node_count = len(nodes)
    node_ids = np.asarray([node_id for node_id, _x, _y in nodes], dtype=np.int64)
    snap_x = np.asarray([x for _node_id, x, _y in nodes], dtype=float)
    snap_y = np.asarray([y for _node_id, _x, y in nodes], dtype=float)
    snap_indices = np.arange(real_node_count, dtype=np.int64)

    if best_edges:
        rows, cols = zip(*best_edges.keys())
        rows_arr = np.asarray(rows, dtype=np.int64)
        cols_arr = np.asarray(cols, dtype=np.int64)
        length_vals = np.asarray(list(best_edges.values()), dtype=float)
    else:
        rows_arr = np.asarray([], dtype=np.int64)
        cols_arr = np.asarray([], dtype=np.int64)
        length_vals = np.asarray([], dtype=float)

    # Bridge every disconnected fragment (dangling footway stubs, unlinked path segments --
    # common OSM gaps) into the main component with ONE synthetic edge each, rather than
    # excluding them as snap candidates. Excluding them would break origin<->POI pairs that
    # are genuinely co-located inside the SAME small fragment (their real, short internal
    # path would vanish); bridging instead lets Dijkstra keep using the true internal path
    # when it's shorter, and only fall back to the bridge (detour-inflated straight-line
    # estimate) when nothing else connects the two ends.
    if real_node_count > 0:
        from scipy.sparse.csgraph import connected_components
        from scipy.spatial import cKDTree

        prelim = csr_matrix(
            (length_vals, (rows_arr, cols_arr)), shape=(real_node_count, real_node_count)
        )
        n_comp, labels = connected_components(prelim, directed=False)
        if n_comp > 1:
            sizes = np.bincount(labels)
            main_label = int(np.argmax(sizes))
            main_mask = labels == main_label
            main_idx = np.nonzero(main_mask)[0]
            other_idx = np.nonzero(~main_mask)[0]

            mean_lat_rad = math.radians(float(snap_y.mean())) if snap_y.size else 0.0
            m_per_deg_lat_tmp = 111320.0
            m_per_deg_lon_tmp = 111320.0 * math.cos(mean_lat_rad)
            main_xy = np.column_stack(
                [snap_x[main_idx] * m_per_deg_lon_tmp, snap_y[main_idx] * m_per_deg_lat_tmp]
            )
            other_xy = np.column_stack(
                [snap_x[other_idx] * m_per_deg_lon_tmp, snap_y[other_idx] * m_per_deg_lat_tmp]
            )
            _, nn_pos = cKDTree(main_xy).query(other_xy)
            nn_main_idx = main_idx[nn_pos]

            # Keep only the single closest (fragment-node, main-node) pair per fragment --
            # one bridge edge per component, not one per node in it.
            approx_dist_m = np.hypot(
                other_xy[:, 0] - main_xy[nn_pos, 0], other_xy[:, 1] - main_xy[nn_pos, 1]
            )
            comp_of_other = labels[other_idx]
            best_pair: dict[int, tuple[int, int, float]] = {}
            for pos, comp_lbl in enumerate(comp_of_other):
                d = approx_dist_m[pos]
                if comp_lbl not in best_pair or d < best_pair[comp_lbl][2]:
                    best_pair[comp_lbl] = (int(other_idx[pos]), int(nn_main_idx[pos]), float(d))

            detour_factor = float(getattr(cfg, "non_bus_dijkstra_detour_factor", 1.6)) if cfg else 1.6
            bridge_rows: list[int] = []
            bridge_cols: list[int] = []
            bridge_lengths: list[float] = []
            for small_node, main_node, _approx_d in best_pair.values():
                real_dist_m = _haversine_m(
                    float(snap_y[small_node]), float(snap_x[small_node]),
                    float(snap_y[main_node]), float(snap_x[main_node]),
                ) * detour_factor
                bridge_rows += [small_node, main_node]
                bridge_cols += [main_node, small_node]
                bridge_lengths += [real_dist_m, real_dist_m]

            rows_arr = np.concatenate([rows_arr, np.asarray(bridge_rows, dtype=np.int64)])
            cols_arr = np.concatenate([cols_arr, np.asarray(bridge_cols, dtype=np.int64)])
            length_vals = np.concatenate([length_vals, np.asarray(bridge_lengths, dtype=float)])
            print(
                f"[Graph] Bridged {len(best_pair)} disconnected component(s) into the main "
                f"'{network_type}' graph (detour-inflated connector edge each).",
                flush=True,
            )

    mat = csr_matrix((length_vals, (rows_arr, cols_arr)), shape=(real_node_count, real_node_count))
    mat.sort_indices()
    indptr = mat.indptr.astype(np.int64)
    indices = mat.indices.astype(np.int64)
    length = mat.data.astype(float)
    wscore = np.full(length.shape, WALK_EDGE_DEFAULT_SCORE, dtype=float) if network_type == "walk" else None

    mean_lat_rad = math.radians(float(snap_y.mean())) if snap_y.size else 0.0
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(mean_lat_rad)

    save_dict = dict(
        indptr=indptr,
        indices=indices,
        length=length,
        node_ids=node_ids,
        snap_x=snap_x,
        snap_y=snap_y,
        snap_indices=snap_indices,
        m_per_deg_lon=np.float64(m_per_deg_lon),
        m_per_deg_lat=np.float64(m_per_deg_lat),
    )
    if wscore is not None:
        save_dict["wscore"] = wscore
    tmp_path = csr_path + ".tmp.npz"
    np.savez(tmp_path, **save_dict)
    os.replace(tmp_path, csr_path)
    print(
        f"[Graph] Built streaming CSR routing matrix for '{network_type}': "
        f"{csr_path} nodes={real_node_count} edges={len(length)}",
        flush=True,
    )
    return save_dict


def get_mode_csr(network_type, cfg: PipelineConfig | None = None):
    """Compact CSR routing bundle for a mode's FULL (unsimplified) graph.

    Routing on the full graph is exact; storing it as a scipy CSR adjacency
    (indptr/indices/length arrays, ~10-15 MB) instead of a NetworkX object (>1 GB)
    lets every worker hold the whole network for a fraction of the RAM. Built once
    per mode in the parent and cached to `<full>_csr.npz`; workers load that file.

    Returns a dict with:
    - `mat`: scipy.sparse.csr_matrix over the full routing topology, with
      same-endpoint parallel edges collapsed to their shortest length.
    - `indptr`, `indices`, `length`: the raw CSR arrays (for fast path walkability).
    - `node_ids`: int64 array, row/col index -> OSM node id.
    - `id_to_idx`: dict OSM node id -> index.
    - `tree`, `snap_indices`, `scale`: cKDTree over real node coords (m), the
      matching matrix row indices for those real nodes, and the projection scale.
    - `wscore`: float array aligned to `length` (walk only; else None).
    """
    import numpy as np
    from scipy.sparse import csr_matrix
    from scipy.spatial import cKDTree

    if cfg is None:
        cfg = PipelineConfig()

    cached = _MODE_CSR_CACHE.get(network_type)
    if cached is not None:
        return cached

    full_path = _resolve_mode_graph_path(network_type, cfg)
    csr_path = full_path[: -len(".graphml")] + "_csr_v3.npz"

    csr_stale = (
        os.path.isfile(csr_path)
        and os.path.isfile(full_path)
        and os.path.getmtime(full_path) > os.path.getmtime(csr_path)
    )
    if csr_stale:
        print(
            f"[Graph] Source graph is newer than CSR cache; rebuilding CSR for '{network_type}'",
            flush=True,
        )
    if os.path.isfile(csr_path) and not csr_stale:
        data = np.load(csr_path, allow_pickle=False)
        indptr = data["indptr"]
        indices = data["indices"]
        length = data["length"]
        node_ids = data["node_ids"]
        snap_x = data["snap_x"]
        snap_y = data["snap_y"]
        snap_indices = data["snap_indices"]
        m_per_deg_lon = float(data["m_per_deg_lon"])
        m_per_deg_lat = float(data["m_per_deg_lat"])
        wscore = data["wscore"] if "wscore" in data.files else None
    else:
        if not os.path.isfile(full_path):
            # First-ever run for a city still needs the existing graph build/download
            # path. Once saved, immediately discard NetworkX and stream the GraphML
            # into the compact artifact used by normal pipeline runs.
            graph = get_mode_graph(network_type, cfg)
            with _ElapsedTimer(f"Graph save {network_type} before streaming CSR"):
                ox.io.save_graphml(graph, filepath=full_path)
            clear_mode_graph_cache()
        with _ElapsedTimer(f"Graph streaming CSR build {network_type}"):
            built = _build_mode_csr_streaming(network_type, full_path, csr_path, cfg)
        indptr = built["indptr"]
        indices = built["indices"]
        length = built["length"]
        node_ids = built["node_ids"]
        snap_x = built["snap_x"]
        snap_y = built["snap_y"]
        snap_indices = built["snap_indices"]
        m_per_deg_lon = float(built["m_per_deg_lon"])
        m_per_deg_lat = float(built["m_per_deg_lat"])
        wscore = built.get("wscore")


    n_nodes = len(node_ids)
    mat = csr_matrix((length, indices, indptr), shape=(n_nodes, n_nodes))
    node_xy = np.column_stack([snap_x * m_per_deg_lon, snap_y * m_per_deg_lat])
    tree = cKDTree(node_xy)
    id_to_idx = {int(nid): i for i, nid in enumerate(node_ids)}
    bundle = {
        "mat": mat,
        "indptr": indptr,
        "indices": indices,
        "length": length,
        "node_ids": node_ids,
        "id_to_idx": id_to_idx,
        "tree": tree,
        "snap_indices": snap_indices,
        # Real-node lon/lat (EPSG:4326), aligned to snap_indices/tree rows. Exposed so
        # callers can recover the snapped coordinate (not just the node index) without
        # loading the full NetworkX graph — used by POI snapping in the snapping stage.
        "snap_x": snap_x,
        "snap_y": snap_y,
        "scale": (m_per_deg_lon, m_per_deg_lat),
        "wscore": wscore,
    }
    _MODE_CSR_CACHE[network_type] = bundle
    return bundle



def get_mode_graph(network_type, cfg: PipelineConfig | None = None):
    """Load or download graph for a specific travel mode.

    Inputs:
    - network_type: mode string (for example walk, bike, drive).
    - cfg: optional PipelineConfig. If not provided, creates a new one.

    Outputs:
    - graph object for that mode, cached in memory.
    """
    if cfg is None:
        cfg = PipelineConfig()

    if network_type in _MODE_GRAPH_CACHE:
            return _MODE_GRAPH_CACHE[network_type]

    graph = None

    if cfg.use_shapefile:
        graph_path = _resolve_mode_graph_path(network_type, cfg)
        try:
            print(
                f"[Graph] Loading {network_type} graph from shapefile cache: "
                f"{graph_path}",
                flush=True,
            )
            with _ElapsedTimer(f"Graph load {network_type} from cache"):
                graph = ox.io.load_graphml(graph_path)
        except Exception:
            print(
                f"[Graph] Cached shapefile graph not found for mode '{network_type}'. "
                f"Building from '{cfg.name_shapefile}'...",
                flush=True,
            )
            with _ElapsedTimer(f"Graph build {network_type} from shapefile"):
                _, graph = graph_from_shapefile(
                    cfg.name_shapefile,
                    network_type=network_type,
                )
            with _ElapsedTimer(f"Graph save {network_type}"):
                ox.io.save_graphml(graph, filepath=graph_path)
            print(
                f"[Graph] Shapefile graph for mode '{network_type}' saved to "
                f"{graph_path}",
                flush=True,
            )
    else:
        # Cache per-mode graphs on disk to avoid repeated Overpass downloads.
        place_name = cfg.city_name
        name_file = _resolve_mode_graph_path(network_type, cfg)
        from utils.services import get_global_radius_m
        buffer_m = get_global_radius_m(cfg) or 0.0

        try:
            with _ElapsedTimer(f"Graph load {network_type}"):
                graph = ox.io.load_graphml(name_file)
        except Exception:
            print(f"[Graph] Cached graph not found for mode '{network_type}'. Downloading from OSM...", flush=True)
            if buffer_m > 0:
                place_gdf = ox.geocode_to_gdf(place_name)
                utm_crs = place_gdf.estimate_utm_crs()
                gdf_metric = place_gdf.to_crs(utm_crs)
                gdf_buffered = gdf_metric.copy()
                gdf_buffered["geometry"] = gdf_metric.geometry.buffer(buffer_m)
                polygon = gdf_buffered.to_crs("EPSG:4326").geometry.iloc[0]
                print(f"[Graph] Expanding graph area by {buffer_m/1000:.1f} km buffer", flush=True)
                # clean_periphery=True (osmnx default) runs stats.count_streets_per_node over
                # the buffered graph. That call hard-crashes the interpreter (SIGSEGV) on large
                # graphs in this environment — verified to still crash even on networkx 3.3 — and
                # only computes the `street_count` attribute, which is unused anywhere in this repo.
                # We pass clean_periphery=False to skip it entirely. The caller already applied its
                # own buffer, so osmnx's extra ~500 m peripheral buffer adds nothing here.
                # osmnx 1.9 emits a FutureWarning for this arg (removed in v2.0); suppress it so the
                # warning isn't mistaken for the cause of a crash it actually prevents.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", FutureWarning)
                    with _ElapsedTimer(f"Graph download {network_type} (buffered polygon)"):
                        graph = ox.graph_from_polygon(
                            polygon,
                            network_type=network_type,
                            simplify=cfg.osm_autobuild_simplify,
                            retain_all=cfg.osm_autobuild_retain_all,
                            clean_periphery=False,
                        )
            else:
                with _ElapsedTimer(f"Graph download {network_type} from OSM"):
                    graph = ox.graph_from_place(
                        place_name,
                        network_type=network_type,
                        simplify=cfg.osm_autobuild_simplify,
                        retain_all=cfg.osm_autobuild_retain_all,
                    )
            with _ElapsedTimer(f"Graph save {network_type}"):
                ox.io.save_graphml(graph, filepath=name_file)
            print(f"[Graph] Graph downloaded and saved: {name_file}", flush=True)

    _MODE_GRAPH_CACHE[network_type] = graph
    return graph

def _tags_file_name(tags, buffer_m: float = 0.0) -> str:
    """Build stable geojson filename for a tags-based POI query.

    Inputs:
    - tags: dictionary used in OSM features query.

    Outputs:
    - str: deterministic filename for cache reuse.
    """
    tags_json = json.dumps(tags, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if buffer_m > 0:
        tags_json += f"|buffer={buffer_m:.1f}"
    key_hash = hashlib.sha1(tags_json.encode("utf-8")).hexdigest()
    return f"tags_{key_hash}.geojson"

def _feature_value_file_name(feature: str, value: TagValue, buffer_m: float = 0.0) -> str:
    """Build stable geojson filename for a feature/value POI query."""
    payload = json.dumps(
        {"feature": feature, "value": value},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    if buffer_m > 0:
        payload += f"|buffer={buffer_m:.1f}"
    key_hash = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"fv_{key_hash}.geojson"

def _city_poi_cache_dir(cache_slug: str) -> str:
    """Return scenario-scoped directory path for POI cache files."""
    return os.path.join("poi", cache_slug)

def _all_tags_file_name(query_tags: TagClause, buffer_m: float = 0.0) -> str:
    payload = json.dumps(query_tags, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if buffer_m > 0:
        payload += f"|buffer={buffer_m:.1f}"
    key_hash = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"all_tags_{key_hash}.geojson"

def _normalize_value_list(values: set[TagValue]) -> list[TagValue]:
    return sorted(list(values), key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=True))

def _iter_tag_clauses(tags: TagQuery | None) -> list[TagClause]:
    if tags is None:
        return []
    if isinstance(tags, list):
        return [clause for clause in tags if isinstance(clause, dict)]
    if isinstance(tags, dict):
        return [tags]
    return []


# Keys that only ever qualify another key inside a multi-key clause (e.g.
# {"leisure": "swimming_pool", "indoor": "yes"}). They must NOT become standalone
# download queries: fetching every access=private / indoor=yes element in a city would
# blow up the universe. Their columns still materialize because OSMnx returns all tags
# of the features fetched via the clause's primary key.
_QUALIFIER_ONLY_KEYS = {"indoor", "access", "building", "water", "covered"}

def _build_city_query_batches() -> dict[str, list[TagValue]]:
    """Group configured POI queries by tag key to enable batched extraction."""
    from utils import services as serv

    by_key: dict[str, set[TagValue]] = {}
    for q in serv.unique_query_keys():
        for clause in _iter_tag_clauses(q.tags):
            primary = {k: v for k, v in clause.items() if str(k) not in _QUALIFIER_ONLY_KEYS}
            # A clause made solely of qualifier keys still needs downloading via its own keys.
            for key, value in (primary or clause).items():
                if isinstance(value, list):
                    for item in value:
                        by_key.setdefault(str(key), set()).add(str(item))
                else:
                    by_key.setdefault(str(key), set()).add(value)
    return {k: _normalize_value_list(v) for k, v in by_key.items()}

def _build_city_universe_tags() -> TagClause:
    """Build one combined tags payload covering all configured POI queries."""
    key_batches = _build_city_query_batches()
    out: TagClause = {}
    for key, values in key_batches.items():
        if any(v is True for v in values):
            out[key] = True
        else:
            out[key] = [str(v) for v in values]
    return out

def _download_city_poi_universe(place_name: str, query_tags: TagClause, buffer_m: float = 0.0) -> gpd.GeoDataFrame:
    """Download one city-wide POI dataset that covers all configured tags."""
    cfg = PipelineConfig()

    if cfg.use_shapefile:
        return feature_from_shapefile(cfg.name_shapefile, query_tags)
    elif cfg.poi_from_shp:
        return poi_from_shp(query_tags=query_tags)
    else:
        print(f"[POI] OSMnx city-universe download: keys={len(query_tags)}")
        return _download_poi_for_place(place_name, query_tags, buffer_m=buffer_m)

def _values_match(series: pd.Series, value: TagValue) -> pd.Series:
    if value is True:
        return series.notna()
    wanted = {str(v) for v in value} if isinstance(value, list) else {str(value)}
    # OSM tags can carry semicolon-separated multi-values (e.g. "sport=climbing;bouldering");
    # a plain == / isin against the raw string misses those, so split before comparing.
    return series.astype(str).apply(lambda s: any(part.strip() in wanted for part in s.split(";")))

def _filter_by_clause(gdf: gpd.GeoDataFrame, tags: TagClause) -> gpd.GeoDataFrame:
    if gdf.empty:
        return gdf
    mask = pd.Series(True, index=gdf.index)
    for key, value in tags.items():
        if key not in gdf.columns:
            # Empty result of the SAME type as the input. The city-universe can be a plain
            # DataFrame (loaded via _read_geojson_without_gdal, which avoids GDAL/GEOS), and
            # gpd.GeoDataFrame(..., crs=gdf.crs) would AttributeError on it — that failure used
            # to be swallowed upstream and silently zeroed every tag query, emptying the dedup
            # drop map. An empty iloc slice preserves columns/dtype/crs for either type.
            return gdf.iloc[0:0].copy()
        mask = mask & _values_match(gdf[key], value)
    out = gdf.loc[mask].copy()
    if "geometry" in out.columns:
        out = out[out["geometry"].notna()].copy()
    return out

def _filter_by_tags(gdf: gpd.GeoDataFrame, tags: TagQuery) -> gpd.GeoDataFrame:
    if isinstance(tags, dict):
        return _filter_by_clause(gdf, tags)
    if not isinstance(tags, list) or not tags:
        return gdf.iloc[0:0].copy()
    parts = []
    for clause in tags:
        if not isinstance(clause, dict):
            continue
        part = _filter_by_clause(gdf, clause)
        if part is not None and not part.empty:
            parts.append(part)
    if not parts:
        return gdf.iloc[0:0].copy()
    # pd.concat preserves the concrete type (GeoDataFrame keeps its crs; a plain DataFrame
    # stays a DataFrame), so don't force-wrap in gpd.GeoDataFrame — that broke the
    # plain-DataFrame universe path.
    out = pd.concat(parts, ignore_index=False)
    out = out[~out.index.duplicated(keep="first")].copy()
    if "geometry" in out.columns:
        out = out[out["geometry"].notna()].copy()
    return out

def _get_city_poi_universe(place_name: str, cache_slug: str, city_poi_dir: str, buffer_m: float = 0.0) -> gpd.GeoDataFrame:
    cache_key = f"{cache_slug}|buffer={buffer_m:.1f}"
    cached = _CITY_POI_UNIVERSE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    universe_tags = _build_city_universe_tags()
    path = os.path.join(city_poi_dir, _all_tags_file_name(universe_tags, buffer_m))
    gdf = _load_cached_poi_if_nonempty(path)
    if gdf is None:
        try:
            gdf = _download_city_poi_universe(place_name, universe_tags, buffer_m=buffer_m)
        except Exception as exc:
            print(f"[POI] City-universe download failed: {exc}")
            gdf = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        if gdf is not None and not gdf.empty and _has_geometry_values(gdf):
            try:
                gdf.to_file(path, driver="GeoJSON")
            except Exception as exc:
                print(f"[POI] Failed to persist city-universe cache {path}: {exc}")
    if gdf is None:
        gdf = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    _CITY_POI_UNIVERSE_CACHE[cache_key] = gdf
    return gdf

def _download_poi_for_place(place_name: str, query_tags: TagClause, buffer_m: float = 0.0):
    """Download POIs for one place, optionally expanding the query area by buffer_m metres."""
    if buffer_m > 0:
        place_gdf = ox.geocode_to_gdf(place_name)
        if place_gdf.empty:
            raise RuntimeError(f"geocode_to_gdf returned no geometry for '{place_name}'")
        utm_crs = place_gdf.estimate_utm_crs()
        gdf_metric = place_gdf.to_crs(utm_crs)
        gdf_buffered = gdf_metric.copy()
        gdf_buffered["geometry"] = gdf_metric.geometry.buffer(buffer_m)
        polygon = gdf_buffered.to_crs("EPSG:4326").geometry.iloc[0]
        print(f"[POI] Query area expanded by {buffer_m/1000:.1f} km buffer", flush=True)
        with _ElapsedTimer(f"POI download (buffered polygon, {len(query_tags)} tags)"):
            return ox.features_from_polygon(polygon, query_tags)

    try:
        with _ElapsedTimer(f"POI download {place_name} ({len(query_tags)} tags)"):
            return ox.features_from_place(place_name, query_tags)
    except Exception as place_exc:
        print(
            f"[POI] features_from_place failed for '{place_name}' ({place_exc}); "
            "retrying with geocoded polygon..."
        )
        place_gdf = ox.geocode_to_gdf(place_name)
        if place_gdf.empty:
            raise RuntimeError(f"geocode_to_gdf returned no geometry for place '{place_name}'")
        polygon = place_gdf.geometry.iloc[0]
        with _ElapsedTimer(f"POI download {place_name} retry (polygon, {len(query_tags)} tags)"):
            return ox.features_from_polygon(polygon, query_tags)

def _coords_are_finite(obj) -> bool:
    """Return True when a GeoJSON coordinate tree contains only finite numbers."""
    if isinstance(obj, (int, float)):
        return math.isfinite(float(obj))
    if isinstance(obj, (list, tuple)):
        return bool(obj) and all(_coords_are_finite(item) for item in obj)
    return False


def _has_geometry_values(frame) -> bool:
    if "__snap_coord" in frame.columns:
        for coord in frame["__snap_coord"].to_numpy():
            if isinstance(coord, tuple) and len(coord) >= 2:
                return True
    if "geometry" not in frame.columns:
        return False
    for geom in frame["geometry"].to_numpy():
        if geom is not None:
            return True
    return False


def _coord_pair_from_lon_lat(pair):
    if not isinstance(pair, (list, tuple)) or len(pair) < 2:
        return None
    lon, lat = pair[0], pair[1]
    if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
        return None
    if not math.isfinite(float(lon)) or not math.isfinite(float(lat)):
        return None
    return (float(lat), float(lon))


def _geojson_vertices(geometry_obj) -> list[tuple[float, float]]:
    """Extract snap vertices as plain `(lat, lon)` tuples from raw GeoJSON."""
    if not isinstance(geometry_obj, dict):
        return []
    gtype = geometry_obj.get("type")
    coords = geometry_obj.get("coordinates")
    if gtype == "Point":
        coord = _coord_pair_from_lon_lat(coords)
        return [coord] if coord is not None else []
    if gtype in ("LineString", "MultiPoint"):
        return [c for c in (_coord_pair_from_lon_lat(pair) for pair in (coords or [])) if c is not None]
    if gtype in ("Polygon", "MultiLineString"):
        out = []
        for line in coords or []:
            out.extend(c for c in (_coord_pair_from_lon_lat(pair) for pair in (line or [])) if c is not None)
        return out
    if gtype == "MultiPolygon":
        out = []
        for poly in coords or []:
            for ring in poly or []:
                out.extend(c for c in (_coord_pair_from_lon_lat(pair) for pair in (ring or [])) if c is not None)
        return out
    if gtype == "GeometryCollection":
        out = []
        for geom in geometry_obj.get("geometries") or []:
            out.extend(_geojson_vertices(geom))
        return out
    return []


def _compact_geometry_token(geometry_type: str, vertices: list) -> str:
    # Avoid json.dumps on the raw geometry_obj: the C JSON encoder recurses into nested
    # coordinate arrays at C level, which overflows the C stack on complex MultiPolygons
    # and causes an unrecoverable segfault. Hashing the flat vertex list + type string
    # is stable (same cached file → same order) and sufficient to distinguish geometries.
    key = geometry_type + ":" + ",".join(f"{lon:.10g}:{lat:.10g}" for lon, lat in vertices)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _safe_shape_from_geojson(geometry_obj):
    """Build one Shapely geometry after rejecting invalid/non-finite coordinates."""
    if not isinstance(geometry_obj, dict):
        return None
    coords = geometry_obj.get("coordinates")
    if coords is not None and not _coords_are_finite(coords):
        return None
    try:
        return shape(geometry_obj)
    except Exception:
        return None


def _read_geojson_without_gdal(path: str) -> pd.DataFrame:
    """Read cached GeoJSON as plain rows with pre-extracted snap coordinates.

    This intentionally avoids GDAL, GeoPandas geometry arrays, and Shapely geometry
    construction. On the Paris workload, several apparently harmless GEOS/Shapely
    property calls hard-crash the interpreter. Snapping only needs stable source keys
    and candidate coordinates, so cached GeoJSON stays as plain Python data here.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    features = data.get("features", []) if isinstance(data, dict) else []

    rows = []
    for ft in features:
        if not isinstance(ft, dict):
            continue
        geometry_obj = ft.get("geometry")
        vertices = _geojson_vertices(geometry_obj)
        if not vertices:
            continue
        row = dict(ft.get("properties", {}) or {})
        row["__snap_coord"] = vertices[0]
        row["__snap_vertices"] = vertices if len(vertices) > 1 else None
        gtype = geometry_obj.get("type", "") if isinstance(geometry_obj, dict) else ""
        row["__geometry_token"] = _compact_geometry_token(gtype, vertices)
        rows.append(row)

    return pd.DataFrame(rows)


def _load_cached_poi_if_nonempty(path: str):
    """Load cached POI file and return None when missing/invalid/empty."""
    try:
        poi = _read_geojson_without_gdal(path)
    except Exception:
        return None
    if poi is None or poi.empty:
        return None
    if not _has_geometry_values(poi):
        return None
    return poi


def get_poi(
    feature: str | None = None,
    value: TagValue | None = None,
    tags: TagQuery | None = None,
    poi_type: str | None = None,
):
    """Load POIs from local cache or download them from OSM.

    Inputs:
    - feature/value: optional key-value OSM filter.
    - tags: optional tags dictionary filter (preferred when provided).

    Outputs:
    - GeoDataFrame: POI features matching requested filters.
    """

    cfg = PipelineConfig()

    # Nomi file
    place_name = cfg.city_name
    city_slug = cfg.city_slug
    poi_cache_slug = cfg.artifact_slug if cfg.use_shapefile else city_slug

    from utils.services import get_global_radius_m
    buffer_m: float = get_global_radius_m(cfg) or 0.0

    os.makedirs("poi", exist_ok=True)
    city_poi_dir = _city_poi_cache_dir(poi_cache_slug)
    os.makedirs(city_poi_dir, exist_ok=True)

    if (feature is None or value is None) and not tags:
        poi_files = [
            os.path.join(city_poi_dir, name)
            for name in os.listdir(city_poi_dir)
            if name.lower().endswith(".geojson")
        ]

        if poi_files:
            frames = []
            for path in poi_files:
                cached = _load_cached_poi_if_nonempty(path)
                if cached is not None:
                    frames.append(cached)

            if frames:
                combined = pd.concat(frames, ignore_index=True)
                if "geometry" in combined.columns:
                    return gpd.GeoDataFrame(combined, geometry="geometry", crs=getattr(frames[0], "crs", "EPSG:4326"))
                return combined

        # Fallback: download all amenities when no local cache is available.
        feature = "amenity"
        value = True

    if tags:
        NAME_FILE = os.path.join(city_poi_dir, _tags_file_name(tags, buffer_m))
    else:
        feature_key = feature if feature is not None else "amenity"
        value_key: TagValue = value if value is not None else True
        NAME_FILE = os.path.join(city_poi_dir, _feature_value_file_name(feature_key, value_key, buffer_m))

    # Provo a caricare i POI gia salvati.
    poi = _load_cached_poi_if_nonempty(NAME_FILE)
    if poi is None:
        if tags and not cfg.poi_from_shp:
            try:
                universe = _get_city_poi_universe(place_name, poi_cache_slug, city_poi_dir, buffer_m=buffer_m)
                if universe is not None and not universe.empty:
                    poi = _filter_by_tags(universe, tags)
                    if poi is not None and not poi.empty:
                        # Persist the per-query result as a fast-path cache, but treat this as
                        # best-effort: when the universe was loaded from the on-disk GeoJSON cache
                        # it comes back as a plain DataFrame (see _read_geojson_without_gdal, which
                        # avoids GDAL/GEOS on purpose), and DataFrame has no .to_file. That must NOT
                        # discard the filtered result — downstream consumers (snapping, poi_dedup)
                        # read the plain-DataFrame form via get_poi_geometries just fine. Previously
                        # the failed write was caught below and silently returned zero POIs, which
                        # made the whole dedup drop-map empty.
                        if hasattr(poi, "to_file"):
                            try:
                                poi.to_file(NAME_FILE, driver="GeoJSON")
                            except Exception as write_exc:
                                print(f"[POI] Could not persist per-query cache {NAME_FILE}: {write_exc}", flush=True)
                        print(
                            f"[POI] Built poi_type={poi_type} from city-universe cache. "
                            f"count={len(poi)} file={NAME_FILE}",
                            flush=True,
                        )
                        return poi
            except Exception as batch_exc:
                print(f"[POI] Batch resolution failed for tags={tags}: {batch_exc}")
            # Avoid duplicate OSMnx work: tags queries rely only on the city-universe dataset.
            print(f"[POI] No matches after city-universe filtering for tags={tags}. Skipping per-query OSMnx fallback.")
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        download_key = (poi_cache_slug, place_name)
        if download_key not in _POI_DOWNLOAD_LOGGED:
            print(f"[POI] Downloading POIs for '{place_name}'...", flush=True)
            _POI_DOWNLOAD_LOGGED.add(download_key)

        try:
            if cfg.use_shapefile:
                print(
                    f"[POI] Loading poi_type={poi_type} from shapefile source...",
                    flush=True,
                )
                # In shapefile mode, selection is based on poi_type -> labels mapping.
                # OSM tags are irrelevant for source filtering.
                poi = feature_from_shapefile(cfg.name_shapefile, query_tags={}, poi_type=poi_type)
            else:
                if tags:
                    if not isinstance(tags, dict):
                        # OR-queries are resolved from the city-universe dataset only.
                        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
                    query_tags: TagClause = tags
                else:
                    query_tags = {feature_key: value_key}
                print(
                    f"[POI] Downloading poi_type={poi_type} from OSM for '{place_name}'...",
                    flush=True,
                )
                poi = _download_poi_for_place(place_name, query_tags, buffer_m=buffer_m)
        except Exception as e:
            print(
                f"[POI] No POIs found for city={place_name}, poi_type={poi_type}, feature={feature}, value={value}, tags={tags}: {e}",
                flush=True,
            )
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        source_name = "shapefile" if cfg.use_shapefile else "OSM"
        if poi.empty:
            # Do not persist empty payloads: they can mask transient Overpass/geocoding issues.
            try:
                if os.path.exists(NAME_FILE):
                    os.remove(NAME_FILE)
            except Exception:
                pass
            print(
                f"[POI] {source_name.capitalize()} query returned 0 features "
                f"for poi_type={poi_type}. Cache not persisted for {NAME_FILE}",
                flush=True,
            )
            return poi
        if not _has_geometry_values(poi):
            try:
                if os.path.exists(NAME_FILE):
                    os.remove(NAME_FILE)
            except Exception:
                pass
            print(
                f"[POI] {source_name.capitalize()} query has no usable geometry "
                f"for poi_type={poi_type}. Cache not persisted for {NAME_FILE}",
                flush=True,
            )
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        poi.to_file(NAME_FILE, driver="GeoJSON")
        print(
            f"[POI] Cached poi_type={poi_type}. count={len(poi)} file={NAME_FILE}",
            flush=True,
        )

    return poi

def print_poi(poi, print_start, print_end):
    """Print POI names in a selected index window.

    Inputs:
    - poi: POI GeoDataFrame.
    - print_start: start index (inclusive).
    - print_end: end index (exclusive).

    Outputs:
    - None. Prints names to stdout.
    """
    if print_start == None or print_start < 0:
        print_start = 0
    
    if print_end == None or print_end > len(poi):
        print_end = len(poi)

    subset = poi.iloc[print_start:print_end]

    for i, row in subset.iterrows():
        print(f"{i+1}: {row.get('name', 'Unnamed')}")


def get_poi_names(poi):
    """Extract POI names from a GeoDataFrame.

    Inputs:
    - poi: POI GeoDataFrame.

    Outputs:
    - list[str]: non-null POI names.
    """
    return [
        name for name in poi["name"].dropna().astype(str).values
    ]

def get_poi_geom(poi):
    """Extract geometry column values as strings.

    Inputs:
    - poi: POI GeoDataFrame.

    Outputs:
    - list[str]: non-null geometry values converted to strings.
    """
    return [
        geometry for geometry in poi["geometry"].dropna().astype(str).values
    ]

def get_poi_geometries(poi):
    """Extract geometry objects, optional names, and stable source keys from POI table.

    Inputs:
    - poi: POI GeoDataFrame.

    Outputs:
    - list[tuple[geometry, name, source_key]]: geometry/name/key triples for downstream snapping.
    """
    out = []
    if "geometry" not in poi.columns and "__snap_coord" not in poi.columns:
        return out

    # Extract only the columns build_poi_source_key may read, once, as numpy arrays,
    # then iterate positionally. Building a full poi.loc[idx] Series per row over an
    # OSM universe (which can have thousands of tag columns and hundreds of thousands
    # of rows) is what previously segfaulted pandas in the snapping stage.
    from utils.poi_identity import SOURCE_KEY_COLUMNS

    if "geometry" in poi.columns:
        geoms = poi["geometry"].to_numpy()
    else:
        geoms = np.empty(len(poi), dtype=object)
        geoms[:] = None
    col_names = tuple(SOURCE_KEY_COLUMNS) + ("__geometry_token", "__snap_coord", "__snap_vertices")
    col_values = {col: poi[col].to_numpy() for col in col_names if col in poi.columns}
    name_vals = col_values.get("name")
    snap_coords = col_values.get("__snap_coord")
    snap_vertices = col_values.get("__snap_vertices")
    geometry_tokens = col_values.get("__geometry_token")

    for i in range(len(geoms)):
        geometry = geoms[i]
        row = {col: vals[i] for col, vals in col_values.items()}
        name = None
        if name_vals is not None:
            nv = name_vals[i]
            try:
                name = None if nv is None or pd.isna(nv) else nv
            except (TypeError, ValueError):
                name = nv

        if snap_coords is not None and geometry_tokens is not None:
            coord = snap_coords[i]
            if isinstance(coord, tuple) and len(coord) >= 2:
                vertices = snap_vertices[i] if snap_vertices is not None else None
                plain_geom = {
                    "snap_coord": coord,
                    "snap_vertices": vertices if isinstance(vertices, list) else None,
                    "geometry_token": str(geometry_tokens[i]),
                }
                source_key = build_poi_source_key(row, plain_geom["geometry_token"])
                out.append((plain_geom, name, source_key))
                continue

        if geometry is None:
            continue
        if isinstance(geometry, float) and math.isnan(geometry):
            continue
        source_key = build_poi_source_key(row, geometry)
        out.append((geometry, name, source_key))
    return out

def get_poi_amenity_types(poi):
    """List unique amenity values present in POI data.

    Inputs:
    - poi: POI GeoDataFrame.

    Outputs:
    - list[str]: sorted unique amenity labels.
    """
    if "amenity" not in poi.columns:
        return []
    return sorted(poi["amenity"].dropna().astype(str).unique().tolist())
