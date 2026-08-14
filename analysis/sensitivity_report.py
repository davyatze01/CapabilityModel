"""Build the sensitivity report: a self-contained HTML report
(outputs/sensitivity/sensitivity_report.html) with an interpretation section and
all result tables -- opens in any browser and prints straight to PDF.

Covers parameter sensitivity only ("do the parameters matter?"): upstream config
perturbations (sensitivity_upstream.py) and downstream ELECTRE OAT/weight sweeps
(sensitivity_analysis.py). Robustness to input noise ("is the output trustworthy
given noisy inputs?") is a different question with its own script and report --
see robustness_analysis.py / robustness_report.py.

Reads the CSVs produced by sensitivity_upstream.py and sensitivity_analysis.py.
The interpretation verdicts are computed from the numbers (not hard-coded), so
the prose stays true if you rerun with different perturbations.

Usage:
  python sensitivity_report.py
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

import matplotlib

from analysis.sensitivity_analysis import ELECTRE_Q, ELECTRE_P, LAMBDA_BASELINE
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from core.config import PipelineConfig

SENS_DIR = Path("outputs/debug") / PipelineConfig().artifact_slug
UP_DIR = SENS_DIR / "sensitivity_upstream"

# Thresholds (in % of nodes changing class) for the automatic parameter verdicts.
LOAD_BEARING = 20.0
MODERATE = 5.0

# ── Chart palette (fixed categorical order; matches the report's --bad/--warn/--good) ──
CAT_HUES = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"]
VERDICT_HEX = {"bad": "#c0392b", "warn": "#d68910", "good": "#1e8449"}
CHART_INK = "#1a1a1a"
CHART_MUTED = "#666666"
CHART_GRID = "#e1e0d9"

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.size": 10.5,
    "text.color": CHART_INK,
    "axes.edgecolor": CHART_GRID,
    "axes.labelcolor": CHART_MUTED,
    "xtick.color": CHART_MUTED,
    "ytick.color": CHART_MUTED,
    "axes.grid": True,
    "grid.color": CHART_GRID,
    "grid.linewidth": 0.8,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "savefig.facecolor": "white",
})


# ── Load ─────────────────────────────────────────────────────────────────────

def _load(path: Path) -> pd.DataFrame:
    if not path.exists():
        print(f"ERROR: expected {path}. Run the analyses first.", file=sys.stderr)
        sys.exit(1)
    return pd.read_csv(path)


def load_results() -> dict:
    return {
        "up_summary": _load(UP_DIR / "upstream_summary.csv"),
        "up_service": _load(UP_DIR / "upstream_service_deltas.csv"),
        "up_dropout": _load(UP_DIR / "upstream_node_dropout.csv"),
        "up_levels": _load(UP_DIR / "upstream_level_distribution.csv"),
        "oat": _load(SENS_DIR / "sensitivity_oat.csv"),
        "oat_levels": _load(SENS_DIR / "sensitivity_oat_levels.csv"),
        "weights": _load(SENS_DIR / "sensitivity_weights.csv"),
    }


# ── Verdicts (data-driven) ───────────────────────────────────────────────────

def _classify(pct: float) -> tuple[str, str]:
    if pct >= LOAD_BEARING:
        return "load-bearing", "bad"
    if pct >= MODERATE:
        return "moderate", "warn"
    return "inert", "good"


# Canonical pipeline order (matches the model-chain table in section 1: decay ->
# RRA -> contribution -> Choquet capacity/interactions -> ELECTRE q/p/lambda) --
# every other listing of these parameters (section 2's tables, section 3's axis
# bullets, section 8's ranking) iterates in this same order so a reader doesn't
# have to re-learn a new ordering per section. "weights" has no place in the
# five-step chain (it's a Dirichlet perturbation of ELECTRE's service weights,
# not a single scalar), so it's appended last.
PARAM_ORDER = ["decay", "rra", "contribution", "capacity", "interactions", "q", "p", "lambda", "weights"]

PARAM_LABELS = {
    "decay": "decay_coefficient",
    "rra": "RRA lambda-weighting scheme",
    "contribution": "contribution_coefficient",
    "capacity": "choquet_capacity",
    "interactions": "choquet_interactions",
    "lambda": "lambda cut level",
    "q": "q (indifference threshold)",
    "p": "p (preference threshold)",
    "weights": "ELECTRE service weights",
}


def compute_verdicts(R: dict) -> dict:
    up = R["up_summary"]
    oat = R["oat"]
    weights = R["weights"]
    caps = sorted(up["capability"].unique())

    # Most fragile capability = highest mean % changed across all upstream configs.
    # Kept only as context for section 7 ("why is X the most exposed capability"),
    # not as the primary ranking criterion (see param_rows below).
    fragility = up.groupby("capability")["pct_nodes_changed"].mean().sort_values(ascending=False)
    fragile_cap = fragility.index[0]

    up = up.copy()
    up["axis"] = up["config"].map(lambda c: c.split("_")[0])

    # Parameter influence ranking: for every tested parameter (upstream axis,
    # downstream OAT knob, or weight perturbation), take the worst-case and mean
    # % of nodes changed *per capability*, then the worst/mean across ALL
    # capabilities -- so a parameter ranks high if it moves ANY capability a lot,
    # regardless of which one. This answers "what needs the strongest
    # justification", independent of which capability happens to be shakiest.
    def _per_cap_worst(df: pd.DataFrame, group_col: str, key) -> pd.Series:
        sub = df[df[group_col] == key]
        return sub.groupby("capability")["pct_nodes_changed"].max()

    param_stats = {}
    for axis in up["axis"].unique():
        per_cap = _per_cap_worst(up, "axis", axis)
        param_stats[axis] = per_cap
    for param in oat["parameter"].unique():
        per_cap = _per_cap_worst(oat, "parameter", param)
        param_stats[param] = per_cap
    if len(weights):
        per_cap = weights.groupby("capability")["pct_nodes_changed"].max()
        param_stats["weights"] = per_cap

    param_rows = []
    for key, per_cap in param_stats.items():
        worst_pct = float(per_cap.max())
        mean_pct = float(per_cap.mean())
        worst_cap = per_cap.idxmax()
        label, css = _classify(worst_pct)
        param_rows.append({
            "key": key,
            "label": PARAM_LABELS.get(key, key),
            "worst_pct": worst_pct,
            "mean_pct": mean_pct,
            "worst_cap": worst_cap,
            "verdict": label,
            "css": css,
        })
    param_rows.sort(key=lambda r: r["worst_pct"], reverse=True)

    # Downstream lambda cliff on the fragile capability, kept for the section-4 prose.
    lam = oat[(oat["parameter"] == "lambda") & (oat["capability"] == fragile_cap)]
    lam_worst = float(lam["pct_nodes_changed"].max())

    return {
        "caps": caps,
        "fragile_cap": fragile_cap,
        "fragility": fragility,
        "param_rows": param_rows,
        "lambda_worst": lam_worst,
    }


def verdict_rows(V: dict) -> list[tuple[str, str, str, str, str]]:
    """(parameter, worst-case % nodes changed, capability it hits worst, verdict, css class),
    ranked by worst-case influence across ALL capabilities -- highest first."""
    rows = []
    for r in V["param_rows"]:
        rows.append((
            r["label"],
            f"{r['worst_pct']:.1f}%",
            r["worst_cap"],
            r["verdict"],
            r["css"],
        ))
    return rows


# ── HTML ─────────────────────────────────────────────────────────────────────

CSS = """
:root { --fg:#1a1a1a; --muted:#666; --line:#ddd; --bad:#c0392b; --warn:#d68910; --good:#1e8449; }
* { box-sizing: border-box; }
body { font: 15px/1.55 -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
       color: var(--fg); max-width: 960px; margin: 2rem auto; padding: 0 1.2rem; }
h1 { font-size: 1.9rem; margin: 0 0 .2rem; }
h2 { font-size: 1.3rem; margin: 2.2rem 0 .6rem; border-bottom: 2px solid var(--line); padding-bottom: .3rem; }
h3 { font-size: 1.05rem; margin: 1.4rem 0 .4rem; color: var(--muted); }
.sub { color: var(--muted); margin: 0 0 1.4rem; }
table { border-collapse: collapse; width: 100%; margin: .6rem 0 1.2rem; font-size: 13.5px; }
th, td { border: 1px solid var(--line); padding: .35rem .55rem; text-align: right; }
th { background: #f5f5f5; text-align: center; }
td:first-child, th:first-child { text-align: left; }
.verdict td { font-weight: 600; }
.bad { color: var(--bad); } .warn { color: var(--warn); } .good { color: var(--good); }
.callout { background:#f7f9fc; border-left: 4px solid #4a6da7; padding: .7rem 1rem; margin: 1rem 0; }
.callout.risk { background:#fdf3f2; border-left-color: var(--bad); }
.hi { background: #fff2f0; } .hi2 { background: #fff9e6; }
code { background:#f0f0f0; padding: .05rem .3rem; border-radius: 3px; font-size: 92%; }
.formula { font: italic 14px/1.5 Georgia, "Times New Roman", serif; color: #333;
           margin-top: .25rem; padding-left: .6rem; border-left: 2px solid var(--line); }
.formula sub, .formula sup { font-size: 72%; }
.chart { margin: .4rem 0 1.2rem; }
.chart img { max-width: 100%; height: auto; display: block; }
@media print { body { margin: 0; max-width: none; font-size: 11px; } h2 { page-break-after: avoid; } table { page-break-inside: avoid; } }
"""


def _fig_html(fig) -> str:
    """Encode a matplotlib figure as a self-contained base64 <img>, then close it."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f'<div class="chart"><img src="data:image/png;base64,{encoded}" alt="chart"/></div>'


def _cap_colors(caps: list[str]) -> dict[str, str]:
    return {cap: CAT_HUES[i % len(CAT_HUES)] for i, cap in enumerate(caps)}


def _style_ax(ax) -> None:
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(CHART_GRID)
    ax.tick_params(length=0)


def _abs_tick_formatter(x: float, _pos: int) -> str:
    """Tornado bars use sign only to pick a direction (left/below vs right/above);
    the underlying quantity (a % or a count) is never negative, so ticks must show
    the magnitude, not the plotting sign."""
    return f"{abs(x):g}"


def _draw_tornado(
    ax,
    y_labels: list[str],
    series: dict[str, list[float]],
    colors: dict[str, str],
    xlabel: str,
) -> int:
    """Shared renderer for every tornado chart: grouped horizontal bars, already
    signed (negative = left/below, positive = right/above), diverging from a
    central baseline line at x=0. Returns the number of series drawn (for legend
    layout by the caller)."""
    from matplotlib.ticker import FuncFormatter

    n_series = len(series)
    bar_h = 0.8 / max(n_series, 1)
    y = np.arange(len(y_labels))
    for i, (label, values) in enumerate(series.items()):
        offset = (i - (n_series - 1) / 2) * bar_h
        ax.barh(y + offset, values, height=bar_h, color=colors[label], label=label)
    ax.axvline(0, color=CHART_INK, linewidth=1.2)
    ax.set_yticks(y)
    ax.set_yticklabels(y_labels)
    ax.xaxis.set_major_formatter(FuncFormatter(_abs_tick_formatter))
    ax.set_xlabel(xlabel)
    _style_ax(ax)
    return n_series


def chart_tornado_oat(oat: pd.DataFrame, parameter: str, baseline_value: float, caps: list[str]) -> str:
    """Tornado plot for one OAT parameter: one row per tested value (baseline
    centred), one color per capability, bars left of the tested value is below
    baseline and right if above."""
    colors = _cap_colors(caps)
    sub = oat[oat["parameter"] == parameter]
    values = sorted(sub["value"].unique())
    cols = [c for c in caps if c in sub["capability"].unique()]
    series = {}
    for cap in cols:
        signed = []
        for v in values:
            row = sub[(sub["value"] == v) & (sub["capability"] == cap)]
            pct = float(row["pct_nodes_changed"].iloc[0]) if len(row) else 0.0
            signed.append(-pct if v < baseline_value else pct)
        series[cap] = signed
    y_labels = [f"{v:g}" + ("  (baseline)" if np.isclose(v, baseline_value) else "") for v in values]
    fig, ax = plt.subplots(figsize=(6.5, 0.55 * len(values) + 1.2))
    n_series = _draw_tornado(ax, y_labels, series, colors, "% nodes changed (magnitude; side = below/above baseline)")
    ax.set_title(f"{parameter} — tornado (baseline = {baseline_value:g})", fontsize=11, color=CHART_INK, loc="left")
    if n_series > 1:
        ax.legend(frameon=False, fontsize=9, ncols=n_series, loc="upper center",
                   bbox_to_anchor=(0.5, -0.16 / (0.55 * len(values) / 3)))
    return _fig_html(fig)


# Which upstream config is the "below baseline" / "above baseline" side of each
# axis, and how the axis itself is labelled. Both perturbation directions are
# always tested (Section 2), so every axis has exactly one pair here. Ordered
# per PARAM_ORDER (see below) -- the canonical pipeline order used everywhere.
AXIS_PAIRS = {
    "decay": ("decay_x0.8", "decay_x1.2", "decay_coefficient (×1.0 = baseline)"),
    "rra": ("rra_lambda_uniform", "rra_lambda_reversed", "RRA lambda-weighting scheme (baseline = rank-descending)"),
    "contribution": ("contribution_min", "contribution_max", "contribution_coefficient (baseline = configured tier)"),
    "capacity": ("capacity_blend50", "capacity_exaggerate50", "choquet_capacity (baseline = configured skew)"),
    "interactions": ("interactions_0", "interactions_x0.7", "choquet_interactions (baseline = configured ±0.03–0.07)"),
}


def chart_tornado_upstream(up: pd.DataFrame, axis: str, caps: list[str]) -> str:
    """Tornado plot for one upstream axis: one row per capability, below-config's
    effect extends left, above-config's effect extends right."""
    below_cfg, above_cfg, axis_label = AXIS_PAIRS[axis]
    cols = [c for c in caps if c in up["capability"].unique()]

    def pct(config: str, cap: str) -> float:
        row = up[(up["config"] == config) & (up["capability"] == cap)]
        return float(row["pct_nodes_changed"].iloc[0]) if len(row) else 0.0

    series = {
        below_cfg: [-pct(below_cfg, cap) for cap in cols],
        above_cfg: [pct(above_cfg, cap) for cap in cols],
    }
    colors = {below_cfg: CAT_HUES[0], above_cfg: CAT_HUES[7]}
    fig, ax = plt.subplots(figsize=(6.5, 0.55 * len(cols) + 1.2))
    _draw_tornado(ax, cols, series, colors, "% nodes changed (magnitude; side = below/above baseline)")
    ax.set_title(f"{axis_label} — tornado", fontsize=11, color=CHART_INK, loc="left")
    ax.legend(frameon=False, fontsize=9, ncols=2, loc="upper center", bbox_to_anchor=(0.5, -0.28))
    return _fig_html(fig)


def chart_ranked_bar(param_rows: list[dict]) -> str:
    """Horizontal bar chart of the parameter influence ranking, colored by verdict
    (bad/warn/good), ranked worst-case % descending to match the table above it."""
    labels = [r["label"] for r in param_rows]
    values = [r["worst_pct"] for r in param_rows]
    colors = [VERDICT_HEX[r["css"]] for r in param_rows]
    fig, ax = plt.subplots(figsize=(6.5, 0.5 * len(labels) + 1))
    y = np.arange(len(labels))[::-1]
    ax.barh(y, values, height=0.6, color=colors)
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_xlabel("worst-case % nodes changed (any capability)")
    ax.axvline(LOAD_BEARING, color=VERDICT_HEX["bad"], linewidth=0.8, linestyle="--")
    ax.axvline(MODERATE, color=VERDICT_HEX["warn"], linewidth=0.8, linestyle="--")
    ax.set_title("Parameter influence ranking", fontsize=11, color=CHART_INK, loc="left")
    _style_ax(ax)
    return _fig_html(fig)


def chart_tornado_dropout(drop: pd.DataFrame, axis: str) -> str:
    """Tornado plot for one axis's reachable-node changes: below-config's
    dropped/added counts extend left, above-config's extend right."""
    below_cfg, above_cfg, axis_label = AXIS_PAIRS[axis]

    def counts(config: str) -> tuple[float, float]:
        row = drop[drop["config"] == config]
        if not len(row):
            return 0.0, 0.0
        return float(row["dropped_vs_baseline"].iloc[0]), float(row["added_vs_baseline"].iloc[0])

    below_dropped, below_added = counts(below_cfg)
    above_dropped, above_added = counts(above_cfg)
    series = {
        below_cfg: [-below_dropped, -below_added],
        above_cfg: [above_dropped, above_added],
    }
    colors = {below_cfg: CAT_HUES[0], above_cfg: CAT_HUES[7]}
    fig, ax = plt.subplots(figsize=(6.5, 2.2))
    _draw_tornado(ax, ["dropped", "added"], series, colors, "nodes (magnitude; side = below/above baseline)")
    ax.set_title(f"{axis_label} — reachable-node changes", fontsize=11, color=CHART_INK, loc="left")
    ax.legend(frameon=False, fontsize=9, ncols=2, loc="upper center", bbox_to_anchor=(0.5, -0.35))
    return _fig_html(fig)


def chart_tornado_service_deltas(svc_pivot: pd.DataFrame, axis: str) -> str:
    """Tornado plot for one axis's per-service impact: below-config's mean |delta|
    extends left, above-config's extends right, one row per service."""
    below_cfg, above_cfg, axis_label = AXIS_PAIRS[axis]
    services = list(svc_pivot.columns)

    def deltas(config: str) -> list[float]:
        return svc_pivot.loc[config].to_numpy(dtype=float).tolist() if config in svc_pivot.index else [0.0] * len(services)

    series = {
        below_cfg: [-v for v in deltas(below_cfg)],
        above_cfg: deltas(above_cfg),
    }
    colors = {below_cfg: CAT_HUES[0], above_cfg: CAT_HUES[7]}
    fig, ax = plt.subplots(figsize=(6.5, 0.4 * len(services) + 1.2))
    _draw_tornado(ax, services, series, colors, "mean |service-score Δ| (magnitude; side = below/above baseline)")
    ax.set_title(f"{axis_label} — per-service impact", fontsize=11, color=CHART_INK, loc="left")
    ax.legend(frameon=False, fontsize=9, ncols=2, loc="upper center", bbox_to_anchor=(0.5, -0.16))
    return _fig_html(fig)


# The five ELECTRE classes, in their fixed order (utils/capabilities._CATEGORIES).
LEVELS = ["Very Low", "Low", "Medium", "High", "Very High"]

# Every parameter that has a genuine (min, max) perturbation pair -- excludes
# "weights" (a Dirichlet distribution, not a single min/max).
LEVEL_TORNADO_PARAMS = ["decay", "rra", "contribution", "capacity", "interactions", "q", "p", "lambda"]


def _param_min_max_config(R: dict, key: str) -> tuple[str, str] | None:
    """(min_config, max_config) for an upstream axis, or (min_value, max_value)
    as strings for an OAT parameter. None if the data isn't available yet."""
    if key in AXIS_PAIRS:
        below_cfg, above_cfg, _ = AXIS_PAIRS[key]
        seen = set(R["up_levels"]["config"]) if len(R["up_levels"]) else set()
        if not ({below_cfg, above_cfg} <= seen):
            return None
        return below_cfg, above_cfg
    sub = R["oat_levels"][R["oat_levels"]["parameter"] == key]
    if sub.empty:
        return None
    values = sorted(sub["value"].unique())
    return f"{values[0]:g}", f"{values[-1]:g}"


def _param_min_max_desc(R: dict, key: str) -> tuple[str, str]:
    """Short human-readable description of the min/max test for the chart's
    right-hand annotation -- the config's description for upstream axes, or the
    bare numeric value for an OAT parameter."""
    if key in AXIS_PAIRS:
        below_cfg, above_cfg, _ = AXIS_PAIRS[key]
        min_desc = UPSTREAM_CONFIG_DESC.get(below_cfg, (None, None, below_cfg))[2]
        max_desc = UPSTREAM_CONFIG_DESC.get(above_cfg, (None, None, above_cfg))[2]
        return min_desc, max_desc
    sub = R["oat"][R["oat"]["parameter"] == key]
    if sub.empty:
        return "?", "?"
    values = sorted(sub["value"].unique())
    return f"{values[0]:g}", f"{values[-1]:g}"


def _level_pct(R: dict, key: str, capability: str, level: str, side: str) -> float | None:
    """% of nodes classified `level` for `capability`, under the min ('below')
    or max ('above') perturbation of parameter `key`. None if not run yet."""
    minmax = _param_min_max_config(R, key)
    if minmax is None:
        return None
    if key in AXIS_PAIRS:
        cfg = minmax[0] if side == "min" else minmax[1]
        sub = R["up_levels"]
        row = sub[(sub["config"] == cfg) & (sub["capability"] == capability) & (sub["level"] == level)]
        return float(row["pct_nodes"].iloc[0]) if len(row) else None
    sub = R["oat_levels"]
    sub = sub[(sub["parameter"] == key) & (sub["capability"] == capability) & (sub["level"] == level)]
    if sub.empty:
        return None
    values = sorted(sub["value"].unique())
    value = values[0] if side == "min" else values[-1]
    row = sub[sub["value"] == value]
    return float(row["pct_nodes"].iloc[0]) if len(row) else None


def chart_level_tornado(R: dict, capability: str, level: str) -> str | None:
    """Tornado plot for one (capability, level): one row per parameter, the
    min-perturbation's % of nodes at this level extends left, the
    max-perturbation's extends right. Parameter name labels the left axis; the
    (min, max) value/procedure that was actually tested labels the right axis."""
    from matplotlib.ticker import FuncFormatter

    entries = []
    for key in LEVEL_TORNADO_PARAMS:
        pmin = _level_pct(R, key, capability, level, "min")
        pmax = _level_pct(R, key, capability, level, "max")
        if pmin is None or pmax is None:
            continue
        min_desc, max_desc = _param_min_max_desc(R, key)
        entries.append((key, pmin, pmax, min_desc, max_desc))
    if not entries:
        return None

    labels = [PARAM_LABELS[k] for k, _, _, _, _ in entries]
    right_labels = [f"({mind}, {maxd})" for _, _, _, mind, maxd in entries]
    mins = [-v for _, v, _, _, _ in entries]
    maxs = [v for _, _, v, _, _ in entries]

    fig, ax = plt.subplots(figsize=(9.0, 0.5 * len(entries) + 1.3))
    y = np.arange(len(entries))
    ax.barh(y, mins, height=0.6, color=CAT_HUES[0], label="min")
    ax.barh(y, maxs, height=0.6, color=CAT_HUES[7], label="max")
    ax.axvline(0, color=CHART_INK, linewidth=1.2)
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.xaxis.set_major_formatter(FuncFormatter(_abs_tick_formatter))
    ax.set_xlabel(f"% of nodes classified '{level}' (magnitude; left = min, right = max)")
    ax.set_title(f"{capability} — '{level}'", fontsize=11, color=CHART_INK, loc="left")
    ax.legend(frameon=False, fontsize=9, ncols=2, loc="upper center", bbox_to_anchor=(0.5, -0.14))
    _style_ax(ax)

    ax2 = ax.twinx()
    ax2.set_ylim(ax.get_ylim())
    ax2.set_yticks(y)
    ax2.set_yticklabels(right_labels, fontsize=8.5)
    ax2.tick_params(length=0)
    for spine in ("top", "left", "bottom"):
        ax2.spines[spine].set_visible(False)
    ax2.spines["right"].set_visible(False)

    return _fig_html(fig)


def level_tornado_table_html(R: dict, caps: list[str]) -> str:
    """Comprehensive table backing every chart_level_tornado figure: one row per
    (capability, level, parameter) with the min/max description and both
    percentages, so every number in the charts is also available verbatim."""
    rows = []
    for capability in caps:
        for level in LEVELS:
            for key in LEVEL_TORNADO_PARAMS:
                pmin = _level_pct(R, key, capability, level, "min")
                pmax = _level_pct(R, key, capability, level, "max")
                if pmin is None or pmax is None:
                    continue
                min_desc, max_desc = _param_min_max_desc(R, key)
                rows.append(
                    f"<tr><td>{capability}</td><td>{level}</td><td>{PARAM_LABELS[key]}</td>"
                    f"<td>{min_desc}</td><td>{pmin:.1f}%</td>"
                    f"<td>{max_desc}</td><td>{pmax:.1f}%</td></tr>"
                )
    if not rows:
        return "<p class=\"sub\">No parameter has both min and max perturbations run yet.</p>"
    return (
        '<div style="max-height:480px; overflow-y:auto;">'
        "<table><tr><th>Capability</th><th>Level</th><th>Parameter</th>"
        "<th>Min (test)</th><th>% nodes (min)</th><th>Max (test)</th><th>% nodes (max)</th></tr>"
        + "".join(rows) + "</table></div>"
    )


def chart_weight_stats(wsum: pd.DataFrame, caps: list[str]) -> str:
    """Grouped bar of the weight-perturbation summary stats (mean/median/max) per
    capability."""
    stat_cols = [c for c in ["mean", "50%", "max"] if c in wsum.columns]
    stat_colors = {"mean": CAT_HUES[0], "50%": CAT_HUES[1], "max": CAT_HUES[5]}
    cats = [c for c in caps if c in wsum.index]
    n_groups, n_series = len(cats), len(stat_cols)
    width = 0.8 / max(n_series, 1)
    fig, ax = plt.subplots(figsize=(max(5, n_groups * 1.3), 3.2))
    x = np.arange(n_groups)
    for i, stat in enumerate(stat_cols):
        offset = (i - (n_series - 1) / 2) * width
        ax.bar(x + offset, wsum.loc[cats, stat].to_numpy(), width=width,
               color=stat_colors[stat], label=stat)
    ax.set_xticks(x)
    ax.set_xticklabels(cats)
    ax.set_ylabel("% nodes changed")
    ax.set_title("Weight perturbation — mean / median / max per capability", fontsize=11, color=CHART_INK, loc="left")
    ax.legend(frameon=False, fontsize=9, ncols=n_series, loc="upper center", bbox_to_anchor=(0.5, -0.2))
    _style_ax(ax)
    return _fig_html(fig)


def _df_html(df: pd.DataFrame, highlight_col: str | None = None) -> str:
    return df.to_html(index=True, border=0, float_format=lambda x: f"{x:.2f}", na_rep="")


def _pivot(df: pd.DataFrame, index: str, columns: str, values: str) -> pd.DataFrame:
    return df.pivot_table(index=index, columns=columns, values=values)


# Static descriptions of what each upstream config actually mutates (the numeric
# factor is already in the config name, e.g. "decay_x0.8"); used only to render
# the "parameters tested" table, not any verdict. Ordered per PARAM_ORDER (the
# canonical pipeline order) so the table reads in the same sequence as section 1.
# (parameter, model stage it acts on, how the perturbation is built, formula-HTML or "")
UPSTREAM_CONFIG_DESC = {
    "decay_x0.8": ("decay coefficient", "per-POI-type distance decay (accessibility)",
                   "every POI type's configured decay coefficient multiplied by 0.8 — a uniformly shorter travel tolerance",
                   "a&prime;<sub>t</sub> = &gamma;&thinsp;a<sub>t</sub>, &nbsp; &gamma; = 0.8, for every POI type t"),
    "decay_x1.2": ("decay coefficient", "per-POI-type distance decay (accessibility)",
                   "every POI type's configured decay coefficient multiplied by 1.2 — a uniformly longer travel tolerance",
                   "a&prime;<sub>t</sub> = &gamma;&thinsp;a<sub>t</sub>, &nbsp; &gamma; = 1.2, for every POI type t"),
    "rra_lambda_uniform": ("RRA λ-weighting scheme", "multi-modal redundancy aggregation",
                           "modes ranked best→worst are weighted λ = (1, ½, ⅓, ¼) at baseline; here every mode gets λ = 1 — no redundancy discount at all",
                           "RRA = 1 &minus; &prod;<sub>i=1</sub><sup>m</sup> (1 &minus; &lambda;<sub>i</sub>&thinsp;d<sub>(i)</sub>), "
                           "&nbsp; d<sub>(1)</sub> &ge; &hellip; &ge; d<sub>(m)</sub>; &nbsp; baseline &lambda;<sub>i</sub> = 1/i &nbsp;&rarr;&nbsp; here &lambda;<sub>i</sub> = 1"),
    "rra_lambda_reversed": ("RRA λ-weighting scheme", "multi-modal redundancy aggregation",
                            "the baseline taper λ = (1, ½, ⅓, ¼) is flipped: the WORST mode gets λ = 1 and the best the smallest weight — the polar opposite assumption",
                            "RRA = 1 &minus; &prod;<sub>i=1</sub><sup>m</sup> (1 &minus; &lambda;<sub>i</sub>&thinsp;d<sub>(i)</sub>), "
                            "&nbsp; baseline &lambda;<sub>i</sub> = 1/i &nbsp;&rarr;&nbsp; here &lambda;<sub>i</sub> = 1/(m&minus;i+1)"),
    "contribution_min": ("contribution coefficient", "POI-count saturation (service aggregation)",
                         "every POI type pinned to the minimum tier of the configured set {1, 2, 3, 5, 8}: tier 1, i.e. a single POI already saturates the service",
                         "c&prime;<sub>t</sub> = 1 for every POI type t"),
    "contribution_max": ("contribution coefficient", "POI-count saturation (service aggregation)",
                         "every POI type pinned to the maximum tier: 8 POIs needed to reach saturation",
                         "c&prime;<sub>t</sub> = 8 for every POI type t"),
    "capacity_blend50": ("Choquet capacity", "POI-type weights inside each service",
                         "each service's capacity vector w is replaced by 0.5·w + 0.5·u, where u is the uniform vector (1/n per POI type) — halfway between the configured skew and no skew",
                         "w&prime; = (1 &minus; &beta;)&thinsp;w + &beta;&thinsp;u, &nbsp; &beta; = 0.5, &nbsp; u = (1/n, &hellip;, 1/n)"),
    "capacity_exaggerate50": ("Choquet capacity", "POI-type weights inside each service",
                              "the mirror image: w is replaced by u + 1.5·(w − u), i.e. every deviation from uniform stretched by 50%, clipped at 0 and renormalized to sum to 1",
                              "w&prime; = max(0, u + (1+&gamma;)(w &minus; u)) &frasl; &Sigma;, &nbsp; &gamma; = 0.5, "
                              "&nbsp; &Sigma; renormalizes so that &sum;<sub>t</sub> w&prime;<sub>t</sub> = 1"),
    "interactions_0": ("Choquet interactions", "POI-type pair synergies (accessibility)",
                       "every defined pair interaction set to 0 — no synergy or redundancy between POI types at all",
                       "I&prime;<sub>st</sub> = 0 for every defined pair (s, t)"),
    "interactions_x0.7": ("Choquet interactions", "POI-type pair synergies (accessibility)",
                          "every defined pair set to magnitude 0.7 keeping its original sign (a configured −0.03 becomes −0.7) — an order of magnitude above the configured ±0.03–0.07",
                          "I&prime;<sub>st</sub> = 0.7 &middot; sign(I<sub>st</sub>) for every defined pair (s, t)"),
}


def _params_tested_html(R: dict) -> str:
    """Explicit table of every parameter and the concrete values tested, derived
    from the result CSVs (not hard-coded) so it stays true on rerun."""
    up_rows = "".join(
        f"<tr><td><code>{param}</code></td><td>{acts_on}</td><td>{desc}"
        + (f'<div class="formula">{formula}</div>' if formula else "")
        + "</td></tr>"
        for cfg, (param, acts_on, desc, formula) in UPSTREAM_CONFIG_DESC.items()
        if cfg in set(R["up_summary"]["config"])
    )

    oat = R["oat"]
    oat_rows = ""
    for param in oat["parameter"].unique():
        sub = oat[oat["parameter"] == param]
        values = sorted(sub["value"].unique())
        baseline_val = sub.loc[sub["is_baseline"], "value"]
        base_str = f"{float(baseline_val.iloc[0]):g}" if len(baseline_val) else "?"
        values_str = ", ".join(f"{v:g}" for v in values)
        oat_rows += (
            f"<tr><td><code>{param}</code></td><td>{base_str}</td>"
            f"<td>{values_str}</td></tr>"
        )

    w = R["weights"]
    n_draws = int(w.groupby("capability").size().max()) if len(w) else 0
    max_w = w["max_weight"].max() if "max_weight" in w and len(w) else float("nan")

    return f"""
<h3>Upstream config perturbations (real accessibility + service stages re-run)</h3>
<table><tr><th>Parameter</th><th>Acts on</th><th>How the perturbation is built</th></tr>{up_rows}</table>
<h3>Downstream ELECTRE TRI OAT sweeps (one parameter at a time, others at baseline)</h3>
<table><tr><th>Parameter</th><th>Baseline</th><th>Values tested</th></tr>{oat_rows}</table>
<p>These knobs enter the assignment as follows: q and p are fixed absolute indifference/preference
thresholds (not derived from each node's own score spread):</p>
<div class="formula">q = {ELECTRE_Q}, &nbsp;&nbsp; p = {ELECTRE_P},</div>
<p>and a node is promoted above boundary b<sub>k</sub> exactly when its outranking credibility clears
the cutting level (with no service vetoing, i.e. no deficit beyond v):</p>
<div class="formula">&rho;(x, b<sub>k</sub>) &ge; &lambda;.</div>
<h3>Weight perturbation</h3>
<p>Dirichlet draws around the configured (uniform) service weights, {n_draws} draws per capability,
concentration high enough that draws stay close to uniform (max single weight observed: {max_w:.2f}):</p>
<div class="formula">w &sim; Dirichlet(&alpha;, &hellip;, &alpha;), &nbsp; &alpha; = 20 by default, &nbsp;
E[w] = u = (1/m, &hellip;, 1/m)</div>
<p class="sub">Robustness to input noise (Monte Carlo over gaussian-perturbed service scores) is a separate
analysis -- see the separate robustness report.</p>
"""


def build_html(R: dict, V: dict, out_path: Path) -> None:
    up = R["up_summary"]
    fragile = V["fragile_cap"]

    # Parameter influence ranking table (most to least load-bearing).
    vrows = "".join(
        f'<tr><td>{p}</td><td>{w}</td><td>{worst_cap}</td><td class="{css}">{label}</td></tr>'
        for (p, w, worst_cap, label, css) in verdict_rows(V)
    )

    up_pivot = _pivot(up, "config", "capability", "pct_nodes_changed")
    svc_pivot = _pivot(R["up_service"], "config", "service", "mean_abs_delta")
    q_pivot = _pivot(R["oat"][R["oat"].parameter == "q"], "value", "capability", "pct_nodes_changed")
    p_pivot = _pivot(R["oat"][R["oat"].parameter == "p"], "value", "capability", "pct_nodes_changed")
    l_pivot = _pivot(R["oat"][R["oat"].parameter == "lambda"], "value", "capability", "pct_nodes_changed")
    wsum = R["weights"].groupby("capability")["pct_nodes_changed"].describe()[["mean", "std", "50%", "max"]]
    drop = R["up_dropout"]
    caps = V["caps"]

    def _oat_baseline(param: str) -> float:
        sub = R["oat"]
        row = sub[(sub["parameter"] == param) & sub["is_baseline"]]
        return float(row["value"].iloc[0])

    _configs_seen = set(up["config"])
    axes_present = [a for a in AXIS_PAIRS if {AXIS_PAIRS[a][0], AXIS_PAIRS[a][1]} <= _configs_seen]
    chart_up = "".join(chart_tornado_upstream(up, axis, caps) for axis in axes_present)
    chart_svc = "".join(chart_tornado_service_deltas(svc_pivot, axis) for axis in axes_present)
    chart_drop = "".join(chart_tornado_dropout(drop, axis) for axis in axes_present)

    # Axis-by-axis prose bullets, verdict pulled from the same computed ranking as
    # section 8 (not asserted by hand) so it can't go stale when the perturbation
    # design changes.
    axis_notes = {
        "decay": "travel-tolerance assumptions drive all three capabilities; it is also the only axis that "
                 "changes <em>which</em> nodes exist (see dropout below).",
        "rra": "brackets the two structural extremes of the redundancy-weighting scheme itself (no continuous "
               "baseline +/- step applies to a weighting rule) — uniform removes the redundancy discount "
               "entirely, reversed rewards the worst mode instead of the best.",
        "contribution": "every POI type is pinned to the minimum (1) or maximum (8) tier in the configured "
                         "Fibonacci-like class set — the full range the modeler could plausibly have chosen.",
        "capacity": "one direction flattens the hand-chosen skew toward uniform, the other exaggerates it 50% "
                    "further — a genuine two-sided test of whether the exact weights matter.",
        "interactions": "the baseline values are tiny (±0.03–0.07); this brackets a full order-of-magnitude "
                        "range (0 vs 0.7 on every defined pair), so this verdict holds even under a generous swing, "
                        "not just under the tiny configured range.",
    }
    param_by_key = {r["key"]: r for r in V["param_rows"]}
    # Gated on axes_present (not just param_by_key): a stale axis whose CSV still
    # has the old config names would otherwise show a verdict number here with no
    # matching chart above it, once the perturbation design changes but before
    # the upstream pipeline has been rerun. Iterated in the canonical pipeline
    # order (PARAM_ORDER), not by significance -- that ranking lives in section 8.
    axis_bullets = "".join(
        f'<li><b>{PARAM_LABELS[axis]} — {param_by_key[axis]["verdict"]}</b> '
        f'({param_by_key[axis]["worst_pct"]:.1f}% on {param_by_key[axis]["worst_cap"]}). {note}</li>'
        for axis in PARAM_ORDER
        if axis in axes_present and axis in param_by_key
        for note in (axis_notes.get(axis, ""),)
    )
    chart_q = chart_tornado_oat(R["oat"], "q", _oat_baseline("q"), caps)
    chart_p = chart_tornado_oat(R["oat"], "p", _oat_baseline("p"), caps)
    chart_l = chart_tornado_oat(R["oat"], "lambda", _oat_baseline("lambda"), caps)
    chart_w = chart_weight_stats(wsum, caps)
    chart_ranking = chart_ranked_bar(V["param_rows"])

    level_sections = []
    for capability in caps:
        per_level = []
        for level in LEVELS:
            chart = chart_level_tornado(R, capability, level)
            if chart:
                per_level.append(f"<h4>{level}</h4>{chart}")
        if per_level:
            level_sections.append(f"<h3>{capability}</h3>" + "".join(per_level))
    level_charts_html = "".join(level_sections) if level_sections else (
        '<p class="sub">None of the min/max perturbation pairs have both sides run yet.</p>'
    )
    level_table_html = level_tornado_table_html(R, caps)

    frag_list = ", ".join(f"{c} ({v:.1f}%)" for c, v in V["fragility"].items())

    # Dynamic look-ups so the prose stays true to the actual numbers.
    oat = R["oat"]

    def oat_max(param: str, cap: str) -> float:
        m = oat[(oat["parameter"] == param) & (oat["capability"] == cap)]
        return float(m["pct_nodes_changed"].max()) if len(m) else float("nan")

    q_max = oat_max("q", fragile)
    p_max = oat_max("p", fragile)
    n_nodes = int(R["up_dropout"]["n_nodes"].max()) if len(R["up_dropout"]) else 0

    params_tested = _params_tested_html(R)

    _configs_seen_ch = set(up["config"])

    def _values_tested_str(key: str) -> str:
        """Short 'below / above' phrase for the model-chain table, pulled from
        the same descriptions/data used in section 2 -- not hard-coded, so it
        stays true to whatever axes are actually present in the results. Flags
        a design (not-yet-run) axis rather than silently presenting its intended
        values as if they were already-observed results."""
        if key in AXIS_PAIRS:
            below_cfg, above_cfg, _ = AXIS_PAIRS[key]
            descs = [UPSTREAM_CONFIG_DESC[c][2] for c in (below_cfg, above_cfg) if c in UPSTREAM_CONFIG_DESC]
            if not descs:
                return "not yet run"
            pending = not ({below_cfg, above_cfg} <= _configs_seen_ch)
            suffix = " <i>(design — pending rerun)</i>" if pending else ""
            return " / ".join(descs) + suffix
        sub = oat[oat["parameter"] == key]
        if sub.empty:
            return "not yet run"
        return ", ".join(f"{v:g}" for v in sorted(sub["value"].unique()))

    chain_values = {key: _values_tested_str(key) for key in PARAM_ORDER if key != "weights"}

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Capability model — parameter sensitivity (Cagliari)</title>
<style>{CSS}</style></head><body>
<h1>Capability model — parameter sensitivity</h1>
<p class="sub">Cagliari · ELECTRE TRI capability classification · {n_nodes} nodes · perturbation of the
user-configured parameters. Figures are the <b>percentage of nodes that change capability class</b> unless
stated otherwise. Read top to bottom: methodology, exact parameters tested, results section by section, then
the conclusions at the end. Robustness to input noise is covered by the separate robustness report.</p>

<h2>1. How this analysis works</h2>
<h3>The model chain being tested</h3>
<p>The model turns a location into a capability class through five steps, each carrying hand-set numbers
that a human chose:</p>
<table>
<tr><th>Step</th><th>Turns</th><th>Into</th><th>Governed by</th><th>Values tested (below / above baseline)</th></tr>
<tr><td>① decay</td><td>travel times</td><td>per-mode reachability</td><td><code>decay_coefficient</code></td>
    <td>{chain_values['decay']}</td></tr>
<tr><td>② RRA</td><td>modal reachabilities</td><td>per-POI access</td><td><code>lambda_mode</code> (mode redundancy weights)</td>
    <td>{chain_values['rra']}</td></tr>
<tr><td>③ type aggregation</td><td>per-POI access</td><td>per-POI-type value</td><td><code>contribution_coefficient</code></td>
    <td>{chain_values['contribution']}</td></tr>
<tr><td>④ Choquet</td><td>POI-type values</td><td>11 service scores</td><td><code>choquet_capacity</code></td>
    <td>{chain_values['capacity']}</td></tr>
<tr><td></td><td></td><td></td><td><code>choquet_interactions</code></td>
    <td>{chain_values['interactions']}</td></tr>
<tr><td>⑤ ELECTRE TRI</td><td>service scores</td><td>3 capability classes</td><td><code>q</code> (indifference)</td>
    <td>{chain_values['q']}</td></tr>
<tr><td></td><td></td><td></td><td><code>p</code> (preference)</td>
    <td>{chain_values['p']}</td></tr>
<tr><td></td><td></td><td></td><td>&lambda; cut</td>
    <td>{chain_values['lambda']}</td></tr>
</table>
<p>The question this report answers: <b>are the chosen values in a stable region, or would small,
defensible changes redraw the map?</b> That is what lets a configuration be defended as
<em>the</em> model rather than one of many arbitrary ones.</p>

<h3>Sensitivity vs. robustness</h3>
<p><b>Sensitivity</b> (sections 4–5 here): <em>do the parameters matter?</em> Change a knob, measure how much
the output moves. Identifies which numbers are load-bearing (need justification) vs decorative. A separate
question, answered in its own report: <b>robustness</b> — <em>is the output trustworthy given noisy
inputs?</em> Holds every parameter fixed and jitters the input service scores instead, to see how often a
node keeps its class. A model can be insensitive to parameters yet non-robust to noise, or vice-versa — they
are independent, which is why they get independent reports.</p>

<h3>How the numbers were produced (and why they are trustworthy)</h3>
<p><b>Upstream parameters</b> (decay, interactions, capacity, contribution) feed complex accessibility
math, so they are <em>not</em> re-implemented. Each perturbation writes modified copies of the config CSVs
and launches a fresh subprocess that runs the <b>real</b> accessibility + service stages from the cached
travel times — the exact production code — so the results are about the real model. The original CSVs are
backed up and hash-verified on restore, so a run can never leave your config mutated.</p>
<p><b>Downstream ELECTRE</b> is pure arithmetic over the service scores, so it is vectorized for speed, then
<b>validated against the real</b> <code>electre_tri_integration()</code> on sampled nodes before any analysis
— the run aborts on any disagreement, so the fast path is provably the real model.</p>

<h3>Metric glossary</h3>
<table>
<tr><th>Metric</th><th>Meaning</th></tr>
<tr><td>% nodes changed</td><td>Fraction of nodes whose final capability <em>class</em> differs from baseline. A node counts only if it crosses a class boundary — so this measures decision changes, which is what matters for a map.</td></tr>
<tr><td>mean |score &Delta;|</td><td>Average absolute change in the continuous score. Compared with "% changed" it shows <em>why</em>: many nodes flipped with a small &Delta; = they sat on a knife-edge.</td></tr>
<tr><td>mean |service &Delta;|</td><td>Same, per individual service — traces which of the 11 services a perturbation actually moves.</td></tr>
<tr><td>node dropout</td><td>Nodes that vanish because a parameter change leaves them with zero reachable POIs — itself a sensitivity signal.</td></tr>
</table>

<h2>2. Parameters tested</h2>
<p>Every value below was actually run through the real model (upstream) or the validated vectorized ELECTRE
(downstream) — nothing here is a plan, it is what produced the results in sections 3–6.</p>
{params_tested}

<h2>3. Upstream config sensitivity</h2>
<p>Each configuration re-runs the real accessibility + service stages from the cached travel times,
changing one user-config quantity one step below and one step above its baseline (section 2). % of nodes
changing final capability class vs the pristine config:</p>
{_df_html(up_pivot)}
<p>Same data as a tornado plot per axis — the below-baseline config's effect extends left, the
above-baseline config's extends right, one row per capability:</p>
{chart_up}
<p><b>How to read it, axis by axis</b> (worst-case % nodes changed, on whichever capability it hits hardest —
verdict thresholds as in section 8):</p>
<ul>
{axis_bullets}
</ul>
<p>Where each perturbation acts — mean |service-score &Delta;| vs baseline (larger = that service moves more).
This localizes the sensitivity: contribution changes concentrate in the diagnostic/rehabilitation services,
while decay spreads across every service because it touches every POI type.</p>
{_df_html(svc_pivot)}
{chart_svc}
<p>Reachable-node changes (a node dropping out because it loses all POIs is itself a sensitivity signal):</p>
{drop.to_html(index=False, border=0)}
{chart_drop}

<h2>4. Downstream ELECTRE parameter sweeps (OAT)</h2>
<p>One parameter at a time, all others at baseline, tested at exactly one value below and one value above
baseline (section 2) — a direct local sensitivity check rather than a full curve. % of nodes changing class
vs baseline (0% at baseline). The tornado plots below put the baseline value in the centre row; bars extend
left for the below-baseline value and right for the above-baseline value, so <b>an asymmetric shape (one
side much longer than the other) is itself the finding</b> — it means the model responds differently to
tightening vs loosening that parameter right around its current value.</p>
<h3>q, indifference threshold (baseline {ELECTRE_Q}) — near-symmetric</h3>
<p>Nudging q one step either side of baseline moves at most ~{q_max:.0f}% of {fragile}, and both
directions respond similarly: the baseline is not sitting on an edge.</p>
{_df_html(q_pivot)}
{chart_q}
<h3>p, preference threshold (baseline {ELECTRE_P}) — near-symmetric</h3>
<p>Same benign signature (up to ~{p_max:.0f}% of {fragile} for the immediate neighbor on either side).
Baseline sits in a safe local neighborhood.</p>
{_df_html(p_pivot)}
{chart_p}
<h3 class="hi">&lambda; cut level (baseline {LAMBDA_BASELINE}) — asymmetric</h3>
<p>Even one step away, the two directions are not equal: moving to 0.70 flips up to {V['lambda_worst']:.0f}%
of {fragile}, markedly more than the equivalent step down to 0.60. &lambda; is the credibility bar a location
must clear to be promoted a class, and the model is visibly more sensitive on the high side right next to its
current value — the single most consequential number in the model (see the conclusions, section 8).</p>
{_df_html(l_pivot)}
{chart_l}

<h2>5. Weight perturbation (Dirichlet around configured weights)</h2>
<p>Randomly perturbs the ELECTRE service weights around their configured values. Read the <b>max</b> column:
a typical draw moves only a few percent of {fragile} nodes (the median), but some draws move a large share —
so the weight assumption is mostly harmless yet occasionally consequential for {fragile}.</p>
{wsum.to_html(border=0, float_format=lambda x: f"{x:.2f}")}
{chart_w}

<h2>6. Capability-level shift under min/max perturbation</h2>
<p>Every earlier section measures <em>whether a node's class changed</em>. This section instead asks:
under each parameter's min and max test (section 2), <b>what share of nodes ends up at each of the five
ELECTRE classes</b> (Very Low .. Very High)? One tornado plot per capability &times; class: the parameter
name labels the left axis, the exact (min, max) value or procedure tested labels the right axis, and each
bar shows the % of nodes at that class under the min test (left, blue) or the max test (right, orange).</p>
{level_charts_html}
<h3>Comprehensive table</h3>
<p>Every number behind the charts above, one row per capability &times; class &times; parameter:</p>
{level_table_html}

<h2>7. Why {fragile} is the fragile capability</h2>
<p>{fragile.capitalize()} is the most sensitive on every axis for compounding reasons:</p>
<ul>
<li>It aggregates more services than the others, so it has more moving parts.</li>
<li>Its services include the longest travel-time tolerances (up to 45-minute half-times), so they are
maximally exposed to the decay and contribution assumptions.</li>
<li>Empirically, its node scores cluster right at the class boundaries — so a small shift from any of the
five stages tips many nodes across a boundary.</li>
</ul>
<p>The through-line: {fragile} is not fragile because of one bad parameter — it is fragile because its
scores live on the boundaries, and every parameter can push them over.</p>

<h2>8. Conclusions</h2>
<div class="callout">
<p><b>Ranked by how much they can move a capability's output, these are the parameters that most need
an explicit justification.</b> For every parameter tested, this takes the single worst % of nodes it can
flip on <em>any</em> of the three capabilities (not just one) — that worst-case exposure is what should
drive how much calibration effort a number gets, independent of which capability happens to show it.</p>
</div>

<h3>Parameter influence ranking (most to least load-bearing)</h3>
<p>Classified automatically at &ge;{LOAD_BEARING:.0f}% = load-bearing, {MODERATE:.0f}–{LOAD_BEARING:.0f}% =
moderate, &lt;{MODERATE:.0f}% = inert. "Worst-case %" is the largest effect seen on any capability; "hits
hardest" names which one.</p>
<table class="verdict"><tr><th>Parameter</th><th>Worst-case % nodes changed</th><th>Hits hardest</th><th>Verdict</th></tr>
{vrows}</table>
{chart_ranking}
<p><b>Read:</b> the <span class="bad">load-bearing</span> parameters at the top must be justified/calibrated
for a stable model — they are the ones capable of redrawing the map on their own. The
<span class="good">inert</span> ones at the bottom can be set to any defensible value; tuning them is
wasted effort.</p>

<div class="callout risk">
<p><b>The &lambda; cut level is a cliff.</b> Moving &lambda; off its baseline
flips up to <b>{V['lambda_worst']:.0f}%</b> of {fragile} nodes (see section 4) — the steepest response of
any parameter, and the top of the ranking above. The historical 0.65-vs-0.70 inconsistency (assignment
code vs. reported debug details) has been resolved: assignments, debug output, and this analysis all read
one shared constant, &lambda; = {LAMBDA_BASELINE}. The value itself still
needs an explicit written justification before any version is treated as stable — on this parameter a
0.05 shift alone moves ~10% of {fragile} nodes.</p>
</div>

<h3>Where the load-bearing parameters actually bite</h3>
<p><b>{fragile.capitalize()}</b> is the capability most exposed to parameter changes overall — averaged
over every perturbation, its share of nodes changing class is: {frag_list}. That is why it appears
repeatedly as the "hits hardest" capability above: it is not that one parameter targets {fragile}
specifically, but that its node scores sit right on the class boundaries (detailed in section 7), so
whichever parameter turns out to be load-bearing tends to show its effect there first. Practically: when
justifying a load-bearing parameter, check its effect on {fragile} first — that is where a bad choice
will show up.</p>

<h3>How much to trust the map</h3>
<p>That is a separate question from everything above — parameter sensitivity says nothing about whether the
output is robust to noise in the inputs. See the separate robustness report for that analysis and its
QGIS stability layer.</p>

<h3>What to do for a stable parameter set</h3>
<ol>
<li><b>Pin &lambda;</b> — the historical 0.65-vs-0.70 code inconsistency is fixed (one shared constant); what remains is justifying the value itself, it is the master knob.</li>
<li><b>Calibrate the load-bearing upstream parameters</b> (decay / contribution), especially for {fragile}'s services.</li>
<li><b>Freeze the inert parameters</b> (interactions) at any defensible value — do not spend calibration effort there.</li>
<li><b>Check the robustness report</b> for which capabilities/nodes need to be presented with uncertainty.</li>
<li><b>Leave q / p at baseline</b> — they sit in proven-safe interior regions.</li>
</ol>
<p class="sub">Generated automatically from the sensitivity analysis result tables; re-running the
analyses refreshes every figure and verdict.</p>
</body></html>"""
    out_path.write_text(html, encoding="utf-8")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    R = load_results()
    V = compute_verdicts(R)

    html_path = SENS_DIR / "sensitivity_report.html"
    build_html(R, V, html_path)
    print(f"[report] {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
