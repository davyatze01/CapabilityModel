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
from utils import delta_g, services as serv
import json
import csv
import numpy as np
from numpy.typing import NDArray
import hashlib

_NON_BUS_CACHE_SCHEMA_VERSION = None        # Expected schema version for the cache to prevent reading incompatible cache files
_NON_BUS_POI_CONFIG_SIGNATURE: str | None = None
_ACCESS_PROGRESS_VALUE: Any | None = None   # Multiprocess counter shared by workers to update the progress bar

_BUS_IMPEDANCE_MATRIX: NDArray[np.float32] | None = None
_BUS_SOURCE_ID_TO_ROW: dict[str, int] | None = None
_BUS_DEST_COORD_TO_COL: dict[tuple[float, float], int] | None = None
_ACCESS_DEDUPLICATE_ENTRIES: bool = True

# --- incremental accessibility matrix cache helpers ---

def _expected_access_mappings(ctx: PipelineContext) -> tuple[dict[str, int], dict[str, int]]:
    """Build deterministic row/column mappings for accessibility matrix cache.

    Inputs:
    - ctx: pipeline context containing ordered nodes and service/POI config.

    Outputs:
    - tuple `(node_to_row, poi_to_col)` where keys are string ids and values are matrix indices.
    """
    node_ids = [str(node_id) for node_id, _ in ctx.nodes_with_coords]
    node_to_row = {node_id: i for i, node_id in enumerate(node_ids)}
    poi_types = _ordered_poi_types()
    poi_to_col = {poi: j for j, poi in enumerate(poi_types)}
    return node_to_row, poi_to_col

def _write_access_meta(
        ctx: PipelineContext,
        run_sig: str,
        n_rows: int,
        n_cols: int,
) -> None:
    """Write metadata companion file for accessibility matrix cache.

    Inputs:
    - ctx: pipeline context with cache configuration paths.
    - run_sig: signature that identifies cache compatibility for current run.
    - n_rows: number of node rows in the matrix.
    - n_cols: number of POI-type columns in the matrix.

    Outputs:
    - None. Writes JSON metadata to disk.
    """
    with open(ctx.config.accessibility_meta_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "schema_version": int(ctx.config.accessibility_matrix_schema_version),
                "run_signature": run_sig,
                "shape": [n_rows, n_cols],
                "dtype": "float32",
            },
            f,
            ensure_ascii=False,
        )

def _load_json_dict_int(path: str) -> dict[str, int]:
    """Load JSON mapping and normalize values to integers.

    Inputs:
    - path: JSON file path containing a dictionary-like mapping.

    Outputs:
    - dict with string keys and integer values.
    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return {str(k): int(v) for k, v in raw.items()}


def _compute_chunksize(total_items: int, workers: int, requested: int) -> int:
    """Pick a chunk size that reduces IPC overhead while keeping workers fed."""
    if total_items <= 0:
        return 1
    workers = max(1, int(workers))
    requested = max(1, int(requested))
    auto = max(1, total_items // (workers * 4))
    return max(1, min(requested, auto if auto > 0 else requested))

# Load cached files from non-bus routing
def _load_non_bus_cache(path):
    """Load per-node non-bus cache payload from pickle.

    Inputs:
    - path: file path to non-bus cache for one origin node.

    Outputs:
    - dict-like cache payload for that node.
    """
    with open(path, "rb") as f:
        return pickle.load(f)

# If the schema version is outdated, that non-bus cache is invalid
def _is_valid_non_bus_cache(payload):
    """Validate minimal non-bus cache schema compatibility.

    Inputs:
    - payload: object loaded from non-bus cache file.

    Outputs:
    - bool: True when payload matches expected schema version and shape.
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

def _ordered_poi_types() -> list[str]:
    """Return stable POI-type order used as accessibility matrix columns.

    Inputs:
    - none.

    Outputs:
    - list of unique POI types preserving first appearance across service queries.
    """
    seen = set()
    out = []
    for service in serv.SERVICE_KEYS:
        for q in serv.get_service_queries(service):
            if q.poi_type not in seen:
                seen.add(q.poi_type)
                out.append(q.poi_type)
    return out


