"""Upstream sensitivity analysis: perturb the user-configured parameters in
config/poi_types.csv and config/services.csv and re-run the REAL accessibility
and service stages from the cached travel times (artifacts/<city>/impedances.npz).

Option-A design (agreed): no re-implementation of the upstream math. Each
configuration runs in a fresh subprocess so the mutated CSVs are picked up by
the normal import-time loaders, with every artifact/cache path rebased into a
per-config workspace -- the baseline artifacts and caches are never touched.

Configurations (each axis tests exactly one value below and one above its
baseline):
  baseline                         pristine CSVs (reference for all comparisons)
  decay_x0.8 / x1.2                poi_types.decay_coefficient scaled (travel tolerance)
  interactions_0 / x0.7            poi_types.choquet_interactions: all defined pairs set to
                                    0 (none) / 0.7 (maximal) -- the baseline values are tiny
                                    (-0.05..0.07) so scaling them stays inert regardless of factor
  capacity_blend50 / exaggerate50  services.choquet_capacity blended 50% toward uniform (less
                                    skewed) / deviations from uniform exaggerated 50% (more skewed)
  contribution_min / max           services.contribution_coefficient: every POI type set to
                                    the minimum (1) / maximum (8) tier observed in the baseline's
                                    discrete Fibonacci-like class set (see CONTRIBUTION_MIN_TIER)

For each configuration the per-node service scores are exported, classified with
the baseline ELECTRE TRI (vectorized, validated in sensitivity_analysis.py), and
compared against the baseline configuration: % of nodes changing final capability
class and mean |service-score delta| per service.

Usage:
  Edit UPSTREAM_ONLY / UPSTREAM_REPORT_ONLY below, then `python sensitivity_upstream.py`.

Runs are resumable: a config with an existing service_scores.csv is skipped.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import os
import shutil
import subprocess
import sys
import time
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Callable

# Allow running directly (python analysis/sensitivity_upstream.py) -- this is how
# run_configs() below relaunches itself as the per-config worker subprocess.
# Without this, the worker's sys.path[0] is analysis/ (not the repo root), so
# `import core...` fails before the worker even creates its output folder.
# Mirrors the bootstrap in ops/*.py and analysis/scenarios.py.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

CONFIG_DIR = Path("config")
MUTABLE_CSVS = ["poi_types.csv", "services.csv"]
def _work_root() -> Path:
    """outputs/debug/<slug>/sensitivity_upstream — resolved per-call so worker
    subprocesses (which inherit CAP_STUDY_CITY via os.environ) agree with the
    parent process on where results live."""
    from core.config import PipelineConfig
    return Path("outputs/debug") / PipelineConfig().artifact_slug / "sensitivity_upstream"
WORKER_ENV_VAR = "CAP_UPSTREAM_WORKER"
BASELINE_NAME = "baseline"

# Name of a single key in CONFIGS to run (e.g. "decay_x0.8"), skipping every other
# config -- None means no filter, run the full sweep.
UPSTREAM_ONLY: str | None = None
# True = skip the sweep entirely and just rebuild upstream_report.md from whatever
# per-config service_scores.csv files already exist under _work_root().
UPSTREAM_REPORT_ONLY: bool = False
# Wall-clock cap per config's subprocess, in seconds -- fail fast if one config hangs
# instead of running forever. Sized for the study city: a small city's full pass
# (routing restore + accessibility + scoring) fits well inside 3600s, but Paris's
# accessibility stage alone can run 60-90+ min (2026-08-19: still at 80% after 44 min),
# so this needs raising for Paris runs -- e.g. 10800 (3h).
UPSTREAM_CONFIG_TIMEOUT_S = 3600
# Per-city override: Paris's node universe is large enough that a full-node sensitivity
# sweep is impractically slow (see UPSTREAM_CONFIG_TIMEOUT_S's Paris note above), so it
# subsamples. Cagliari runs on all nodes -- it must match what the cached impedances
# bundle was actually routed for, or load_impedance_bundle()'s origins_sig check fails
# and the worker can't recompute (it only symlinks the cached bundle, doesn't reroute).
UPSTREAM_LIGHT_MAX_NODES_BY_CITY: dict[str, int | None] = {
    "paris": 3000,
}
# Minimum free disk space (GB) required before launching each config's subprocess -- abort
# loudly instead of letting a routing-reroute bug (or anything else) silently fill the disk,
# as happened 2026-08-26 (a worker wrote 500+ GB into its own workspace before crashing).
# Sized well above one config's expected footprint; raise if legitimately tight on space.
MIN_FREE_DISK_GB: float = 100.0


# ── CSV mutations ────────────────────────────────────────────────────────────

def _scale_decay(rows: list[dict], factor: float) -> None:
    for row in rows:
        row["decay_coefficient"] = f"{float(row['decay_coefficient']) * factor:g}"


def _cap_interactions(rows: list[dict], magnitude: float) -> None:
    """Set every *defined* choquet_interactions entry to a fixed bracketing
    magnitude (0 = no interactions at all, 0.7 = maximal interactions), preserving
    each entry's original sign (a configured -0.03 becomes -0.7, not +0.7; only the
    strength is bracketed, not the direction of the effect). Undefined (None,
    diagonal) pairs stay undefined -- the loader (utils/services.py) hard-requires
    the diagonal to be None and rejects any other value. The configured magnitudes
    are tiny (-0.05..0.07), so scaling them by any factor stays inert -- this
    instead asks "what if the true interaction strength were an order of
    magnitude larger than we assumed?"."""
    for row in rows:
        raw = (row.get("choquet_interactions") or "").strip()
        if not raw:
            continue
        values = ast.literal_eval(raw.replace("None", "None"))
        capped = [
            None if v is None else (0.0 if magnitude == 0 else (magnitude if v >= 0 else -magnitude))
            for v in values
        ]
        row["choquet_interactions"] = "[" + ",".join("None" if v is None else f"{v:g}" for v in capped) + "]"


