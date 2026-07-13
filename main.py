import faulthandler
import os
import sys
import traceback

from runtime_setup import run_runtime_setup

faulthandler.enable(all_threads=True)

# Change this to "paris" to switch the whole pipeline to Paris.
study_city = "cagliari"

# ── Execution knobs (edit here instead of setting environment variables) ──────────────
# SAFE_MODE: gentle execution to avoid pinning the machine at full load — caps native math
#   library threads to 1 per process, halves the worker count to ~physical_cores//2, and runs
#   workers at below-normal priority. Results are identical; only scheduling changes.
SAFE_MODE = False
# WORKER_COUNT: force the number of pool workers. None = automatic (memory/CPU derived, then
#   the safe-mode cap if SAFE_MODE). Set to 1 for a single-process "survival" run.
WORKER_COUNT = None


def _reexec_under_run_safe_if_needed() -> None:
    """Re-run this entrypoint through run_safe.sh when not already in a cgroup scope.

    VS Code's play button launches `python main.py` directly. This function replaces
    the current process with `run_safe.sh main.py` so every run gets the systemd
    memory-cgroup protection regardless of how it was started.

    Inside the VS Code Flatpak sandbox systemd-run lives on the host, not in the
    sandbox — so we use `flatpak-spawn --host` to exec run_safe.sh there.
    """
    if os.environ.get("CAP_MEM_BUDGET_GB"):
        return  # already inside a run_safe.sh cgroup scope
    if os.environ.get("CAP_SKIP_RUN_SAFE"):
        return

    root = os.path.dirname(os.path.abspath(__file__))
    run_safe = os.path.join(root, "run_safe.sh")
    if not os.path.isfile(run_safe):
        print(f"[Safe mode] run_safe.sh not found at {run_safe}; continuing without cgroup.", flush=True)
        return

    entry = os.path.abspath(__file__)
    print("[Safe mode] Re-launching under run_safe.sh for memory-cgroup protection.", flush=True)

    in_flatpak = os.path.isfile("/.flatpak-info")
    if in_flatpak:
        import shutil as _shutil
        spawn = _shutil.which("flatpak-spawn")
        if spawn:
            # Run bash + run_safe.sh on the host where systemd-run is available.
            os.execv(spawn, [spawn, "--host", "bash", run_safe, entry, *sys.argv[1:]])
        print("[Safe mode] flatpak-spawn not available; continuing without cgroup.", flush=True)
        return

    os.execv("/usr/bin/env", ["env", "bash", run_safe, entry, *sys.argv[1:]])


