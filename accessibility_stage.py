import os
import pickle
import math
import multiprocessing as mp
import threading
import time
from typing import Any, cast
from tqdm import tqdm
from context import PipelineContext
from pipeline_types import (
    BusRoutingStageResult,
    NonBusRoutingStageResult,
    AccessibilityStageResult,
    AccessibilityNodeResult,
)
from utils import decay, delta_g, services as serv

_R5_ROUTING_PKL_PATH = None                 # Path of the pickle cache for bus routing
_R5_DEPARTURE_ISO = None                    # Day and time of departure for the bus query, displayed in iso format. It is always 15-10-2025 midday
_R5_ROUTING_CACHE = None                    # Object that will contain the bus routing cache
_NON_BUS_CACHE_SCHEMA_VERSION = None        # Expected schema version for the cache to prevent reading incompatible cache files
_ACCESS_PROGRESS_VALUE: Any | None = None   # Multiprocess counter shared by workers to update the progress bar

# Load cached files from non-bus routing
def _load_non_bus_cache(path):
    with open(path, "rb") as f:
        return pickle.load(f)

# If the schema version is outdated, that non-bus cache is invalid
def _is_valid_non_bus_cache(payload):
    if not isinstance(payload, dict):
        return False
    if payload.get("schema_version") != _NON_BUS_CACHE_SCHEMA_VERSION:
        return False
    if "origin" not in payload or "services" not in payload:
        return False
    if not isinstance(payload["services"], dict):
        return False
    return True

def _is_valid_routing_cache(payload):
    if not isinstance(payload, dict):
        return False
    if "departure_iso" not in payload:
        return False
    if "origins_sig" not in payload:
        return False
    if "destinations_sig" not in payload:
        return False
    if "routes" not in payload:
        return False
    if not isinstance(payload["routes"], dict):
        return False
    return True

# The global variables for each worker are initialized with the passed parameter
def _init_accessibility_worker(routing_pkl_path, departure_iso, non_bus_cache_schema_version, access_progress_value=None):
    global _R5_ROUTING_PKL_PATH, _R5_DEPARTURE_ISO, _R5_ROUTING_CACHE, _NON_BUS_CACHE_SCHEMA_VERSION, _ACCESS_PROGRESS_VALUE
    _R5_ROUTING_PKL_PATH = routing_pkl_path
    _R5_DEPARTURE_ISO = departure_iso
    _R5_ROUTING_CACHE = None
    _NON_BUS_CACHE_SCHEMA_VERSION = non_bus_cache_schema_version
    _ACCESS_PROGRESS_VALUE = access_progress_value

# Load cached files from r5 bus routing
def _get_routing_cache():
    global _R5_ROUTING_CACHE
    if _R5_ROUTING_CACHE is None:
        if not _R5_ROUTING_PKL_PATH:
            raise RuntimeError("Routing cache path is not configured in worker.")
        with open(_R5_ROUTING_PKL_PATH, "rb") as f:
            payload = pickle.load(f)
        if not _is_valid_routing_cache(payload):
            raise RuntimeError(
                f"Invalid routing cache payload: {_R5_ROUTING_PKL_PATH}"
            )
        _R5_ROUTING_CACHE = payload
    return _R5_ROUTING_CACHE
# Computes the accessibility value for one node of the graph, which is a source.
# It is the result of the aggregation of singular accessibility calculations for each set of poi_type in the graph.
# The accessibility values of the poi_types are grouped based on the service distribution
def _compute_node_accessibility(item):
    try:
        node_id, cache_path = item
        if not os.path.exists(cache_path):
            return None
        try:
            state = _load_non_bus_cache(cache_path)
        except Exception:
            return None
        if not _is_valid_non_bus_cache(state):
            return None

        missing_bus_ods = 0
        poi_beta_cache: dict[str, float] = {}
        services_state = state.get("services", {})
        needed_bus_destinations: set[tuple[float, float]] = set()
        for service in serv.SERVICE_KEYS:
            entries = services_state.get(service, [])
            for entry in entries:
                if entry.get("cache_hit"):
                    continue
                for coord in entry.get("poi_coords", []):
                    needed_bus_destinations.add((round(float(coord[0]), 6), round(float(coord[1]), 6)))

        if needed_bus_destinations:
            routing_cache = _get_routing_cache()
            if _R5_DEPARTURE_ISO is None:
                raise RuntimeError("Routing departure ISO is not configured in worker.")
            if routing_cache.get("departure_iso") != _R5_DEPARTURE_ISO:
                raise RuntimeError("Routing cache departure does not match worker departure.")
            
            origin_key = (
                round(float(state["origin"][0]), 6),
                round(float(state["origin"][1]), 6),
            )
            
            routes = routing_cache.get("routes", {})
            bus_impedance_by_destination = {}
            for dest in needed_bus_destinations:
                dest_key = (
                    round(float(dest[0]), 6),
                    round(float(dest[1]), 6),
                )
                route_info = routes.get((origin_key, dest_key))
                if route_info is not None:
                    impedance = route_info.get("impedance")
                    if impedance is not None:
                        bus_impedance_by_destination[dest_key] = float(impedance)

        else:
            bus_impedance_by_destination = {}


        # For each poi_type, compute the accessibility. 
        # Each entry is composed of the poi_type, the impedance values for that poi type from the origin, and the coords of the snapped POIs of that type.
        # The beta constant for accessibility is computed for the poi_type, based on the pre-configured impedance value that will bring the decay function value to 0.5
        # # The bus impedance is loaded from the routing pickle cache and the decay is calculated
        # The decays for each mode are merged and then we merge with the RRA the decays of the POI types
        def _compute_entry_accessibility(entry):
            # If accessibility is already computed, return the cached value
            nonlocal missing_bus_ods, bus_impedance_by_destination
            if entry["cache_hit"]:
                return entry["accessibility_value"]

            # Calculate the constant for decay function based on the impedance value that brings decay to 0.5
            poi_type = entry["poi_type"]
            beta = poi_beta_cache.get(poi_type)
            if beta is None:
                beta = math.log(2) / float(serv.get_decay_constant(poi_type))
                poi_beta_cache[poi_type] = beta
            decay_bus = []

            # Calculate the decay based on impedance value
            for coord in entry["poi_coords"]:
                coord_key = (round(float(coord[0]), 6), round(float(coord[1]), 6))
                imp_bus = bus_impedance_by_destination.get(coord_key)
                if imp_bus is None:
                    missing_bus_ods += 1
                    imp_bus = None
                if imp_bus:
                    decay_bus.append(decay.distance_decay(beta, imp_bus))
                else:
                    decay_bus.append(0.0)

            # Use RRA to aggregate decays for each modality
            rra, acc = delta_g.merge_rra_and_accessibility(
                entry["decay_walk"],
                entry["decay_bike"],
                entry["decay_drive"],
                decay_bus,
                poi_type=entry.get("poi_type"),
            )
            delta_g.save_rra(entry["cache_file"], rra)
            return acc

        # Group the poi types based on the services distribution
        accessibility_by_service = {}
        for service in serv.SERVICE_KEYS:
            entries = services_state.get(service, [])
            service_items = []
            for entry in entries:
                value = _compute_entry_accessibility(entry)
                service_items.append({"poi_type": entry.get("poi_type"), "accessibility": value})
            accessibility_by_service[service] = service_items

        return AccessibilityNodeResult(
            node_id=node_id,
            lat=state["origin"][0],
            lon=state["origin"][1],
            accessibility_by_service=accessibility_by_service,
            missing_bus_ods=missing_bus_ods,
        )
    finally:
        if _ACCESS_PROGRESS_VALUE is not None:
            with _ACCESS_PROGRESS_VALUE.get_lock():
                _ACCESS_PROGRESS_VALUE.value += 1.0

