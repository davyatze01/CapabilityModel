import csv
import os
import shutil
from tqdm import tqdm

tqdm.monitor_interval = 0  # disable background monitor thread (avoids Windows AV noise)

from core.context import PipelineContext
from core.pipeline_types import ServiceStageResult, CapabilityStageResult
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
    # capabilities is the ordered (name, services) list from config/capability.csv,
    # restricted to those with enabled=True -- a disabled capability gets no
    # capability_<name> column at all, not just a blank one, and is excluded from
    # the recap averages.
    capabilities = [
        (name, services)
        for name, services in cap.CAPABILITY_SERVICES.items()
        if cap.CAPABILITY_ENABLED.get(name, True)
    ]
    capability_names = [name for name, _ in capabilities]

    # Build a deduplicated ordered list of all services across all capabilities.
    all_services: list[str] = []
    seen_services: set[str] = set()
    for _, services in capabilities:
        for s in services:
            if s not in seen_services:
                seen_services.add(s)
                all_services.append(s)

    output_path = ctx.output_paths["capabilities"]
    rows_written = 0
    sums = {name: 0.0 for name in capability_names}
    counts = {name: 0 for name in capability_names}

    # See PipelineConfig.capability_score_mode: "discrete" (default) keeps the current
    # 5-band-midpoint score; "continuous" is a statistics-only knob for scenario testing.
    score_fn = (
        cap.electre_tri_continuous_score
        if getattr(ctx.config, "capability_score_mode", "discrete") == "continuous"
        else cap.electre_tri_integration
    )

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        header = ["node_id", "lat", "lon"]
        header.extend(f"capability_{name}" for name in capability_names)
        header.extend(f"service_{s}" for s in all_services)
        writer.writerow(header)

        pbar = tqdm(total=len(svc.node_results), desc="Capability stage", mininterval=1) if ctx.config.enable_progress else None
        try:
            for node in svc.node_results:
                scores = node.service_scores

                capability_values = []
                for name, services in capabilities:
                    vals = [scores[s] for s in services]
                    value = score_fn(vals, name) if vals else 0.0
                    sums[name] += value
                    counts[name] += 1
                    capability_values.append(value)

                row = [node.node_id, node.lat, node.lon]
                row.extend(capability_values)
                row.extend(scores.get(s, 0.0) for s in all_services)
                writer.writerow(row)

                rows_written += 1
                if pbar:
                    pbar.update(1)
        finally:
            if pbar:
                pbar.close()

    averages = {
        name: (sums[name] / counts[name]) if counts[name] else 0.0
        for name in capability_names
    }

    experiments_dir = "experiments"
    os.makedirs(experiments_dir, exist_ok=True)

    dst_path = os.path.join(experiments_dir, f"{ctx.config.artifact_slug}_capability.csv")
    dst_path = _ensure_unique_path(dst_path)
    shutil.move(output_path, dst_path)
    moved_output_paths = {"capabilities": dst_path}

    # The recap file's column set follows the currently configured capabilities. If
    # that set changes between runs, older rows in the same recap file were written
    # under a different header and will not line up column-for-column with new ones.
    recap_path = os.path.join(experiments_dir, "capability_experiments_recap.csv")
    recap_header = ["city_slug", "artifact_slug"]
    recap_header.extend(f"avg_capability_{name}" for name in capability_names)
    should_write_header = (not os.path.exists(recap_path)) or os.path.getsize(recap_path) == 0
    with open(recap_path, "a", newline="", encoding="utf-8") as recap_f:
        writer = csv.writer(recap_f)
        if should_write_header:
            writer.writerow(recap_header)
        recap_row = [ctx.config.city_slug, ctx.config.artifact_slug]
        recap_row.extend(averages[name] for name in capability_names)
        writer.writerow(recap_row)

    return CapabilityStageResult(output_paths=moved_output_paths, rows_written=rows_written)
