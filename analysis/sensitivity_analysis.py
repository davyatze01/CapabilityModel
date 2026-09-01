"""Sensitivity analysis of the capability model (ELECTRE TRI) parameters.

Works entirely from an exported experiment CSV (service_* columns per node) --
no routing is rerun. The ELECTRE TRI aggregation from utils/capabilities.py is
re-implemented vectorized (numpy) and validated against the reference
implementation before any analysis runs, so the results are guaranteed to be
about the real model, not a drifted copy.

This covers sensitivity only (how much do the *parameters* matter?):
  * OAT sweeps of q (indifference), p (preference), lambda cutting level, and veto threshold:
    for each value, % of nodes whose assigned category changes vs baseline and
    the mean |score delta|, per capability.
  * Weight perturbation: Dirichlet draws around the uniform weights; distribution
    of the % of nodes that change class.

Robustness (how stable are the *results* under input noise?) lives in
robustness_analysis.py / robustness_report.py -- a separate concern with its own
deliverables (a QGIS stability layer), split out so it doesn't have to be rerun
just to refresh a parameter sweep. Both scripts import load_capability_matrices()
from here so they always reason about the exact same validated baseline.

Outputs (under --out-dir, default outputs/sensitivity/):
  * sensitivity_oat.csv          one row per (parameter, value, capability)
  * sensitivity_weights.csv      one row per (draw, capability)
  * report.md                    human-readable summary of everything above

Usage
-----
  python sensitivity_analysis.py                    # newest experiments/Cagliari_*.csv
  python sensitivity_analysis.py --csv path.csv --seed 7
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

import numpy as np
import pandas as pd

from utils.capabilities import (
    CAPABILITY_SERVICES,
    _BOUNDARIES,
    _CATEGORIES,
    _ELECTRE_PARAMS,
    electre_tri_integration,
)
from core.config import ELECTRE_Q, ELECTRE_P, ELECTRE_LAMBDA_CUT

# The lambda actually used by the assignments. Historically the code cut at 0.65
# while the debug details reported 0.70; both now read config.ELECTRE_LAMBDA_CUT,
# and this baseline follows it automatically.
LAMBDA_BASELINE = float(ELECTRE_LAMBDA_CUT)

CAT_MIDPOINTS = np.array(
    [(lo + hi) / 2 for lo, hi in zip([0.0] + _BOUNDARIES, _BOUNDARIES + [1.0])]
)
BOUNDS = np.asarray(_BOUNDARIES, dtype=float)

CSV : Path | None = None
OUT_DIR : Path | None = None
N_WEIGHT_DRAWS = 200
WEIGHT_CONCENTRATION = 20.0
SEED = 42


# ── Vectorized ELECTRE TRI (mirrors utils/capabilities.electre_tri_details) ──

def electre_assign(
    X: np.ndarray,
    weights: np.ndarray,
    q: float,
    p: float,
    lam: float,
    veto: float,
) -> np.ndarray:
    """Assign an ELECTRE TRI category index (0..4) to every row of X.

    X: (n_nodes, n_services) service scores in [0, 1].
    weights: (n_services,) positive, will be normalized.
    q, p: absolute indifference/preference thresholds (ELECTRE_Q/ELECTRE_P), fixed
    across all rows -- mirrors electre_tri_details, not std-scaled per node.
    Returns int array (n_nodes,) of category indices.
    """
    X = np.asarray(X, dtype=float)
    n, m = X.shape
    w = np.asarray(weights, dtype=float)
    w = w / w.sum()

    std = X.std(axis=1)  # population std, same as statistics.pstdev -- only used
    const = std < 1e-9   # to detect the "all scores equal" collapse case below
    pq_range = p - q

    assigned = np.zeros(n, dtype=int)
    inf_veto = np.isinf(veto)
    for k, b in enumerate(BOUNDS):
        d = X - b
        # partial concordance c_j: 1 if d >= -q, 0 if d <= -p, linear between.
        cj = np.clip((d + p) / pq_range, 0.0, 1.0)
        C = cj @ w
        cred = C
        if not inf_veto:
            gap = b - X  # (n, m)
            # full veto: any service more than v below the boundary
            full = (gap > veto).any(axis=1)
            # partial discordance for p < gap <= v, attenuates when dj > C
            dj = np.clip((gap - p) / max(veto - p, 1e-12), 0.0, 1.0)
            with np.errstate(divide="ignore", invalid="ignore"):
                factor = np.where(
                    dj > C[:, None],
                    (1.0 - dj) / np.maximum(1.0 - C[:, None], 1e-12),
                    1.0,
                )
            adjust = np.prod(factor, axis=1)
            # reference only applies discordance when C < 1
            cred = np.where(C < 1.0, C * adjust, C)
            cred = np.where(full & (C < 1.0), 0.0, cred)
        outranks = cred >= lam
        assigned = np.where(outranks, k + 1, assigned)

    # Constant rows: thresholds collapse; placement by boundary bisection.
    if const.any():
        assigned[const] = np.searchsorted(BOUNDS, X[const, 0], side="right")
    return np.minimum(assigned, len(_CATEGORIES) - 1)


def validate_against_reference(X: np.ndarray, capability: str, rng: np.random.Generator) -> None:
    """Cross-check the vectorized ELECTRE against electre_tri_integration on a sample."""
    m = X.shape[1]
    idx = rng.choice(len(X), size=min(250, len(X)), replace=False)
    veto = float(_ELECTRE_PARAMS[capability]["v"])
    ours = electre_assign(
        X[idx], np.full(m, 1.0 / m), ELECTRE_Q, ELECTRE_P, LAMBDA_BASELINE, veto
    )
    ours_scores = CAT_MIDPOINTS[ours]
    ref_scores = np.array([electre_tri_integration(list(X[i]), capability) for i in idx])
    max_diff = np.max(np.abs(ours_scores - ref_scores))
    if max_diff > 1e-12:
        bad = int(np.argmax(np.abs(ours_scores - ref_scores)))
        raise AssertionError(
            f"Vectorized ELECTRE disagrees with reference for {capability}: "
            f"max |diff|={max_diff} (e.g. node sample #{bad}: ours={ours_scores[bad]}, ref={ref_scores[bad]}). "
            "Refusing to run the analysis on a wrong model."
        )
    print(f"[validate] {capability}: vectorized == reference on {len(idx)} sampled nodes ✓", flush=True)


# ── Analyses ─────────────────────────────────────────────────────────────────

def pct_changed(a: np.ndarray, b: np.ndarray) -> float:
    return float((a != b).mean() * 100.0)


def mean_abs_delta(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(CAT_MIDPOINTS[a] - CAT_MIDPOINTS[b]).mean())


def run_oat_sweeps(caps_X: dict[str, np.ndarray], baseline: dict[str, np.ndarray]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One-at-a-time parameter sweeps; all other parameters at baseline."""
    # Each parameter tests exactly one value below and one above its baseline
    # (plus the baseline itself, for the tornado plots' centre row).
    sweeps = {
        "q": [ELECTRE_Q - 0.01, ELECTRE_Q, ELECTRE_Q + 0.01],
        "p": [ELECTRE_P - 0.02, ELECTRE_P, ELECTRE_P + 0.02],
        "lambda": [LAMBDA_BASELINE - 0.05, LAMBDA_BASELINE, LAMBDA_BASELINE + 0.05],
    }
    base = {
        "q": ELECTRE_Q,
        "p": ELECTRE_P,
        "lambda": LAMBDA_BASELINE,
        "veto": float("inf"),
    }
    rows = []
    level_rows = []
    for param, values in sweeps.items():
        for value in values:
            kw = dict(base)
            kw[param] = value
            if kw["q"] >= kw["p"]:
                continue  # q < p is a structural requirement of the model
            for capability, X in caps_X.items():
                m = X.shape[1]
                assigned = electre_assign(
                    X, np.full(m, 1.0 / m), kw["q"], kw["p"], kw["lambda"], kw["veto"]
                )
                rows.append(
                    {
                        "parameter": param,
                        "value": value,
                        "capability": capability,
                        "pct_nodes_changed": pct_changed(assigned, baseline[capability]),
                        "mean_abs_score_delta": mean_abs_delta(assigned, baseline[capability]),
                        "is_baseline": np.isclose(value, base[param]) if np.isfinite(value) else np.isinf(base[param]),
                    }
                )
                # Level distribution (% of nodes at each of the 5 ELECTRE classes)
                # under this value -- backs the min/max level-shift tornado plots
                # in sensitivity_report.py, which need the class distribution
                # itself, not just whether a node's class changed vs baseline.
                n = len(assigned)
                for level_i, level_name in enumerate(_CATEGORIES):
                    pct = float((assigned == level_i).mean() * 100) if n else 0.0
                    level_rows.append(
                        {"parameter": param, "value": value, "capability": capability,
                         "level": level_name, "pct_nodes": round(pct, 2)}
                    )
        print(f"[oat] swept {param} ({len(values)} values)", flush=True)
    return pd.DataFrame(rows), pd.DataFrame(level_rows)


