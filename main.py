import faulthandler
import os
import sys
import traceback

import core.notify as notify
from core.runtime_setup import run_runtime_setup


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
WORKER_COUNT = 8
# LIGHT_OUTPUT: skip the per-hexagon POI/interface exports — they exist only to feed the
#   web interface and are by far the heaviest post-routing step. The run still produces the
#   capability CSVs, the GeoPackage, and the QGIS project. Meant for colleagues starting
#   from a shipped impedances.npz who only need to inspect results in QGIS.
#   Also settable without editing this file: CAP_LIGHT_OUTPUT=1 python main.py
LIGHT_OUTPUT = True
# NOTIFY_CRASH: send a Telegram message when the run stops for ANY reason — unhandled
#   exception, Ctrl+C, OOM/cgroup SIGKILL, hard crash, terminal dying — plus one on a clean
#   finish. Uses a detached watchdog process (outside the run_safe.sh cgroup) so even a
#   SIGKILL of the pipeline gets reported. Needs notify_config.json (gitignored) with the
#   bot token and chat id; setup steps and a --test command are documented in notify.py.
NOTIFY_CRASH = True
# POI_RADIUS_KM: fixed POI search radius in km, overriding the usual decay-based threshold
#   (see core.config.PipelineConfig.poi_radius_m). None = normal behavior (radius derived
#   from poi_radius_decay_threshold). Set to e.g. 5.0 for a fixed-radius sensitivity run.
#   Non-bus impedance is aggregated at routing time using this radius (see
#   utils.delta_g.accessibility_non_bus_from_snap_map) so it cannot be reused across a radius
#   change; setting this bucket-isolates the non-bus cache and impedance bundle under
#   artifacts/<slug>/non_bus_r<radius> / impedances_r<radius>.npz so a rerun at thes SAME
#   radius still hits cache, while ARTIFACT_SLUG_SUFFIX below keeps this run's final outputs
#   (gpkg/QGIS project) from overwriting the normal run's.
POI_RADIUS_KM = None
# ARTIFACT_SLUG_SUFFIX: namespaces every output path under artifacts/<slug>_<suffix>/ and
#   outputs/.../<slug>_<suffix> (see PipelineConfig.artifact_slug_suffix), so a radius/profile
#   experiment never overwrites the normal run's outputs. Snapping and bus/subway routing are
#   radius-independent (see config.py's radius_bucket comment) — symlink those subfolders from
#   the normal artifacts/<slug>/ dir into the new one before running to reuse them instead of
#   re-routing from scratch.
ARTIFACT_SLUG_SUFFIX = None
# If true, main will generate an interactive dashboard for inspecting the results
DEBUG_REPORT = False
# If true, skip the pipeline entirely and just (re)generate the debug report from the
# last run's artifacts already on disk (non_bus/bus/accessibility/service caches,
# grid_params.json, the spatial gpkg). Useful after a debug_pipeline.py-only change.
DEBUG_REPORT_ONLY = False
# If true, main will generate a robustness analysis dashboard
ROBUSTNESS_REPORT = False
# If true, main will generate a dashboard that evaluates the model's sensitivity when changing the parameters
SENSITIVITY_REPORT = False
# PAID_POI_AFFORDABILITY: general affordability multiplier u(y) applied to paid poi_types
#   (core.profiles.PAID_POI_TYPES) — e.g. 0.7 discounts every paid POI's contribution to
#   accessibility by 30%, same mechanism scenarios.py's personas use. None = baseline (1.0,
#   no discount). Free/public POIs are always u=1 regardless of this knob. Does NOT bucket
#   the artifact namespace like POI_RADIUS_KM does, and the accessibility-matrix cache's
#   signature doesn't account for this value (see core.profiles.Profile.affordability) —
#   delete artifacts/<slug>/ before a run where you change this, per project convention.
PAID_POI_AFFORDABILITY: float | None = 1.0

