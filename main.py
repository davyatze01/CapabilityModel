import shutup
import io

from config import PipelineConfig
from context import build_context
from snapping_stage import run_snapping_stage
from bus_routing_stage import run_bus_routing_stage
from non_bus_routing_stage import run_non_bus_routing_stage
from accessibility_stage import run_accessibility_stage
from service_stage import run_service_stage
from capability_stage import run_capability_stage
from artifact_bundle import load_impedance_bundle, write_impedance_bundle
from plot_shapefile import plot_all_experiments
from generate_experiment_shapefiles import generate_shapefile_by_csv
import faulthandler
import sys
import traceback

faulthandler.enable(all_threads=True)
shutup.please()

# Force line-buffered stdout so stage logs appear immediately in terminals/IDEs.
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(line_buffering=True)

import logging
logger = logging.getLogger(__name__)

def main():
    """Run the full capability pipeline end-to-end and print generated output paths."""
    # Create a context with the configuration values specified in PipelineConfig. All global values accessed by multiple stages are found here.
    cfg = PipelineConfig()
    ctx = build_context(cfg)

    loaded = load_impedance_bundle(ctx)
    if loaded is not None:
        print(
            "[Artifact] Loaded impedance bundle. "
            "Skipping snapping, bus routing, and non-bus routing.",
            flush=True,
        )
        bus, non_bus = loaded
    else:
        print(
            "[Artifact] No valid impedance bundle found; recomputing impedances "
            "(this may take a while).",
            flush=True,
        )
        print("[Stage] Snapping", flush=True)
        # In the snapping stage, each of the pois is snapped to the closest point of the corresponding network.
        # Pois that are lines or geometries are snapped to a candidate set of points and the closest to the origin is selected when performing routing.
        snap = run_snapping_stage(ctx)

        # Using the snapped pois, we compute bus routes and distances.
        print("[Stage] Bus Routing", flush=True)
        bus = run_bus_routing_stage(ctx, snap)

        # Using the snapped pois, we compute walk, car and bike routes and distances (non-bus).
        print("[Stage] Non-Bus Routing", flush=True)
        non_bus = run_non_bus_routing_stage(ctx, snap)

        write_impedance_bundle(ctx, bus, non_bus)
        print(f"[Artifact] Wrote impedance bundle: {cfg.impedance_artifact_path}", flush=True)

    # We compute impedances, decay and accessibilities for each Origin-Destination pair based on the routing results. The Accessibility values are then aggregated for POI type
    print("[Stage] Accessibility", flush=True)
    acc = run_accessibility_stage(ctx, non_bus, bus)

    # Each poi type contributes to one or multiple services. Based on the accessibility to the poi types, we compute the opportunity for services
    print("[Stage] Service Aggregation", flush=True)
    svc = run_service_stage(ctx, acc)

    # Each service contributes to a capability. For each capability, we aggregate the opportunity for the services and compute a final capability score
    print("[Stage] Capability Aggregation", flush=True)
    cap = run_capability_stage(ctx, svc)

    # The results are written as csv in the outputs folder, one for each capability
    print("Wrote results to:", flush=True)
    for path in cap.output_paths.values():
        print(f"- {path}", flush=True)

    # Create the plot for every experiment
    generated_plots = plot_all_experiments(k_meters=10)
    for plot_path in generated_plots:
        print(plot_path)

    # Generate shapefile for every experiment
    for path in generate_shapefile_by_csv():
        print(path)

if __name__ == "__main__":
    try:
        main()
    except BaseException:
        traceback.print_exc()
        sys.exit(1)
