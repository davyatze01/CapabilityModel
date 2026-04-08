import csv
import hashlib
import os
import signal
import shutil
import subprocess
from pathlib import Path

from pipeline_types import BusRoutingStageResult, PipelineContext, SnappingStageResult
from snapping_stage import build_selected_routing_destinations

COORD_ROUND = 6


def _coords_signature(coords: list[tuple[float, float]]) -> str:
    """Hash ordered coordinates to identify exact routing inputs across runs."""
    h = hashlib.sha1()
    for lat, lon in coords:
        h.update(f"{round(float(lat), COORD_ROUND)},{round(float(lon), COORD_ROUND)};".encode("ascii"))
    return h.hexdigest()


def _write_r5r_point_inputs(
    nodes_with_coords,
    destinations,
    origins_csv,
    destinations_csv,
):
    """Write origin/destination CSV files consumed by the R routing script."""
    Path(origins_csv).parent.mkdir(parents=True, exist_ok=True)

    with open(origins_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "lon", "lat"])
        for node_id, data in nodes_with_coords:
            writer.writerow([str(node_id), float(data["x"]), float(data["y"])])

    with open(destinations_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "lon", "lat"])
        for idx, (lat, lon) in enumerate(destinations):
            writer.writerow([f"d{idx}", float(lon), float(lat)])


def _run_r5r_script(script_path: str) -> None:
    """Run the external R routing script and stream its logs."""
    rscript_exe = shutil.which("Rscript")
    if not rscript_exe:
        raise RuntimeError(
            "Rscript executable not found. Add Rscript to your PATH so the pipeline can run the R routing script."
        )
    if os.name == "nt":
        proc = subprocess.Popen(
            [rscript_exe, script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        proc = subprocess.Popen(
            [rscript_exe, script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="", flush=True)
        return_code = proc.wait()
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, [rscript_exe, script_path])
    except KeyboardInterrupt:
        if proc.poll() is None:
            try:
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        raise


def _require_existing_routing_csv(path: str) -> None:
    if not os.path.isfile(path):
        raise RuntimeError(
            "Missing routing CSV while skip_routing=True. "
            f"Expected file: {path}"
        )
    if os.path.getsize(path) <= 0:
        raise RuntimeError(
            "Routing CSV is empty while skip_routing=True. "
            f"File: {path}"
        )


def run_bus_routing_stage(ctx: PipelineContext, snap: SnappingStageResult) -> BusRoutingStageResult:
    """Execute transit routing stage and produce expanded routing CSV artifacts."""
    cfg = ctx.config
    departure_iso = cfg.bus_departure_dt.isoformat()
    routing_csv = cfg.bus_routing_matrix_path

    all_candidate_snapped_coords = set()
    has_multi_snap_candidates = False
    for snap_info_for_key in snap.poi_bus_snap_info_by_type.values():
        for candidates in snap_info_for_key.values():
            if len(candidates) > 1:
                has_multi_snap_candidates = True
            for snapped, _ in candidates:
                all_candidate_snapped_coords.add(tuple(snapped))

    if has_multi_snap_candidates:
        unique_snapped_coords = build_selected_routing_destinations(
            ctx.nodes_with_coords,
            snap.poi_bus_snap_info_by_type,
            cfg.enable_progress,
        )
        print(
            "Bus destination reduction: "
            f"candidate_nodes={len(all_candidate_snapped_coords)} "
            f"selected_nodes={len(unique_snapped_coords)}"
        )
    else:
        unique_snapped_coords = sorted(all_candidate_snapped_coords)

    origins = [(data["y"], data["x"]) for _, data in ctx.nodes_with_coords]
    destinations = list(unique_snapped_coords)
    origins_sig = _coords_signature(origins)
    destinations_sig = _coords_signature(destinations)

    _write_r5r_point_inputs(
        ctx.nodes_with_coords,
        destinations,
        cfg.bus_routing_origins_input_path,
        cfg.bus_routing_destinations_input_path,
    )

    if cfg.skip_routing:
        _require_existing_routing_csv(routing_csv)
        print(f"[Bus] skip_routing=True. Reusing existing routing CSV: {routing_csv}", flush=True)
        return BusRoutingStageResult(
            routing_csv=routing_csv,
            routing_departure_iso=departure_iso,
            origins_sig=origins_sig,
            destinations_sig=destinations_sig,
        )

    reuse_existing_csv = (
        cfg.skip_r5r_if_csv_exists
        and os.path.isfile(routing_csv)
        and os.path.getsize(routing_csv) > 0
    )

    if reuse_existing_csv:
        print(f"[Bus] Reusing existing routing CSV: {routing_csv}", flush=True)
    else:
        r_script_path = os.path.join("utils", "r5_routing.r")
        chunk_dir = os.path.join("outputs", "r5r_chunks")
        if os.path.isdir(chunk_dir):
            shutil.rmtree(chunk_dir)
        print("[Bus] Launching Rscript...", flush=True)
        _run_r5r_script(r_script_path)
        print("[Bus] Rscript completed.", flush=True)

    return BusRoutingStageResult(
        routing_csv=routing_csv,
        routing_departure_iso=departure_iso,
        origins_sig=origins_sig,
        destinations_sig=destinations_sig,
    )