def _blend_capacity(rows: list[dict], blend: float) -> None:
    """Blend each service's choquet_capacity toward uniform. blend=1 -> uniform."""
    for row in rows:
        values = [float(v) for v in ast.literal_eval(row["choquet_capacity"])]
        n = len(values)
        uniform = 1.0 / n
        blended = [round((1 - blend) * v + blend * uniform, 4) for v in values]
        row["choquet_capacity"] = "[" + ", ".join(f"{v:g}" for v in blended) + "]"


def _exaggerate_capacity(rows: list[dict], factor: float) -> None:
    """Mirror image of _blend_capacity: push each service's choquet_capacity
    *away* from uniform instead of toward it (deviations from uniform scaled by
    1+factor), so the axis has a genuine below/above-baseline pair instead of
    two variants that both flatten the hand-chosen skew. Clipped at 0 and
    renormalized to sum to 1 (capacities are a partition of unit mass)."""
    for row in rows:
        values = [float(v) for v in ast.literal_eval(row["choquet_capacity"])]
        n = len(values)
        uniform = 1.0 / n
        exaggerated = [max(0.0, uniform + (1 + factor) * (v - uniform)) for v in values]
        total = sum(exaggerated)
        normalized = [round(v / total, 4) for v in exaggerated]
        row["choquet_capacity"] = "[" + ", ".join(f"{v:g}" for v in normalized) + "]"




def _cap_contribution(rows: list[dict], value: int) -> None:
    for row in rows:
        n = len(ast.literal_eval(row["contribution_coefficient"]))
        row["contribution_coefficient"] = "[" + ", ".join(f"{value:g}" for _ in range(n)) + "]"


# name -> (csv filename, mutation function). RRA lambda-mode configs mutate no
# CSV (mutation=None, like baseline) -- their perturbation is an env var applied
# to the worker subprocess instead; see ENV_CONFIGS below.
CONFIGS: dict[str, tuple[str, Callable[[list[dict]], None]] | None] = {
    BASELINE_NAME: None,
    "decay_x0.8": ("poi_types.csv", lambda rows: _scale_decay(rows, 0.8)),
    "decay_x1.2": ("poi_types.csv", lambda rows: _scale_decay(rows, 1.2)),
    "interactions_0": ("poi_types.csv", lambda rows: _cap_interactions(rows, 0.0)),
    "interactions_x0.7": ("poi_types.csv", lambda rows: _cap_interactions(rows, 0.7)),
    "capacity_blend50": ("services.csv", lambda rows: _blend_capacity(rows, 0.5)),
    "capacity_exaggerate50": ("services.csv", lambda rows: _exaggerate_capacity(rows, 0.5)),
    "contribution_min": ("services.csv", lambda rows: _cap_contribution(rows, CONTRIBUTION_MIN_TIER)),
    "contribution_max": ("services.csv", lambda rows: _cap_contribution(rows, CONTRIBUTION_MAX_TIER)),
    "rra_lambda_uniform": None,
    "rra_lambda_reversed": None,
}