def main():
    """Run the full capability pipeline end-to-end and print generated output paths."""
    _reexec_under_run_safe_if_needed()

    # Translate the script knobs into the env vars the runtime/stages read. Must happen
    # before run_runtime_setup() so the math-thread caps take effect before numpy is imported
    # (and propagate to spawned workers via inherited environment).
    if SAFE_MODE:
        os.environ["CAP_SAFE_MODE"] = "1"
    if WORKER_COUNT is not None:
        os.environ["CAP_WORKERS"] = str(int(WORKER_COUNT))

    run_runtime_setup()
    os.environ["CAP_STUDY_CITY"] = study_city

    import shutup
    from config import PipelineConfig
    
    from context import build_context
    from snapping_stage import run_snapping_stage
    from public_transport_routing_stage import run_public_transport_routing_stage
    from non_bus_routing_stage import run_non_bus_routing_stage
    from accessibility_stage import run_accessibility_stage
    from service_stage import run_service_stage
    from capability_stage import run_capability_stage
    from artifact_bundle import load_impedance_bundle, write_impedance_bundle
    from poi_exports import generate_poi_exports
    from pipeline_runner import generate_spatial_outputs

    shutup.please()

    # Create a context with the configuration values specified in PipelineConfig.
    # All global values accessed by multiple stages are found here.
    cfg = PipelineConfig(study_city=study_city)
    print(f"[Config] study_city={cfg.study_city}  city_name={cfg.city_name}", flush=True)
    print(f"[Config] boundary shapefile: {cfg.name_shapefile}  (use_shapefile={cfg.use_shapefile})", flush=True)
    print(f"[Config] POI source: poi_from_shp={cfg.poi_from_shp}", flush=True)
    if cfg.poi_from_shp:
        for p in cfg.poi_shapefile_paths:
            print(f"[Config]   {p}", flush=True)
    ctx = build_context(cfg)
    snap = None

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
        from utils import services as serv
        global_radius_m = serv.get_global_radius_m(cfg)
        if global_radius_m is not None:
            max_type = max(serv.POI_DECAY_COEFFICIENTS, key=lambda t: serv.POI_DECAY_COEFFICIENTS[t])
            max_coeff = serv.POI_DECAY_COEFFICIENTS[max_type]
            print(
                f"[Radius] global_radius={global_radius_m/1000:.1f} km  "
                f"(max_decay_coeff={max_coeff}  poi_type={max_type}  threshold={cfg.poi_radius_decay_threshold})",
                flush=True,
            )

        print("[Stage] Snapping", flush=True)
        # In the snapping stage, each of the pois is snapped to the closest point of the corresponding network.
        # Pois that are lines or geometries are snapped to a candidate set of points and the closest to the origin is selected when performing routing.
        snap = run_snapping_stage(ctx)
        total_poi_instances = sum(len(info) for info in snap.poi_bus_snap_info_by_type.values())
        print(
            f"[Pipeline] origins={len(ctx.nodes_with_coords)}  "
            f"poi_types={len(snap.poi_bus_snap_info_by_type)}  "
            f"poi_instances={total_poi_instances}",
            flush=True,
        )

        if not os.path.exists(cfg.poi_export_geopackage_path):
            print("[Stage] POI Export (pre-routing)", flush=True)
            pre_route_poi_exports = generate_poi_exports(ctx, snap=snap)
            print(
                "[Output] Pre-routing POI geopackage: "
                + ", ".join(str(path) for path in pre_route_poi_exports.values()),
                flush=True,
            )
        else:
            print("[Stage] POI Export (pre-routing) skipped — already exported.", flush=True)

        # Using the snapped pois, we compute bus routes and distances.
        print("[Stage] Bus Routing", flush=True)
        bus = run_public_transport_routing_stage(ctx, snap, transport_type="bus")

        # Cities with a combined feed (e.g. France/IDFM) also route subway as a second,
        # independent public-transport modality. Artifacts land under artifacts/<city>/subway/
        # and are picked up by the accessibility stage via cfg.subway_* paths.
        if cfg.enable_subway:
            print("[Stage] Subway Routing", flush=True)
            run_public_transport_routing_stage(ctx, snap, transport_type="metro")

        # Using the snapped pois, we compute walk, car and bike routes and distances (non-bus).
        print("[Stage] Non-Bus Routing", flush=True)
        non_bus = run_non_bus_routing_stage(ctx, snap)

        write_impedance_bundle(ctx, bus, non_bus)
        print(f"[Artifact] Wrote impedance bundle: {cfg.impedance_artifact_path}", flush=True)

    # We compute impedances, decay and accessibilities for each Origin-Destination pair based on the routing results.
    # The Accessibility values are then aggregated for POI type.
    print("[Stage] Accessibility", flush=True)
    acc = run_accessibility_stage(ctx, non_bus, bus)

    # Always run the post-routing export: it is the authoritative version that carries the
    # per-(hexagon, POI) service/capability powers (from `acc`). The pre-routing export only
    # writes a provisional id-only version and creates the GeoPackage — gating this on the
    # GeoPackage's existence would skip the powered export and leave only id-only hex files.
    print("[Stage] POI Export (post-routing)", flush=True)
    poi_exports = generate_poi_exports(ctx, snap=snap, non_bus=non_bus, acc=acc)

    # Each poi type contributes to one or multiple services. Based on the accessibility to the poi types, we compute the opportunity for services.
    print("[Stage] Service Aggregation", flush=True)
    svc = run_service_stage(ctx, acc)

    # Each service contributes to a capability. For each capability, we aggregate the opportunity for the services and compute a final capability score.
    print("[Stage] Capability Aggregation", flush=True)
    cap = run_capability_stage(ctx, svc)

    # The results are written as csv in the outputs folder, one for each capability.
    print(
        "[Output] Capability CSV files: "
        + ", ".join(str(path) for path in cap.output_paths.values()),
        flush=True,
    )
    print(
        "[Output] POI export files: "
        + ", ".join(str(path) for path in poi_exports.values()),
        flush=True,
    )

    # Generate plots, a combined shapefile, and a GeoPackage output.
    spatial_outputs = generate_spatial_outputs(cfg, cap)

    shapefile_path = spatial_outputs["shapefile_path"]
    gpkg_path = spatial_outputs["gpkg_path"]

    print(f"[Done] Spatial output: {gpkg_path or shapefile_path}", flush=True)
    if gpkg_path:
        print(f"[Done] GeoPackage output: {gpkg_path}", flush=True)
    if shapefile_path:
        print(f"[Done] Shapefile output: {shapefile_path}", flush=True)

if __name__ == "__main__":
    try:
        main()
    except BaseException:
        traceback.print_exc()
        sys.exit(1)