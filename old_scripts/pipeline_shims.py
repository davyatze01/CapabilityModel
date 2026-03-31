import os
from helpers import PipelineConfig, build_context
from snapping_stage import run_snapping_stage
from bus_routing_stage import run_bus_routing_stage
from non_bus_routing_stage import run_non_bus_routing_stage
from accessibility_stage import run_accessibility_stage
from service_stage import run_service_stage
from capability_stage import run_capability_stage


def empty_cache(non_bus_cache_dir: str | None = None):
    cache_dir = non_bus_cache_dir or os.path.join("cache", "non_bus")
    print("Starting cache cleanup...")
    for folder in ["route_cache", "rra_cache", "poi_geom_cache", cache_dir]:
        if not os.path.isdir(folder):
            print(f"Skip missing folder: {folder}")
            continue
        print(f"Cleaning folder: {folder}")
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                os.remove(path)
        print(f"Finished folder: {folder}")
    output_csvs = [
        os.path.join("outputs", "capability_restorativeness.csv"),
        os.path.join("outputs", "capability_nutrition.csv"),
        os.path.join("outputs", "capability_care.csv"),
    ]
    for output_csv in output_csvs:
        if os.path.isfile(output_csv):
            print(f"Removing file: {output_csv}")
            os.remove(output_csv)
        else:
            print(f"Skip missing file: {output_csv}")
    print("Cache cleanup completed.")


def run_pipeline(
    max_nodes=None,
    max_pois=None,
    seed=42,
    enable_progress=True,
    skip_routing=False,
):
    cfg = PipelineConfig(
        skip_routing=skip_routing,
        max_nodes=max_nodes,
        max_pois=max_pois,
        seed=seed,
        enable_progress=enable_progress,
    )
    ctx = build_context(cfg)

    snap = run_snapping_stage(ctx)
    bus = run_bus_routing_stage(ctx, snap)

    non_bus = run_non_bus_routing_stage(ctx, snap)
    acc = run_accessibility_stage(ctx, non_bus, bus)
    if acc.missing_bus_ods_total:
        print(f"Missing routing OD lookups: {acc.missing_bus_ods_total}")

    svc = run_service_stage(ctx, acc)
    cap = run_capability_stage(ctx, svc)

    print("Wrote results to:")
    for path in cap.output_paths.values():
        print(f"- {path}")
