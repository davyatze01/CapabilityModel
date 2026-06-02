import multiprocessing as mp
import os

from config import PipelineConfig
from pipeline_types import PipelineContext
from utils import capabilities as cap
from utils import graphml


def build_context(config: PipelineConfig) -> PipelineContext:
    """Build shared runtime context from configuration.

    Inputs:
    - config: pipeline configuration with paths, debug options, and worker settings.

    Outputs:
    - PipelineContext: graph, node list, output paths, worker count, and service groupings.
    """
    graph = graphml.get_mode_graph("walk", config)
    nodes = list(graph.nodes(data=True))
    nodes_with_coords = [item for item in nodes if "y" in item[1] and "x" in item[1]]

    if config.debug_max_nodes is not None:
        import random
        rng = random.Random(config.seed)
        nodes_with_coords = rng.sample(
            nodes_with_coords,
            min(config.debug_max_nodes, len(nodes_with_coords)),
        )

    os.makedirs("outputs", exist_ok=True)
    os.makedirs(config.non_bus_cache_dir, exist_ok=True)
    os.makedirs(config.poi_snap_cache_dir, exist_ok=True)
    os.makedirs(os.path.dirname(config.bus_impedance_matrix_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(config.accessibility_matrix_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(config.service_matrix_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(config.impedance_artifact_path) or ".", exist_ok=True)

    output_paths = {
        "restorativeness": os.path.join("outputs", "capability_restorativeness.csv"),
        "nutrition": os.path.join("outputs", "capability_nutrition.csv"),
        "care": os.path.join("outputs", "capability_care.csv"),
    }

    workers = (
        max(1, mp.cpu_count())
        if config.worker_count is None
        else max(1, int(config.worker_count))
    )

    return PipelineContext(
        config=config,
        graph=graph,
        nodes_with_coords=nodes_with_coords,
        workers=workers,
        output_paths=output_paths,
        rest_services=cap.CAPABILITY_SERVICES["restorativeness"],
        nut_services=cap.CAPABILITY_SERVICES["nutrition"],
        care_services=cap.CAPABILITY_SERVICES["care"],
    )
