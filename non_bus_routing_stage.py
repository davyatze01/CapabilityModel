import os
import pickle
import math
import multiprocessing as mp
import threading
import time
from typing import Any, cast

from tqdm import tqdm
import walkability

from context import PipelineContext
from pipeline_types import SnappingStageResult, NonBusRoutingStageResult
from utils import delta_g, services as serv
from snapping_stage import select_best_snap_candidate_for_origin, coord_key
from config import PipelineConfig

_POI_BUS_SNAP_INFO: dict[Any, Any] | None = None
_POI_MODE_SNAP_INFO: dict[Any, Any] | None = None
_NON_BUS_PROGRESS_VALUE: Any | None = None
_POI_WORK_UNITS_BY_KEY: dict[Any, int] | None = None
_NON_BUS_CACHE_DIR: str = ""
_NON_BUS_CACHE_SCHEMA_VERSION: int = 0
_NON_BUS_POI_CONFIG_SIGNATURE: str = ""
_PIPELINE_CONFIG: PipelineConfig | None = None

def _non_bus_cache_path(node_id):
    """Build cache file path for one node id.

    Inputs:
    - node_id: origin node identifier.

    Outputs:
    - str: non-bus cache file path.
    """
    if not _NON_BUS_CACHE_DIR:
        raise RuntimeError("non-bus cache directory is not initialized")
    return os.path.join(_NON_BUS_CACHE_DIR, f"{node_id}.pkl")


def _write_non_bus_cache(path, payload):
    """Write non-bus cache payload for a node.

    Inputs:
    - path: destination pickle path.
    - payload: node-level non-bus cache payload.

    Outputs:
    - None. Writes pickle file.
    """
    with open(path, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)


def _load_non_bus_cache(path):
    """Load node-level non-bus cache payload from disk.

    Inputs:
    - path: cache pickle path.

    Outputs:
    - dict-like payload for one node.
    """
    with open(path, "rb") as f:
        return pickle.load(f)


