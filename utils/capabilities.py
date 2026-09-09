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
- `config/capability.csv` is the source of truth for which capabilities exist,
  their services, and their ELECTRE TRI weight/veto/enabled settings -- see
  `_load_capability_config()`
"""

import ast
import bisect
import csv
import math
import os
from collections import Counter, OrderedDict
from pathlib import Path
from statistics import pstdev

from core.config import ELECTRE_Q, ELECTRE_P, ELECTRE_BOUNDARIES, ELECTRE_LAMBDA_CUT


# Per-capability singleton measures used by the legacy Choquet-based capability
# aggregation (cap() / choquet_integral() below). The current pipeline does not
# call these -- capability_stage.py uses electre_tri_integration() instead --
# and this dict is not read from config/capability.csv, so it only has entries
# for the case study's original three capabilities. Kept for reference/manual
# use; will KeyError if called for a capability outside that original set.
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
        "impatient_and_rehabilitation": 1.0, # core (inpatient/rehab access)
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

_BOUNDARIES  = ELECTRE_BOUNDARIES
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

    q = ELECTRE_Q
    p = ELECTRE_P
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
    - q = ELECTRE_Q  (indifference; absolute, from config.py)
    - p = ELECTRE_P  (preference; absolute, from config.py)
    - v = read from config/capability.csv column veto_threshold
    """
    return float(electre_tri_details(x, capability)["score"])


def electre_tri_continuous_score(x, capability):
    """Aggregate service scores into one capability score, keeping ELECTRE TRI's
    continuous credibility instead of collapsing it to a 5-band midpoint.

    `electre_tri_integration()` assigns a node to one of 5 categories and returns
    that category's midpoint (0.1/0.3/0.5/0.7/0.9) -- every node in the same band
    gets an identical score, which is exactly what makes level-based comparisons
    lose resolution. This instead averages the raw outranking credibility
    `sigma(x, b_k)` computed against each of the 4 interior boundaries (same
    concordance/discordance/veto machinery, before the lambda-cut classification
    step), giving a smooth, monotonically non-decreasing score in [0, 1] with the
    same qualitative behavior. Statistics-only: not used by the production
    capability_<name> map/level output unless PipelineConfig.capability_score_mode
    is explicitly set to "continuous" (see stages/capability_stage.py).
    """
    details = electre_tri_details(x, capability)
    boundaries = details["boundaries"]
    if not boundaries:
        # Degenerate case (all service scores equal): no boundary credibilities were
        # computed, so fall back to the discrete band score already picked in details.
        return float(details["score"])
    return float(sum(b["credibility"] for b in boundaries) / len(boundaries))


CAPABILITY_CSV_PATH = Path(__file__).resolve().parents[1] / "config" / "capability.csv"


def _load_capability_config(
    path: Path, valid_services: "set[str] | None" = None
) -> tuple["OrderedDict[str, list[str]]", dict[str, dict[str, float]], dict[str, float], dict[str, bool]]:
    """Load and validate capability configuration rows from config/capability.csv.

    This is the single source of truth for which capabilities exist, which
    services belong to each, and the ELECTRE TRI weight/veto/enabled settings
    for each capability -- an analyst can add, remove, rename, or reweight
    capabilities entirely through this file.

    When valid_services is given, every service in `services` must exist in
    it; passing None skips that check here (the reverse-direction check --
    every service referenced by a capability has POIs configured -- is already
    enforced by utils.services._bootstrap_compatibility_checks(), and doing it
    here too would make this module import utils.services, which itself
    imports this module to run that check, creating a circular import).
    `electre_weight` falls back to equal weighting across the capability's
    services when missing or malformed; `veto_threshold` falls back to
    infinity (no veto); `enabled` falls back to true.

    Inputs:
    - path: path to the capability configuration CSV.
    - valid_services: optional set of service identifiers to validate
      `services` against; skipped when None.

    Outputs:
    - tuple of (ordered services per capability, weight per service per
      capability, veto threshold per capability, enabled flag per
      capability), each keyed by capability, in file order.
    """
    if not path.is_file():
        raise ValueError(f"Invalid capability config CSV (path={path}): file not found")

    capability_services: "OrderedDict[str, list[str]]" = OrderedDict()
    weights: dict[str, dict[str, float]] = {}
    veto: dict[str, float] = {}
    enabled: dict[str, bool] = {}

    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames) if reader.fieldnames is not None else None
        required = ["capability", "services", "electre_weight", "veto_threshold", "enabled"]
        if fieldnames is None:
            raise ValueError(
                f"Invalid capability config CSV (path={path}): missing header row, expected {required}"
            )
        missing = [c for c in required if c not in fieldnames]
        if missing:
            raise ValueError(
                f"Invalid capability config CSV (path={path}): missing required columns {missing}; found {fieldnames}"
            )

        for idx, row in enumerate(reader, start=2):
            capability = (row.get("capability") or "").strip()
            if not capability:
                raise ValueError(
                    f"Invalid capability config CSV (path={path} row={idx} column=capability): expected non-empty string"
                )
            if capability in capability_services:
                raise ValueError(
                    f"Invalid capability config CSV (path={path} row={idx} column=capability): duplicate capability {capability!r}"
                )

            services_raw = (row.get("services") or "").strip()
            try:
                services = ast.literal_eval(services_raw)
            except Exception as exc:
                raise ValueError(
                    f"Invalid capability config CSV (path={path} row={idx} column=services): "
                    f"expected Python list literal, got {services_raw!r}"
                ) from exc
            if not isinstance(services, list) or not services:
                raise ValueError(
                    f"Invalid capability config CSV (path={path} row={idx} column=services): expected non-empty list"
                )
            services_clean = [str(s).strip() for s in services]
            if valid_services is not None:
                for s in services_clean:
                    if s not in valid_services:
                        raise ValueError(
                            f"Invalid capability config CSV (path={path} row={idx} column=services): unknown service {s!r}"
                        )

            weight_raw = (row.get("electre_weight") or "").strip()
            row_weights = None
            if weight_raw:
                try:
                    weight_values = ast.literal_eval(weight_raw)
                except Exception:
                    weight_values = None
                if isinstance(weight_values, list) and len(weight_values) == len(services_clean):
                    try:
                        row_weights = {s: float(w) for s, w in zip(services_clean, weight_values)}
                    except (TypeError, ValueError):
                        row_weights = None
            if row_weights is None:
                row_weights = {s: 1.0 / len(services_clean) for s in services_clean}

            veto_raw = (row.get("veto_threshold") or "").strip()
            try:
                row_veto = float(veto_raw) if veto_raw else _DEFAULT_V
            except ValueError:
                row_veto = _DEFAULT_V

            enabled_raw = (row.get("enabled") or "").strip().lower()
            row_enabled = enabled_raw in ("true", "1", "yes") if enabled_raw else True

            capability_services[capability] = services_clean
            weights[capability] = row_weights
            veto[capability] = row_veto
            enabled[capability] = row_enabled

    return capability_services, weights, veto, enabled


