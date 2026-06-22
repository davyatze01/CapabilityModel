"""Capability aggregation utilities.

This module defines the mapping from services to capabilities and exposes two
aggregation strategies:

- a Choquet integral helper used by the service layer
- an ELECTRE TRI wrapper used by the capability layer

The current pipeline uses `electre_tri_integration()` inside
`capability_stage.py` to collapse a vector of service scores into a single
capability score per node.

Important implementation note:
- ELECTRE TRI compares each alternative against a boundary profile (the ideal
  all-ones vector) and returns the credibility σ(x, b_ideal) ∈ [0, 1]
- the module also writes `config/capability.csv` at import time so the CSV stays
  aligned with the hardcoded capability-service mapping below
"""

import bisect
import os
from collections import Counter, OrderedDict
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd


from config import ELECTRE_Q_FACTOR, ELECTRE_P_FACTOR


# Ordered service lists for each capability.
# The dictionary values are only used to preserve a stable ordering.
CAP_RESTORATIVENESS_IDX = {
    "sport_and_movement": 0,
    "scenic_views": 1,
    "quietness": 2,
    "cultural_activities": 3,
    "nature_contact": 4,
}

CAP_NUTRITION_IDX = {
    "eating_out": 0,
    "food_access": 1,
}

CAP_CARE_IDX = {
    "medicines_and_supplies": 0,
    "diagnosis_and_prevention": 1,
    "emergency_services": 2,
    "care_services": 3,
}

# Per-capability singleton measures used by the Choquet aggregation.
# These are the base importance values for individual services.
CAP_SINGLETON_M = {
    "restorativeness": {
        "sport_and_movement": 1.0,     # direct restorative mechanism (activity, stress relief)
        "scenic_views": 0.8,           # strong contributor
        "quietness": 1.0,              # core restorative condition
        "cultural_activities": 0.6,    # restorative but more context dependent
        "nature_contact": 1.0,         # core restorative mechanism
    },

    "nutrition": {
        "eating_out": 0.6,             # provides nutrition but quality/control varies
        "food_access": 1.0,            # unified access to fresh and ready food retail
    },

    "care": {
        "medicines_and_supplies": 0.8,       # strong contributor
        "diagnosis_and_prevention": 1.0,     # core diagnostic/preventive access
        "emergency_services": 1.0,           # core
        "care_services": 1.0,                # core (ongoing care)
    },
}

def cap(S, capability):
    """Return the capability measure for a subset of services.

    Inputs:
    - S: ordered/list-like subset of services for one capability.
    - capability: target capability key, one of `restorativeness`, `nutrition`,
      or `care`.

    Outputs:
    - float: fuzzy measure value used by Choquet aggregation.

    Behavior:
    - empty subset -> 0
    - singleton subset -> the corresponding value from `CAP_SINGLETON_M`
    - larger subsets -> a simple saturation rule based on the strongest
      singleton in the subset
    """
    if len(S) == 0:
        return 0
    elif len(S) == 1:
        return CAP_SINGLETON_M[capability][S[0]]
    else:
        singletons =  [CAP_SINGLETON_M[capability][k] for k in S]
        m = max(singletons)
        return min(1, m + 0.2 * (1 - m))
    

def choquet_integral(x, capability):
    """Aggregate service scores into one capability score via Choquet integral.

    Inputs:
    - x: service score list aligned to the capability service order.
    - capability: target capability key.

    Outputs:
    - float: aggregated capability score.

    Notes:
    - The scores are sorted from low to high before accumulation.
    - Each increment is weighted by the fuzzy measure of the remaining tail.
    """
    n = len(x)
    order = sorted(range(n), key=lambda i: x[i])
    x_sorted = [x[i] for i in order]
    services = CAPABILITY_SERVICES[capability]

    total = 0.0
    prev = 0.0
    for j in range(n):
        tail = [services[i] for i in order[j:]]
        total += (x_sorted[j] - prev) * cap(tail, capability)
        prev = x_sorted[j]
    return total

_BOUNDARIES  = [0.2, 0.4, 0.6, 0.8]
_CATEGORIES  = ["Very Low", "Low", "Medium", "High", "Very High"]
_CAT_SCORE   = {cat: (lo + hi) / 2
                for cat, lo, hi in zip(
                    _CATEGORIES,
                    [0.0] + _BOUNDARIES,
                    _BOUNDARIES + [1.0],
                )}