# RRA's redundancy weighting (utils/decay.py: rra_breakdown) isn't a CSV
# parameter, so it can't be perturbed by mutating config/*.csv like the other
# axes -- it's baked into the accessibility formula. Instead of a below/above
# baseline pair (there's no continuous "baseline +/- step" for a weighting
# scheme), this axis brackets the two structural extremes:
#   uniform  -- every mode counted at full weight (lambda=1), i.e. no
#               redundancy discount at all: having several slow backup modes is
#               worth exactly as much as 1 fast one.
#   reversed -- the taper is flipped: the WORST mode gets lambda=1 and the
#               BEST gets the smallest weight -- the polar opposite of the
#               baseline assumption (rewarding redundancy) instead of a
#               continuous perturbation of it.
# Applied by setting RRA_LAMBDA_MODE in the worker subprocess's environment
# (read by utils.decay.rra_breakdown); config/*.csv is left untouched.
ENV_CONFIGS: dict[str, dict[str, str]] = {
    "rra_lambda_uniform": {"RRA_LAMBDA_MODE": "uniform"},
    "rra_lambda_reversed": {"RRA_LAMBDA_MODE": "rank_asc"},
}


def _check_disk_space(min_free_gb: float = MIN_FREE_DISK_GB) -> None:
    free_gb = shutil.disk_usage(".").free / 1e9
    if free_gb < min_free_gb:
        raise RuntimeError(
            f"Only {free_gb:.1f} GB free disk space (< {min_free_gb:g} GB minimum) -- "
            "aborting the sweep before launching another subprocess. Free up space and rerun "
            "(resumable: configs with an existing service_scores.csv are skipped)."
        )


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


# contribution_coefficient values are drawn from a small discrete tier set --
# "how many POIs of this type to reach 90% saturation" -- not a continuous
# quantity (see accessibility_from_rra() in utils/delta_g.py). Scaling by an
# arbitrary factor lands off-tier (3*0.5=1.5 is not a class a modeler could
# have picked). Mirroring the choquet_interactions bracketing design: assign
# every POI type the minimum or maximum tier actually configured right now,
# read live from config/services.csv so a rescale of the tier set (e.g. the
# 2026-08 move from {1,2,3,5,8} to {5,10,30,50,80}) can't silently desync
# this sweep from what's really configured.
def _contribution_tier_bounds() -> tuple[int, int]:
    _, rows = _read_csv(CONFIG_DIR / "services.csv")
    tiers = {int(v) for row in rows for v in ast.literal_eval(row["contribution_coefficient"])}
    return min(tiers), max(tiers)


CONTRIBUTION_MIN_TIER, CONTRIBUTION_MAX_TIER = _contribution_tier_bounds()


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _config_hash() -> str:
    """sha1 of poi_types.csv + services.csv concatenated -- identifies the exact
    config state a cached sweep was computed against."""
    h = hashlib.sha1()
    for name in MUTABLE_CSVS:
        h.update((CONFIG_DIR / name).read_bytes())
    return h.hexdigest()


# ── Worker: runs inside the subprocess with mutated CSVs in place ────────────

