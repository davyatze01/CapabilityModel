from context import PipelineContext
from pipeline_types import AccessibilityStageResult, ServiceStageResult, ServiceNodeResult
from utils import services as serv
from tqdm import tqdm


def run_service_stage(ctx: PipelineContext, acc: AccessibilityStageResult) -> ServiceStageResult:
    """Aggregate POI-type accessibility values into one score per service for each node.

    Inputs:
    - ctx: pipeline context with service configuration and progress settings.
    - acc: node-level accessibility results grouped by service and POI type.

    Outputs:
    - ServiceStageResult: one service-score dictionary per node.
    """
    out = ServiceStageResult()
    pbar = tqdm(total=len(acc.node_results), desc="Service stage", mininterval=1) if ctx.config.enable_progress else None
    try:
        for node in acc.node_results:
            service_scores = {}
            for service in serv.SERVICE_KEYS:
                items = node.accessibility_by_service.get(service, [])
                values = [float(item["accessibility"]) for item in items]
                service_scores[service] = serv.choquet_integral(values, service) if values else 0.0
            out.node_results.append(
                ServiceNodeResult(
                    node_id=node.node_id,
                    lat=node.lat,
                    lon=node.lon,
                    service_scores=service_scores,
                )
            )
            if pbar:
                pbar.update(1)
    finally:
        if pbar:
            pbar.close()
    return out
