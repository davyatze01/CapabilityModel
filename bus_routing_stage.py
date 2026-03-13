import os
import multiprocessing as mp
from pathlib import Path

from helpers import PipelineContext, SnappingStageResult, BusRoutingStageResult
from utils import r5_routing
from snapping_stage import build_selected_routing_destinations


def _resolve_routing_sample_csv_path(base_path: str, routing_mode: str, wait_model: str | None) -> str:
    path = Path(base_path)
    suffix = path.suffix or ".csv"
    stem = path.stem
    token = routing_mode
    if routing_mode == r5_routing.MODE_FAST:
        token = f"{routing_mode}_{r5_routing._normalize_fast_wait_model(wait_model)}"
    return str(path.with_name(f"{stem}_{token}{suffix}"))

# Check if the DB is present and compatible for skipping bus routing
def _validate_routing_artifacts_for_skip(skip_routing, routing_db):
    if not skip_routing:
        return
    if not os.path.isfile(routing_db):
        raise RuntimeError(
            "Missing routing artifacts while SKIP_ROUTING=True. "
            f"Expected file: {routing_db}"
        )
    # Hard break: reject legacy schema caches when skip mode is active.
    idx = r5_routing.open_routing_index(routing_db)
    idx.close()

# This function runs the bus routing stage.
# If the csv and db are already in cache and the skip_routing mode is active (enabled by default)
def run_bus_routing_stage(ctx: PipelineContext, snap: SnappingStageResult) -> BusRoutingStageResult:
    cfg = ctx.config
    routing_mode = r5_routing.MODE_FAST
    routing_csv = _resolve_routing_sample_csv_path(
        cfg.r5_sample_csv_path,
        routing_mode=routing_mode,
        wait_model=cfg.r5_fast_wait_model,
    )
    routing_db = cfg.r5_fast_db
    routing_chunk_size = cfg.r5_fast_chunk_size
    routing_workers_default = mp.cpu_count() if cfg.r5_fast_workers is None else int(cfg.r5_fast_workers)
    routing_workers = routing_workers_default
    routing_workers = max(1, routing_workers)

    # If the bus routing step has already been computed, skip this part and return directly the results from cache
    if cfg.skip_routing:
        _validate_routing_artifacts_for_skip(cfg.skip_routing, routing_db)
        print(
            "Routing configuration: "
            f"mode={routing_mode} workers={routing_workers} chunk={routing_chunk_size} "
            "origins=SKIPPED destinations=SKIPPED "
            "persist_outputs=True"
        )
        print(f"Skipping routing build. Reusing DB: {routing_db} (sample csv: {routing_csv})")
        return BusRoutingStageResult(
            routing_csv=routing_csv,
            routing_db=routing_db,
            routing_departure_iso=cfg.r5_fixed_departure.isoformat(),
        )

    
    all_candidate_snapped_coords = set()
    has_multi_snap_candidates = False
    for snap_info_for_key in snap.poi_bus_snap_info_by_type.values():
        for candidates in snap_info_for_key.values():
            if len(candidates) > 1:
                has_multi_snap_candidates = True
            for snapped, _ in candidates:
                all_candidate_snapped_coords.add(tuple(snapped))

    if has_multi_snap_candidates:
        unique_snapped_coords = build_selected_routing_destinations(
            ctx.nodes_with_coords,
            snap.poi_bus_snap_info_by_type,
            cfg.enable_progress,
        )
        print(
            "Bus destination reduction: "
            f"candidate_nodes={len(all_candidate_snapped_coords)} "
            f"selected_nodes={len(unique_snapped_coords)}"
        )
    else:
        unique_snapped_coords = sorted(all_candidate_snapped_coords)

    origins = [(data["y"], data["x"]) for _, data in ctx.nodes_with_coords]
    destinations = list(unique_snapped_coords)
    print(
        "Routing configuration: "
        f"mode={routing_mode} workers={routing_workers} chunk={routing_chunk_size} "
        f"origins={len(origins)} destinations={len(destinations)} "
        "persist_outputs=True"
    )
    routing_summary = r5_routing.build_routing_store_resilient(
        origins=origins,
        destinations=destinations,
        mode=routing_mode,
        pbf_path=cfg.r5_pbf_path,
        gtfs_path=cfg.r5_gtfs_path,
        jar_path=cfg.r5_jar_path,
        departure_dt=cfg.r5_fixed_departure,
        workers=routing_workers,
        out_csv=routing_csv,
        out_db=routing_db,
        chunk_size=routing_chunk_size,
        enable_progress=cfg.enable_progress,
        sample_csv_path=routing_csv,
        sample_rows=cfg.r5_sample_rows,
        sample_missing_share=cfg.r5_sample_missing_share,
        r5_fast_wait_model=cfg.r5_fast_wait_model,
        r5_tripplanner_workers=cfg.r5_tripplanner_workers,
        r5_tripplanner_timeout_s=cfg.r5_tripplanner_timeout_s,
        max_retries=cfg.r5_max_retries,
        retry_delay_s=cfg.r5_retry_delay_s,
        attempt_timeout_s=cfg.r5_attempt_timeout_s,
        r5_max_time_walking_min=cfg.r5_max_time_walking_min,
        r5_departure_window_min=cfg.r5_departure_window_min,
        persist_outputs=True,
    )
    print(
        "Built routing store: "
        f"mode={routing_summary['mode']} rows={routing_summary['rows']} "
        f"missing={routing_summary['missing_rows']} resumed={routing_summary.get('resumed', False)} "
        f"processed_origins={routing_summary.get('processed_origins', 0)} "
        f"attempt={routing_summary.get('supervisor_attempt', 1)}/"
        f"{routing_summary.get('supervisor_attempts_total', 1)} "
        f"workers={routing_summary.get('supervisor_workers_used')} "
        f"chunk={routing_summary.get('supervisor_chunk_size_used')} "
        f"csv={routing_summary['out_csv']} db={routing_summary['out_db']}"
    )
    return BusRoutingStageResult(
        routing_csv=routing_csv,
        routing_db=routing_db,
        routing_departure_iso=cfg.r5_fixed_departure.isoformat(),
    )


# compatibility shim
validate_routing_artifacts_for_skip = _validate_routing_artifacts_for_skip
