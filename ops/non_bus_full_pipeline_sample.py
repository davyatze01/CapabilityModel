#!/usr/bin/env python3
"""Honest ETA probe for the non-bus routing stage: runs the REAL production path
(same multiprocessing pool, same worker count, same per-worker caches, same on-disk
node cache) on a random sample of real origins, instead of extrapolating from a
sequential single-process loop.

Why this exists
----------------
ops/non_bus_routing_bench.py compares routing *backends* (scipy/networkit/osrm) on a
small sequential sample -- useful for backend selection, but it never touches the
`multiprocessing.Pool`, the `maxtasksperchild` worker recycling, or the process-global
memo caches in utils/delta_g.py (_COORD_NODE_MEMO/_NODE_IDX_MEMO), so its "ETA(all N
origins)" number can't see costs that only show up under the real pool (e.g. per-worker
memory growth across `maxtasksperchild=200` origins). That gap is exactly what made a
prior ETA of "a couple of hours" turn into a 9-hour run that reached 4% before the
cgroup OOM-killed it.

This script builds the REAL boundary-filtered, hex-grid-sampled origin list (the exact
set production routes) and randomly subsamples `--sample` origins FROM THAT LIST, then
calls the production `run_non_bus_routing_stage` unmodified -- same pool size, same
cache directory (so sampled nodes' results are reused, not wasted, by a later full
run), same progress bar and RAM/proc postfix you'd see in production.

An earlier version of this script used `config.debug_max_nodes`, which samples the RAW
node pool *before* the boundary filter and hex-grid dedup step -- that systematically
thinned out dense city-center hexagons (many raw nodes competing for the same small hex
cell get discarded by the pre-filter, so the cell often ends up with no representative
node at all) while leaving sparse suburban hexagons untouched. Two samples of 41 and
191 "origins" built that way showed no memory pressure at all, then the real run OOM'd
in ~2 minutes once it hit actual dense-POI origins (2026-07-16) -- the debug_max_nodes
sample simply never contained the origins that mattered.

Usage
-----
    # Run exactly like a production run, under the same memory cap, so a stall or
    # runaway shows up the same way it would in the real pipeline:
    ./run_safe.sh ops/non_bus_full_pipeline_sample.py --sample 200

Report back: wall-clock time, the extrapolated ETA for all origins, and whether the
RAM/proc figures in the progress bar were still climbing at the end of the sample (if
they were still climbing steadily instead of leveling off, the sample is too small to
trust the ETA -- rerun with a larger --sample before trusting the number).
"""

from __future__ import annotations

import argparse
import os
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
    parser.add_argument(
        "--sample", type=int, default=200,
        help="Number of real origins to route through the actual worker pool "
             "(default 200 -- large enough to span several maxtasksperchild=200 "
             "worker recycles at typical worker counts, small enough to bound "
             "worst-case wall time).",
    )
    args = parser.parse_args()

    os.environ["CAP_STUDY_CITY"] = args.city
    run_runtime_setup()

    import random

    import shutup
    from core.config import PipelineConfig
    from core.context import build_context
    from stages.snapping_stage import load_snap_checkpoint, run_snapping_stage
    from routing.non_bus_routing_stage import run_non_bus_routing_stage

    shutup.please()

    # Build the REAL context -- no debug_max_nodes -- so the origin list is the exact
    # boundary-filtered, hex-grid-sampled 3,185 origins production would route.
    #
    # `config.debug_max_nodes` (the previous version of this script) samples the RAW
    # node pool *before* the boundary filter and hex-grid dedup step. That randomly
    # thins out dense city-center hexagons (many raw nodes competing for the same
    # small hex cells get discarded by the pre-filter, so those cells often end up
    # with no representative node at all) while leaving sparser suburban hexagons
    # untouched -- systematically biasing the "sample" away from exactly the
    # dense-POI origins that turned out to matter (a handful of dense origins OOM'd
    # the real run in ~2 minutes on 2026-07-16, while two debug_max_nodes samples of
    # 41 and 191 "origins" showed no memory pressure at all). Subsampling from the
    # ALREADY hex-sampled list preserves the real spatial/density distribution.
    cfg = PipelineConfig(study_city=args.city)
    ctx = build_context(cfg)
    total_origins = len(ctx.nodes_with_coords)
    print(f"[Sample] city={cfg.study_city} total_origins={total_origins}", flush=True)

    rng = random.Random(cfg.seed)
    if args.sample < len(ctx.nodes_with_coords):
        ctx.nodes_with_coords = rng.sample(ctx.nodes_with_coords, args.sample)
    print(
        f"[Sample] Routing {len(ctx.nodes_with_coords)} real (spatially-representative) "
        f"origins through the PRODUCTION pool (workers={cfg.non_bus_max_workers}, "
        f"cache_dir={cfg.non_bus_cache_dir})...",
        flush=True,
    )

    snap = load_snap_checkpoint(ctx)
    if snap is None:
        print("[Sample] No snapping checkpoint found; running snapping stage...", flush=True)
        snap = run_snapping_stage(ctx)

    t0 = time.monotonic()
    result = run_non_bus_routing_stage(ctx, snap)
    elapsed_s = time.monotonic() - t0

    newly_computed = result.computed_nodes
    print(
        f"[Sample] Done. cached={result.cached_nodes} computed={newly_computed} "
        f"elapsed={elapsed_s:.1f}s",
        flush=True,
    )
    if newly_computed <= 0:
        print(
            "[Sample] All sampled origins were already cached from a prior run -- "
            "nothing was actually timed. Clear/rotate the sample or pass a fresh "
            "--sample size, or delete the relevant .pkl files under "
            f"{cfg.non_bus_cache_dir} for these origins, then rerun.",
            flush=True,
        )
        return

    rate = newly_computed / elapsed_s
    eta_full_s = total_origins / rate
    print(
        f"[Sample] rate={rate:.4f} origins/s (whole pool, workers={cfg.non_bus_max_workers}) "
        f"ETA(all {total_origins} origins)={eta_full_s / 3600.0:.1f}h",
        flush=True,
    )


if __name__ == "__main__":
    main()
