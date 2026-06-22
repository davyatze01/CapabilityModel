import csv
import os
import shutil
from tqdm import tqdm

tqdm.monitor_interval = 0  # disable background monitor thread (avoids Windows AV noise)

from context import PipelineContext
from pipeline_types import ServiceStageResult, CapabilityStageResult
from utils import capabilities as cap


def _ensure_unique_path(path: str) -> str:
    """Return a non-existing path by adding an incrementing suffix when needed."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    idx = 2
    while True:
        candidate = f"{root}_{idx}{ext}"
        if not os.path.exists(candidate):
            return candidate
        idx += 1


def run_capability_stage(ctx: PipelineContext, svc: ServiceStageResult) -> CapabilityStageResult:
    """Aggregate service scores into capability scores and write a single capability CSV.

    Inputs:
    - ctx: pipeline context with capability-service mapping and output paths.
    - svc: service scores computed for each node.

    Outputs:
    - CapabilityStageResult: written output paths and number of processed rows.
    """
    # Build a deduplicated ordered list of all services across all capabilities.
    all_services: list[str] = []
    seen_services: set[str] = set()
    for services in cap.CAPABILITY_SERVICES.values():
        for s in services:
            if s not in seen_services:
                seen_services.add(s)
                all_services.append(s)

    output_path = ctx.output_paths["capabilities"]
    rows_written = 0
    rest_sum = 0.0
    nut_sum = 0.0
    care_sum = 0.0

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        header = [
            "node_id", "lat", "lon",
            "capability_restorativeness", "capability_nutrition", "capability_care",
        ]
        header.extend(f"service_{s}" for s in all_services)
        writer.writerow(header)

        pbar = tqdm(total=len(svc.node_results), desc="Capability stage", mininterval=1) if ctx.config.enable_progress else None
        try:
            for node in svc.node_results:
                scores = node.service_scores

                rest_vals = [scores[s] for s in cap.CAP_RESTORATIVENESS_IDX]
                nut_vals = [scores[s] for s in cap.CAP_NUTRITION_IDX]
                care_vals = [scores[s] for s in cap.CAP_CARE_IDX]

                capability_rest = cap.electre_tri_integration(rest_vals, "restorativeness") if rest_vals else 0.0
                capability_nut = cap.electre_tri_integration(nut_vals, "nutrition") if nut_vals else 0.0
                capability_care = cap.electre_tri_integration(care_vals, "care") if care_vals else 0.0

                row = [node.node_id, node.lat, node.lon, capability_rest, capability_nut, capability_care]
                row.extend(scores.get(s, 0.0) for s in all_services)
                writer.writerow(row)

                rest_sum += capability_rest
                nut_sum += capability_nut
                care_sum += capability_care
                rows_written += 1
                if pbar:
                    pbar.update(1)
        finally:
            if pbar:
                pbar.close()

    avg_rest = (rest_sum / rows_written) if rows_written else 0.0
    avg_nut = (nut_sum / rows_written) if rows_written else 0.0
    avg_care = (care_sum / rows_written) if rows_written else 0.0

    experiments_dir = "experiments"
    os.makedirs(experiments_dir, exist_ok=True)

    basename = os.path.basename(output_path)
    dst_path = os.path.join(experiments_dir, f"{ctx.config.artifact_slug}_{basename}")
    dst_path = _ensure_unique_path(dst_path)
    shutil.move(output_path, dst_path)
    moved_output_paths = {"capabilities": dst_path}

    recap_path = os.path.join(experiments_dir, "capability_experiments_recap.csv")
    recap_header = [
        "city_slug",
        "artifact_slug",
        "avg_capability_restorativeness",
        "avg_capability_nutrition",
        "avg_capability_care",
    ]
    should_write_header = (not os.path.exists(recap_path)) or os.path.getsize(recap_path) == 0
    with open(recap_path, "a", newline="", encoding="utf-8") as recap_f:
        writer = csv.writer(recap_f)
        if should_write_header:
            writer.writerow(recap_header)
        writer.writerow([ctx.config.city_slug, ctx.config.artifact_slug, avg_rest, avg_nut, avg_care])

    return CapabilityStageResult(output_paths=moved_output_paths, rows_written=rows_written)
