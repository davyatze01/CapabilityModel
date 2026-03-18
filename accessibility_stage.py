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
import json
import csv
import numpy as np
from numpy.typing import NDArray

_NON_BUS_CACHE_SCHEMA_VERSION = None        # Expected schema version for the cache to prevent reading incompatible cache files
_ACCESS_PROGRESS_VALUE: Any | None = None   # Multiprocess counter shared by workers to update the progress bar

_BUS_IMPEDANCE_MATRIX: NDArray[np.float32] | None = None
_BUS_SOURCE_ID_TO_ROW: dict[str, int] | None = None
_BUS_DEST_COORD_TO_COL: dict[tuple[float, float], int] | None = None
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

# The global variables for each worker are initialized with the passed parameter
def _init_accessibility_worker(
    non_bus_cache_schema_version,
    matrix_path,
    source_id_to_row_path,
    dest_id_to_col_path,
    dest_csv_path,
    access_progress_value=None,
):
    global _NON_BUS_CACHE_SCHEMA_VERSION, _ACCESS_PROGRESS_VALUE
    global _BUS_SOURCE_ID_TO_ROW, _BUS_DEST_COORD_TO_COL, _BUS_IMPEDANCE_MATRIX
    _NON_BUS_CACHE_SCHEMA_VERSION = non_bus_cache_schema_version
    _ACCESS_PROGRESS_VALUE = access_progress_value
    with open(source_id_to_row_path, encoding="utf-8") as f:
        source_id_to_row_raw = json.load(f)
    _BUS_SOURCE_ID_TO_ROW = {str(k): int(v) for k, v in source_id_to_row_raw.items()}

    with open(dest_id_to_col_path, encoding="utf-8") as f:
        dest_id_to_col_raw = json.load(f)
    dest_id_to_col = {str(k): int(v) for k, v in dest_id_to_col_raw.items()}

    _BUS_DEST_COORD_TO_COL = {}
    with open(dest_csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            coord = (round(float(row["lat"]), 6), round(float(row["lon"]), 6))
            col = dest_id_to_col.get(row["id"])
            if col is not None:
                _BUS_DEST_COORD_TO_COL[coord] = int(col)

    _BUS_IMPEDANCE_MATRIX = np.memmap(
        matrix_path, dtype=np.float32, mode="r",
        shape=(len(_BUS_SOURCE_ID_TO_ROW), len(dest_id_to_col))
    )

def _validate_bus_matrix_meta(meta_path, expected_departure_iso, expected_origins_sig, expected_destinations_sig):
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)

    if meta.get("departure_iso") != expected_departure_iso:
        raise RuntimeError("Bus impedance matrix metadata is stale (departure mismatch).")
    if meta.get("origins_sig") != expected_origins_sig:
        raise RuntimeError("Bus impedance matrix metadata is stale (origins signature mismatch).")
    if meta.get("destinations_sig") != expected_destinations_sig:
        raise RuntimeError("Bus impedance matrix metadata is stale (destinations signature mismatch).")




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

        source_id_to_row = _BUS_SOURCE_ID_TO_ROW
        dest_coord_to_col = _BUS_DEST_COORD_TO_COL
        impedance_matrix = _BUS_IMPEDANCE_MATRIX
        if source_id_to_row is None or dest_coord_to_col is None or impedance_matrix is None:
            raise RuntimeError("Bus impedance matrix is not initialized in worker.")
        
        
        source_row = source_id_to_row.get(str(node_id))
        imp_cache: dict[tuple[float, float], float | None] = {}

        def _get_imp(coord_key: tuple[float, float]) -> float | None:
            if coord_key in imp_cache:
                return imp_cache[coord_key]

            if source_row is None:
                imp_cache[coord_key] = None
                return None

            dest_col = dest_coord_to_col.get(coord_key)
            if dest_col is None:
                imp_cache[coord_key] = None
                return None

            v = float(impedance_matrix[source_row, dest_col])
            imp = v if v > 0 else None
            imp_cache[coord_key] = imp
            return imp



        # For each poi_type, compute the accessibility. 
        # Each entry is composed of the poi_type, the impedance values for that poi type from the origin, and the coords of the snapped POIs of that type.
        # The beta constant for accessibility is computed for the poi_type, based on the pre-configured impedance value that will bring the decay function value to 0.5
        # # The bus impedance is loaded from the cached numpy dense matrix and the decay is calculated
        # The decays for each mode are merged and then we merge with the RRA the decays of the POI types
        def _compute_entry_accessibility(entry):
            nonlocal missing_bus_ods
            if entry["cache_hit"]:
                return entry["accessibility_value"]

            poi_type = entry["poi_type"]
            beta = poi_beta_cache.get(poi_type)
            if beta is None:
                beta = math.log(2) / float(serv.get_decay_constant(poi_type))
                poi_beta_cache[poi_type] = beta

            decay_bus = []
            for coord in entry["poi_coords"]:
                coord_key = (round(float(coord[0]), 6), round(float(coord[1]), 6))
                imp_bus = _get_imp(coord_key)
                if imp_bus is None:
                    missing_bus_ods += 1
                    decay_bus.append(0.0)
                else:
                    decay_bus.append(decay.distance_decay(beta, imp_bus))

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
        _validate_bus_matrix_meta(
            ctx.config.bus_impedance_meta_path,
            bus.routing_departure_iso,
            bus.origins_sig,
            bus.destinations_sig,
        )


        while pending:
            base_progress[0] = float(total_nodes - len(pending))
            shared_progress = mp.Value("d", 0.0)
            shared_progress_ref[0] = shared_progress
            try:
                with mp.Pool(
                    processes=pool_workers,
                    initializer=_init_accessibility_worker,
                    initargs=(
                        ctx.config.non_bus_cache_schema_version,
                        ctx.config.bus_impedance_matrix_path,
                        ctx.config.bus_source_id_to_row_path,
                        ctx.config.bus_dest_id_to_col_path,
                        ctx.config.bus_routing_destinations_input_path,
                        shared_progress,
                    ),
                ) as pool:
                    pending_batch = list(pending.items())
                    made_progress = False
                    for data in pool.imap_unordered(_compute_node_accessibility, pending_batch, chunksize=20):
                        if data is None:
                            continue
                        result.node_results.append(data)
                        result.missing_bus_ods_total += int(data.missing_bus_ods)
                        if data.node_id in pending:
                            pending.pop(data.node_id, None)
                            made_progress = True
                if not pending:
                    break
                if not made_progress:
                    raise RuntimeError(
                         f"Accessibility pool made no progress; remaining_nodes={len(pending)}"
                    )
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