CAPABILITY_SERVICES, CAP_ELECTRE_W, _veto_by_capability, CAPABILITY_ENABLED = _load_capability_config(
    CAPABILITY_CSV_PATH
)

# Runtime lookup consumed by electre_tri_integration().
_ELECTRE_PARAMS = {
    capability: {"v": veto} for capability, veto in _veto_by_capability.items()
}


# ── Shared capability-grid visualization constants ───────────────────────────
# Single source of truth for the map/legend styling, imported by
# pipeline_runner.py (QGIS project + gpkg styling) and
# generate_capability_legend.py (standalone legend PNGs). Kept here rather than
# duplicated with "must stay in sync" comments so the two rendering paths can
# never drift.

# Default signature colours, in CAPABILITY_SERVICES order (restorativeness,
# nutrition, care). Overridable per run via PipelineConfig.capability_colors.
_DEFAULT_CAPABILITY_COLORS = ["#006BFF", "#FFA200", "#EB4CCC"]


def get_capability_colors(cfg=None) -> dict[str, str]:
    """Map each capability to its signature color, in CAPABILITY_SERVICES order.

    Inputs:
    - cfg: optional PipelineConfig. When given, colors come from
      cfg.capability_colors instead of the module default.

    Outputs:
    - dict of capability name -> hex color string.
    """
    colors = list(cfg.capability_colors) if cfg is not None else _DEFAULT_CAPABILITY_COLORS
    return dict(zip(CAPABILITY_SERVICES.keys(), colors))


# Every service under a capability, the capability grid itself, and its legend
# all use this one color. Kept as a module-level default for callers that don't
# have a PipelineConfig handy (standalone legend/dashboard scripts); callers that
# do have one should call get_capability_colors(cfg) instead.
CAPABILITY_COLORS = get_capability_colors()

# ELECTRE TRI class boundaries and human labels. Five classes over [0, 1], split
# at the real configured cut points (core.config.ELECTRE_BOUNDARIES) -- NOT a
# naive uniform quintile. This used to be a hardcoded [0.0, 0.2, 0.4, 0.6, 0.8,
# 1.0], disconnected from the boundaries actually used for classification
# (_BOUNDARIES above); it happened to still color cells correctly under
# capability_score_mode="discrete" (every stored value already snaps to one of
# the 5 real class midpoints, which all landed inside the matching uniform
# bucket by coincidence), but would misclassify under "continuous" mode, and
# the legend text always showed the wrong numeric ranges regardless.
ELECTRE_BOUNDS = [0.0] + list(ELECTRE_BOUNDARIES) + [1.0]
ELECTRE_LABELS = [
    f"{name} ({ELECTRE_BOUNDS[i]:.2f}–{ELECTRE_BOUNDS[i + 1]:.2f})"
    for i, name in enumerate(["Very Low", "Low", "Medium", "High", "Very High"])
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


# Shared sequential palette, temporarily replacing each capability's own signature-hue
# shading for every map/legend consumer except the power-scaling dashboard (which keeps
# CAPABILITY_COLORS -- it needs 3 visually DISTINCT group colors, not a shared magnitude
# scale). 10 stops, one per 0.1-wide service-score bucket: [0,0.1), [0.1,0.2), ..., [0.9,1.0].
SERVICE_COLOR_STOPS: list[str] = [
    "#d7191c", "#e85b3b", "#f99d59", "#fec981", "#ffedab",
    "#ebf7ad", "#c4e687", "#96d265", "#58b453", "#1a9641",
]


def service_step_color(value: float) -> str:
    """Bucket a service score in [0, 1] into one of the 10 SERVICE_COLOR_STOPS.

    Ten equal-width buckets, [0,0.1) through [0.9,1.0]; 1.0 itself (and anything above,
    or non-finite) falls in the last/first bucket rather than overflowing.
    """
    if value is None or not math.isfinite(value):
        return SERVICE_COLOR_STOPS[0]
    idx = min(int(max(0.0, min(1.0, value)) * 10), len(SERVICE_COLOR_STOPS) - 1)
    return SERVICE_COLOR_STOPS[idx]


# 5-color palette for the capability grid's ELECTRE classes, replacing each capability's
# own white->hue shading. Endpoints reuse the service scale's ends so the two legends
# read as one consistent scheme.
CAPABILITY_COLOR_STOPS: list[str] = [
    SERVICE_COLOR_STOPS[0], "#fdae61", "#ffffc0", "#a6d96a", SERVICE_COLOR_STOPS[-1],
]