_DEFAULT_V = float("inf")


def electre_tri_integration(x, capability):
    """Aggregate service scores into one capability score via ELECTRE TRI.

    Assigns the alternative to one of five ordered categories defined by the
    boundary profiles [0.2, 0.4, 0.6, 0.8] and returns the midpoint of the
    assigned category as a continuous score in (0, 1).

    Thresholds (uniform across all services):
    - q = std(x) * ELECTRE_Q_FACTOR  (indifference; factors from config.py)
    - p = std(x) * ELECTRE_P_FACTOR  (preference)
    - v = read from config/capability.csv column veto_threshold
    """
    services = CAPABILITY_SERVICES[capability]
    if not services:
        return 0.0
    if len(x) != len(services):
        raise ValueError(
            f"Expected {len(services)} service scores for capability={capability!r}, got {len(x)}."
        )

    x_arr = [float(xi) for xi in x]
    std = float(np.std(x_arr))

    # When all scores are equal the thresholds collapse to zero → bisect directly.
    if std < 1e-9:
        idx = bisect.bisect_right(_BOUNDARIES, x_arr[0])
        return _CAT_SCORE[_CATEGORIES[min(idx, len(_CATEGORIES) - 1)]]

    q = std * ELECTRE_Q_FACTOR
    p = std * ELECTRE_P_FACTOR
    v = _ELECTRE_PARAMS[capability]["v"]
    w = 1.0 / len(x_arr)          # uniform weights (equal for all criteria)
    inf_veto = v == float("inf")
    pq_range = p - q               # always > 0 since p > q

    # ELECTRE TRI-B pessimistic rule: find the highest boundary that the
    # alternative outranks (credibility ≥ λ=0.65) and assign to the next category.
    assigned_idx = 0               # default: Very Low
    for k, b in enumerate(_BOUNDARIES):
        # --- concordance (per-criterion partial agreement) ---
        C = 0.0
        for xj in x_arr:
            d = xj - b
            if d >= -q:
                C += 1.0
            elif d > -p:
                C += (d + p) / pq_range
        C *= w

        # --- credibility (concordance attenuated by discordance) ---
        cred = C
        if not inf_veto and C < 1.0:
            for xj in x_arr:
                gap = b - xj       # how much the boundary dominates this criterion
                if gap > v:        # full veto
                    cred = 0.0
                    break
                if gap > p:
                    dj = (gap - p) / (v - p)
                    if dj > C:
                        cred *= (1.0 - dj) / (1.0 - C)

        if cred >= 0.65:
            assigned_idx = k + 1

    return _CAT_SCORE[_CATEGORIES[assigned_idx]]


# Stable service order for each capability.
CAPABILITY_SERVICES = {
    "restorativeness": list(CAP_RESTORATIVENESS_IDX.keys()),
    "nutrition": list(CAP_NUTRITION_IDX.keys()),
    "care": list(CAP_CARE_IDX.keys()),
}

# Equal default ELECTRE weights for all services inside each capability.
CAP_ELECTRE_W = {
    capability: {service: 1.0 / len(services) for service in services}
    for capability, services in CAPABILITY_SERVICES.items()
}


output_path = Path(__file__).resolve().parents[1] / "config" / "capability.csv"

# Read existing veto values so user edits in the CSV are preserved.
_saved_v: dict[str, float] = {}
if output_path.exists():
    try:
        _existing = pd.read_csv(output_path)
        for _, row in _existing.iterrows():
            _saved_v[row["capability"]] = float(row.get("veto_threshold", _DEFAULT_V))
    except Exception:
        pass

rows = []
for capability, services in CAPABILITY_SERVICES.items():
    rows.append({
        "capability":    capability,
        "services":      services,
        "electre_weight": [CAP_ELECTRE_W[capability][s] for s in services],
        "veto_threshold": _saved_v.get(capability, _DEFAULT_V),
        "enabled":        True,
    })

df = pd.DataFrame(rows)
df.to_csv(output_path, index=False)

# Runtime lookup consumed by electre_tri_integration().
_ELECTRE_PARAMS = {
    row["capability"]: {
        "v": float(row["veto_threshold"]),
    }
    for _, row in df.iterrows()
}