def run_worker(config_name: str) -> int:
    """Run accessibility + service stages into the per-config workspace."""
    workdir = (_work_root() / config_name).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    from core.config import PipelineConfig, normalize_study_city
    from core.context import build_context

    # CAP_POI_RADIUS_M (set by run_configs() below) freezes poi_radius_m for this whole
    # worker process, including any PipelineConfig() built deep in the call graph (e.g.
    # utils.graphml.get_poi()) -- see the field's comment in core/config.py.
    city = normalize_study_city(os.environ.get("CAP_STUDY_CITY", "cagliari"))
    light_max_nodes = UPSTREAM_LIGHT_MAX_NODES_BY_CITY.get(city)
    # debug_max_nodes is NOT passed here: build_context() would subsample nodes_with_coords
    # before load_impedance_bundle() below validates it against the cached bundle's
    # origins_sig (baked in from the FULL node set) -- subsampling first guarantees a
    # mismatch (see the 2026-08-26 baseline failures, both Cagliari and Paris). Build the
    # full node set, load the bundle against it, then subsample afterward.
    cfg = PipelineConfig()
    # Rebase every artifact path from artifacts/<slug>/ into the workspace so
    # nothing of the baseline caches is read or written by this run.
    base_prefix = os.path.join(cfg.artifacts_root_dir, cfg.artifact_slug)
    baseline_impedances = Path(base_prefix) / "impedances.npz"
    if not baseline_impedances.exists():
        print(f"ERROR: {baseline_impedances} not found — run the pipeline once first.", file=sys.stderr)
        return 1
    for f in dataclass_fields(cfg):
        value = getattr(cfg, f.name, None)
        if isinstance(value, str) and value.startswith(base_prefix):
            object.__setattr__(cfg, f.name, str(workdir / os.path.relpath(value, base_prefix)))

    # The cached travel times are the one shared input: link them in read-only.
    link = Path(cfg.impedance_artifact_path)
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.exists():
        os.symlink(baseline_impedances.resolve(), link)

    # Every other artifact path was just rebased into the isolated workdir above ("nothing
    # of the baseline caches is read or written by this run") -- but accessibility/service
    # also read cached routing data straight off disk (bus/subway matrices, non-bus per-node
    # aggregates), not just the in-memory bundle load_impedance_bundle() returns. Left
    # rebased, those land on fresh EMPTY directories and the accessibility stage silently
    # reroutes all of Paris from scratch to refill them (2026-08-26: two separate incidents,
    # 500+ GB each, filled the disk both times). Symlink them back in read-only too, same as
    # impedances.npz. non_bus_cache_dir is radius-bucketed (this worker's pinned poi_radius_m
    # differs from the base run's, which used the decay-derived default), so its basename
    # under base_prefix may differ from cfg's rebased one -- resolve it from what's actually
    # on disk rather than assuming a name. bus/ and subway/ are never radius-bucketed, so
    # their basenames always match.
    def _link_dir(src: Path, dest: Path) -> None:
        if dest.exists() or dest.is_symlink():
            return
        if not src.is_dir():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(src.resolve(), dest)

    base_non_bus_candidates = sorted(Path(base_prefix).glob("non_bus*"))
    if base_non_bus_candidates:
        _link_dir(base_non_bus_candidates[0], Path(cfg.non_bus_cache_dir))
    _link_dir(Path(base_prefix) / "bus", Path(cfg.bus_routing_matrix_path).parent)
    if cfg.enable_subway:
        _link_dir(Path(base_prefix) / "subway", Path(cfg.subway_routing_matrix_path).parent)

    from exports.artifact_bundle import load_impedance_bundle
    from stages.accessibility_stage import run_accessibility_stage
    from stages.service_stage import run_service_stage

    ctx = build_context(cfg)
    loaded = load_impedance_bundle(ctx)
    if loaded is None:
        print("ERROR: impedance bundle failed to load in workspace.", file=sys.stderr)
        return 1
    bus, non_bus = loaded

    if light_max_nodes is not None and len(ctx.nodes_with_coords) > light_max_nodes:
        import random
        rng = random.Random(cfg.seed)
        ctx.nodes_with_coords = rng.sample(ctx.nodes_with_coords, light_max_nodes)
        print(f"[worker:{config_name}] sampled down to {light_max_nodes} nodes for scoring", flush=True)
    print(f"[worker:{config_name}] impedances loaded; running accessibility stage...", flush=True)
    acc = run_accessibility_stage(ctx, non_bus, bus)
    print(f"[worker:{config_name}] accessibility done ({len(acc.node_results)} nodes); service stage...", flush=True)
    svc = run_service_stage(ctx, acc)

    out_csv = workdir / "service_scores.csv"
    from utils import services as serv

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["node_id", "lat", "lon"] + [f"service_{s}" for s in serv.SERVICE_KEYS])
        for node in svc.node_results:
            writer.writerow(
                [node.node_id, node.lat, node.lon]
                + [node.service_scores.get(s, 0.0) for s in serv.SERVICE_KEYS]
            )
    print(f"[worker:{config_name}] wrote {out_csv}", flush=True)
    return 0


# ── Orchestrator ─────────────────────────────────────────────────────────────

