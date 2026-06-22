from __future__ import annotations

import atexit
import os
import pickle
import math
import multiprocessing as mp
import signal
import threading
import time
import uuid
from typing import Any, cast

from tqdm import tqdm
import psutil
import walkability

from context import PipelineContext
from pipeline_types import SnappingStageResult, NonBusRoutingStageResult
from utils import delta_g, services as serv
from snapping_stage import select_best_snap_candidate_for_origin, coord_key
from config import PipelineConfig

# Module-level reference so atexit/signal handlers can always reach the active pool.
_ACTIVE_NON_BUS_POOL: mp.Pool | None = None


def _terminate_active_pool() -> None:
    """Terminate any running non-bus pool. Called by atexit and signal handlers."""
    global _ACTIVE_NON_BUS_POOL
    pool = _ACTIVE_NON_BUS_POOL
    if pool is None:
        return
    _ACTIVE_NON_BUS_POOL = None
    try:
        pool.terminate()
    except Exception:
        pass
    try:
        pool.join(timeout=5)
    except Exception:
        pass


atexit.register(_terminate_active_pool)


def _pool_exit_signal_handler(signum, frame):
    """Terminate any running pools and raise SystemExit so atexit handlers also run."""
    _terminate_active_pool()
    try:
        import accessibility_stage as _acc
        _acc._terminate_active_accessibility_pool()
    except Exception:
        pass
    raise SystemExit(1)


# Only register signal handlers in the main process.
# Worker processes also import this module; registering SIGTERM in them causes
# pool shutdown to trigger Python's atexit chain on already-closed queues → deadlock.
if mp.parent_process() is None:
    # SIGTERM covers `kill <pid>` and process managers.
    # SIGBREAK covers Ctrl+Break on Windows and is the signal Windows sends when a
    # console window is closed (CTRL_CLOSE_EVENT gets mapped to SIGBREAK by Python).
    try:
        signal.signal(signal.SIGTERM, _pool_exit_signal_handler)
    except (OSError, ValueError):
        pass
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, _pool_exit_signal_handler)
        except (OSError, ValueError):
            pass

_POI_RADIUS_LOGGED: set[str] = set()
_POI_BUS_SNAP_INFO: dict[Any, Any] | None = None
_POI_MODE_SNAP_INFO: dict[Any, Any] | None = None
_ORIGIN_NODES_BY_ID: dict[Any, Any] | None = None
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


def _non_bus_version_path(pkl_path: str) -> str:
    return pkl_path + ".v"


