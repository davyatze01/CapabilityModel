import csv
import os
import shutil
from tqdm import tqdm

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
    """Aggregate service scores into capability scores and write capability CSV outputs.

    Inputs:
    - ctx: pipeline context with capability-service mapping and output paths.
    - svc: service scores computed for each node.

    Outputs:
    - CapabilityStageResult: written output paths and number of processed rows.
    """
    output_paths = ctx.output_paths
    rows_written = 0
    rest_sum = 0.0
    nut_sum = 0.0
    care_sum = 0.0

    with (
        open(output_paths["restorativeness"], "w", newline="", encoding="utf-8") as f_rest,
        open(output_paths["nutrition"], "w", newline="", encoding="utf-8") as f_nut,
        open(output_paths["care"], "w", newline="", encoding="utf-8") as f_care,
    ):
        writer_rest = csv.writer(f_rest)
        writer_nut = csv.writer(f_nut)
        writer_care = csv.writer(f_care)

        header_rest = ["node_id", "lat", "lon", "capability_restorativeness"]
        header_rest.extend([f"service_{service}" for service in ctx.rest_services])
        writer_rest.writerow(header_rest)

        header_nut = ["node_id", "lat", "lon", "capability_nutrition"]
        header_nut.extend([f"service_{service}" for service in ctx.nut_services])
        writer_nut.writerow(header_nut)

        header_care = ["node_id", "lat", "lon", "capability_care"]
        header_care.extend([f"service_{service}" for service in ctx.care_services])
        writer_care.writerow(header_care)

        pbar = tqdm(total=len(svc.node_results), desc="Capability stage", mininterval=0) if ctx.config.enable_progress else None
        try:
            for node in svc.node_results:
                scores = node.service_scores

                rest_vals = [scores[s] for s in cap.CAP_RESTORATIVENESS_IDX]
                nut_vals = [scores[s] for s in cap.CAP_NUTRITION_IDX]
                care_vals = [scores[s] for s in cap.CAP_CARE_IDX]

                capability_rest = cap.choquet_integral(rest_vals, "restorativeness") if rest_vals else 0.0
                capability_nut = cap.choquet_integral(nut_vals, "nutrition") if nut_vals else 0.0
                capability_care = cap.choquet_integral(care_vals, "care") if care_vals else 0.0

                row_rest = [node.node_id, node.lat, node.lon, capability_rest]
                row_rest.extend(scores[s] for s in ctx.rest_services)
                writer_rest.writerow(row_rest)

                row_nut = [node.node_id, node.lat, node.lon, capability_nut]
                row_nut.extend(scores[s] for s in ctx.nut_services)
                writer_nut.writerow(row_nut)

                row_care = [node.node_id, node.lat, node.lon, capability_care]
                row_care.extend(scores[s] for s in ctx.care_services)
                writer_care.writerow(row_care)

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

    moved_output_paths = {}
    for capability_key, src_path in output_paths.items():
        basename = os.path.basename(src_path)
        dst_path = os.path.join(experiments_dir, f"{ctx.config.artifact_slug}_{basename}")
        dst_path = _ensure_unique_path(dst_path)
        shutil.move(src_path, dst_path)
        moved_output_paths[capability_key] = dst_path

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
