#!/usr/bin/env python3
"""Benchmark harness comparing non-bus routing backends on real Paris data.

Background
----------
The non-bus routing stage (non_bus_routing_stage.py -> utils/delta_g.py) computes,
for every hex-grid origin and each of walk/bike/drive, one radius-bounded Dijkstra
against that mode's full CSR routing graph. For Paris (tens of thousands of origins)
this currently takes about a year of wall-clock. This script does NOT change the
production pipeline. It measures, on a fixed sample of real origins, how three
routing backends compare on speed and on correctness (do they find the same POIs
reachable, at the same distance):

  - scipy   : the current production path (scipy.sparse.csgraph.dijkstra), used as
              the correctness reference.
  - networkit: an in-process C++ graph library (no server/subprocess), MIT-licensed,
              actively maintained, confirmed to install cleanly on this machine's
              Python version. Faster Dijkstra kernel, not true contraction
              hierarchies.
  - osrm    : a local OSRM instance (run via podman/docker, official
              ghcr.io/project-osrm/osrm-backend image) using true contraction
              hierarchies. Can be dramatically faster, but reintroduces an
              external-process resource ceiling of the same *shape* that caused
              real pain with the R5 bus-routing integration (see
              public_transport_routing_stage.py's stall watchdog) -- that's exactly
              why this is a benchmark and not a production swap.

An independent GRAPH_SIMPLIFY knob measures the effect of topology-simplifying the
mode graphs (ox.simplify_graph collapses degree-2 interstitial nodes into single
edges, preserving shortest-path distances) stacked with each backend.

Usage
-----
    # Edit the knobs below, then:
    python ops/non_bus_routing_bench.py
    # or override from the CLI:
    python ops/non_bus_routing_bench.py --backend networkit --simplify --sample 1561
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field

# Allow running from anywhere: put the project root on sys.path and make it the cwd,
# since graph/POI cache paths in config.py are relative to the project root.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

# run_runtime_setup must run before numpy/osmnx are imported (it caps native math
# threads), so import the stage modules only after calling it inside main().
from core.runtime_setup import run_runtime_setup  # noqa: E402

# ── Edit here to configure the benchmark ───────────────────────────────────────────
STUDY_CITY = "paris"
# "scipy" (baseline/reference), "networkit", or "osrm".
ROUTING_BACKEND = "scipy"
# Apply ox.simplify_graph to each mode graph before routing (post-load, so it works
# for Paris's shapefile-sourced graphs too, unlike osm_autobuild_simplify which only
# affects the OSM-download path).
GRAPH_SIMPLIFY = False
# Number of origins to sample for this run. Cagliari's full hex-grid origin count
# (1561 non_bus cache files) takes ~15 minutes end-to-end on the current scipy path,
# so this is used as a fast, real-scale reference point for extrapolating Paris's ETA.
SAMPLE_ORIGINS = 1561
RANDOM_SEED = 42
ROUTING_MODES = ("walk", "bike", "drive")


@dataclass
class BackendResult:
    backend: str
    mode: str
    simplify: bool
    setup_seconds: float
    routing_seconds: float
    origins_routed: int
    # origin_id -> {poi_source_key: distance_m_or_None}
    distances_by_origin: dict = field(default_factory=dict)
    peak_rss_gb: float = 0.0


def _sample_origins(ctx, n: int, seed: int):
    """Deterministically sample n origins so every backend routes the same set."""
    rng = random.Random(seed)
    pool = list(ctx.nodes_with_coords)
    if n >= len(pool):
        return pool
    return rng.sample(pool, n)


def _targets_for_origin(cfg, snap, origin_latlon):
    """Flatten every in-radius POI source coordinate across all services/types.

    Mirrors the radius filter in utils.delta_g.accessibility_non_bus_from_snap_map,
    but collapses all POI queries into one target set per origin -- exactly what the
    real pipeline effectively pays for once per (origin, mode), since every query for
    that origin hits the same cached Dijkstra result. Returns
    {source_key: (lat, lon)}.
    """
    from utils import delta_g, services as serv

    radius_m = serv.get_global_radius_m(cfg)
    targets: dict = {}
    for poi_key, mode_infos in snap.poi_mode_snap_info_by_type.items():
        for mode in ("walk", "bike", "drive"):
            info = mode_infos.get(mode) or {}
            for src_key, src_info in info.items():
                if src_key in targets:
                    continue
                coord = src_info.get("source_coord") if isinstance(src_info, dict) else src_info
                if not isinstance(coord, (list, tuple)) or len(coord) < 2:
                    continue
                coord = (float(coord[0]), float(coord[1]))
                if radius_m is not None:
                    d = delta_g._haversine_m(origin_latlon[0], origin_latlon[1], coord[0], coord[1])
                    if d > radius_m:
                        continue
                targets[src_key] = coord
    return targets


# ---------------------------------------------------------------------------
# scipy backend (production path, used as the correctness reference)
# ---------------------------------------------------------------------------

def _run_scipy_backend(cfg, mode, origins, snap, progress_cb) -> BackendResult:
    from utils import delta_g, services as serv

    t_setup0 = time.monotonic()
    # Warms the CSR bundle + KD-tree once; every call below reuses it.
    delta_g.graphml.get_mode_csr(mode, cfg)
    setup_s = time.monotonic() - t_setup0

    radius_m = serv.get_global_radius_m(cfg)
    detour_factor = float(getattr(cfg, "non_bus_dijkstra_detour_factor", 1.6))
    cutoff_m = None if radius_m is None else radius_m * detour_factor

    distances_by_origin = {}
    t0 = time.monotonic()
    for node_id, data in origins:
        delta_g.reset_origin_caches()
        origin = (data["y"], data["x"])
        targets = _targets_for_origin(cfg, snap, origin)
        origin_idx = delta_g._snap_node_idx(mode, origin[0], origin[1], cfg)
        dist, _pred, _ = delta_g._get_mode_lengths_and_paths(
            origin, mode, radius_m, origin_idx=origin_idx, cfg=cfg, cutoff_m=cutoff_m
        )
        out = {}
        for src_key, coord in targets.items():
            pidx = delta_g._snap_node_idx(mode, coord[0], coord[1], cfg)
            d = dist[pidx]
            out[src_key] = float(d) if d != float("inf") else None
        distances_by_origin[node_id] = out
        progress_cb()
    routing_s = time.monotonic() - t0

    return BackendResult(
        backend="scipy", mode=mode, simplify=False,
        setup_seconds=setup_s, routing_seconds=routing_s,
        origins_routed=len(origins), distances_by_origin=distances_by_origin,
    )


# ---------------------------------------------------------------------------
# networkit backend (in-process, no subprocess/server)
# ---------------------------------------------------------------------------

def _build_networkit_graph(cfg, mode, simplify):
    """Build an nk.Graph from the mode's CSR bundle (optionally topology-simplified).

    Uses the SAME node-index space as the CSR bundle's KD-tree, so origin/POI
    snapping (delta_g._snap_node_idx) resolves to identical indices for both the
    scipy and networkit backends -- required for the distances to be diffable.
    """
    import networkit as nk
    from utils import graphml

    if simplify:
        import osmnx as ox
        full_graph = graphml.get_mode_graph(mode, cfg)
        simplified = full_graph if full_graph.graph.get("simplified") else ox.simplify_graph(full_graph.copy())
        print(
            f"[Bench][networkit] Simplified '{mode}': "
            f"nodes {full_graph.number_of_nodes()} -> {simplified.number_of_nodes()}  "
            f"edges {full_graph.number_of_edges()} -> {simplified.number_of_edges()}",
            flush=True,
        )
        node_list = list(simplified.nodes())
        id_to_idx = {nid: i for i, nid in enumerate(node_list)}
        g = nk.Graph(len(node_list), weighted=True, directed=True)
        for u, v, d in simplified.edges(data=True):
            w = float(d.get("length", 0.0) or 0.0)
            g.addEdge(id_to_idx[u], id_to_idx[v], w, addMissing=False)
        return g, id_to_idx, node_list
    else:
        bundle = graphml.get_mode_csr(mode, cfg)
        indptr, indices, length = bundle["indptr"], bundle["indices"], bundle["length"]
        n = len(bundle["node_ids"])
        g = nk.Graph(n, weighted=True, directed=True)
        for u in range(n):
            for j in range(int(indptr[u]), int(indptr[u + 1])):
                g.addEdge(u, int(indices[j]), float(length[j]), addMissing=False)
        return g, bundle["id_to_idx"], None


def _run_networkit_backend(cfg, mode, origins, snap, simplify, progress_cb) -> BackendResult:
    import networkit as nk
    from utils import delta_g

    t_setup0 = time.monotonic()
    g, id_to_idx, simplified_nodes = _build_networkit_graph(cfg, mode, simplify)
    setup_s = time.monotonic() - t_setup0
    print(
        f"[Bench][networkit] mode={mode} simplify={simplify} "
        f"nodes={g.numberOfNodes()} edges={g.numberOfEdges()} setup={setup_s:.1f}s",
        flush=True,
    )

    # NetworKit's Dijkstra only supports early-stop for a single target node, not a
    # cutoff radius or a target set. Unlike scipy's bounded `limit=`, this runs a full
    # unbounded single-source Dijkstra per origin -- an intentionally honest
    # apples-to-apples-minus-one comparison: it shows networkit's raw kernel speed,
    # but on an unsimplified graph its per-origin cost is pessimistic relative to
    # scipy's radius-bounded search. Simplification narrows that gap by shrinking the
    # whole graph rather than just the search frontier.
    #
    # To keep the correctness diff meaningful, distances beyond the SAME cutoff scipy
    # uses (radius * detour_factor) are clamped to None here too. Without this, every
    # POI scipy correctly reports as "beyond the routing cutoff" would show up as a
    # false "reachability mismatch" against networkit's unbounded result, even though
    # both searches agree on the real network distance.
    from utils import services as serv
    radius_m = serv.get_global_radius_m(cfg)
    detour_factor = float(getattr(cfg, "non_bus_dijkstra_detour_factor", 1.6))
    cutoff_m = None if radius_m is None else radius_m * detour_factor

    def snap_idx(lat, lon):
        if simplified_nodes is not None:
            full_nid = delta_g._nearest_mode_node(mode, lat, lon, cfg)
            return id_to_idx.get(full_nid)
        return delta_g._snap_node_idx(mode, lat, lon, cfg)

    distances_by_origin = {}
    t0 = time.monotonic()
    for node_id, data in origins:
        origin = (data["y"], data["x"])
        targets = _targets_for_origin(cfg, snap, origin)
        origin_idx = snap_idx(origin[0], origin[1])
        out = {}
        if origin_idx is None:
            distances_by_origin[node_id] = {k: None for k in targets}
            progress_cb()
            continue
        dij = nk.distance.Dijkstra(g, origin_idx, storePaths=False, storeNodesSortedByDistance=False)
        dij.run()
        for src_key, coord in targets.items():
            pidx = snap_idx(coord[0], coord[1])
            if pidx is None:
                out[src_key] = None
                continue
            d = dij.distance(pidx)
            if d >= 1e300 or (cutoff_m is not None and d > cutoff_m):
                out[src_key] = None
            else:
                out[src_key] = float(d)
        distances_by_origin[node_id] = out
        progress_cb()
    routing_s = time.monotonic() - t0

    return BackendResult(
        backend="networkit", mode=mode, simplify=simplify,
        setup_seconds=setup_s, routing_seconds=routing_s,
        origins_routed=len(origins), distances_by_origin=distances_by_origin,
    )


# ---------------------------------------------------------------------------
# OSRM backend (external process via podman/docker; true contraction hierarchies)
# ---------------------------------------------------------------------------

_OSRM_PROFILES = {"walk": "foot", "bike": "bicycle", "drive": "car"}
_OSRM_STARTUP_MARKER = "running and waiting for requests"
_OSRM_STARTUP_TIMEOUT_S = 120.0


def _container_runtime() -> str:
    import shutil as _sh
    for exe in ("podman", "docker"):
        if _sh.which(exe):
            return exe
    raise RuntimeError(
        "[Bench][osrm] Neither podman nor docker found on PATH. OSRM is distributed as "
        "the official ghcr.io/project-osrm/osrm-backend container image; install one of "
        "podman or docker to run this backend (Fedora: `sudo dnf install podman`)."
    )


def _ensure_city_pbf(cfg) -> str:
    """Reuse the same clipped per-city PBF the bus-routing/R5 stage already builds."""
    from routing.public_transport_routing_stage import _prepare_r5r_data_bundle

    data_dir = _prepare_r5r_data_bundle(cfg)
    pbf_path = os.path.join(data_dir, f"{cfg.city_slug}.osm.pbf")
    if not os.path.isfile(pbf_path):
        raise RuntimeError(f"[Bench][osrm] Expected clipped PBF not found at {pbf_path}")
    return pbf_path


def _run(cmd: list[str], timeout_s: float, log_prefix: str) -> None:
    import subprocess

    print(f"{log_prefix} $ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, timeout=timeout_s, capture_output=True, text=True)
    if proc.stdout:
        print(proc.stdout[-4000:], flush=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{log_prefix} failed (exit {proc.returncode}): {proc.stderr[-2000:]}")


def _osrm_preprocess(cfg, mode: str, pbf_path: str, runtime: str, work_dir: str) -> str:
    """Run osrm-extract + osrm-partition + osrm-customize (MLD) for one mode/profile.

    Runs once per mode; the resulting .osrm* files are cached under work_dir and
    reused on subsequent invocations (keyed by mode + pbf mtime).
    """
    profile = _OSRM_PROFILES[mode]
    os.makedirs(work_dir, exist_ok=True)
    local_pbf_name = f"{cfg.city_slug}_{mode}.osm.pbf"
    local_pbf = os.path.join(work_dir, local_pbf_name)
    osrm_file = os.path.join(work_dir, local_pbf_name.replace(".osm.pbf", ".osrm"))

    if os.path.isfile(osrm_file) and os.path.getmtime(osrm_file) > os.path.getmtime(pbf_path):
        print(f"[Bench][osrm] Reusing preprocessed data for mode={mode}: {osrm_file}", flush=True)
        return osrm_file

    import shutil as _sh
    _sh.copyfile(pbf_path, local_pbf)

    vol = f"{work_dir}:/data"
    base = [runtime, "run", "--rm", "-v", vol, "ghcr.io/project-osrm/osrm-backend"]
    log_prefix = f"[Bench][osrm][{mode}]"
    _run(base + ["osrm-extract", "-p", f"/opt/{profile}.lua", f"/data/{local_pbf_name}"],
         timeout_s=1800, log_prefix=log_prefix)
    _run(base + ["osrm-partition", f"/data/{local_pbf_name.replace('.osm.pbf', '.osrm')}"],
         timeout_s=1800, log_prefix=log_prefix)
    _run(base + ["osrm-customize", f"/data/{local_pbf_name.replace('.osm.pbf', '.osrm')}"],
         timeout_s=1800, log_prefix=log_prefix)
    return osrm_file


def _start_osrm_routed(runtime: str, work_dir: str, osrm_file: str, port: int):
    """Launch osrm-routed in the background; block until it logs readiness or times out.

    Mirrors the stall-watchdog philosophy already used for the R5 subprocess in
    public_transport_routing_stage.py: fail loudly at a known ceiling instead of
    hanging indefinitely if the container never becomes ready.
    """
    import subprocess
    import threading
    import queue as _queue

    vol = f"{work_dir}:/data"
    osrm_name = os.path.basename(osrm_file)
    cmd = [
        runtime, "run", "--rm", "-p", f"{port}:5000", "-v", vol,
        "ghcr.io/project-osrm/osrm-backend",
        "osrm-routed", "--algorithm", "mld", f"/data/{osrm_name}",
    ]
    print(f"[Bench][osrm] $ {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    ready = threading.Event()
    lines_q: "_queue.Queue[str]" = _queue.Queue()

    def _reader():
        for line in proc.stdout:
            lines_q.put(line)
            if _OSRM_STARTUP_MARKER in line:
                ready.set()

    threading.Thread(target=_reader, daemon=True).start()

    deadline = time.monotonic() + _OSRM_STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            print("[Bench][osrm-routed] " + lines_q.get(timeout=1.0).rstrip(), flush=True)
        except _queue.Empty:
            pass
        if ready.is_set():
            return proc
        if proc.poll() is not None:
            raise RuntimeError("[Bench][osrm] osrm-routed exited before becoming ready.")
    proc.terminate()
    raise RuntimeError(
        f"[Bench][osrm] osrm-routed did not report readiness within "
        f"{_OSRM_STARTUP_TIMEOUT_S:.0f}s -- treating as a hang and killing it."
    )


def _run_osrm_backend(cfg, mode, origins, snap, simplify, progress_cb) -> BackendResult:
    import urllib.request
    import urllib.parse

    if simplify:
        print(
            "[Bench][osrm] Note: GRAPH_SIMPLIFY has no additional effect on OSRM -- "
            "osrm-partition/osrm-customize already build a contraction hierarchy from "
            "the full topology, which subsumes plain degree-2 simplification.",
            flush=True,
        )

    runtime = _container_runtime()
    pbf_path = _ensure_city_pbf(cfg)
    work_dir = os.path.join("outputs", "non_bus_bench", "osrm", cfg.artifact_slug)

    t_setup0 = time.monotonic()
    osrm_file = _osrm_preprocess(cfg, mode, pbf_path, runtime, work_dir)
    port = {"walk": 5001, "bike": 5002, "drive": 5003}[mode]
    proc = _start_osrm_routed(runtime, work_dir, osrm_file, port)
    setup_s = time.monotonic() - t_setup0

    from utils import services as serv
    radius_m = serv.get_global_radius_m(cfg)

    try:
        distances_by_origin = {}
        t0 = time.monotonic()
        CHUNK = 500  # mirrors the R5 destination chunk size proven safe in this repo
        for node_id, data in origins:
            origin = (data["y"], data["x"])
            targets = _targets_for_origin(cfg, snap, origin)
            keys = list(targets.keys())
            out = {}
            for i in range(0, len(keys), CHUNK):
                batch_keys = keys[i:i + CHUNK]
                coords = [(origin[1], origin[0])] + [(targets[k][1], targets[k][0]) for k in batch_keys]
                coord_str = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in coords)
                url = (
                    f"http://127.0.0.1:{port}/table/v1/{_OSRM_PROFILES[mode]}/{coord_str}"
                    f"?sources=0&annotations=distance"
                )
                with urllib.request.urlopen(url, timeout=30) as resp:
                    payload = json.loads(resp.read())
                dists = payload.get("distances", [[]])[0][1:]
                for k, d in zip(batch_keys, dists):
                    out[k] = float(d) if d is not None else None
            distances_by_origin[node_id] = out
            progress_cb()
        routing_s = time.monotonic() - t0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    return BackendResult(
        backend="osrm", mode=mode, simplify=simplify,
        setup_seconds=setup_s, routing_seconds=routing_s,
        origins_routed=len(origins), distances_by_origin=distances_by_origin,
    )


# ---------------------------------------------------------------------------
# Correctness diff + report
# ---------------------------------------------------------------------------

def _diff_against_baseline(baseline: BackendResult, candidate: BackendResult) -> dict:
    max_abs_diff = 0.0
    sum_abs_diff = 0.0
    n_compared = 0
    n_reachability_mismatch = 0
    # Origin node_ids are ints from a live run but come back as strings after a JSON
    # round-trip (JSON object keys are always strings) -- normalize both sides to str
    # so a baseline loaded from disk still matches a freshly computed candidate.
    candidate_by_str_id = {str(k): v for k, v in candidate.distances_by_origin.items()}
    for node_id, base_targets in baseline.distances_by_origin.items():
        cand_targets = candidate_by_str_id.get(str(node_id), {})
        for src_key, base_d in base_targets.items():
            cand_d = cand_targets.get(src_key)
            if (base_d is None) != (cand_d is None):
                n_reachability_mismatch += 1
                continue
            if base_d is None:
                continue
            diff = abs(base_d - cand_d)
            max_abs_diff = max(max_abs_diff, diff)
            sum_abs_diff += diff
            n_compared += 1
    return {
        "n_compared": n_compared,
        "n_reachability_mismatch": n_reachability_mismatch,
        "max_abs_diff_m": max_abs_diff,
        "mean_abs_diff_m": (sum_abs_diff / n_compared) if n_compared else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--city", default=os.environ.get("CAP_STUDY_CITY", STUDY_CITY))
    parser.add_argument("--backend", default=ROUTING_BACKEND, choices=["scipy", "networkit", "osrm"])
    parser.add_argument("--simplify", action="store_true", default=GRAPH_SIMPLIFY)
    parser.add_argument("--sample", type=int, default=SAMPLE_ORIGINS)
    parser.add_argument("--modes", default=",".join(ROUTING_MODES))
    parser.add_argument("--baseline-json", default=None,
                         help="Path to a prior scipy-backend report JSON to diff against "
                              "(this run doesn't have to recompute the baseline every time).")
    args = parser.parse_args()

    os.environ["CAP_STUDY_CITY"] = args.city
    run_runtime_setup()

    import shutup
    from tqdm import tqdm
    import psutil
    from core.config import PipelineConfig
    from core.context import build_context
    from stages.snapping_stage import load_snap_checkpoint, run_snapping_stage

    shutup.please()

    cfg = PipelineConfig(study_city=args.city)
    print(
        f"[Bench] city={cfg.study_city} artifact_slug={cfg.artifact_slug} "
        f"backend={args.backend} simplify={args.simplify} sample={args.sample}",
        flush=True,
    )

    print("[Bench] Building context (node list from CSR bundle)...", flush=True)
    ctx = build_context(cfg)
    print(f"[Bench] {len(ctx.nodes_with_coords)} total origins available.", flush=True)

    print("[Bench] Loading snapping checkpoint...", flush=True)
    snap = load_snap_checkpoint(ctx)
    if snap is None:
        print("[Bench] No checkpoint found; running snapping stage (one-time cost)...", flush=True)
        snap = run_snapping_stage(ctx)

    origins = _sample_origins(ctx, args.sample, RANDOM_SEED)
    print(f"[Bench] Sampled {len(origins)} origins (seed={RANDOM_SEED}).", flush=True)

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    proc = psutil.Process()
    results: dict[str, BackendResult] = {}

    for mode in modes:
        print(f"[Bench] === mode={mode} backend={args.backend} ===", flush=True)
        pbar = tqdm(total=len(origins), desc=f"{args.backend}/{mode}", mininterval=1)

        def _tick():
            pbar.update(1)
            rss_gb = proc.memory_info().rss / (1024.0 ** 3)
            elapsed = max(pbar.format_dict.get("elapsed", 1e-6), 1e-6)
            pbar.set_postfix({"origins/s": f"{pbar.n / elapsed:.2f}", "RSS": f"{rss_gb:.1f}GB"}, refresh=False)

        try:
            if args.backend == "scipy":
                res = _run_scipy_backend(cfg, mode, origins, snap, _tick)
            elif args.backend == "networkit":
                res = _run_networkit_backend(cfg, mode, origins, snap, args.simplify, _tick)
            else:
                res = _run_osrm_backend(cfg, mode, origins, snap, args.simplify, _tick)
        finally:
            pbar.close()

        rate = res.origins_routed / res.routing_seconds if res.routing_seconds > 0 else float("inf")
        eta_full_s = (len(ctx.nodes_with_coords) / rate) if rate > 0 else float("inf")
        print(
            f"[Bench] mode={mode} setup={res.setup_seconds:.1f}s "
            f"routing={res.routing_seconds:.1f}s rate={rate:.2f} origins/s "
            f"ETA(all {len(ctx.nodes_with_coords)} origins)={eta_full_s / 3600.0:.1f}h",
            flush=True,
        )
        results[mode] = res

    report = {
        "city": cfg.study_city,
        "artifact_slug": cfg.artifact_slug,
        "backend": args.backend,
        "simplify": args.simplify,
        "sample_origins": len(origins),
        "total_origins": len(ctx.nodes_with_coords),
        "modes": {
            mode: {
                "setup_seconds": r.setup_seconds,
                "routing_seconds": r.routing_seconds,
                "origins_per_second": r.origins_routed / r.routing_seconds if r.routing_seconds > 0 else None,
                "eta_full_hours": (len(ctx.nodes_with_coords) / (r.origins_routed / r.routing_seconds)) / 3600.0
                if r.routing_seconds > 0 else None,
            }
            for mode, r in results.items()
        },
    }

    # Persist full per-origin distances (not just summary stats) so a later run of
    # another backend can diff against this one via --baseline-json. For 1561-scale
    # samples this is at most tens of MB -- cheap relative to what it buys.
    report["distances_by_origin"] = {
        mode: r.distances_by_origin for mode, r in results.items()
    }

    if args.baseline_json:
        if not os.path.isfile(args.baseline_json):
            raise FileNotFoundError(f"--baseline-json not found: {args.baseline_json}")
        with open(args.baseline_json) as f:
            baseline_report = json.load(f)
        if baseline_report.get("sample_origins") != len(origins):
            raise RuntimeError(
                f"Baseline was sampled with {baseline_report.get('sample_origins')} origins, "
                f"this run used {len(origins)} -- re-run both with the same --sample (and "
                f"RANDOM_SEED, unchanged in this script) so they cover identical origins."
            )
        report["diff_vs_baseline"] = {}
        for mode, r in results.items():
            baseline_dist = baseline_report.get("distances_by_origin", {}).get(mode)
            if baseline_dist is None:
                print(f"[Bench] Baseline has no data for mode={mode}; skipping diff.", flush=True)
                continue
            baseline_res = BackendResult(
                backend=baseline_report.get("backend", "scipy"), mode=mode, simplify=False,
                setup_seconds=0.0, routing_seconds=0.0, origins_routed=len(baseline_dist),
                distances_by_origin=baseline_dist,
            )
            diff = _diff_against_baseline(baseline_res, r)
            report["diff_vs_baseline"][mode] = diff
            flag = " *** REACHABILITY MISMATCH ***" if diff["n_reachability_mismatch"] else ""
            print(
                f"[Bench] Diff vs baseline mode={mode}: compared={diff['n_compared']} "
                f"mean_abs_diff_m={diff['mean_abs_diff_m']} max_abs_diff_m={diff['max_abs_diff_m']} "
                f"reachability_mismatches={diff['n_reachability_mismatch']}{flag}",
                flush=True,
            )
    elif args.backend != "scipy":
        print(
            "[Bench] No --baseline-json given: run with --backend scipy first (writes a "
            "report with full distances under outputs/non_bus_bench/), then pass that "
            "report's path here via --baseline-json for a correctness diff. This run's "
            "own report still has its own full distances saved for later use as a "
            "baseline or comparison target.",
            flush=True,
        )

    out_dir = os.path.join("outputs", "non_bus_bench")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{cfg.artifact_slug}_{args.backend}_{'simplified' if args.simplify else 'full'}_{int(time.time())}.json")
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"[Bench] Wrote report: {out_path}", flush=True)


if __name__ == "__main__":
    main()
