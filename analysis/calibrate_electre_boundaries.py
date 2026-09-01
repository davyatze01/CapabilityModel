"""One-off calibration: derive ELECTRE TRI's 5-class boundaries via natural breaks
(Jenks) instead of hand-picking them.

Loads the Paris (mgp_boundary) and Cagliari experiment CSVs (the same reference
population used to freeze ELECTRE_Q/ELECTRE_P), computes each node's ELECTRE-weighted
mean of its raw service scores per capability -- a boundary-free scalar, since ELECTRE's
own score is defined relative to the boundaries we're trying to find and so can't be
used here. Pools those values across both cities and every capability (one shared set
of boundaries, not per-city or per-capability), and runs Jenks natural breaks to find
the 4 cut points that best separate the pooled distribution into 5 classes.

This does not change any production behavior by itself -- it only prints a candidate
list. Pasting it into ELECTRE_BOUNDARIES in core/config.py is a separate, deliberate
step.

Usage: python analysis/calibrate_electre_boundaries.py
"""

from __future__ import annotations

import numpy as np

from analysis.sensitivity_analysis import default_experiment_csv, load_capability_matrices
from utils.capabilities import CAPABILITY_SERVICES, CAP_ELECTRE_W

CITY_SLUGS = ["mgp_boundary", "Cagliari"]  # Paris (mgp_boundary) + Cagliari, pooled equally
N_CLASSES = 5
# jenks_natural_breaks is an exact O(n^2) DP -- fine at Cagliari's scale (1563 nodes x 3
# capabilities pooled = 4689 values, what ran successfully before) but Paris's ~23k nodes
# pool to ~69k values, which would take forever. Cap the pool via random subsample, split
# evenly across cities -- Jenks breaks describe distribution shape, so a few thousand
# representative samples suffice; the DP just doesn't need to be exact on all of them.
JENKS_SAMPLE_CAP = 9000


def jenks_natural_breaks(values: list[float], n_classes: int) -> list[float]:
    """Fisher's exact algorithm for 1-D natural-breaks classification (equivalent to
    Jenks natural breaks): partitions `values` into `n_classes` contiguous groups that
    minimize the sum of within-group variance, and returns the `n_classes - 1` cut
    points between them (each cut point is the midpoint between the last value of one
    group and the first value of the next).
    """
    data = sorted(values)
    n = len(data)
    if n_classes < 1:
        raise ValueError("n_classes must be >= 1")
    if n_classes >= n:
        raise ValueError(f"need more than {n_classes} distinct values, got {n}")

    INF = float("inf")
    # variance[i][j]: minimal total within-group variance of data[0:j+1] split into i
    # groups. class_break[i][j]: start index of the i-th (last) group in that optimum.
    variance = [[INF] * n for _ in range(n_classes + 1)]
    class_break = [[0] * n for _ in range(n_classes + 1)]

    running_sum = 0.0
    running_sq_sum = 0.0
    for j in range(n):
        x = data[j]
        running_sum += x
        running_sq_sum += x * x
        count = j + 1
        mean = running_sum / count
        variance[1][j] = running_sq_sum - count * mean * mean
        class_break[1][j] = 0

    for i in range(2, n_classes + 1):
        for j in range(i - 1, n):
            best_cost = INF
            best_start = 0
            running_sum = 0.0
            running_sq_sum = 0.0
            for start in range(j, i - 2, -1):
                x = data[start]
                running_sum += x
                running_sq_sum += x * x
                count = j - start + 1
                mean = running_sum / count
                cost_this_group = running_sq_sum - count * mean * mean
                prev_cost = 0.0 if start == 0 else variance[i - 1][start - 1]
                total_cost = prev_cost + cost_this_group
                if total_cost < best_cost:
                    best_cost = total_cost
                    best_start = start
            variance[i][j] = best_cost
            class_break[i][j] = best_start

    breaks_idx = []
    j = n - 1
    for i in range(n_classes, 1, -1):
        start = class_break[i][j]
        breaks_idx.append(start)
        j = start - 1
    breaks_idx.reverse()

    return [(data[idx - 1] + data[idx]) / 2 for idx in breaks_idx]


def _capability_weights(capability: str, services: list[str]) -> list[float]:
    """Mirrors the weight-resolution logic in electre_tri_details: per-service ELECTRE
    weights, falling back to uniform if missing or degenerate."""
    cap_weights = CAP_ELECTRE_W.get(capability, {})
    weights = [float(cap_weights.get(s, 1.0 / len(services))) for s in services]
    weight_sum = sum(weights)
    if weight_sum <= 0:
        return [1.0 / len(services)] * len(services)
    return [w / weight_sum for w in weights]


def main() -> None:
    rng = np.random.default_rng(42)
    pooled_by_city: dict[str, list[float]] = {}
    for city_slug in CITY_SLUGS:
        csv_path = default_experiment_csv(city_slug)
        if csv_path is None:
            raise RuntimeError(f"no experiments/{city_slug}_capability_*.csv found")
        print(f"[load] {city_slug}: {csv_path}", flush=True)
        _df, caps_X, _baseline = load_capability_matrices(csv_path, rng)

        city_values: list[float] = []
        for capability, services in CAPABILITY_SERVICES.items():
            X = caps_X[capability]
            w = np.array(_capability_weights(capability, services))
            node_means = X @ w
            city_values.extend(node_means.tolist())
            print(
                f"[calibrate] {city_slug}/{capability}: {len(node_means)} nodes, "
                f"mean={node_means.mean():.4f}, std={node_means.std():.4f}",
                flush=True,
            )
        pooled_by_city[city_slug] = city_values

    total_pooled = sum(len(v) for v in pooled_by_city.values())
    print(f"[calibrate] pooled {total_pooled} values across {len(CITY_SLUGS)} cities x {len(CAPABILITY_SERVICES)} capabilities", flush=True)

    # Split the sample cap evenly across cities -- a single pool-then-uniform-draw
    # would let Paris's larger node count (~69k vs Cagliari's ~4.7k) dominate the subsample.
    per_city_cap = [JENKS_SAMPLE_CAP // len(CITY_SLUGS)] * len(CITY_SLUGS)
    per_city_cap[0] += JENKS_SAMPLE_CAP - sum(per_city_cap)  # remainder to the first city
    pooled: list[float] = []
    for city_slug, cap in zip(CITY_SLUGS, per_city_cap):
        values = pooled_by_city[city_slug]
        if len(values) > cap:
            values = rng.choice(values, size=cap, replace=False).tolist()
            print(f"[calibrate] {city_slug}: subsampled to {cap}", flush=True)
        else:
            print(f"[calibrate] {city_slug}: kept all {len(values)} (below {cap} cap)", flush=True)
        pooled.extend(values)

    breaks = jenks_natural_breaks(pooled, N_CLASSES)
    print(f"[calibrate] Jenks natural breaks (k={N_CLASSES}): {[round(b, 4) for b in breaks]}")
    print("[calibrate] Paste into core/config.py as ELECTRE_BOUNDARIES.")


if __name__ == "__main__":
    main()
