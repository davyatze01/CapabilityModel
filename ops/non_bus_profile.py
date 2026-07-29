#!/usr/bin/env python3
"""Profile the non-bus routing hot loop on a few REAL origins, single-process.

Confirms (before optimizing) where per-origin time actually goes -- specifically
whether POI->node snapping (_snap_node_idx / cKDTree.query) dominates, as inferred
from the complexity analysis, vs Dijkstra, impedance, or haversine filtering.

Runs _process_node directly in-process (no worker pool, so cProfile sees everything)
on --n origins. Picks the origins CLOSEST to the city centroid by default, since dense
central-Paris origins are the worst case (most in-radius POIs) and the ones that drive
both runtime and the memory blowups.

Usage:
    ./run_safe.sh ops/non_bus_profile.py --n 5
"""

from __future__ import annotations

import argparse
import cProfile
import os
import pstats
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

from core.runtime_setup import run_runtime_setup  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--city", default=os.environ.get("CAP_STUDY_CITY", "paris"))
    parser.add_argument("--n", type=int, default=5, help="Number of origins to profile.")
    parser.add_argument("--central", action="store_true", default=True,
                        help="Pick origins nearest the centroid (densest, worst case).")
    args = parser.parse_args()

    os.environ["CAP_STUDY_CITY"] = args.city
    run_runtime_setup()

    import shutup
    shutup.please()
    from core.config import PipelineConfig
    from core.context import build_context
    from stages.snapping_stage import load_snap_checkpoint, run_snapping_stage
    from utils import delta_g, services as serv
    import routing.non_bus_routing_stage as nb

    cfg = PipelineConfig(study_city=args.city)
    ctx = build_context(cfg)
    print(f"[Profile] city={cfg.study_city} total_origins={len(ctx.nodes_with_coords)}", flush=True)

    snap = load_snap_checkpoint(ctx)
    if snap is None:
        # No checkpoint, but the per-POI snap cache on disk makes this fast (it's what
        # the sample script does too). This must run BEFORE profiling starts so its
        # one-time cost isn't attributed to the per-origin routing body.
        print("[Profile] No snapping checkpoint; rebuilding snap result from cache...", flush=True)
        snap = run_snapping_stage(ctx)

    # Densest origins = nearest the centroid of all origins.
    ys = [d["y"] for _, d in ctx.nodes_with_coords]
    xs = [d["x"] for _, d in ctx.nodes_with_coords]
    cy, cx = sum(ys) / len(ys), sum(xs) / len(xs)
    ordered = sorted(
        ctx.nodes_with_coords,
        key=lambda nd: (nd[1]["y"] - cy) ** 2 + (nd[1]["x"] - cx) ** 2,
    )
    origins = ordered[: args.n]
    print(f"[Profile] Profiling {len(origins)} central (densest) origins.", flush=True)

    # Pre-snap origins (as production does) and initialize worker globals in-process.
    coords_by_id = {nid: (d["y"], d["x"]) for nid, d in origins if "y" in d and "x" in d}
    origin_nodes_by_id = delta_g.snap_origin_nodes_by_mode(coords_by_id, cfg, modes=("walk", "bike", "drive"))
    nb._init_worker(
        cfg,
        snap.poi_bus_snap_info_by_type,
        snap.poi_mode_snap_info_by_type,
        None,
        cfg.non_bus_cache_dir,
        cfg.non_bus_cache_schema_version,
        serv.config_signature(),
        "",
        origin_nodes_by_id,
    )

    # Warm one origin first so one-time setup (CSR load etc.) isn't attributed to the
    # profiled body, then profile the rest.
    t0 = time.monotonic()
    nb._process_node(origins[0])
    print(f"[Profile] Warm-up origin done in {time.monotonic() - t0:.1f}s.", flush=True)

    prof = cProfile.Profile()
    t0 = time.monotonic()
    to_profile = origins[1:]
    for i, item in enumerate(to_profile, start=1):
        # Print BEFORE each origin so a long/stuck origin is visible immediately, not
        # only after it finishes -- no silent gaps (CLAUDE.md).
        print(f"[Profile] origin {i}/{len(to_profile)} (node_id={item[0]}) ...", flush=True)
        t_o = time.monotonic()
        prof.enable()
        nb._process_node(item)
        prof.disable()
        print(f"[Profile]   done in {time.monotonic() - t_o:.1f}s", flush=True)
    wall = time.monotonic() - t0
    n_prof = max(1, len(to_profile))
    print(f"[Profile] Profiled {n_prof} origins in {wall:.1f}s ({wall / n_prof:.1f}s/origin).", flush=True)

    out_path = os.path.join("outputs", "non_bus_bench", "profile_report.txt")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        fh.write(f"city={cfg.study_city} n_profiled={n_prof} wall={wall:.1f}s "
                 f"per_origin={wall / n_prof:.1f}s\n\n")
        st = pstats.Stats(prof, stream=fh)
        fh.write("=== Top 30 by total (self) time ===\n")
        st.sort_stats("tottime").print_stats(30)
        fh.write("\n=== Top 30 by cumulative time ===\n")
        st.sort_stats("cumulative").print_stats(30)

    stats = pstats.Stats(prof)
    print("\n=== Top 20 by total (self) time ===", flush=True)
    stats.sort_stats("tottime").print_stats(20)
    print(f"\n[Profile] Full report written to {out_path}", flush=True)


if __name__ == "__main__":
    main()
