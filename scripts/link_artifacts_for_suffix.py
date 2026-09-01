"""One-off setup: symlink the routing-stage artifacts (unaffected by a downstream-only config
change like a saturation-tier remap or ELECTRE boundary swap) from a base artifact_slug into a
new artifact_slug_suffix namespace, so a main.py run with ARTIFACT_SLUG_SUFFIX set reuses them
instead of rerouting from scratch. Only accessibility/ and service/ (and everything
downstream: outputs/gpkg/, outputs/poi_exports/) are left for that run to recompute fresh --
those are exactly the stages a saturation/ELECTRE-boundary change actually affects.

Run with: python3 scripts/link_artifacts_for_suffix.py
"""

from __future__ import annotations

import os
from pathlib import Path

# ── Knobs ────────────────────────────────────────────────────────────────────
BASE_SLUG = "mgp_boundary"
SUFFIX = "scenario2"
ARTIFACTS_ROOT = Path("artifacts")

# Routing-stage paths, safe to reuse verbatim -- everything else under artifacts/<slug>/
# (accessibility/, service/) depends on the config being changed and must NOT be linked.
REUSABLE_PATHS: tuple[str, ...] = ("snapping", "bus", "subway", "non_bus", "walkability", "impedances.npz")


def link_reusable_artifacts(base_slug: str = BASE_SLUG, suffix: str = SUFFIX) -> None:
    base_dir = ARTIFACTS_ROOT / base_slug
    dest_dir = ARTIFACTS_ROOT / f"{base_slug}_{suffix}"
    if not base_dir.is_dir():
        raise RuntimeError(f"{base_dir} not found -- run main.py for {base_slug} first.")
    dest_dir.mkdir(parents=True, exist_ok=True)

    for name in REUSABLE_PATHS:
        src = (base_dir / name).resolve()
        dest = dest_dir / name
        if not src.exists():
            print(f"[skip] {src} not found (not used by this study city/run)")
            continue
        if dest.exists() or dest.is_symlink():
            print(f"[skip] {dest} already exists")
            continue
        os.symlink(src, dest, target_is_directory=src.is_dir())
        print(f"[link] {dest} -> {src}")

    print(f"[done] {dest_dir} ready -- accessibility/ and service/ will be computed fresh.")


if __name__ == "__main__":
    link_reusable_artifacts()