# Initializes workers and each computes node accessibility for the assigned nodes
def run_accessibility_stage(
    ctx: PipelineContext,
    non_bus: NonBusRoutingStageResult,
    bus: BusRoutingStageResult,
) -> AccessibilityStageResult:
    pending = {node_id: non_bus.cache_paths[node_id] for node_id, _ in ctx.nodes_with_coords}
    pool_workers = ctx.workers
    attempt = 0
    result = AccessibilityStageResult()
    total_nodes = len(pending)
    pbar = tqdm(total=total_nodes, desc="Accessibility stage", mininterval=1) if ctx.config.enable_progress else None
    base_progress = [0.0]
    displayed_progress = [0.0]
    shared_progress_ref: list[Any | None] = [None]
    stop_event = threading.Event()

    def _monitor():
        while not stop_event.wait(1):
            if pbar is None:
                continue
            current = base_progress[0]
            shared = shared_progress_ref[0]
            if shared is not None:
                shared_obj = cast(Any, shared)
                with shared_obj.get_lock():
                    current += float(shared_obj.value)
            pbar_total = float(pbar.total or total_nodes)
            if current > pbar_total:
                current = pbar_total
            delta = current - displayed_progress[0]
            if delta > 0:
                pbar.update(delta)
                displayed_progress[0] = current
            pbar.refresh()

    monitor_thread = threading.Thread(target=_monitor, daemon=True)
    monitor_thread.start()

    try:
        while pending:
            base_progress[0] = float(total_nodes - len(pending))
            shared_progress = mp.Value("d", 0.0)
            shared_progress_ref[0] = shared_progress
            try:
                with mp.Pool(
                    processes=pool_workers,
                    initializer=_init_accessibility_worker,
                    initargs=(
                        bus.routing_pkl,
                        bus.routing_departure_iso,
                        ctx.config.non_bus_cache_schema_version,
                        shared_progress,
                    ),
                ) as pool:
                    pending_batch = list(pending.items())
                    for data in pool.imap_unordered(_compute_node_accessibility, pending_batch, chunksize=20):
                        if data is None:
                            continue
                        result.node_results.append(data)
                        result.missing_bus_ods_total += int(data.missing_bus_ods)
                        pending.pop(data.node_id, None)
                break
            except Exception as e:
                attempt += 1
                if attempt > ctx.config.pool_max_retries:
                    raise RuntimeError(
                        f"Accessibility pool failed after {ctx.config.pool_max_retries + 1} attempts. Last error: {e}"
                    ) from e
                new_workers = max(1, pool_workers // 2)
                print(
                    f"Accessibility pool failed ({type(e).__name__}: {e}). "
                    f"Retry {attempt}/{ctx.config.pool_max_retries} with workers={new_workers} "
                    f"remaining_nodes={len(pending)}."
                )
                pool_workers = new_workers
                time.sleep(ctx.config.pool_retry_delay_s)
    finally:
        stop_event.set()
        monitor_thread.join(timeout=2)
        if pbar:
            shared_progress_ref[0] = None
            base_progress[0] = float(total_nodes)
            remaining = base_progress[0] - displayed_progress[0]
            if remaining > 0:
                pbar.update(remaining)
            pbar.refresh()
            pbar.close()

    return result


# compatibility alias for old code
compute_node_accessibility = _compute_node_accessibility
