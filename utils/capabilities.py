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
import csv
import os
from collections import Counter, OrderedDict
from pathlib import Path
from statistics import pstdev

from core.config import ELECTRE_Q_FACTOR, ELECTRE_P_FACTOR, ELECTRE_LAMBDA_CUT


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


def electre_tri_details(x, capability):
    """Return a step-by-step ELECTRE TRI explanation for one capability.

    Inputs:
    - x: service score list aligned to the capability service order.
    - capability: target capability key.

    Outputs:
    - dict with thresholds, per-boundary concordance/credibility steps,
      assigned category, and returned midpoint score.
    """
    services = CAPABILITY_SERVICES[capability]
    if not services:
        return {
            "capability": capability,
            "services": [],
            "service_scores": {},
            "boundaries": [],
            "assigned_category": _CATEGORIES[0],
            "assigned_category_index": 0,
            "score": 0.0,
        }
    if len(x) != len(services):
        raise ValueError(
            f"Expected {len(services)} service scores for capability={capability!r}, got {len(x)}."
        )

    x_arr = [float(xi) for xi in x]
    service_scores = {service: x_arr[idx] for idx, service in enumerate(services)}
    std = float(pstdev(x_arr))
    v = _ELECTRE_PARAMS[capability]["v"]

    details = {
        "capability": capability,
        "services": list(services),
        "service_scores": service_scores,
        "std": std,
        "q_factor": float(ELECTRE_Q_FACTOR),
        "p_factor": float(ELECTRE_P_FACTOR),
        "veto_threshold": float(v),
        "lambda_cut": float(ELECTRE_LAMBDA_CUT),
        "boundaries": [],
    }

    if std < 1e-9:
        idx = bisect.bisect_right(_BOUNDARIES, x_arr[0])
        assigned_idx = min(idx, len(_CATEGORIES) - 1)
        details.update(
            {
                "mode": "constant_scores",
                "q": 0.0,
                "p": 0.0,
                "assigned_category": _CATEGORIES[assigned_idx],
                "assigned_category_index": assigned_idx,
                "score": _CAT_SCORE[_CATEGORIES[assigned_idx]],
                "rule": "All service scores are equal, so thresholds collapse to zero and the score is placed by boundary bisection.",
            }
        )
        return details

    q = std * ELECTRE_Q_FACTOR
    p = std * ELECTRE_P_FACTOR
    inf_veto = v == float("inf")
    pq_range = p - q
    # Per-service ELECTRE weights (from CAP_ELECTRE_W / the electre_weight column
    # in config/capability.csv). Default to uniform if a weight is missing or the
    # weights are degenerate, so the global concordance below stays well-defined.
    cap_weights = CAP_ELECTRE_W.get(capability, {})
    weights = [float(cap_weights.get(s, 1.0 / len(x_arr))) for s in services]
    weight_sum = sum(weights)
    if weight_sum <= 0:
        weights = [1.0 / len(x_arr)] * len(x_arr)
        weight_sum = 1.0
    details["weights"] = {service: weights[idx] for idx, service in enumerate(services)}

    assigned_idx = 0
    for k, b in enumerate(_BOUNDARIES):
        concordance_terms = []
        weighted_concordance_sum = 0.0
        for j, (service, xj) in enumerate(zip(services, x_arr)):
            d = xj - b
            if d >= -q:
                c_j = 1.0
                rule = "full"
            elif d > -p:
                c_j = (d + p) / pq_range
                rule = "partial"
            else:
                c_j = 0.0
                rule = "none"
            weighted_concordance_sum += weights[j] * c_j
            concordance_terms.append(
                {
                    "service": service,
                    "score": xj,
                    "difference_vs_boundary": d,
                    "partial_concordance": c_j,
                    "weight": weights[j],
                    "rule": rule,
                }
            )

        # Global concordance: sum(w_j * c_j) / sum(w_j).
        C = weighted_concordance_sum / weight_sum
        cred = C
        discordance_terms = []
        veto_triggered = False

        if not inf_veto and C < 1.0:
            for service, xj in zip(services, x_arr):
                gap = b - xj
                term = {
                    "service": service,
                    "gap": gap,
                    "discordance": 0.0,
                    "adjusted_credibility": cred,
                    "rule": "inactive",
                }
                if gap > v:
                    cred = 0.0
                    veto_triggered = True
                    term["rule"] = "full_veto"
                    term["adjusted_credibility"] = cred
                    discordance_terms.append(term)
                    break
                if gap > p:
                    dj = (gap - p) / (v - p)
                    term["discordance"] = dj
                    if dj > C:
                        cred *= (1.0 - dj) / (1.0 - C)
                        term["rule"] = "attenuates"
                    else:
                        term["rule"] = "below_concordance"
                    term["adjusted_credibility"] = cred
                discordance_terms.append(term)

        outranks = cred >= ELECTRE_LAMBDA_CUT
        if outranks:
            assigned_idx = k + 1

        details["boundaries"].append(
            {
                "boundary_index": k,
                "boundary_value": b,
                "concordance_terms": concordance_terms,
                "global_concordance": C,
                "discordance_terms": discordance_terms,
                "credibility": cred,
                "veto_triggered": veto_triggered,
                "outranks_boundary": outranks,
                "assigned_category_if_stopped_here": _CATEGORIES[min(k + 1, len(_CATEGORIES) - 1)] if outranks else _CATEGORIES[assigned_idx],
            }
        )

    details.update(
        {
            "mode": "electre_tri",
            "q": q,
            "p": p,
            "assigned_category": _CATEGORIES[assigned_idx],
            "assigned_category_index": assigned_idx,
            "score": _CAT_SCORE[_CATEGORIES[assigned_idx]],
        }
    )
    return details


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
    return float(electre_tri_details(x, capability)["score"])


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


