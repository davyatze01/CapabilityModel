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

Usage
-----
  python robustness_analysis.py                    # newest experiments/Cagliari_*.csv
  python robustness_analysis.py --csv path.csv --n-mc 500 --seed 7
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from sensitivity_analysis import (
    ELECTRE_P_FACTOR,
    ELECTRE_Q_FACTOR,
    LAMBDA_BASELINE,
    default_experiment_csv,
    electre_assign,
    load_capability_matrices,
)
from utils.capabilities import _CATEGORIES


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
                    noisy, w, ELECTRE_Q_FACTOR, ELECTRE_P_FACTOR, LAMBDA_BASELINE, float("inf")
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


def write_report(out_dir: Path, csv_path: Path, n_nodes: int, mc_summary: pd.DataFrame, n_mc: int) -> Path:
    lines = [
        "# Capability model — robustness to input noise (Cagliari)",
        "",
        f"* Input: `{csv_path}` ({n_nodes} nodes)",
        f"* Baseline: q_factor={ELECTRE_Q_FACTOR}, p_factor={ELECTRE_P_FACTOR}, "
        f"lambda={LAMBDA_BASELINE}, veto=inf, uniform weights (all held fixed -- only the "
        "input service scores are perturbed)",
        "",
        f"## Robustness to input noise (Monte Carlo, {n_mc} reps per sigma)",
        "",
        "Stability = share of repetitions assigning the node's modal class.",
        "",
        mc_summary.round(3).to_string(index=False),
        "",
        "Per-node stability (for QGIS join on node_id): `robustness_node_stability.csv`",
        "",
    ]
    report = out_dir / "robustness_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, default=None, help="experiment CSV (default: newest experiments/Cagliari_*.csv)")
    ap.add_argument("--out-dir", type=Path, default=Path("outputs/sensitivity"))
    ap.add_argument("--n-mc", type=int, default=200, help="Monte Carlo repetitions per sigma")
    ap.add_argument("--sigmas", type=float, nargs="+", default=[0.02, 0.05, 0.10])
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    csv_path = args.csv or default_experiment_csv()
    if csv_path is None:
        print("ERROR: no experiments/Cagliari_capability_*.csv found; pass --csv.", file=sys.stderr)
        return 1
    print(f"[load] {csv_path}", flush=True)

    rng = np.random.default_rng(args.seed)
    try:
        df, caps_X, _baseline = load_capability_matrices(csv_path, rng)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    node_ids = df["node_id"].to_numpy()

    node_stab, mc_summary = run_input_noise(caps_X, node_ids, args.sigmas, args.n_mc, rng)
    node_stab.to_csv(args.out_dir / "robustness_node_stability.csv", index=False)

    report = write_report(args.out_dir, csv_path, len(df), mc_summary, args.n_mc)
    print(f"\n[done] Report: {report}")
    print(f"[done] Table: {args.out_dir}/robustness_node_stability.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
