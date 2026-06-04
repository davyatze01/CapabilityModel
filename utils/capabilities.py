"""Capability aggregation utilities.

This module defines the mapping from services to capabilities and exposes two
aggregation strategies:

- a Choquet integral helper used by the service layer
- an ELECTRE III wrapper used by the capability layer

The current pipeline uses `electre_iii_integration()` inside
`capability_stage.py` to collapse a vector of service scores into a single
capability score per node.

Important implementation note:
- the ELECTRE III call is used here as a compact scoring mechanism, not as a
  full ranking workflow over a large set of alternatives
- the module also writes `config/capability.csv` at import time so the CSV stays
  aligned with the hardcoded capability-service mapping below
"""

import os
from collections import Counter, OrderedDict
from pathlib import Path

import geopandas as gpd
import pandas as pd
import numpy as np
from pyDecision.algorithm.e_iii import electre_iii


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

def electre_iii_integration(x, capability):
    """Aggregate service scores into one capability score via ELECTRE III.

    This is a compact scoring wrapper around `pyDecision.algorithm.e_iii.electre_iii`.
    It creates three reference alternatives:

    - all zeros: worst profile
    - the observed service-score vector: current node
    - all ones: ideal profile

    The returned value is the global concordance of the observed node against
    the ideal profile, clipped to [0, 1].
    """
    services = CAPABILITY_SERVICES[capability]
    if not services:
        return 0.0
    if len(x) != len(services):
        raise ValueError(
            f"Expected {len(services)} service scores for capability={capability!r}, got {len(x)}."
        )

    x_arr = np.asarray([max(0.0, min(1.0, float(v))) for v in x], dtype=float)
    n_criteria = len(services)
    weights = np.asarray([CAP_ELECTRE_W[capability][service] for service in services], dtype=float)
    # Threshold choice:
    # - q = 0: no indifference region
    # - p = 1: full preference only at the top end of the score range
    # - v = 1: veto only for a total failure on the criterion
    #
    # This makes the wrapper behave like a smooth normalized outranking score
    # for inputs already clamped to [0, 1].
    q = np.zeros(n_criteria, dtype=float)
    p = np.ones(n_criteria, dtype=float)
    v = np.ones(n_criteria, dtype=float)
    dataset = np.vstack(
        [np.zeros(n_criteria, dtype=float), x_arr, np.ones(n_criteria, dtype=float)]
    )

    global_concordance, _, *_ = electre_iii(dataset, p, q, v, weights, graph=False)
    score = float(global_concordance[1, 2])  # node outranking best-profile degree
    return max(0.0, min(1.0, score))


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


rows = []

# Regenerate the CSV view of the capability mapping so the config file mirrors
# the current hardcoded mapping and ELECTRE weights.
for capability, services in CAPABILITY_SERVICES.items():
    electre_values = [
        CAP_ELECTRE_W[capability][service]
        for service in services
    ]


    rows.append({
        "capability": capability,
        "services": services,
        "electre_weight": electre_values,
        "enabled": True
    })


df = pd.DataFrame(rows)

output_path = Path(__file__).resolve().parents[1] / "config" / "capability.csv"
df.to_csv(output_path, index=False)