def run_weight_perturbation(
    caps_X: dict[str, np.ndarray],
    baseline: dict[str, np.ndarray],
    n_draws: int,
    concentration: float,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Dirichlet perturbation of the (uniform) service weights."""
    rows = []
    for capability, X in caps_X.items():
        m = X.shape[1]
        alphas = np.full(m, concentration)  # centered on uniform
        for draw in range(n_draws):
            w = rng.dirichlet(alphas)
            assigned = electre_assign(
                X, w, ELECTRE_Q, ELECTRE_P, LAMBDA_BASELINE, float("inf")
            )
            rows.append(
                {
                    "draw": draw,
                    "capability": capability,
                    "pct_nodes_changed": pct_changed(assigned, baseline[capability]),
                    "mean_abs_score_delta": mean_abs_delta(assigned, baseline[capability]),
                    "max_weight": float(w.max()),
                }
            )
        print(f"[weights] {capability}: {n_draws} Dirichlet draws done", flush=True)
    return pd.DataFrame(rows)


# ── Shared loading (used by both sensitivity_analysis.py and robustness_analysis.py) ──

def default_experiment_csv(city_slug : str = "Cagliari") -> Path | None:
    # Pick the most recently written CSV (the current run). A lexical sort here
    # is wrong: "Cagliari_capability_9.csv" sorts after "..._80.csv", so string
    # ordering silently grabs a stale file.
    candidates = glob.glob(f"experiments/{city_slug}_capability*.csv")
    if not candidates:
        return None
    return Path(max(candidates, key=lambda p: os.path.getmtime(p)))


def load_capability_matrices(
    csv_path: Path, rng: np.random.Generator
) -> tuple[pd.DataFrame, dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Load an experiment CSV and build the validated per-capability service-score
    matrices plus their baseline ELECTRE TRI classification. Shared by
    sensitivity_analysis.py and robustness_analysis.py so both tools reason about
    exactly the same baseline (and both get the same reference-vs-vectorized
    validation before anything downstream trusts the numbers).
    """
    df = pd.read_csv(csv_path)
    caps_X: dict[str, np.ndarray] = {}
    for capability, services in CAPABILITY_SERVICES.items():
        cols = [f"service_{s}" for s in services]
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise ValueError(f"{csv_path} lacks columns {missing} for {capability}.")
        caps_X[capability] = df[cols].to_numpy(dtype=float)
        validate_against_reference(caps_X[capability], capability, rng)

    baseline = {
        capability: electre_assign(
            X,
            np.full(X.shape[1], 1.0 / X.shape[1]),
            ELECTRE_Q,
            ELECTRE_P,
            LAMBDA_BASELINE,
            float(_ELECTRE_PARAMS[capability]["v"]),
        )
        for capability, X in caps_X.items()
    }

    # Cross-check the baseline against the capability_* columns already in the CSV.
    for capability in caps_X:
        col = f"capability_{capability}"
        if col in df.columns:
            match = float((CAT_MIDPOINTS[baseline[capability]] == df[col].to_numpy()).mean() * 100)
            print(f"[baseline] {capability}: {match:.1f}% of nodes match the CSV's {col}", flush=True)

    return df, caps_X, baseline


# ── Report ───────────────────────────────────────────────────────────────────


def run_sensitivity_pipeline(
        city_slug : str = "Cagliari",
        csv : Path | None = CSV,
        out_dir : Path | None = OUT_DIR,
        n_weight_draws : int = N_WEIGHT_DRAWS,
        weight_concentration : float = WEIGHT_CONCENTRATION,
        seed : int = SEED
) -> None:

    csv_path = csv or default_experiment_csv(city_slug)
    if csv_path is None:
        raise RuntimeError(f"[Sensitivity] ERROR: no experiments/{city_slug}_*.csv found")
    print(f"[load] {csv_path}", flush=True)

    rng = np.random.default_rng(seed)
    try:
        df, caps_X, baseline = load_capability_matrices(csv_path, rng)
    except ValueError as exc:
        raise RuntimeError("[Sensitivity] ERROR: unable to load capability matrices") from exc

    if out_dir is None:
        out_dir = Path(f"outputs/debug/{city_slug}")

    out_dir.mkdir(parents=True, exist_ok=True)

    oat, oat_levels = run_oat_sweeps(caps_X, baseline)
    oat.to_csv(out_dir / "sensitivity_oat.csv", index=False)
    oat_levels.to_csv(out_dir / "sensitivity_oat_levels.csv", index=False)

    wdf = run_weight_perturbation(caps_X, baseline, n_weight_draws, weight_concentration, rng)
    wdf.to_csv(out_dir / "sensitivity_weights.csv", index=False)

    print(f"\n[done] Tables: {out_dir}/sensitivity_oat.csv, sensitivity_oat_levels.csv, sensitivity_weights.csv")


if __name__ == "__main__":
    run_sensitivity_pipeline()