# ── Shared capability-grid visualization constants ───────────────────────────
# Single source of truth for the map/legend styling, imported by
# pipeline_runner.py (QGIS project + gpkg styling) and
# generate_capability_legend.py (standalone legend PNGs). Kept here rather than
# duplicated with "must stay in sync" comments so the two rendering paths can
# never drift.

# Signature color for each capability. Every service under a capability, the
# capability grid itself, and its legend all use this one color.
CAPABILITY_COLORS = {
    "nutrition": "#FFA200",
    "care": "#EB4CCC",
    "restorativeness": "#006BFF",
}

# ELECTRE TRI class boundaries and human labels. Five classes over [0, 1].
ELECTRE_BOUNDS = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
ELECTRE_LABELS = [
    "Very Low (0.0–0.2)",
    "Low (0.2–0.4)",
    "Medium (0.4–0.6)",
    "High (0.6–0.8)",
    "Very High (0.8–1.0)",
]
# Short names (no range), for compact legend rows / band categories.
ELECTRE_SHORT_LABELS = ["Very Low", "Low", "Medium", "High", "Very High"]

# Iso-band outline stroke widths (mm, QGIS symbol units), thin at Very Low up to
# thick at Very High so the level reads from line weight alone. The capability
# legend reuses these exact per-level widths for each swatch's outline.
ISO_BAND_WIDTHS_MM = [0.3, 0.6, 0.9, 1.3, 1.8]

# Interpolation stops (white -> capability color) used to derive the 5 discrete
# per-level shades of a capability's color.
CAPABILITY_SHADE_FRACTIONS = [0.2, 0.4, 0.6, 0.8, 1.0]


def capability_shade_hexes(color_hex: str) -> list[str]:
    """Return the 5 discrete shade hexes for one capability's color.

    Each shade is a linear interpolation from white (t=0) to the capability's
    signature color (t=1) evaluated at CAPABILITY_SHADE_FRACTIONS -- four
    progressively deeper hues plus the full color at 1.0, one per ELECTRE class.
    """
    from matplotlib.colors import LinearSegmentedColormap

    cmap = LinearSegmentedColormap.from_list("shade", ["white", color_hex])
    hexes = []
    for frac in CAPABILITY_SHADE_FRACTIONS:
        r, g, b, _ = cmap(frac)
        hexes.append("#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255)))
    return hexes


output_path = Path(__file__).resolve().parents[1] / "config" / "capability.csv"

# Read existing veto values so user edits in the CSV are preserved.
_saved_v: dict[str, float] = {}
if output_path.exists():
    try:
        with open(output_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                capability = str(row.get("capability", "")).strip()
                if not capability:
                    continue
                raw_veto = str(row.get("veto_threshold", "")).strip()
                if raw_veto:
                    _saved_v[capability] = float(raw_veto)
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

with open(output_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(
        f,
        fieldnames=["capability", "services", "electre_weight", "veto_threshold", "enabled"],
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)

# Runtime lookup consumed by electre_tri_integration().
_ELECTRE_PARAMS = {
    str(row["capability"]): {"v": float(row["veto_threshold"])}
    for row in rows
}
