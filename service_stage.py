import os
import json
import hashlib
import numpy as np
from tqdm import tqdm

from context import PipelineContext
from pipeline_types import AccessibilityStageResult, ServiceStageResult, ServiceNodeResult
from utils import services as serv

def _service_run_signature(ctx: PipelineContext) -> str:
    """Compute cache compatibility signature for service matrix artifacts.

    Inputs:
    - ctx: pipeline context with service/cache configuration and accessibility metadata path.

    Outputs:
    - str SHA1 signature used to validate service cache reuse.
    """
    access_sig = ""
    if os.path.exists(ctx.config.accessibility_meta_path):
        with open(ctx.config.accessibility_meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        access_sig = str(meta.get("run_signature", ""))

    payload = {
        "schema": int(ctx.config.service_matrix_schema_version),
        "aggregation_version": "v2_normalized_service_choquet",
        "access_signature": access_sig,
        "services": list(serv.SERVICE_KEYS),
        "poi_csv_path": str(serv.CONFIG_CSV_PATH),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _write_service_matrix_cache(ctx: PipelineContext, out: ServiceStageResult) -> None:
    """Persist service scores into dense matrix cache plus mapping metadata.

    Inputs:
    - ctx: pipeline context with service cache paths/configuration.
    - out: computed service-stage result to store.

    Outputs:
    - None. Writes matrix and JSON sidecar files.
    """
    if not ctx.config.service_matrix_cache_enabled:
        return
    
    node_ids = [str(node.node_id) for node in out.node_results]
    node_to_row = {node_id: i for i, node_id in enumerate(node_ids)}
    service_to_col = {svc: j for j, svc in enumerate(serv.SERVICE_KEYS)}
    run_sig = _service_run_signature(ctx)

    n = len(node_ids)
    s = len(serv.SERVICE_KEYS)
    os.makedirs(os.path.dirname(ctx.config.service_matrix_path) or ".", exist_ok=True)

    mat = np.memmap(ctx.config.service_matrix_path, dtype=np.float32, mode="w+", shape=(n, s))
    mat[:] = 0.0

    for node in out.node_results:
        row = node_to_row[str(node.node_id)]
        for service, score in node.service_scores.items():
            col = service_to_col.get(service)
            if col is not None:
                mat[row, col] = np.float32(float(score))

    mat.flush()

    with open(ctx.config.service_node_to_row_path, "w", encoding="utf-8") as f:
        json.dump(node_to_row, f, ensure_ascii=False)
    with open(ctx.config.service_to_col_path, "w", encoding="utf-8") as f:
        json.dump(service_to_col, f, ensure_ascii=False)
    with open(ctx.config.service_meta_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "schema_version": int(ctx.config.service_matrix_schema_version),
                "run_signature": run_sig,
                "shape": [n, s],
                "dtype": "float32",
            },
            f,
            ensure_ascii=False,
        )

def _try_load_service_matrix_cache(ctx: PipelineContext, acc: AccessibilityStageResult) -> ServiceStageResult | None:
    """Load service matrix cache when fully compatible with current run.

    Inputs:
    - ctx: pipeline context with service cache paths/configuration.
    - acc: accessibility-stage output used to preserve node ordering and geometry.

    Outputs:
    - ServiceStageResult when cache is valid; otherwise None.
    """
    if not ctx.config.service_matrix_cache_enabled:
        return None
    
    needed = [
        ctx.config.service_meta_path,
        ctx.config.service_node_to_row_path,
        ctx.config.service_to_col_path,
        ctx.config.service_matrix_path,
    ]

    if not all(os.path.exists(p) for p in needed):
        return None
    
    with open(ctx.config.service_meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    if int(meta.get("schema_version", -1)) != int(ctx.config.service_matrix_schema_version):
        return None
    if meta.get("run_signature") != _service_run_signature(ctx):
        return None
    
    with open(ctx.config.service_node_to_row_path, encoding="utf-8") as f:
        node_to_row_raw = json.load(f)
    node_to_row = {str(k): int(v) for k, v in node_to_row_raw.items()}

    with open(ctx.config.service_to_col_path, encoding="utf-8") as f:
        service_to_col_raw = json.load(f)
    service_to_col = {str(k): int(v) for k, v in service_to_col_raw.items()}

    n, s = int(meta["shape"][0]), int(meta["shape"][1])
    mat = np.memmap(ctx.config.service_matrix_path, dtype=np.float32, mode="r", shape=(n, s))

    out = ServiceStageResult()
    for node in acc.node_results:
        row = node_to_row.get(str(node.node_id))
        if row is None:
            return None
        
        service_scores = {}
        for service in serv.SERVICE_KEYS:
            col = service_to_col.get(service)
            service_scores[service] = float(mat[row, col]) if col is not None else 0.0

        out.node_results.append(
            ServiceNodeResult(
                node_id=node.node_id,
                lat=node.lat,
                lon=node.lon,
                service_scores=service_scores,
            )
        )
    return out

def run_service_stage(ctx: PipelineContext, acc: AccessibilityStageResult) -> ServiceStageResult:
    """Aggregate POI-type accessibility values into one score per service for each node.

    Inputs:
    - ctx: pipeline context with service configuration and progress settings.
    - acc: node-level accessibility results grouped by service and POI type.

    Outputs:
    - ServiceStageResult: one service-score dictionary per node.
    """
    # Fast path: skip aggregation when service matrix cache is compatible.
    cached = _try_load_service_matrix_cache(ctx, acc)
    if cached is not None:
        return cached

    # Slow path: aggregate from accessibility payload and then persist cache.
    out = ServiceStageResult()
    pbar = tqdm(total=len(acc.node_results), desc="Service stage", mininterval=1) if ctx.config.enable_progress else None
    zero_service_counts = {service: 0 for service in serv.SERVICE_KEYS}
    empty_input_counts = {service: 0 for service in serv.SERVICE_KEYS}
    all_zero_node_count = 0
    nonzero_input_zero_output_counts = {service: 0 for service in serv.SERVICE_KEYS}
    sample_limit = 5
    sampled_zero_services: dict[str, int] = {service: 0 for service in serv.SERVICE_KEYS}
    try:
        for node in acc.node_results:
            service_scores = {}
            node_all_zero = True
            for service in serv.SERVICE_KEYS:
                items = node.accessibility_by_service.get(service, [])
                values = [float(item["accessibility"]) for item in items]
                if not values:
                    empty_input_counts[service] += 1
                    score = 0.0
                else:
                    score = serv.choquet_integral(values, service)
                    if score == 0.0 and max(values) > 0.0:
                        nonzero_input_zero_output_counts[service] += 1
                        if sampled_zero_services[service] < sample_limit:
                            print(
                                f"[Service] Zero score with positive inputs: node_id={node.node_id} "
                                f"service={service} poi_types={[item.get('poi_type') for item in items]} "
                                f"accessibility={values}",
                                flush=True,
                            )
                            sampled_zero_services[service] += 1
                service_scores[service] = score
                if score > 0.0:
                    node_all_zero = False
                else:
                    zero_service_counts[service] += 1
            out.node_results.append(
                ServiceNodeResult(
                    node_id=node.node_id,
                    lat=node.lat,
                    lon=node.lon,
                    service_scores=service_scores,
                )
            )
            if node_all_zero:
                all_zero_node_count += 1
            if pbar:
                pbar.update(1)
    finally:
        if pbar:
            pbar.close()

    total_nodes = len(acc.node_results)
    print(
        f"[Service] Summary: nodes={total_nodes} all_zero_nodes={all_zero_node_count} "
        f"cache={'enabled' if ctx.config.service_matrix_cache_enabled else 'disabled'}",
        flush=True,
    )
    for service in serv.SERVICE_KEYS:
        print(
            f"[Service] service={service} zero_scores={zero_service_counts[service]}/{total_nodes} "
            f"empty_inputs={empty_input_counts[service]} "
            f"nonzero_input_zero_output={nonzero_input_zero_output_counts[service]}",
            flush=True,
        )

    _write_service_matrix_cache(ctx, out)
    return out