def run_configs(only: str | None) -> None:
    _work_root().mkdir(parents=True, exist_ok=True)

    hash_path = _work_root() / "config_hash.txt"
    current_hash = _config_hash()
    has_cached_runs = any((_work_root() / name / "service_scores.csv").exists() for name in CONFIGS)
    stale = has_cached_runs and (
        not hash_path.exists() or hash_path.read_text().strip() != current_hash
    )
    if stale:
        reason = "no config_hash.txt on record (cache predates staleness tracking)" if not hash_path.exists() \
            else "poi_types.csv/services.csv changed since the last sweep"
        print(f"[config] {reason} — wiping cached per-config results (stale).", flush=True)
        for name in CONFIGS:
            shutil.rmtree(_work_root() / name, ignore_errors=True)
    hash_path.write_text(current_hash)

    backup_dir = _work_root() / "_config_backup"
    backup_dir.mkdir(exist_ok=True)

    pristine: dict[str, tuple[list[str], list[dict], str]] = {}
    for name in MUTABLE_CSVS:
        src = CONFIG_DIR / name
        shutil.copy2(src, backup_dir / name)
        fieldnames, rows = _read_csv(src)
        pristine[name] = (fieldnames, rows, _sha1(src))

    # Freeze the candidate node/graph set across the whole sweep: get_global_radius_m()
    # derives its buffer from poi_types.csv's decay_coefficient, which the decay axis
    # mutates -- left alone, that silently points decay_x0.8/x1.2 at a DIFFERENT OSM
    # graph extract than baseline, invalidating the shared impedances.npz they're
    # supposed to reuse. Compute it once from the still-pristine config and pin every
    # worker to it, so only the aggregation math (not the reachable universe) varies.
    from core.config import PipelineConfig
    from utils.services import get_global_radius_m
    baseline_radius_m = get_global_radius_m(PipelineConfig())

    def restore() -> None:
        for name in MUTABLE_CSVS:
            shutil.copy2(backup_dir / name, CONFIG_DIR / name)
        for name in MUTABLE_CSVS:
            if _sha1(CONFIG_DIR / name) != pristine[name][2]:
                raise RuntimeError(f"config/{name} restore verification FAILED — check {backup_dir}")

    try:
        for config_name, mutation in CONFIGS.items():
            if only and config_name != only:
                continue
            done_marker = _work_root() / config_name / "service_scores.csv"
            if done_marker.exists():
                print(f"[skip] {config_name}: already computed ({done_marker})", flush=True)
                continue

            # Start from pristine CSVs, apply this config's single mutation.
            restore()
            if mutation is not None:
                csv_name, fn = mutation
                fieldnames, rows, _ = pristine[csv_name]
                rows_copy = [dict(r) for r in rows]
                fn(rows_copy)
                _write_csv(CONFIG_DIR / csv_name, fieldnames, rows_copy)
                print(f"[config] {config_name}: mutated config/{csv_name}", flush=True)

            _check_disk_space()

            env_overrides = ENV_CONFIGS.get(config_name)
            if env_overrides:
                print(f"[config] {config_name}: worker env {env_overrides}", flush=True)
            worker_env = {
                **os.environ, WORKER_ENV_VAR: config_name,
                "CAP_POI_RADIUS_M": str(baseline_radius_m),
                **(env_overrides or {}),
            }

            t0 = time.time()
            print(f"[run] {config_name}: launching stage subprocess...", flush=True)
            result = subprocess.run(
                [sys.executable, __file__],
                timeout=UPSTREAM_CONFIG_TIMEOUT_S,
                env=worker_env,
            )
            if result.returncode != 0:
                raise RuntimeError(f"config {config_name} failed (exit {result.returncode}) — aborting sweep.")
            print(f"[run] {config_name}: done in {(time.time() - t0) / 60:.1f} min", flush=True)
    finally:
        restore()
        print("[config] pristine CSVs restored and verified.", flush=True)


# ── Comparison / report ──────────────────────────────────────────────────────