def _is_valid_non_bus_cache(payload):
    """Validate non-bus cache schema shape and version.

    Inputs:
    - payload: cache object loaded from pickle.

    Outputs:
    - bool: True when cache can be safely reused.
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("schema_version") != _NON_BUS_CACHE_SCHEMA_VERSION:
        return False
    if payload.get("poi_config_signature") != _NON_BUS_POI_CONFIG_SIGNATURE:
        return False
    if "origin" not in payload or "services" not in payload:
        return False
    if not isinstance(payload["services"], dict):
        return False
    return True


def _has_valid_non_bus_cache(path):
    """Check whether a node cache file exists and is reusable.

    Inputs:
    - path: non-bus cache path.

    Outputs:
    - bool: True when file exists and passes schema validation.
    """
    if not os.path.exists(path):
        return False
    try:
        payload = _load_non_bus_cache(path)
    except Exception:
        return False
    return _is_valid_non_bus_cache(payload)


def _init_worker(
    pipeline_config = None,
    poi_bus_snap_info_by_type=None,
    poi_mode_snap_info_by_type=None,
    graph=None,
    mode_graphs=None,
    non_bus_progress_value=None,
    non_bus_cache_dir: str = "",
    non_bus_cache_schema_version: int = 0,
    non_bus_poi_config_signature: str = "",
):
    """Initialize worker state for non-bus multiprocessing stage.

    Inputs:
    - poi_bus_snap_info_by_type: bus snap candidates keyed by POI key.
    - poi_mode_snap_info_by_type: snap candidates split by mode and POI key.
    - graph: base graph object shared with utilities.
    - mode_graphs: mapping of mode -> graph.
    - non_bus_progress_value: shared progress counter.
    - non_bus_cache_dir: cache directory for node payloads.
    - non_bus_cache_schema_version: expected cache schema version.

    Outputs:
    - None. Populates worker globals and warm caches.
    """
    global _POI_BUS_SNAP_INFO, _POI_MODE_SNAP_INFO, _NON_BUS_PROGRESS_VALUE, _POI_WORK_UNITS_BY_KEY
    global _NON_BUS_CACHE_DIR, _NON_BUS_CACHE_SCHEMA_VERSION, _NON_BUS_POI_CONFIG_SIGNATURE, _PIPELINE_CONFIG
    _PIPELINE_CONFIG = pipeline_config
    _NON_BUS_CACHE_DIR = non_bus_cache_dir or ""
    _NON_BUS_CACHE_SCHEMA_VERSION = int(non_bus_cache_schema_version)
    _NON_BUS_POI_CONFIG_SIGNATURE = str(non_bus_poi_config_signature or "")
    _POI_BUS_SNAP_INFO = poi_bus_snap_info_by_type or {}
    _POI_WORK_UNITS_BY_KEY = {}
    for poi_key, snap_info_by_source in _POI_BUS_SNAP_INFO.items():
        _POI_WORK_UNITS_BY_KEY[poi_key] = len(snap_info_by_source)
    _POI_MODE_SNAP_INFO = poi_mode_snap_info_by_type or {}
    _NON_BUS_PROGRESS_VALUE = non_bus_progress_value
    if graph is not None and delta_g._G_CACHE is None:
        delta_g._G_CACHE = graph
    if mode_graphs:
        for mode, mode_graph in mode_graphs.items():
            if mode not in delta_g._MODE_GRAPH_CACHE:
                delta_g._MODE_GRAPH_CACHE[mode] = mode_graph


def _process_node(node_item):
    """Compute non-bus modal ingredients for one origin node.

    Inputs:
    - node_item: `(node_id, node_data)` pair with origin coordinates.

    Outputs:
    - dict node payload ready for non-bus cache persistence, or None for invalid nodes.
    """
    node_id, data = node_item
    if "y" not in data or "x" not in data:
        return None
    origin = (data["y"], data["x"])

    service_results = {}
    for service in serv.SERVICE_KEYS:
        entries = []
        for query in serv.get_service_queries(service):
            poi_key = serv.query_key(query)
            cfg = _PIPELINE_CONFIG
            if cfg is None:
                raise RuntimeError("Worker config not initialized")
            try:
                data = delta_g.accessibility_non_bus_from_snap_map(
                    cfg,
                    query.poi_type,
                    origin,
                    (_POI_MODE_SNAP_INFO or {}).get(poi_key, {}),
                    tags=query.tags,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Non-bus routing failed for node_id={node_id}, poi_type={query.poi_type}, cause={exc!r}"
                ) from exc

            imp_walk = data["imp_walk"]
            imp_bike = data["imp_bike"]
            imp_drive = data["imp_drive"]
            source_coords = data.get("source_coords", [])
            poi_coords = []
            snap_info_for_key = _POI_BUS_SNAP_INFO.get(poi_key, {}) if _POI_BUS_SNAP_INFO else {}
            for coord in source_coords:
                snap_info = snap_info_for_key.get(coord_key(coord))
                snapped_coord, _ = select_best_snap_candidate_for_origin(origin, coord, snap_info)
                poi_coords.append(snapped_coord)

            entries.append({
                "poi_type": query.poi_type,
                "imp_walk": imp_walk,
                "imp_bike": imp_bike,
                "imp_drive": imp_drive,
                "poi_coords": poi_coords,
                "walk_path_scores" : data.get("walk_path_scores", [])
            })

            if _NON_BUS_PROGRESS_VALUE is not None and _POI_WORK_UNITS_BY_KEY is not None:
                units = float(_POI_WORK_UNITS_BY_KEY.get(poi_key, 0))
                if units > 0:
                    with _NON_BUS_PROGRESS_VALUE.get_lock():
                        _NON_BUS_PROGRESS_VALUE.value += units
        service_results[service] = entries

    return {
        "schema_version": _NON_BUS_CACHE_SCHEMA_VERSION,
        "poi_config_signature": _NON_BUS_POI_CONFIG_SIGNATURE,
        "node_id": node_id,
        "origin": origin,
        "services": service_results,
    }


def run_non_bus_routing_stage(
    ctx: PipelineContext,
    snap: SnappingStageResult,
) -> NonBusRoutingStageResult:
    """Run non-bus routing ingredient computation with cache reuse and retries.

    Inputs:
    - ctx: pipeline context with workers, config, and node list.
    - snap: snapping outputs used to resolve POI candidates by mode.

    Outputs:
    - NonBusRoutingStageResult: cache paths and computed/cached node counters.
    """
    global _NON_BUS_CACHE_DIR, _NON_BUS_CACHE_SCHEMA_VERSION, _NON_BUS_POI_CONFIG_SIGNATURE, _PIPELINE_CONFIG
    _PIPELINE_CONFIG = ctx.config
    _NON_BUS_CACHE_DIR = _PIPELINE_CONFIG.non_bus_cache_dir
    _NON_BUS_CACHE_SCHEMA_VERSION = _PIPELINE_CONFIG.non_bus_cache_schema_version
    _NON_BUS_POI_CONFIG_SIGNATURE = serv.config_signature()

    cache_paths = {}
    nodes_to_compute = []
    cached_nodes = 0
    for node_id, data in ctx.nodes_with_coords:
        cache_path = _non_bus_cache_path(node_id)
        cache_paths[node_id] = cache_path
        if _has_valid_non_bus_cache(cache_path):
            cached_nodes += 1
            continue
        nodes_to_compute.append((node_id, data))

    # Warm walkability edge cache once in parent process so workers do not all
    # attempt an expensive first-time build concurrently.
    walk_graph = snap.shared_mode_graphs.get("walk")
    if walk_graph is not None:
        try:
            print("[Non-bus] Preparing walkability edge cache...", flush=True)
            walkability.get_or_build_edge_walkability_index(
                cfg=_PIPELINE_CONFIG,
                G=walk_graph,
                force_rebuild=False,
                schema_version=1,
            )
            print("[Non-bus] Walkability edge cache ready.", flush=True)
        except Exception as exc:
            print(
                f"[Non-bus] Walkability cache warm-up skipped due to error: {exc}",
                flush=True,
            )

    total_non_bus_pois = 0
    for service in serv.SERVICE_KEYS:
        for query in serv.get_service_queries(service):
            poi_key = serv.query_key(query)
            total_non_bus_pois += len(snap.poi_bus_snap_info_by_type.get(poi_key, {}))
    total_non_bus_tasks = len(ctx.nodes_with_coords) * total_non_bus_pois

    pbar_non_bus = tqdm(total=total_non_bus_tasks, desc="Non-bus routing stage", mininterval=1) if _PIPELINE_CONFIG.enable_progress else None
    non_bus_base_progress = [float(cached_nodes * total_non_bus_pois)]
    non_bus_displayed_progress = [0.0]
    non_bus_shared_progress: list[Any | None] = [None]
    stop_event = threading.Event()

    def _monitor():
        while not stop_event.wait(1):
            if pbar_non_bus is None:
                continue
            current = non_bus_base_progress[0]
            shared = non_bus_shared_progress[0]
            if shared is not None:
                shared_obj = cast(Any, shared)
                with shared_obj.get_lock():
                    current += float(shared_obj.value)
            pbar_total = float(pbar_non_bus.total or total_non_bus_tasks)
            if current > pbar_total:
                current = pbar_total
            delta = current - non_bus_displayed_progress[0]
            if delta > 0:
                pbar_non_bus.update(delta)
                non_bus_displayed_progress[0] = current
            pbar_non_bus.refresh()

    monitor_thread = threading.Thread(target=_monitor, daemon=True)
    monitor_thread.start()

    pending_non_bus = {node_id: data for node_id, data in nodes_to_compute}
    non_bus_pool_workers = ctx.workers
    safe_cap = max(1, int(getattr(_PIPELINE_CONFIG, "non_bus_max_workers", non_bus_pool_workers)))
    original = non_bus_pool_workers
    non_bus_pool_workers = max(1, min(non_bus_pool_workers, safe_cap))
    print(
        f"[Non-bus] Using workers={non_bus_pool_workers} "
        f"(requested={original}, cap={safe_cap}).",
        flush=True,
    )
    non_bus_attempt = 0
    try:
        while pending_non_bus:
            completed_nodes = len(nodes_to_compute) - len(pending_non_bus)
            non_bus_base_progress[0] = float((cached_nodes + completed_nodes) * total_non_bus_pois)
            shared_progress = mp.Value("d", 0.0)
            non_bus_shared_progress[0] = shared_progress
            try:
                with mp.Pool(
                    processes=non_bus_pool_workers,
                    initializer=_init_worker,
                    initargs=(
                        ctx.config,
                        snap.poi_bus_snap_info_by_type,
                        snap.poi_mode_snap_info_by_type,
                        ctx.graph,
                        snap.shared_mode_graphs,
                        shared_progress,
                        _PIPELINE_CONFIG.non_bus_cache_dir,
                        _PIPELINE_CONFIG.non_bus_cache_schema_version,
                        _NON_BUS_POI_CONFIG_SIGNATURE,
                    ),
                ) as pool:
                    pending_batch = list(pending_non_bus.items())
                    made_progress = False
                    for partial in pool.imap_unordered(_process_node, pending_batch, chunksize=20):
                        if partial is None:
                            continue
                        node_id = partial["node_id"]
                        cache_path = _non_bus_cache_path(node_id)
                        _write_non_bus_cache(cache_path, partial)
                        if node_id in pending_non_bus:
                            pending_non_bus.pop(node_id, None)
                            made_progress = True
                if not pending_non_bus:
                    break
                if not made_progress:
                    raise RuntimeError(
                         f"Non-bus pool made no progress; remaining_nodes={len(pending_non_bus)}"
                    )
            except Exception as e:
                if isinstance(e, PermissionError):
                    print(
                        "[Non-bus] Multiprocessing unavailable (PermissionError). "
                        "Falling back to sequential execution.",
                        flush=True,
                    )
                    _init_worker(
                        ctx.config,
                        snap.poi_bus_snap_info_by_type,
                        snap.poi_mode_snap_info_by_type,
                        ctx.graph,
                        snap.shared_mode_graphs,
                        None,
                        _PIPELINE_CONFIG.non_bus_cache_dir,
                        _PIPELINE_CONFIG.non_bus_cache_schema_version,
                        _NON_BUS_POI_CONFIG_SIGNATURE,
                    )
                    pending_batch = list(pending_non_bus.items())
                    for node_id, data in pending_batch:
                        partial = _process_node((node_id, data))
                        if partial is None:
                            continue
                        cache_path = _non_bus_cache_path(node_id)
                        _write_non_bus_cache(cache_path, partial)
                        pending_non_bus.pop(node_id, None)
                    break
                non_bus_attempt += 1
                if non_bus_attempt > _PIPELINE_CONFIG.pool_max_retries:
                    raise RuntimeError(
                        f"Non-bus pool failed after {_PIPELINE_CONFIG.pool_max_retries + 1} attempts. Last error: {e}"
                    ) from e
                new_workers = max(1, non_bus_pool_workers // 2)
                print(
                    f"Non-bus pool failed ({type(e).__name__}: {e}). "
                    f"Retry {non_bus_attempt}/{_PIPELINE_CONFIG.pool_max_retries} with workers={new_workers} "
                    f"remaining_nodes={len(pending_non_bus)}."
                )
                non_bus_pool_workers = new_workers
                time.sleep(_PIPELINE_CONFIG.pool_retry_delay_s)
    finally:
        stop_event.set()
        monitor_thread.join(timeout=2)
        if pbar_non_bus:
            non_bus_shared_progress[0] = None
            non_bus_base_progress[0] = float(total_non_bus_tasks)
            remaining_delta = non_bus_base_progress[0] - non_bus_displayed_progress[0]
            if remaining_delta > 0:
                pbar_non_bus.update(remaining_delta)
            pbar_non_bus.close()

    return NonBusRoutingStageResult(
        cache_paths=cache_paths,
        cached_nodes=cached_nodes,
        computed_nodes=len(nodes_to_compute) - len(pending_non_bus),
    )


# compatibility shims
has_valid_non_bus_cache = _has_valid_non_bus_cache
load_non_bus_cache = _load_non_bus_cache
write_non_bus_cache = _write_non_bus_cache