def _accessibility_run_signature(ctx: PipelineContext, bus: BusRoutingStageResult) -> str:
    """Compute cache compatibility signature for accessibility matrix artifacts.

    Inputs:
    - ctx: pipeline context with schema/config values.
    - bus: bus routing stage result carrying routing signatures/timestamp.

    Outputs:
    - str SHA1 signature used to validate cache reuse.
    """
    payload = {
        "schema": int(ctx.config.accessibility_matrix_schema_version),
        "bus_departure": bus.routing_departure_iso,
        "bus_origins_sig": bus.origins_sig,
        "bus_destinations_sig": bus.destinations_sig,
        "non_bus_cache_schema": int(ctx.config.non_bus_cache_schema_version),
        "poi_config_signature": serv.config_signature(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()



def _open_or_create_accessibility_matrix_cache(
    ctx: PipelineContext,
    bus: BusRoutingStageResult,
) -> tuple[np.memmap | None, dict[str, int], dict[str, int]]:
    """Open compatible accessibility matrix cache or create a new blank one.

    Inputs:
    - ctx: pipeline context with cache paths and node universe.
    - bus: bus routing stage result used for run-signature compatibility.

    Outputs:
    - tuple `(matrix_memmap_or_none, node_to_row, poi_to_col)`.
    """
    if not ctx.config.accessibility_matrix_cache_enabled:
        return None, {}, {}
    
    expected_node_to_row, expected_poi_to_col = _expected_access_mappings(ctx)
    run_sig = _accessibility_run_signature(ctx, bus)
    n_rows = len(expected_node_to_row)
    n_cols = len(expected_poi_to_col)

    os.makedirs(os.path.dirname(ctx.config.accessibility_matrix_path) or ".", exist_ok=True)

    needed = [
        ctx.config.accessibility_meta_path,
        ctx.config.accessibility_node_to_row_path,
        ctx.config.accessibility_poi_to_col_path,
        ctx.config.accessibility_matrix_path,
    ]
    cache_exists = all(os.path.exists(p) for p in needed)

    # Reuse cache only when metadata, dimensions, and index mappings match exactly.
    if cache_exists:
        try:
            with open(ctx.config.accessibility_meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            node_to_row = _load_json_dict_int(ctx.config.accessibility_node_to_row_path)
            poi_to_col = _load_json_dict_int(ctx.config.accessibility_poi_to_col_path)

            compatible = (
                int(meta.get("schema_version", -1)) == int(ctx.config.accessibility_matrix_schema_version)
                and meta.get("run_signature") == run_sig
                and meta.get("shape") == [n_rows, n_cols]
                and node_to_row == expected_node_to_row
                and poi_to_col == expected_poi_to_col
            )

            if compatible:
                mat = np.memmap(
                    ctx.config.accessibility_matrix_path,
                    dtype=np.float32,
                    mode="r+",
                    shape=(n_rows, n_cols),
                )
                return mat, node_to_row, poi_to_col
        except Exception:
            pass  # fall through to rebuild

    # Otherwise rebuild a blank matrix initialized with NaN (not-yet-computed sentinel).
    mat = np.memmap(
        ctx.config.accessibility_matrix_path,
        dtype=np.float32,
        mode="w+",
        shape=(n_rows, n_cols),
    )
    mat[:] = np.nan
    mat.flush()

    with open(ctx.config.accessibility_node_to_row_path, "w", encoding="utf-8") as f:
        json.dump(expected_node_to_row, f, ensure_ascii=False)
    with open(ctx.config.accessibility_poi_to_col_path, "w", encoding="utf-8") as f:
        json.dump(expected_poi_to_col, f, ensure_ascii=False)
    _write_access_meta(ctx, run_sig, n_rows, n_cols)

    return mat, expected_node_to_row, expected_poi_to_col

def _row_is_complete(mat: np.memmap, row: int, required_cols: np.ndarray) -> bool:
    """Check whether one node row has all required POI values already computed.

    Inputs:
    - mat: accessibility matrix memmap.
    - row: row index for one source node.
    - required_cols: array of POI column indices required by current configuration.

    Outputs:
    - bool: True when the row has no NaN in required columns.
    """
    return not np.isnan(mat[row, required_cols]).any()

def _build_node_result_from_matrix_row(
    node_id: Any,
    node_data: dict[str, Any],
    row: int,
    mat: np.memmap,
    poi_to_col: dict[str, int],
) -> AccessibilityNodeResult:
    """Reconstruct node-level accessibility payload from one cache matrix row.

    Inputs:
    - node_id: source node id.
    - node_data: source node attributes containing coordinates.
    - row: row index of the source node in matrix.
    - mat: accessibility matrix memmap.
    - poi_to_col: POI-type to matrix-column mapping.

    Outputs:
    - AccessibilityNodeResult rebuilt in the same format produced by compute path.
    """
    accessibility_by_service: dict[str, list[dict[str, Any]]] = {}
    for service in serv.SERVICE_KEYS:
        items: list[dict[str, Any]] = []
        for q in serv.get_service_queries(service):
            col = poi_to_col.get(q.poi_type)
            value = 0.0
            if col is not None:
                v = float(mat[row, col])
                value = 0.0 if np.isnan(v) else v
            items.append({"poi_type": q.poi_type, "accessibility": value})
        accessibility_by_service[service] = items

    return AccessibilityNodeResult(
        node_id=node_id,
        lat=float(node_data["y"]),
        lon=float(node_data["x"]),
        accessibility_by_service=accessibility_by_service,
    )

def _write_node_result_to_matrix(
    mat: np.memmap,
    row: int,
    poi_to_col: dict[str, int],
    node_result: AccessibilityNodeResult,
) -> None:
    """Write one computed node result into its matrix row.

    Inputs:
    - mat: accessibility matrix memmap.
    - row: destination row index for this node.
    - poi_to_col: POI-type to matrix-column mapping.
    - node_result: computed accessibility payload for one node.

    Outputs:
    - None. Updates matrix cells in-place.
    """
    for service in serv.SERVICE_KEYS:
        for item in node_result.accessibility_by_service.get(service, []):
            poi = str(item["poi_type"])
            col = poi_to_col.get(poi)
            if col is not None:
                mat[row, col] = np.float32(float(item["accessibility"]))
                
# The global variables for each worker are initialized with the passed parameter
def _init_accessibility_worker(
    non_bus_cache_schema_version,
    non_bus_poi_config_signature,
    matrix_path,
    source_id_to_row_path,
    dest_id_to_col_path,
    dest_csv_path,
    deduplicate_entries=True,
    access_progress_value=None,
):
    """Initialize worker-local state for accessibility multiprocessing.

    Inputs:
    - non_bus_cache_schema_version: expected cache schema version.
    - matrix_path: path to dense bus impedance matrix.
    - source_id_to_row_path: path to origin-id -> row index JSON.
    - dest_id_to_col_path: path to destination-id -> column index JSON.
    - dest_csv_path: path to destination CSV used to map coords to destination ids.
    - access_progress_value: optional shared progress counter.

    Outputs:
    - None. Populates worker globals used during node computation.
    """
    global _NON_BUS_CACHE_SCHEMA_VERSION, _NON_BUS_POI_CONFIG_SIGNATURE, _ACCESS_PROGRESS_VALUE
    global _BUS_SOURCE_ID_TO_ROW, _BUS_DEST_COORD_TO_COL, _BUS_IMPEDANCE_MATRIX
    global _ACCESS_DEDUPLICATE_ENTRIES
    _NON_BUS_CACHE_SCHEMA_VERSION = non_bus_cache_schema_version
    _NON_BUS_POI_CONFIG_SIGNATURE = str(non_bus_poi_config_signature or "")
    _ACCESS_DEDUPLICATE_ENTRIES = bool(deduplicate_entries)
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
    """Validate bus matrix metadata before accessibility computation starts.

    Inputs:
    - meta_path: metadata JSON path.
    - expected_departure_iso: departure datetime expected by current run.
    - expected_origins_sig: expected origin signature.
    - expected_destinations_sig: expected destination signature.

    Outputs:
    - None. Raises RuntimeError when metadata is stale or incompatible.
    """
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
    """Compute service-grouped accessibility for a single origin node.

    Inputs:
    - item: `(node_id, cache_path)` pair for one origin node.

    Outputs:
    - AccessibilityNodeResult for valid caches, or None when node cache is unusable.
    """
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

        poi_beta_cache: dict[str, float] = {}
        services_state = state.get("services", {})

        source_id_to_row = _BUS_SOURCE_ID_TO_ROW
        dest_coord_to_col = _BUS_DEST_COORD_TO_COL
        impedance_matrix = _BUS_IMPEDANCE_MATRIX
        if source_id_to_row is None or dest_coord_to_col is None or impedance_matrix is None:
            raise RuntimeError("Bus impedance matrix is not initialized in worker.")
        
        
        source_row = source_id_to_row.get(str(node_id))
        imp_cache: dict[tuple[float, float], float | None] = {}
        coord_key_cache: dict[tuple[float, float], tuple[float, float]] = {}
        exp = math.exp

        def _get_imp(coord_key: tuple[float, float]) -> float | None:
            """Return bus impedance for one destination coordinate using lazy memoization.

            Inputs:
            - coord_key: destination coordinate key `(lat, lon)` rounded to 6 decimals.

            Outputs:
            - float impedance in minutes, or None if lookup is unavailable.
            """
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

        def _normalize_coord_key(coord: tuple[float, float]) -> tuple[float, float]:
            cached = coord_key_cache.get(coord)
            if cached is not None:
                return cached
            norm = (round(float(coord[0]), 6), round(float(coord[1]), 6))
            coord_key_cache[coord] = norm
            return norm



        # For each poi_type, compute the accessibility. 
        # Each entry is composed of the poi_type, the impedance values for that poi type from the origin, and the coords of the snapped POIs of that type.
        # The beta constant for accessibility is computed for the poi_type, based on the pre-configured impedance value that will bring the decay function value to 0.5
        # # The bus impedance is loaded from the cached numpy dense matrix and the decay is calculated
        # The decays for each mode are merged and then we merge with the RRA the decays of the POI types
        def _compute_entry_accessibility(entry):
            """Compute accessibility for one POI-type entry of a service.

            Inputs:
            - entry: non-bus cache entry with mode decays and destination coordinates.

            Outputs:
            - accessibility_value
            """
            poi_type = entry["poi_type"]
            beta = poi_beta_cache.get(poi_type)
            if beta is None:
                beta = math.log(2) / float(serv.get_decay_constant(poi_type))
                poi_beta_cache[poi_type] = beta

            decay_bus = []
            neg_beta = -beta
            for coord in entry["poi_coords"]:
                coord_key = _normalize_coord_key(coord)
                imp_bus = _get_imp(coord_key)
                if imp_bus is None:
                    decay_bus.append(0.0)
                else:
                    decay_bus.append(exp(neg_beta * imp_bus))

            imp_walk = entry.get("imp_walk", [])
            imp_bike = entry.get("imp_bike", [])
            imp_drive = entry.get("imp_drive", [])
            decay_walk = []
            decay_bike = []
            decay_drive = []
            for imp in imp_walk:
                decay_walk.append(0.0 if imp is None else exp(neg_beta * float(imp)))
            for imp in imp_bike:
                decay_bike.append(0.0 if imp is None else exp(neg_beta * float(imp)))
            for imp in imp_drive:
                decay_drive.append(0.0 if imp is None else exp(neg_beta * float(imp)))

            # Use RRA to aggregate decays for each modality
            _, acc = delta_g.merge_rra_and_accessibility(
                decay_walk,
                decay_bike,
                decay_drive,
                decay_bus,
                poi_type=entry.get("poi_type"),
            )
            return acc
        
        # Group the poi types based on the services distribution
        accessibility_by_service = {}
        if _ACCESS_DEDUPLICATE_ENTRIES:
            entry_by_key: dict[str, dict[str, Any]] = {}
            for service in serv.SERVICE_KEYS:
                entries = services_state.get(service, [])
                for entry in entries:
                    key = str(entry["poi_type"])
                    if key not in entry_by_key:
                        entry_by_key[key] = entry

            value_by_key: dict[str, float] = {}
            for key, entry in entry_by_key.items():
                value = _compute_entry_accessibility(entry)
                value_by_key[key] = value

            for service in serv.SERVICE_KEYS:
                entries = services_state.get(service, [])
                service_items = []
                for entry in entries:
                    key = str(entry["poi_type"])
                    service_items.append({"poi_type": entry.get("poi_type"), "accessibility": value_by_key[key]})
                accessibility_by_service[service] = service_items
        else:
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
    """Run accessibility stage over all nodes with multiprocessing and retries.

    Inputs:
    - ctx: pipeline context with worker count, config paths, and progress settings.
    - non_bus: non-bus stage result with per-node cache paths.
    - bus: bus stage result with routing signatures used for metadata validation.

    Outputs:
    - AccessibilityStageResult: node-level accessibility outputs.
    """
    pool_workers = ctx.workers
    attempt = 0
    result = AccessibilityStageResult()
    mat = None
    node_to_row: dict[str, int] = {}
    poi_to_col: dict[str, int] = {}

    mat, node_to_row, poi_to_col = _open_or_create_accessibility_matrix_cache(ctx, bus)

    # Preload complete node rows from cache and compute only missing rows.
    pending: dict[Any, str] = {}
    if mat is not None:
        required_cols = np.array(
            [poi_to_col[poi] for poi in _ordered_poi_types() if poi in poi_to_col],
            dtype=np.int64,
        )
        for node_id, data in ctx.nodes_with_coords:
            row = node_to_row[str(node_id)]
            if required_cols.size > 0 and _row_is_complete(mat, row, required_cols):
                result.node_results.append(
                    _build_node_result_from_matrix_row(node_id, data, row, mat, poi_to_col)
                )
            else:
                cache_path = non_bus.cache_paths.get(node_id)
                if cache_path is None:
                    cache_path = non_bus.cache_paths.get(str(node_id))
                if cache_path is not None:
                    pending[node_id] = cache_path
    else:
        pending = {}
        for node_id, _ in ctx.nodes_with_coords:
            cache_path = non_bus.cache_paths.get(node_id)
            if cache_path is None:
                cache_path = non_bus.cache_paths.get(str(node_id))
            if cache_path is not None:
                pending[node_id] = cache_path
    
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
                        ctx.config.non_bus_cache_schema_version,
                        serv.config_signature(),
                        ctx.config.bus_impedance_matrix_path,
                        ctx.config.bus_source_id_to_row_path,
                        ctx.config.bus_dest_id_to_col_path,
                        ctx.config.bus_routing_destinations_input_path,
                        ctx.config.accessibility_deduplicate_entries,
                        shared_progress,
                    ),
                ) as pool:
                    pending_batch = list(pending.items())
                    made_progress = False
                    chunksize = _compute_chunksize(
                        total_items=len(pending_batch),
                        workers=pool_workers,
                        requested=ctx.config.accessibility_chunksize,
                    )
                    for data in pool.imap_unordered(_compute_node_accessibility, pending_batch, chunksize=chunksize):
                        if data is None:
                            continue
                        result.node_results.append(data)
                        if mat is not None:
                            row = node_to_row.get(str(data.node_id))
                            if row is not None:
                                _write_node_result_to_matrix(mat, row, poi_to_col, data)
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

    if mat is not None:
        mat.flush()
    return result


# compatibility alias for old code
compute_node_accessibility = _compute_node_accessibility
