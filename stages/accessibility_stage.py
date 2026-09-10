import atexit
import os
import pickle
import math
import multiprocessing as mp
import sqlite3
import threading
import time
from typing import Any, cast
from tqdm import tqdm
from core.context import PipelineContext
from core.pipeline_types import (
    BusRoutingStageResult,
    NonBusRoutingStageResult,
    AccessibilityStageResult,
    AccessibilityNodeResult,
)
from utils import delta_g, decay, services as serv
import json
import csv
import numpy as np
from numpy.typing import NDArray
import hashlib

_NON_BUS_CACHE_SCHEMA_VERSION = None        # Expected schema version for the cache to prevent reading incompatible cache files
_NON_BUS_POI_CONFIG_SIGNATURE: str | None = None
_ACCESS_PROGRESS_VALUE: Any | None = None   # Multiprocess counter shared by workers to update the progress bar

_ACTIVE_ACCESSIBILITY_POOL = None  # mp.Pool | None


def _terminate_active_accessibility_pool() -> None:
    """Terminate any running accessibility pool. Called by atexit and signal handlers."""
    global _ACTIVE_ACCESSIBILITY_POOL
    pool = _ACTIVE_ACCESSIBILITY_POOL
    if pool is None:
        return
    _ACTIVE_ACCESSIBILITY_POOL = None
    try:
        pool.terminate()
    except Exception:
        pass
    try:
        pool.join(timeout=5)
    except Exception:
        pass


atexit.register(_terminate_active_accessibility_pool)

_BUS_IMPEDANCE_MATRIX: NDArray[np.float32] | None = None
_BUS_SOURCE_ID_TO_ROW: dict[str, int] | None = None
# Optional second public-transport modality (subway). Left None when the city has no subway,
# in which case subway is excluded from the RRA entirely (mode count stays 4).
_SUBWAY_IMPEDANCE_MATRIX: NDArray[np.float32] | None = None
_SUBWAY_SOURCE_ID_TO_ROW: dict[str, int] | None = None
_ACCESS_DEDUPLICATE_ENTRIES: bool = True
# Per-service POI dedup: source_keys each poi_type must drop (owned by another type).
_POI_DROP_BY_TYPE: dict[str, set[str]] = {}
# Shared per-poi_type {src_keys, source_coords} catalog (see exports/artifact_bundle.py):
# origin-invariant POI identity, loaded once per worker. Each node entry's "kept_idx"
# indexes into this instead of carrying its own copy of the keys/coords.
_POI_CATALOG: dict[str, dict[str, Any]] = {}

_POI_RADIUS_ENABLED: bool = False

# Per-node sparse accessibility_by_poi is written to a compressed .npz right here in the
# worker, as soon as it's computed, instead of being returned through IPC. That dict can
# hold thousands of entries per node in dense areas (Paris), so shipping it to the main
# process and only compressing it there meant every worker buffered the full uncompressed
# payload for its entire chunk before anything was written or freed.
_POI_BY_NODE_DIR: str = ""
_SOURCE_KEY_TO_ID: dict[str, int] = {}

# Individual-profile knobs (see profiles.py). Defaults reproduce the baseline universal
# traveler exactly: every non-bus mode fused, per-POI utility multiplier u(y) = 1.0 for all POIs.
_ENABLED_NON_BUS_MODES: tuple[str, ...] = ("walk", "bike", "drive")
_POI_UTIL_DEFAULT: float = 1.0        # general affordability multiplier
_POI_UTIL_CANTEEN: float = 1.0        # status-benefit override for canteen instances
_CANTEEN_OSMIDS: frozenset[str] = frozenset()  # OSM ids the canteen override applies to


