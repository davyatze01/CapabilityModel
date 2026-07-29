#!/usr/bin/env python3
"""One-time, low-memory builder for the compact CSR routing bundles.

Background
----------
Routing workers run off compact CSR/KD-tree bundles (`graph/<...>_<mode>_csr_v2.npz`),
not the full multi-GB NetworkX graphs. The first run for a new city now streams each
GraphML file directly into CSR, avoiding NetworkX materialization for cached graphs;
prebuilding still keeps the main pipeline from doing that I/O immediately before routing.

This script builds those CSR bundles ahead of time, **single-process, one mode at a
time**. After it finishes, normal pipeline runs load the CSR straight from disk and never
materialize a full graph for snapping, context startup, or non-bus routing.

Usage
-----
    # from the project root, ideally under the memory cap:
    ./run_safe.sh ops/prebuild_csr.py
    # or directly:
    python ops/prebuild_csr.py --city paris --modes walk,bike,drive
"""

from __future__ import annotations

import argparse
import gc
import os
import sys

# Allow running from anywhere: put the project root on sys.path and make it the cwd,
# since graph/POI cache paths in config.py are relative to the project root.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

# run_runtime_setup must run before numpy/osmnx are imported (it caps native math
# threads), so import the stage modules only after calling it inside main().
from core.runtime_setup import run_runtime_setup  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--city",
        default=os.environ.get("CAP_STUDY_CITY", "paris"),
        help="study_city to build for (default: paris or $CAP_STUDY_CITY).",
    )
    parser.add_argument(
        "--modes",
        default="walk,bike,drive",
        help="comma-separated modes to build (default: walk,bike,drive).",
    )
    args = parser.parse_args()

    os.environ["CAP_STUDY_CITY"] = args.city
    run_runtime_setup()

    import shutup
    from core.config import PipelineConfig
    from utils import graphml

    shutup.please()

    cfg = PipelineConfig(study_city=args.city)
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    print(
        f"[Prebuild] city={cfg.study_city} artifact_slug={cfg.artifact_slug} "
        f"modes={modes}",
        flush=True,
    )

    for mode in modes:
        print(f"[Prebuild] Building CSR for mode={mode} ...", flush=True)
        bundle = graphml.get_mode_csr(mode, cfg)
        print(
            f"[Prebuild]   mode={mode} done: nodes={len(bundle['node_ids'])} "
            f"edges={len(bundle['length'])}",
            flush=True,
        )
        # Drop any fallback NetworkX graph plus the CSR bundle before the next mode so
        # only one mode's data is ever resident. The normal cached-GraphML path streams
        # directly and should not populate the full graph cache.
        graphml.clear_mode_graph_cache()
        graphml._MODE_CSR_CACHE.pop(mode, None)
        gc.collect()

    print("[Prebuild] All requested CSR bundles are ready on disk.", flush=True)


if __name__ == "__main__":
    main()