def _write_non_bus_cache(path, payload):
    """Write non-bus cache payload for a node.

    Inputs:
    - path: destination pickle path.
    - payload: node-level non-bus cache payload.

    Outputs:
    - None. Writes pickle file and a lightweight .v sidecar with the schema version.
    """
    # Unique temp name per writer (pid + random) so two processes never collide
    # on the same .tmp file — the source of Windows PermissionError(13) "file is
    # used by another process" when workers/retries overlap.
    tmp_path = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    with open(tmp_path, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    # os.replace can transiently fail on Windows when an AV scanner or indexer
    # holds a brief handle on tmp_path or the destination. Retry before giving up.
    for attempt in range(5):
        try:
            os.replace(tmp_path, path)
            break
        except PermissionError:
            if attempt == 4:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                raise
            time.sleep(0.1 * (attempt + 1))
    # Write schema version sidecar so _has_valid_non_bus_cache can skip unpickling.
    try:
        with open(_non_bus_version_path(path), "w") as fv:
            fv.write(str(_NON_BUS_CACHE_SCHEMA_VERSION))
    except OSError:
        pass


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
    if "origin" not in payload or "services" not in payload:
        return False
    if not isinstance(payload["services"], dict):
        return False
    return True


def _has_valid_non_bus_cache(path):
    """Check whether a node cache file exists and is reusable.

    Fast path: reads a tiny .v sidecar file written alongside the pkl to avoid
    fully unpickling the node payload just to check the schema version.
    Falls back to full unpickle for caches that predate the sidecar convention.

    Inputs:
    - path: non-bus cache path.

    Outputs:
    - bool: True when file exists and passes schema validation.
    """
    if not os.path.exists(path):
        return False
    v_path = _non_bus_version_path(path)
    if os.path.exists(v_path):
        try:
            with open(v_path, "r") as fv:
                return int(fv.read().strip()) == _NON_BUS_CACHE_SCHEMA_VERSION
        except Exception:
            pass
    # Sidecar missing — fall back to full unpickle (backward compat).
    try:
        payload = _load_non_bus_cache(path)
    except Exception:
        return False
    return _is_valid_non_bus_cache(payload)


def _init_worker(
    pipeline_config = None,
    poi_bus_snap_info_by_type=None,
    poi_mode_snap_info_by_type=None,
    non_bus_progress_value=None,
    non_bus_cache_dir: str = "",
    non_bus_cache_schema_version: int = 0,
    non_bus_poi_config_signature: str = "",
    walk_graph_signature: str = "",
    origin_nodes_by_id=None,
):
    """Initialize worker state for non-bus multiprocessing stage.

    Inputs:
    - poi_bus_snap_info_by_type: bus snap candidates keyed by POI key.
    - poi_mode_snap_info_by_type: snap candidates split by mode and POI key.
    - non_bus_progress_value: shared progress counter.
    - non_bus_cache_dir: cache directory for node payloads.
    - non_bus_cache_schema_version: expected cache schema version.

    Outputs:
    - None. Populates worker globals and warm caches.
    """
    global _POI_BUS_SNAP_INFO, _POI_MODE_SNAP_INFO, _NON_BUS_PROGRESS_VALUE, _POI_WORK_UNITS_BY_KEY
    global _NON_BUS_CACHE_DIR, _NON_BUS_CACHE_SCHEMA_VERSION, _NON_BUS_POI_CONFIG_SIGNATURE, _PIPELINE_CONFIG
    global _ORIGIN_NODES_BY_ID
    _PIPELINE_CONFIG = pipeline_config
    _ORIGIN_NODES_BY_ID = origin_nodes_by_id or {}
    _NON_BUS_CACHE_DIR = non_bus_cache_dir or ""
    _NON_BUS_CACHE_SCHEMA_VERSION = int(non_bus_cache_schema_version)
    _NON_BUS_POI_CONFIG_SIGNATURE = str(non_bus_poi_config_signature or "")
    _POI_BUS_SNAP_INFO = poi_bus_snap_info_by_type or {}
    _POI_WORK_UNITS_BY_KEY = {}
    for poi_key, snap_info_by_source in _POI_BUS_SNAP_INFO.items():
        _POI_WORK_UNITS_BY_KEY[poi_key] = len(snap_info_by_source)
    _POI_MODE_SNAP_INFO = poi_mode_snap_info_by_type or {}
    _NON_BUS_PROGRESS_VALUE = non_bus_progress_value
    # Graphs are loaded lazily from disk by each worker via delta_g._get_mode_graph.
    # Do NOT pass them via initargs — pickling large NetworkX graphs to N workers
    # over Windows pipes causes the pool to appear to hang.
    delta_g._WALK_GRAPH_SIG_OVERRIDE = str(walk_graph_signature or "") or None
    # In workers the walkability index is loaded from disk and the signature is
    # overridden above, so edge geometry is never read again — drop it on load to
    # cut each worker's graph footprint dramatically.
    delta_g._STRIP_GRAPH_GEOMETRY = True
    # Yield CPU under safe mode so routing workers don't pin the machine at full load.
    from runtime_setup import lower_process_priority_if_safe
    lower_process_priority_if_safe()


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
    # Drop the previous origin's shortest-path caches so per-worker memory stays
    # bounded to a single origin instead of growing across every node processed.
    delta_g.reset_origin_caches()
    origin = (data["y"], data["x"])
    origin_nodes_by_mode = (_ORIGIN_NODES_BY_ID or {}).get(node_id)

    service_results = {}
    for service in serv.SERVICE_KEYS:
        entries = []
        for query in serv.get_service_queries(service):
            poi_key = serv.query_key(query)
            cfg = _PIPELINE_CONFIG
            if cfg is None:
                raise RuntimeError("Worker config not initialized")
            radius_m = serv.get_global_radius_m(cfg)
            try:
                data = delta_g.accessibility_non_bus_from_snap_map(
                    cfg,
                    query.poi_type,
                    origin,
                    (_POI_MODE_SNAP_INFO or {}).get(poi_key, {}),
                    tags=query.tags,
                    radius_m=radius_m,
                    origin_nodes_by_mode=origin_nodes_by_mode,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Non-bus routing failed for node_id={node_id}, poi_type={query.poi_type}, cause={exc!r}"
                ) from exc

            imp_walk = data["imp_walk"]
            imp_bike = data["imp_bike"]
            imp_drive = data["imp_drive"]
            source_items = data.get("source_items", [])
            if not source_items:
                source_coords = data.get("source_coords", [])
                source_items = [{"source_key": coord_key(coord), "source_coord": coord} for coord in source_coords]

            poi_coords = []
            poi_source_keys = []
            poi_source_coords = []
            snap_info_for_key = _POI_BUS_SNAP_INFO.get(poi_key, {}) if _POI_BUS_SNAP_INFO else {}
            for item in source_items:
                coord = tuple(item.get("source_coord", (0.0, 0.0)))
                source_key = item.get("source_key")
                if source_key is None:
                    source_key = coord_key(coord)
                snap_info = snap_info_for_key.get(source_key)
                if snap_info is None:
                    snap_info = snap_info_for_key.get(coord_key(coord))
                snapped_coord, _ = select_best_snap_candidate_for_origin(origin, coord, snap_info)
                poi_coords.append(snapped_coord)
                poi_source_keys.append(source_key)
                poi_source_coords.append(coord)

            entries.append({
                "poi_type": query.poi_type,
                "imp_walk": imp_walk,
                "imp_bike": imp_bike,
                "imp_drive": imp_drive,
                "poi_coords": poi_coords,
                "source_keys": poi_source_keys,
                "source_coords": poi_source_coords,
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

    # Remove any half-written .tmp files left by a previously killed pool worker.
    cache_dir = _PIPELINE_CONFIG.non_bus_cache_dir
    for stale in os.scandir(cache_dir) if os.path.isdir(cache_dir) else []:
        if stale.name.endswith(".tmp"):
            try:
                os.remove(stale.path)
            except OSError:
                pass

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
    walk_graph_signature = ""
    if walk_graph is not None:
        try:
            print("[Non-bus] Preparing walkability edge cache...", flush=True)
            walk_cache_obj = walkability.get_or_build_edge_walkability_index(
                cfg=_PIPELINE_CONFIG,
                G=walk_graph,
                force_rebuild=False,
                schema_version=1,
            )
            walk_graph_signature = walk_cache_obj.graph_signature
            print("[Non-bus] Walkability edge cache ready.", flush=True)
        except Exception as exc:
            print(
                f"[Non-bus] Walkability cache warm-up skipped due to error: {exc}",
                flush=True,
            )

    # Precompute origin node indices per mode once in the parent (one batched KD-tree
    # query per mode), so workers reuse them instead of snapping origins lazily. This
    # also triggers building the compact CSR routing matrices in the parent, so each
    # worker loads a few-MB matrix from disk instead of the full NetworkX graph.
    origin_nodes_by_id: dict[Any, Any] = {}
    if nodes_to_compute and snap.shared_mode_graphs:
        routing_modes = [m for m in ("walk", "bike", "drive") if m in snap.shared_mode_graphs]
        coords_by_id = {
            node_id: (data["y"], data["x"])
            for node_id, data in nodes_to_compute
            if "y" in data and "x" in data
        }
        try:
            origin_nodes_by_id = delta_g.snap_origin_nodes_by_mode(
                coords_by_id, _PIPELINE_CONFIG, modes=tuple(routing_modes)
            )
            print(
                f"[Non-bus] Pre-snapped {len(coords_by_id)} origins to "
                f"{len(routing_modes)} mode graphs.",
                flush=True,
            )
        except Exception as exc:
            print(f"[Non-bus] Origin pre-snap skipped due to error: {exc}", flush=True)
            origin_nodes_by_id = {}

    global_radius_m = serv.get_global_radius_m(_PIPELINE_CONFIG)
    if global_radius_m is not None:
        print(
            f"[Non-bus] POI radius filtering enabled  global_radius={global_radius_m/1000:.1f} km",
            flush=True,
        )

    total_non_bus_pois = 0
    for service in serv.SERVICE_KEYS:
        for query in serv.get_service_queries(service):
            poi_key = serv.query_key(query)
            total_non_bus_pois += len(snap.poi_bus_snap_info_by_type.get(poi_key, {}))
    total_non_bus_tasks = len(ctx.nodes_with_coords) * total_non_bus_pois
    initial_done = cached_nodes * total_non_bus_pois

    pbar_non_bus = tqdm(total=total_non_bus_tasks, initial=initial_done, desc="Non-bus routing stage", mininterval=1) if _PIPELINE_CONFIG.enable_progress else None
    non_bus_base_progress = [float(initial_done)]
    non_bus_displayed_progress = [float(initial_done)]
    non_bus_shared_progress: list[Any | None] = [None]
    stop_event = threading.Event()

    main_proc = psutil.Process()

    def _memory_postfix() -> dict[str, str]:
        # System-wide RAM plus the resident memory of this process tree
        # (parent + worker children), so the operator can see the pool grow.
        vm = psutil.virtual_memory()
        rss = main_proc.memory_info().rss
        for child in main_proc.children(recursive=True):
            try:
                rss += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        gb = 1024.0 ** 3
        return {
            "RAM": f"{vm.percent:.0f}% ({(vm.total - vm.available) / gb:.1f}/{vm.total / gb:.1f}GB)",
            "proc": f"{rss / gb:.1f}GB",
        }

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
            try:
                pbar_non_bus.set_postfix(_memory_postfix(), refresh=False)
            except Exception:
                pass
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
            pool = None
            try:
                pool = mp.Pool(
                    processes=non_bus_pool_workers,
                    maxtasksperchild=200,
                    initializer=_init_worker,
                    initargs=(
                        ctx.config,
                        snap.poi_bus_snap_info_by_type,
                        snap.poi_mode_snap_info_by_type,
                        shared_progress,
                        _PIPELINE_CONFIG.non_bus_cache_dir,
                        _PIPELINE_CONFIG.non_bus_cache_schema_version,
                        _NON_BUS_POI_CONFIG_SIGNATURE,
                        walk_graph_signature,
                        origin_nodes_by_id,
                    ),
                )
                global _ACTIVE_NON_BUS_POOL
                _ACTIVE_NON_BUS_POOL = pool
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
                pool.close()
                pool.join()
                pool = None
                _ACTIVE_NON_BUS_POOL = None
                if not pending_non_bus:
                    break
                if not made_progress:
                    raise RuntimeError(
                         f"Non-bus pool made no progress; remaining_nodes={len(pending_non_bus)}"
                    )
            except (Exception, KeyboardInterrupt) as e:
                if pool is not None:
                    pool.terminate()
                    pool.join()
                    pool = None
                _ACTIVE_NON_BUS_POOL = None
                if isinstance(e, KeyboardInterrupt):
                    raise
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
                        None,
                        _PIPELINE_CONFIG.non_bus_cache_dir,
                        _PIPELINE_CONFIG.non_bus_cache_schema_version,
                        _NON_BUS_POI_CONFIG_SIGNATURE,
                        walk_graph_signature,
                        origin_nodes_by_id,
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
