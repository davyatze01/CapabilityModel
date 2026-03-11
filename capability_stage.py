import csv
from tqdm import tqdm

from helpers import PipelineContext, ServiceStageResult, CapabilityStageResult
from utils import capabilities as cap


def run_capability_stage(ctx: PipelineContext, svc: ServiceStageResult) -> CapabilityStageResult:
    output_paths = ctx.output_paths
    rows_written = 0

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

                f_rest.flush()
                f_nut.flush()
                f_care.flush()
                rows_written += 1
                if pbar:
                    pbar.update(1)
        finally:
            if pbar:
                pbar.close()

    return CapabilityStageResult(output_paths=output_paths, rows_written=rows_written)