def build_report() -> None:
    import numpy as np
    import pandas as pd

    from analysis.sensitivity_analysis import electre_assign, CAT_MIDPOINTS, LAMBDA_BASELINE
    from utils.capabilities import CAPABILITY_SERVICES, _CATEGORIES, _ELECTRE_PARAMS
    from core.config import ELECTRE_Q, ELECTRE_P

    base_csv = _work_root() / BASELINE_NAME / "service_scores.csv"
    if not base_csv.exists():
        print(f"ERROR: baseline results missing ({base_csv}); run without --report-only first.", file=sys.stderr)
        sys.exit(1)
    base = pd.read_csv(base_csv).set_index("node_id").sort_index()

    def classify(df: pd.DataFrame) -> dict[str, np.ndarray]:
        out = {}
        for capability, services in CAPABILITY_SERVICES.items():
            X = df[[f"service_{s}" for s in services]].to_numpy(dtype=float)
            m = X.shape[1]
            out[capability] = electre_assign(
                X, np.full(m, 1.0 / m), ELECTRE_Q, ELECTRE_P,
                LAMBDA_BASELINE, float(_ELECTRE_PARAMS[capability]["v"]),
            )
        return out

    service_cols = [c for c in base.columns if c.startswith("service_")]

    def level_rows_for(config_name: str, classes: dict[str, np.ndarray]) -> list[dict]:
        """One row per (capability, level): % of nodes assigned that ELECTRE
        class under this config. Backs the min/max level-shift tornado plots in
        sensitivity_report.py, which need the actual class distribution, not
        just whether a node's class changed."""
        out = []
        for capability, idx in classes.items():
            n = len(idx)
            for level_i, level_name in enumerate(_CATEGORIES):
                pct = float((idx == level_i).mean() * 100) if n else 0.0
                out.append({"config": config_name, "capability": capability, "level": level_name, "pct_nodes": round(pct, 2)})
        return out

    rows = []
    svc_rows = []
    dropout_rows = []
    level_rows = level_rows_for(BASELINE_NAME, classify(base))
    for config_name in CONFIGS:
        if config_name == BASELINE_NAME:
            continue
        cfg_csv = _work_root() / config_name / "service_scores.csv"
        if not cfg_csv.exists():
            print(f"[report] {config_name}: missing, skipped", flush=True)
            continue
        df = pd.read_csv(cfg_csv).set_index("node_id").sort_index()

        # A parameter change can add or drop reachable nodes (e.g. a smaller decay
        # radius leaves some origins with no POIs). That is itself a sensitivity
        # signal, so record it and compare class/score changes on the shared nodes.
        shared = base.index.intersection(df.index)
        dropped = len(base.index.difference(df.index))
        added = len(df.index.difference(base.index))
        dropout_rows.append(
            {"config": config_name, "n_nodes": len(df),
             "dropped_vs_baseline": dropped, "added_vs_baseline": added}
        )
        if dropped or added:
            print(f"[report] {config_name}: {dropped} dropped, {added} added vs baseline "
                  f"(comparing {len(shared)} shared nodes)", flush=True)

        base_shared = base.loc[shared]
        df_shared = df.loc[shared]
        base_classes_shared = classify(base_shared)
        classes = classify(df_shared)
        # Level distribution over this config's own full (not shared-with-baseline)
        # node set -- it describes "what does the map look like under this config",
        # not a diff against baseline.
        level_rows.extend(level_rows_for(config_name, classify(df)))
        for capability in CAPABILITY_SERVICES:
            changed = float((classes[capability] != base_classes_shared[capability]).mean() * 100)
            delta = float(np.abs(CAT_MIDPOINTS[classes[capability]] - CAT_MIDPOINTS[base_classes_shared[capability]]).mean())
            rows.append(
                {"config": config_name, "capability": capability,
                 "pct_nodes_changed": round(changed, 2), "mean_abs_score_delta": round(delta, 4)}
            )
        for col in service_cols:
            svc_rows.append(
                {"config": config_name, "service": col.removeprefix("service_"),
                 "mean_abs_delta": round(float((df_shared[col] - base_shared[col]).abs().mean()), 4)}
            )

    summary = pd.DataFrame(rows)
    svc_summary = pd.DataFrame(svc_rows)
    dropout = pd.DataFrame(dropout_rows)
    level_dist = pd.DataFrame(level_rows)
    level_dist.to_csv(_work_root() / "upstream_level_distribution.csv", index=False)
    summary.to_csv(_work_root() / "upstream_summary.csv", index=False)
    svc_summary.to_csv(_work_root() / "upstream_service_deltas.csv", index=False)
    dropout.to_csv(_work_root() / "upstream_node_dropout.csv", index=False)

    print(summary.pivot_table(index="config", columns="capability", values="pct_nodes_changed").to_string())


def run_upstream(only=None, report_only=False) -> None:

    if not report_only:
        run_configs(only)
    build_report()


if __name__ == "__main__":
    worker = os.environ.get(WORKER_ENV_VAR)
    if worker:
        sys.exit(run_worker(worker))
    else:
        run_upstream(only=UPSTREAM_ONLY, report_only=UPSTREAM_REPORT_ONLY)