def _reexec_under_run_safe_if_needed() -> None:
    """Re-run this entrypoint through run_safe.sh when not already in a cgroup scope.

    VS Code's play button launches `python main.py` directly. This function replaces
    the current process with `run_safe.sh main.py` so every run gets the systemd
    memory-cgroup protection regardless of how it was started.

    Inside the VS Code Flatpak sandbox systemd-run lives on the host, not in the
    sandbox — so we use `flatpak-spawn --host` to exec run_safe.sh there.

    Linux-only: the cgroup cap and hardware-crash retries exist for this
    workstation specifically. On Windows/macOS (colleagues' machines) the
    pipeline runs plainly — main.py is the same single entrypoint everywhere.
    """
    if sys.platform != "linux":
        return
    if os.environ.get("CAP_MEM_BUDGET_GB"):
        return  # already inside a run_safe.sh cgroup scope
    if os.environ.get("CAP_SKIP_RUN_SAFE"):
        return

    root = os.path.dirname(os.path.abspath(__file__))
    run_safe = os.path.join(root, "scripts", "run_safe.sh")
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

    # Arm AFTER the re-exec so the watchdog tracks the real (scoped) pipeline process,
    # not the pre-exec launcher. Fails fast here if notify_config.json is missing/broken.
    if NOTIFY_CRASH:
        notify.install_crash_notifier(f"capability pipeline ({study_city})")

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
    from core.config import PipelineConfig
    
    from core.context import build_context
    from stages.snapping_stage import run_snapping_stage, load_snap_checkpoint, write_snap_checkpoint
    from routing.public_transport_routing_stage import run_public_transport_routing_stage
    from routing.non_bus_routing_stage import run_non_bus_routing_stage
    from stages.accessibility_stage import run_accessibility_stage
    from stages.service_stage import run_service_stage
    from stages.capability_stage import run_capability_stage
    from exports.artifact_bundle import load_impedance_bundle, write_impedance_bundle
    from exports.poi_exports import generate_poi_exports
    from core.pipeline_runner import generate_spatial_outputs
    from tools.debug_pipeline import run_debug_pipeline
    from analysis.robustness_analysis import run_robustness_pipeline
    from analysis.sensitivity_analysis import run_sensitivity_pipeline
    from analysis.sensitivity_upstream import run_upstream
    from analysis.sensitivity_report import main as build_sensitivity_report

    shutup.please()

    # Create a context with the configuration values specified in PipelineConfig.
    # All global values accessed by multiple stages are found here.
    light_output = LIGHT_OUTPUT or os.environ.get("CAP_LIGHT_OUTPUT") == "1"

    cfg_kwargs = {}
    if POI_RADIUS_KM is not None:
        cfg_kwargs["poi_radius_m"] = float(POI_RADIUS_KM) * 1000.0
    if ARTIFACT_SLUG_SUFFIX:
        cfg_kwargs["artifact_slug_suffix"] = ARTIFACT_SLUG_SUFFIX
    cfg = PipelineConfig(study_city=study_city, **cfg_kwargs)
    if light_output:
        print("[Config] LIGHT_OUTPUT: skipping hexagon/interface POI exports (CSV + gpkg + QGIS only).", flush=True)
    print(f"[Config] study_city={cfg.study_city}  city_name={cfg.city_name}", flush=True)
    print(f"[Config] boundary shapefile: {cfg.name_shapefile}  (use_shapefile={cfg.use_shapefile})", flush=True)
    print(f"[Config] POI source: poi_from_shp={cfg.poi_from_shp}", flush=True)
    if cfg.poi_from_shp:
        for p in cfg.poi_shapefile_paths:
            print(f"[Config]   {p}", flush=True)
    if DEBUG_REPORT_ONLY:
        print("[Config] DEBUG_REPORT_ONLY: regenerating debug report from existing artifacts only.", flush=True)
        run_debug_pipeline(cfg.city_slug)
        return

    ctx = build_context(cfg)
    if PAID_POI_AFFORDABILITY is not None:
        from core.profiles import Profile
        ctx.profile = Profile(
            key="paid_poi_affordability_override",
            enabled_modes=frozenset({"walk", "bike", "drive", "bus"}),
            walk_speed_kmh=cfg.speed_walk_kmh,
            affordability=PAID_POI_AFFORDABILITY,
            canteen_utility=1.0,
        )
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
        # A crash in a later stage (e.g. bus routing) leaves no impedance bundle, so this block
        # re-runs from snapping. The snap checkpoint lets that restart skip the stage's uncached
        # per-run work (POI dedup, radius filter, CSR loads) entirely.
        snap = load_snap_checkpoint(ctx)
        if snap is not None:
            print("[Snap] Loaded snapping checkpoint; skipping snapping recompute.", flush=True)
        else:
            snap = run_snapping_stage(ctx)
            write_snap_checkpoint(ctx, snap)
        total_poi_instances = sum(len(info) for info in snap.poi_bus_snap_info_by_type.values())
        print(
            f"[Pipeline] origins={len(ctx.nodes_with_coords)}  "
            f"poi_types={len(snap.poi_bus_snap_info_by_type)}  "
            f"poi_instances={total_poi_instances}",
            flush=True,
        )

        if light_output:
            print("[Stage] POI Export (pre-routing) skipped — light output mode.", flush=True)
        elif not os.path.exists(cfg.poi_export_geopackage_path):
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
    if light_output:
        print("[Stage] POI Export (post-routing) skipped — light output mode.", flush=True)
        poi_exports = {}
    else:
        print("[Stage] POI Export (post-routing)", flush=True)
        poi_exports = generate_poi_exports(ctx, snap=snap, non_bus=non_bus, acc=acc)

        # generate_poi_exports only writes the provisional id-only hex_pois export;
        # the powered + quantile-scaled per-POI sp/cp values come from the score
        # report pass, which overwrites it. Without this the hex_pois store is
        # id-only (see pipeline_runner.py, which runs both back to back).
        print("[Stage] Score Report (hex_pois service/capability power)", flush=True)
        from analysis.score_report import generate_score_report
        generate_score_report(ctx=ctx)

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
    if poi_exports:
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

    if NOTIFY_CRASH:
        notify.mark_success(f"Spatial output: {gpkg_path or shapefile_path}")

    if DEBUG_REPORT:
        print("\n[Stage] Generating debug report...", flush=True)
        run_debug_pipeline(cfg.city_slug)

    if ROBUSTNESS_REPORT:
        print("\n[Stage] Generating robustness report...", flush=True)
        run_robustness_pipeline(cfg.artifact_slug)

    if SENSITIVITY_REPORT:
        print("\n[Stage] Generating sensitivity report (downstream)...", flush=True)
        run_sensitivity_pipeline(cfg.artifact_slug)
        print("\n[Stage] Generating sensitivity report (upstream)...", flush=True)
        run_upstream()
        print("\n[Stage] Generating sensitivity HTML dashboard...", flush=True)
        build_sensitivity_report()

if __name__ == "__main__":
    from routing.public_transport_routing_stage import RscriptNotFoundError

    try:
        try:
            main()
        except RscriptNotFoundError:
            print("[Main] Rscript not found; running scripts/setup_r.py ...", flush=True)
            import scripts.setup_r as setup_r
            setup_r.main()          # exits the process itself if setup fails or is declined
            print("[Main] Retrying pipeline run...", flush=True)
            main()
    except KeyboardInterrupt:
        traceback.print_exc()
        # Intentional Ctrl+C: stand the watchdog down, but don't send a message.
        notify.mark_interrupted()
        sys.exit(130)
    except BaseException:
        traceback.print_exc()
        # No-op unless install_crash_notifier() already armed; the watchdog covers
        # deaths this handler can't see (SIGKILL, terminal crash).
        notify.notify_exception(traceback.format_exc())
        sys.exit(1)