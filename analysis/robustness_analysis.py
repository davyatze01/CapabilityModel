"""Robustness of the capability model (ELECTRE TRI) to input noise.

Split out from sensitivity_analysis.py: this is a different question from
parameter sensitivity ("do the parameters matter?") -- here every parameter is
held at baseline and the *input* service scores are jittered instead, to ask
"is the output trustworthy given noisy inputs?". Kept separate so a robustness
rerun (slow: N Monte Carlo repetitions per sigma per capability) doesn't have to
happen just to refresh a parameter sweep, and vice versa.

Works entirely from an exported experiment CSV (service_* columns per node) --
no routing is rerun. Reuses load_capability_matrices() from sensitivity_analysis.py
so both tools reason about exactly the same validated baseline (vectorized
ELECTRE TRI, checked against the reference implementation before anything runs).

Method
------
Monte Carlo: gaussian noise on the service scores (several sigmas, clipped to
[0, 1]), N repetitions per sigma; per-node class stability = share of
repetitions assigning the node's modal class. Written per node for mapping in
QGIS (join on node_id) via robustness_report.py.

Outputs (under --out-dir, default outputs/sensitivity/):
  * robustness_node_stability.csv  one row per (node, capability, sigma)
  * robustness_report.md           human-readable summary
"""

from __future__ import annotations

import time
from pathlib import Path
from analysis.robustness_report import generate_robustness_report
from analysis.robustness_report import SIGMA, COORDS_CSV, BUILD_QGIS

import numpy as np
import pandas as pd

from analysis.sensitivity_analysis import (
    ELECTRE_P,
    ELECTRE_Q,
    LAMBDA_BASELINE,
    default_experiment_csv,
    electre_assign,
    load_capability_matrices,
)
from utils.capabilities import _CATEGORIES

OUT_DIR : Path | None = None
N_MC : int = 200
SIGMAS: list[float] = [0.02,0.05,0.10]
SEED: int = 42


def run_input_noise(
    caps_X: dict[str, np.ndarray],
    node_ids: np.ndarray,
    sigmas: list[float],
    n_mc: int,
    rng: np.random.Generator,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Monte Carlo robustness to gaussian noise on the service scores.

    Returns (per-node stability table, summary table).
    """
    node_rows = []
    summary_rows = []
    for capability, X in caps_X.items():
        m = X.shape[1]
        w = np.full(m, 1.0 / m)
        for sigma in sigmas:
            counts = np.zeros((len(X), len(_CATEGORIES)), dtype=np.int32)
            t0 = time.time()
            for rep in range(n_mc):
                noisy = np.clip(X + rng.normal(0.0, sigma, size=X.shape), 0.0, 1.0)
                assigned = electre_assign(
                    noisy, w, ELECTRE_Q, ELECTRE_P, LAMBDA_BASELINE, float("inf")
                )
                counts[np.arange(len(X)), assigned] += 1
                if (rep + 1) % max(1, n_mc // 4) == 0:
                    print(
                        f"[mc] {capability} sigma={sigma}: {rep + 1}/{n_mc} reps "
                        f"({time.time() - t0:.1f}s)",
                        flush=True,
                    )
            modal = counts.argmax(axis=1)
            stability = counts.max(axis=1) / n_mc
            node_rows.append(
                pd.DataFrame(
                    {
                        "node_id": node_ids,
                        "capability": capability,
                        "sigma": sigma,
                        "modal_class": [_CATEGORIES[i] for i in modal],
                        "stability": stability,
                    }
                )
            )
            summary_rows.append(
                {
                    "capability": capability,
                    "sigma": sigma,
                    "mean_stability": float(stability.mean()),
                    "pct_nodes_stability_lt_0.8": float((stability < 0.8).mean() * 100.0),
                    "pct_nodes_stability_lt_0.5": float((stability < 0.5).mean() * 100.0),
                }
            )
    return pd.concat(node_rows, ignore_index=True), pd.DataFrame(summary_rows)


def run_robustness_pipeline(
        city_slug : str = "Cagliari", 
        csv_path : Path | None = None, 
        out_dir : Path | None = OUT_DIR, 
        n_mc : int = N_MC, 
        sigmas: list[float] = SIGMAS,
        seed: int = SEED,
        sigma : float = SIGMA,
        coords_csv : Path | None = COORDS_CSV,
        build_qgis : bool = BUILD_QGIS) -> None:

    csv_path = csv_path or default_experiment_csv(city_slug)
    if csv_path is None:
        raise RuntimeError(f"[Robustness] ERROR: no experiments /{city_slug}_capability_*.csv found")
    print(f"[load] {csv_path}", flush=True)

    if out_dir is None:
        out_dir = Path(f"outputs/debug/{city_slug}")

    rng = np.random.default_rng(seed)
    try:
        df, caps_X, _baseline = load_capability_matrices(csv_path, rng)
    except ValueError as exc:
        raise RuntimeError("[Robustness] ERROR in loading capability matrices") from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    node_ids = df["node_id"].to_numpy()

    node_stab, _mc_summary = run_input_noise(caps_X, node_ids, sigmas, n_mc, rng)
    node_stab.to_csv(out_dir / "robustness_node_stability.csv", index=False)

    print(f"\n[done] Table: {out_dir}/robustness_node_stability.csv")
    generate_robustness_report(sigma, coords_csv or csv_path, build_qgis, out_dir)


if __name__ == "__main__":
    run_robustness_pipeline()