def _utility_for_source_key(source_key, poi_type: str) -> float:
    """Per-POI utility u(y): canteen override for canteen instances, else affordability for
    paid poi_types, else 1.0 (free/public)."""
    if _CANTEEN_OSMIDS:
        from core.profiles import osmid_from_source_key
        osmid = osmid_from_source_key(source_key)
        if osmid is not None and osmid in _CANTEEN_OSMIDS:
            return _POI_UTIL_CANTEEN
    if _POI_UTIL_DEFAULT != 1.0:
        from core.profiles import PAID_POI_TYPES
        if poi_type in PAID_POI_TYPES:
            return _POI_UTIL_DEFAULT
    return 1.0

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
    from utils import poi_dedup
    dedup_enabled = bool(ctx.config.poi_service_dedup_enabled and poi_dedup.is_osm_mode(ctx.config))
    payload = {
        "schema": int(ctx.config.accessibility_matrix_schema_version),
        "bus_departure": bus.routing_departure_iso,
        "bus_origins_sig": bus.origins_sig,
        "bus_destinations_sig": bus.destinations_sig,
        "non_bus_cache_schema": int(ctx.config.non_bus_cache_schema_version),
        "poi_config_signature": serv.config_signature(),
        # Toggling/altering per-service POI dedup changes per-poi_type accessibility,
        # so its state must invalidate the cached matrix.
        "poi_dedup_enabled": dedup_enabled,
        "poi_dedup_drop_sig": _file_sha1(ctx.config.poi_ownership_drop_path) if dedup_enabled else "",
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _file_sha1(path: str) -> str:
    """SHA1 of a file's bytes, or empty string when absent/unreadable."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except Exception:
        return ""



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

def _load_poi_by_node(path: str) -> dict[str, dict[str, float]]:
    """Load per-node per-POI accessibility from companion JSON file.

    Inputs:
    - path: companion file path written after each accessibility run.

    Outputs:
    - dict mapping node_id_str -> {source_key: accessibility_value}.
      Returns an empty dict when the file is absent or unreadable.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_source_key_to_id(gpkg_path: str) -> dict[str, int]:
    """Read source_key -> integer poi id from the POI export GeoPackage.

    Uses sqlite3 directly to avoid loading geometry data.
    Returns {} when the GPKG is absent or lacks the expected table/columns.
    """
    if not os.path.exists(gpkg_path):
        return {}
    try:
        con = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
        rows = con.execute("SELECT source_key, id FROM pois_used").fetchall()
        con.close()
        return {row[0]: int(row[1]) for row in rows}
    except Exception:
        return {}


def _write_poi_by_node_sparse(
    node_id: Any,
    accessibility_by_poi: dict[str, float],
    source_key_to_id: dict[str, int],
    out_dir: str,
) -> None:
    """Persist per-POI accessibility for one node as a compact compressed numpy file.

    Stores only nonzero values whose source_key has a known poi id in source_key_to_id.
    The file {out_dir}/{node_id}.npz contains two 1-D arrays:
      - 'poi_ids'  uint32: poi integer ids from the POI export GPKG
      - 'values'   float32: corresponding accessibility values
    """
    poi_ids_list: list[int] = []
    values_list: list[float] = []
    for sk, val in accessibility_by_poi.items():
        if val > 0.0:
            pid = source_key_to_id.get(sk)
            if pid is not None:
                poi_ids_list.append(pid)
                values_list.append(val)
    if not poi_ids_list:
        return
    path = os.path.join(out_dir, f"{node_id}.npz")
    np.savez_compressed(
        path,
        poi_ids=np.array(poi_ids_list, dtype=np.uint32),
        values=np.array(values_list, dtype=np.float32),
    )


def _build_node_result_from_matrix_row(
    node_id: Any,
    node_data: dict[str, Any],
    row: int,
    mat: np.memmap,
    poi_to_col: dict[str, int],
    accessibility_by_poi: dict[str, float] | None = None,
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
        accessibility_by_poi=accessibility_by_poi or {},
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
    poi_radius_enabled=False,
    poi_radius_m=None,
    poi_radius_decay_threshold=0.05,
    poi_radius_max_speed_kmh=60.0,
    subway_matrix_path=None,
    subway_source_id_to_row_path=None,
    subway_dest_id_to_col_path=None,
    subway_dest_csv_path=None,
    poi_drop_map_path=None,
    enabled_non_bus_modes=("walk", "bike", "drive"),
    poi_util_default=1.0,
    poi_util_canteen=1.0,
    canteen_osmids=(),
    poi_by_node_dir=None,
    poi_export_geopackage_path=None,
    poi_catalog_path=None,
):
    """Initialize worker-local state for accessibility multiprocessing.

    Inputs:
    - non_bus_cache_schema_version: expected cache schema version.
    - matrix_path: path to dense bus impedance matrix.
    - source_id_to_row_path: path to origin-id -> row index JSON.
    - dest_id_to_col_path: path to destination-id -> column index JSON.
    - dest_csv_path: path to destination CSV used to map coords to destination ids.
    - access_progress_value: optional shared progress counter.
    - poi_radius_enabled: whether to apply per-origin radius filtering to bus impedances.
    - poi_radius_m: fixed radius in metres (skips decay computation when set).
    - poi_radius_decay_threshold: threshold for computing radius from decay coefficient.
    - poi_radius_max_speed_kmh: reference speed for computing radius.
    - subway_matrix_path / subway_source_id_to_row_path / subway_dest_id_to_col_path /
      subway_dest_csv_path: optional paths for the subway modality. When all are provided the
      worker loads a second impedance matrix; when None subway is excluded from the RRA.

    Outputs:
    - None. Populates worker globals used during node computation.
    """
    global _NON_BUS_CACHE_SCHEMA_VERSION, _NON_BUS_POI_CONFIG_SIGNATURE, _ACCESS_PROGRESS_VALUE
    global _BUS_SOURCE_ID_TO_ROW, _BUS_IMPEDANCE_MATRIX
    global _SUBWAY_SOURCE_ID_TO_ROW, _SUBWAY_IMPEDANCE_MATRIX
    global _ACCESS_DEDUPLICATE_ENTRIES, _POI_DROP_BY_TYPE, _POI_CATALOG
    global _POI_RADIUS_ENABLED
    global _ENABLED_NON_BUS_MODES, _POI_UTIL_DEFAULT, _POI_UTIL_CANTEEN, _CANTEEN_OSMIDS
    global _POI_BY_NODE_DIR, _SOURCE_KEY_TO_ID
    _ENABLED_NON_BUS_MODES = tuple(enabled_non_bus_modes)
    _POI_UTIL_DEFAULT = float(poi_util_default)
    _POI_UTIL_CANTEEN = float(poi_util_canteen)
    _CANTEEN_OSMIDS = frozenset(str(x) for x in canteen_osmids)
    # The radius test itself now runs at routing time (utils.delta_g writes in_radius per
    # POI); the worker only needs to know whether to honour it.
    _POI_RADIUS_ENABLED = bool(poi_radius_enabled)
    _NON_BUS_CACHE_SCHEMA_VERSION = non_bus_cache_schema_version
    _NON_BUS_POI_CONFIG_SIGNATURE = str(non_bus_poi_config_signature or "")
    _ACCESS_DEDUPLICATE_ENTRIES = bool(deduplicate_entries)
    from utils import poi_dedup
    _POI_DROP_BY_TYPE = poi_dedup.load_drop_map(poi_drop_map_path) if poi_drop_map_path else {}
    if poi_catalog_path and os.path.exists(poi_catalog_path):
        with open(poi_catalog_path, "rb") as f:
            _POI_CATALOG = pickle.load(f)
    else:
        _POI_CATALOG = {}
    _ACCESS_PROGRESS_VALUE = access_progress_value
    with open(source_id_to_row_path, encoding="utf-8") as f:
        source_id_to_row_raw = json.load(f)
    _BUS_SOURCE_ID_TO_ROW = {str(k): int(v) for k, v in source_id_to_row_raw.items()}

    with open(dest_id_to_col_path, encoding="utf-8") as f:
        dest_id_to_col_raw = json.load(f)
    dest_id_to_col = {str(k): int(v) for k, v in dest_id_to_col_raw.items()}

    _BUS_IMPEDANCE_MATRIX = np.memmap(
        matrix_path, dtype=np.float32, mode="r",
        shape=(len(_BUS_SOURCE_ID_TO_ROW), len(dest_id_to_col))
    )

    # Optional subway modality: load a second impedance matrix when configured.
    _SUBWAY_SOURCE_ID_TO_ROW = None
    _SUBWAY_IMPEDANCE_MATRIX = None
    if subway_matrix_path:
        with open(subway_source_id_to_row_path, encoding="utf-8") as f:
            subway_src_raw = json.load(f)
        _SUBWAY_SOURCE_ID_TO_ROW = {str(k): int(v) for k, v in subway_src_raw.items()}

        with open(subway_dest_id_to_col_path, encoding="utf-8") as f:
            subway_dest_raw = json.load(f)
        subway_dest_id_to_col = {str(k): int(v) for k, v in subway_dest_raw.items()}

        _SUBWAY_IMPEDANCE_MATRIX = np.memmap(
            subway_matrix_path, dtype=np.float32, mode="r",
            shape=(len(_SUBWAY_SOURCE_ID_TO_ROW), len(subway_dest_id_to_col))
        )

    _POI_BY_NODE_DIR = poi_by_node_dir or ""
    _SOURCE_KEY_TO_ID = (
        _load_source_key_to_id(poi_export_geopackage_path)
        if _POI_BY_NODE_DIR and poi_export_geopackage_path
        else {}
    )
    if not _SOURCE_KEY_TO_ID:
        _POI_BY_NODE_DIR = ""

    # Yield CPU under safe mode so accessibility workers don't pin the machine at full load.
    from core.runtime_setup import lower_process_priority_if_safe
    lower_process_priority_if_safe()

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
        impedance_matrix = _BUS_IMPEDANCE_MATRIX
        if source_id_to_row is None or impedance_matrix is None:
            raise RuntimeError("Bus impedance matrix is not initialized in worker.")
        
        
        source_row = source_id_to_row.get(str(node_id))

        # Subway is optional: only active when the worker was initialized with a subway matrix.
        subway_enabled = (
            _SUBWAY_IMPEDANCE_MATRIX is not None
            and _SUBWAY_SOURCE_ID_TO_ROW is not None
        )
        subway_source_row = _SUBWAY_SOURCE_ID_TO_ROW.get(str(node_id)) if subway_enabled else None

        _rra_lambda_mode = os.environ.get("RRA_LAMBDA_MODE", "rank_desc")

        # For each poi_type, compute the accessibility.
        # Each entry is composed of the poi_type, the impedance values for that poi type from the origin, and the coords of the snapped POIs of that type.
        # The beta constant for accessibility is computed for the poi_type, based on the pre-configured impedance value that will bring the decay function value to 0.5
        # # The bus impedance is loaded from the cached numpy dense matrix and the decay is calculated
        # The decays for each mode are merged and then we merge with the RRA the decays of the POI types
        def _compute_per_poi_accessibility(entry) -> list[float]:
            """Compute one accessibility value per individual POI in an entry.

            Each POI in kept_idx[i] / source_keys[i] is evaluated independently
            before being aggregated at the poi_type level. This preserves per-POI
            scores for downstream export (service_power / capability_power).

            Vectorized over all POIs of this entry at once (numpy), instead of a
            per-POI Python loop -- entries can hold thousands of POIs in dense
            areas, and the old loop's per-POI math.exp/sorted() calls dominated
            accessibility runtime (profiled: ~7.5s/origin node for Paris). Same
            formulas as before, just batched; see git history for the scalar version.

            Inputs:
            - entry: non-bus cache entry with per-POI parallel lists.

            Outputs:
            - list of per-POI accessibility values aligned to entry["kept_idx"].
            """
            poi_type = entry["poi_type"]
            beta = poi_beta_cache.get(poi_type)
            if beta is None:
                beta = math.log(2) / float(serv.get_decay_coefficient(poi_type))
                poi_beta_cache[poi_type] = beta
            neg_beta = -beta

            kept_idx = entry["kept_idx"]
            n = int(kept_idx.shape[0])
            if n == 0:
                return []

            imp_walk_arr = entry.get("imp_walk")
            imp_bike_arr = entry.get("imp_bike")
            imp_drive_arr = entry.get("imp_drive")

            # source_keys is origin-invariant, so it lives once in the shared _POI_CATALOG
            # (see exports/artifact_bundle.py) instead of this per-node entry -- kept_idx
            # resolves this entry's POIs into it, positionally aligned with the imp arrays.
            catalog_keys = _POI_CATALOG.get(poi_type, {}).get("src_keys")
            source_keys = catalog_keys[kept_idx].astype(str).tolist() if catalog_keys is not None else [None] * n

            # Per-service dedup: this poi_type does not own these physical POIs, so they
            # are counted under another poi_type of the same service. Zeroing keeps index
            # alignment with source_keys and is equivalent to removal in the Delta-g sum.
            drop_keys = _POI_DROP_BY_TYPE.get(poi_type)

            valid = np.ones(n, dtype=bool)
            if drop_keys:
                for i in range(n):
                    if source_keys[i] in drop_keys:
                        valid[i] = False

            if _POI_RADIUS_ENABLED:
                valid &= entry["in_radius"]

            # Matrix columns were resolved once at routing time (utils.delta_g._dest_col_map)
            # instead of re-hashing rounded coords per POI on every read. Columns are filled
            # for invalid rows too -- harmless, since rra is masked by `valid` below.
            dest_cols = entry["dest_col"].astype(np.int64)
            subway_cols = dest_cols if subway_enabled else None

            def _matrix_decay(index_arr, matrix, src_row):
                out = np.zeros(n, dtype=np.float64)
                if src_row is None or index_arr is None:
                    return out
                mask = index_arr >= 0
                if mask.any():
                    vals = np.asarray(matrix[src_row, index_arr[mask]], dtype=np.float64)
                    pos = vals > 0
                    if pos.any():
                        rows = np.nonzero(mask)[0][pos]
                        out[rows] = np.exp(neg_beta * vals[pos])
                return out

            def _list_decay(values_arr):
                out = np.zeros(n, dtype=np.float64)
                if values_arr is None or len(values_arr) == 0:
                    return out
                limit = min(n, len(values_arr))
                sub = np.asarray(values_arr[:limit], dtype=np.float64)
                present = np.isfinite(sub)
                if present.any():
                    out[:limit] = np.where(present, np.exp(neg_beta * sub), 0.0)
                return out

            d_bus = _matrix_decay(dest_cols, impedance_matrix, source_row)
            d_subway = (
                _matrix_decay(subway_cols, _SUBWAY_IMPEDANCE_MATRIX, subway_source_row)
                if subway_enabled
                else None
            )

            # Individual-profile mode gating: a disabled non-bus mode is forced to 0.0
            # regardless of its real computed decay, which ranks last and is a no-op in
            # the 1 - prod(1 - w) formula, leaving the enabled modes with the correct top
            # redundancy weights (exactly as if m were smaller). Bus is a public-transport
            # mode and always kept.
            d_walk = _list_decay(imp_walk_arr) if "walk" in _ENABLED_NON_BUS_MODES else np.zeros(n)
            d_bike = _list_decay(imp_bike_arr) if "bike" in _ENABLED_NON_BUS_MODES else np.zeros(n)
            d_drive = _list_decay(imp_drive_arr) if "drive" in _ENABLED_NON_BUS_MODES else np.zeros(n)

            # Per-POI accessibility A^i_k(x, y) is exactly the RRA over modes (formula 2):
            # rank each POI's modal decays descending, apply the fixed redundancy weights
            # lambda = 1, 1/2, ..., 1/m (best mode full weight), then combine as
            # 1 - prod(1 - weighted). Passing subway only when enabled keeps the mode
            # count at 4 for cities without it (identical results) and 5 where it exists.
            cols = [d_walk, d_bike, d_drive, d_bus]
            if subway_enabled:
                cols.append(d_subway)
            decay_matrix = np.stack(cols, axis=1)
            m = decay_matrix.shape[1]
            lambdas = np.asarray(decay._lambda_series(m, _rra_lambda_mode), dtype=np.float64)
            sorted_desc = -np.sort(-decay_matrix, axis=1)
            weighted = sorted_desc * lambdas[np.newaxis, :]
            rra = 1.0 - np.prod(1.0 - weighted, axis=1)

            # Per-POI utility u(y): affordability, with the canteen (status-benefit)
            # override for specific instances. Defaults to 1.0 (baseline unchanged).
            if _POI_UTIL_DEFAULT != 1.0 or _POI_UTIL_CANTEEN != 1.0:
                util = np.fromiter(
                    (
                        _utility_for_source_key(source_keys[i] if i < len(source_keys) else None, poi_type)
                        for i in range(n)
                    ),
                    dtype=np.float64,
                    count=n,
                )
                rra = rra * util

            rra = np.where(valid, rra, 0.0)
            return rra.tolist()

        # --- Step 1: compute per-POI accessibility for every unique entry ----
        # Deduplicate by poi_type so each entry is processed once.
        entry_by_poi_type: dict[str, dict[str, Any]] = {}
        for service in serv.SERVICE_KEYS:
            for entry in services_state.get(service, []):
                key = str(entry["poi_type"])
                if key not in entry_by_poi_type:
                    entry_by_poi_type[key] = entry

        # per_poi_accs[poi_type][i] = accessibility of the i-th individual POI
        per_poi_accs: dict[str, list[float]] = {}
        for key, entry in entry_by_poi_type.items():
            per_poi_accs[key] = _compute_per_poi_accessibility(entry) if _ACCESS_DEDUPLICATE_ENTRIES else [
                _compute_per_poi_accessibility(entry)[i]
                for i in range(len(entry["kept_idx"]))
            ]

        # --- Step 2: RRA-aggregate per-POI → per-poi-type (for service stage) -
        # merge_rra_and_accessibility already does RRA over its decay lists; call
        # it with the individual accessibility values treated as single-mode decays.
        poi_type_acc: dict[str, float] = {}
        for key, accs in per_poi_accs.items():
            if not accs:
                poi_type_acc[key] = 0.0
            else:
                _, agg = delta_g.merge_rra_and_accessibility(
                    accs, [], [], [],
                    poi_type=key,
                )
                poi_type_acc[key] = agg

        # --- Step 3: build accessibility_by_service (unchanged shape) --------
        accessibility_by_service: dict[str, list[dict[str, Any]]] = {}
        for service in serv.SERVICE_KEYS:
            items = []
            for entry in services_state.get(service, []):
                pt = str(entry["poi_type"])
                items.append({"poi_type": pt, "accessibility": poi_type_acc.get(pt, 0.0)})
            accessibility_by_service[service] = items

        # --- Step 4: build flat source_key → accessibility map ---------------
        accessibility_by_poi: dict[str, float] = {}
        for key, entry in entry_by_poi_type.items():
            kept_idx = entry.get("kept_idx", np.empty(0, dtype=np.int32))
            catalog_keys = _POI_CATALOG.get(key, {}).get("src_keys")
            source_keys = catalog_keys[kept_idx].astype(str).tolist() if catalog_keys is not None else []
            accs = per_poi_accs.get(key, [])
            for sk, acc_val in zip(source_keys, accs):
                if sk and acc_val > 0.0:
                    accessibility_by_poi[str(sk)] = acc_val

        # Compress and persist right here, in the worker, and drop the dict immediately
        # -- it never gets pickled for IPC or held across the rest of this chunk. See
        # _POI_BY_NODE_DIR docstring above for why that round trip mattered.
        if _POI_BY_NODE_DIR and accessibility_by_poi:
            _write_poi_by_node_sparse(
                node_id, accessibility_by_poi, _SOURCE_KEY_TO_ID, _POI_BY_NODE_DIR
            )
            accessibility_by_poi = {}

        return AccessibilityNodeResult(
            node_id=node_id,
            lat=state["origin"][0],
            lon=state["origin"][1],
            accessibility_by_service=accessibility_by_service,
            accessibility_by_poi=accessibility_by_poi,
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

    # Resolve per-service POI ownership and persist the drop map BEFORE the matrix
    # cache check, so the run signature reflects the current dedup state. Writing an
    # empty map when disabled/shapefile clears any stale map from a prior run.
    from utils import poi_dedup
    if ctx.config.poi_service_dedup_enabled and poi_dedup.is_osm_mode(ctx.config):
        try:
            drop_map = poi_dedup.build_drop_map(ctx.config)
            poi_dedup.write_drop_map(ctx.config, drop_map=drop_map)
            n_dropped = sum(len(v) for v in drop_map.values())
            print(
                f"[Accessibility] POI service dedup: {n_dropped} duplicate POI assignments "
                f"removed across {len(drop_map)} poi_types.",
                flush=True,
            )
        except Exception as exc:
            print(f"[Accessibility] POI dedup drop-map build failed ({exc}); proceeding without dedup.", flush=True)
            poi_dedup.write_drop_map(ctx.config, drop_map={})
    else:
        poi_dedup.write_drop_map(ctx.config, drop_map={})

    mat, node_to_row, poi_to_col = _open_or_create_accessibility_matrix_cache(ctx, bus)

    # Set up per-node sparse accessibility directory — individual POI accessibility is
    # written as one small compressed .npz per node instead of a single giant JSON file.
    # Workers write it themselves (see _POI_BY_NODE_DIR) as soon as each node is computed,
    # so it is never fully resident in memory during or after the accessibility stage --
    # not in a worker's chunk buffer, and not pickled across IPC to this main process.
    poi_by_node_dir: str = ctx.config.accessibility_poi_by_node_dir or ""
    if poi_by_node_dir and os.path.exists(ctx.config.poi_export_geopackage_path):
        os.makedirs(poi_by_node_dir, exist_ok=True)
    else:
        poi_by_node_dir = ""

    # Preload complete node rows from cache and compute only missing rows.
    pending: dict[Any, str] = {}
    reused_cached_rows = 0
    scheduled_rows = 0
    if mat is not None:
        required_cols = np.array(
            [poi_to_col[poi] for poi in _ordered_poi_types() if poi in poi_to_col],
            dtype=np.int64,
        )
        for node_id, data in ctx.nodes_with_coords:
            row = node_to_row[str(node_id)]
            # A node is a full cache hit only when its matrix row is complete AND its
            # per-node sparse file already exists (or no sparse files are needed).
            need_sparse = bool(poi_by_node_dir)
            sparse_exists = (
                os.path.exists(os.path.join(poi_by_node_dir, f"{node_id}.npz"))
                if need_sparse else True
            )
            if required_cols.size > 0 and _row_is_complete(mat, row, required_cols) and sparse_exists:
                result.node_results.append(
                    _build_node_result_from_matrix_row(
                        node_id, data, row, mat, poi_to_col,
                    )
                )
                reused_cached_rows += 1
            else:
                cache_path = non_bus.cache_paths.get(node_id)
                if cache_path is None:
                    cache_path = non_bus.cache_paths.get(str(node_id))
                if cache_path is not None:
                    pending[node_id] = cache_path
                    scheduled_rows += 1
    else:
        pending = {}
        for node_id, _ in ctx.nodes_with_coords:
            cache_path = non_bus.cache_paths.get(node_id)
            if cache_path is None:
                cache_path = non_bus.cache_paths.get(str(node_id))
            if cache_path is not None:
                pending[node_id] = cache_path
                scheduled_rows += 1

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

    # Individual-profile knobs passed to each worker. When ctx carries no profile these
    # reproduce the baseline exactly (all non-bus modes, utility multiplier 1.0 everywhere).
    _profile = getattr(ctx, "profile", None)
    _profile_mode_tuple = tuple(
        getattr(ctx.config, "enabled_non_bus_modes", ("walk", "bike", "drive"))
    )
    if _profile is not None:
        _profile_util_default = float(_profile.affordability)
        _profile_util_canteen = float(_profile.canteen_utility)
        _profile_canteen_osmids = tuple(str(x) for x in _profile.canteen_source_keys)
    else:
        _profile_util_default = 1.0
        _profile_util_canteen = 1.0
        _profile_canteen_osmids = ()

    try:
        while pending:
            base_progress[0] = float(total_nodes - len(pending))
            shared_progress = mp.Value("d", 0.0)
            shared_progress_ref[0] = shared_progress
            pool = None
            try:
                global _ACTIVE_ACCESSIBILITY_POOL
                pool = mp.Pool(
                    processes=pool_workers,
                    # Windows-only: worker recycling races the Pool's result-handler thread
                    # on the same overlapped pipe there and raises "concurrent send_bytes()
                    # calls are not supported" (see non_bus_routing_stage.py for detail).
                    maxtasksperchild=200 if os.name != "nt" else None,
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
                        ctx.config.poi_radius_enabled,
                        ctx.config.poi_radius_m,
                        ctx.config.poi_radius_decay_threshold,
                        ctx.config.poi_radius_max_speed_kmh,
                        # Subway modality (None when disabled for this city).
                        ctx.config.subway_impedance_matrix_path if ctx.config.enable_subway else None,
                        ctx.config.subway_source_id_to_row_path if ctx.config.enable_subway else None,
                        ctx.config.subway_dest_id_to_col_path if ctx.config.enable_subway else None,
                        ctx.config.subway_routing_destinations_input_path if ctx.config.enable_subway else None,
                        ctx.config.poi_ownership_drop_path,
                        # Individual-profile knobs (baseline defaults when ctx has no profile).
                        _profile_mode_tuple,
                        _profile_util_default,
                        _profile_util_canteen,
                        _profile_canteen_osmids,
                        poi_by_node_dir,
                        ctx.config.poi_export_geopackage_path,
                        ctx.config.non_bus_poi_catalog_path,
                    ),
                )
                _ACTIVE_ACCESSIBILITY_POOL = pool
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
                    if mat is not None:
                        row = node_to_row.get(str(data.node_id))
                        if row is not None:
                            _write_node_result_to_matrix(mat, row, poi_to_col, data)
                    # accessibility_by_poi was already compressed to .npz and dropped by
                    # the worker (see _POI_BY_NODE_DIR); nothing left to write here.
                    result.node_results.append(data)
                    if data.node_id in pending:
                        pending.pop(data.node_id, None)
                        made_progress = True
                pool.close()
                pool.join()
                pool = None
                _ACTIVE_ACCESSIBILITY_POOL = None
                if not pending:
                    break
                if not made_progress:
                    raise RuntimeError(
                         f"Accessibility pool made no progress; remaining_nodes={len(pending)}"
                    )
            except (Exception, KeyboardInterrupt) as e:
                if pool is not None:
                    pool.terminate()
                    pool.join()
                    pool = None
                _ACTIVE_ACCESSIBILITY_POOL = None
                if isinstance(e, KeyboardInterrupt):
                    raise
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

    total_entries = 0
    nonzero_entries = 0
    nonzero_rows = 0
    for node in result.node_results:
        row_has_nonzero = False
        for items in node.accessibility_by_service.values():
            for item in items:
                total_entries += 1
                if float(item["accessibility"]) > 0.0:
                    nonzero_entries += 1
                    row_has_nonzero = True
        if row_has_nonzero:
            nonzero_rows += 1

    print(
        f"[Accessibility] Summary: rows={len(result.node_results)} "
        f"nonzero_rows={nonzero_rows} "
        f"nonzero_accessibility={nonzero_entries}/{total_entries} "
        f"reused_cached_rows={reused_cached_rows} scheduled_rows={scheduled_rows}",
        flush=True,
    )
    return result


# compatibility alias for old code
compute_node_accessibility = _compute_node_accessibility
