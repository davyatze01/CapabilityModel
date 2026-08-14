from __future__ import annotations

from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
import numpy as np

# -----------------------------
# STYLE
# -----------------------------

mpl.rcParams["font.family"] = "Garamond-Math"
mpl.rcParams["font.serif"] = ["Garamond-Math"]
mpl.rcParams["font.size"] = 11
mpl.rcParams["mathtext.fontset"] = "custom"
mpl.rcParams["mathtext.rm"] = r"Garamond\-Math"
mpl.rcParams["mathtext.it"] = r"Garamond\-Math:italic"
mpl.rcParams["mathtext.bf"] = r"Garamond\-Math:bold"

# -----------------------------
# SETUP
# -----------------------------

SURFACE_AXIS_MAX = 100.0
RESOURCES_THRESHOLD = 360.0
UPPER_RESOURCES_THRESHOLD = 840.0
N_POINTS = 500
CM_TO_INCH = 1 / 2.54
THRESHOLD_COLOR = "#C000D9"
INCOME = 1200.0
HOUSING_OPPORTUNITIES = {"buying": 15.0, "renting": 19.0}
HOUSING_BOUNDARIES = [28.0, 40.0, 56.0, 66.0]  # sqm, affordable-surface cut points at 30% of income
HOUSING_CATEGORIES = ["Q1", "Q2", "Q3", "Q4", "Q5"]
HOUSING_Q = 2.0   # sqm, indifference threshold
HOUSING_P = 6.0   # sqm, preference threshold
HOUSING_LAMBDA_CUT = 0.75  # minimum outranking credibility to be assigned above a boundary
HOUSING_WEIGHTS = {"renting": 0.5, "buying": 0.5}  # uniform by default

ACCESS_CURVES = (
    # (resources intercept, slope, color, line width, description)
    (INCOME, HOUSING_OPPORTUNITIES["renting"], "#0071B2", 1.0, "renting"),
    (INCOME, HOUSING_OPPORTUNITIES["buying"], "#FFB300", 1, "buying"),
)


def electre_tri_classify_housing(t30_res_by_curve: dict[str, float]) -> str:
    """ELECTRE TRI classification of housing affordability, combining renting and
    buying into one category instead of scoring each separately -- treats them as
    two criteria of the same household situation. No veto (assumes an infinite
    veto threshold): with only two criteria that represent the same household's two
    access routes, there isn't yet a case for one to veto the other.
    """
    assigned_idx = 0
    for k, boundary in enumerate(HOUSING_BOUNDARIES):
        weighted_concordance = 0.0
        weight_sum = 0.0
        for description, t30_res in t30_res_by_curve.items():
            w = HOUSING_WEIGHTS.get(description, 1.0 / len(t30_res_by_curve))
            d = t30_res - boundary
            if d >= -HOUSING_Q:
                c_j = 1.0
            elif d <= -HOUSING_P:
                c_j = 0.0
            else:
                c_j = (d + HOUSING_P) / (HOUSING_P - HOUSING_Q)
            weighted_concordance += w * c_j
            weight_sum += w
        credibility = weighted_concordance / weight_sum
        if credibility >= HOUSING_LAMBDA_CUT:
            assigned_idx = k + 1
    return HOUSING_CATEGORIES[assigned_idx]


def plot_form_of_access() -> Figure:
    fig, ax = plt.subplots(
        figsize=(10 * CM_TO_INCH, 10 * CM_TO_INCH),
        dpi=300,
    )

    t30_res_by_curve: dict[str, float] = {}

    for resources_intercept, slope, curve_color, curve_linewidth, description in ACCESS_CURVES:
        surface_intercept = resources_intercept / slope
        curve_surface = np.linspace(0.0, surface_intercept, N_POINTS)
        curve_resources = (resources_intercept - slope * curve_surface) / INCOME

        curve_label = (
            rf"$\iota_{{\mathrm{{res}}}} = {resources_intercept:g}"
            rf" - {slope:g}\varsigma$  {description}"
        )
        ax.plot(
            curve_surface,
            curve_resources,
            color=curve_color,
            linewidth=curve_linewidth,
            alpha=1.0,
            label=curve_label,
        )

        for threshold in (RESOURCES_THRESHOLD,):  # UPPER_RESOURCES_THRESHOLD (70%) disabled, see below
            intersection_surface = (resources_intercept - threshold) / slope
            if threshold == RESOURCES_THRESHOLD:
                t30_res_by_curve[description] = intersection_surface
            ax.plot(
                [intersection_surface, intersection_surface],
                [0.0, threshold / INCOME],
                color=curve_color,
                linewidth=0.8,
                linestyle="--",
                alpha=1,
                zorder=1,
            )

    ax.axhline(
        RESOURCES_THRESHOLD / INCOME,
        color=THRESHOLD_COLOR,
        linewidth=0.5,
        linestyle="--",
        alpha=1,
        zorder=1,
    )
    ax.text(
        SURFACE_AXIS_MAX - 2.0,
        RESOURCES_THRESHOLD / INCOME - 0.0125,
        r"$30\%\ \iota_{\mathrm{res}}$",
        color=THRESHOLD_COLOR,
        fontsize=9,
        ha="right",
        va="top",
    )
    # 70% threshold disabled on this plot -- keeping the code for whenever it's needed again.
    # ax.axhline(
    #     UPPER_RESOURCES_THRESHOLD / INCOME,
    #     color=THRESHOLD_COLOR,
    #     linewidth=0.5,
    #     linestyle="--",
    #     alpha=1,
    #     zorder=1,
    # )
    # ax.text(
    #     SURFACE_AXIS_MAX - 2.0,
    #     UPPER_RESOURCES_THRESHOLD / INCOME + 0.0125,
    #     r"$70\%\ \iota_{\mathrm{res}}$",
    #     color=THRESHOLD_COLOR,
    #     fontsize=9,
    #     ha="right",
    #     va="bottom",
    # )

    ax.set_xlim(0.0, SURFACE_AXIS_MAX)
    ax.set_ylim(0.0, 1.05)
    ax.set_xlabel(r"Affordable surface ($ς_{\mathrm{aff}}$) [sqm]", fontsize=10, labelpad=30)
    ax.set_ylabel(r"Monthly residual resources ($\iota_{\mathrm{res}}$) [fraction of income]", fontsize=10)

    ax.tick_params(axis="both", labelsize=10, colors="#404040")

    for spine in ax.spines.values():
        spine.set_color("#808080")
        spine.set_linewidth(0.9)

    legend = ax.legend(frameon=True, fontsize=10, loc="upper right")
    legend.get_frame().set_edgecolor("#d9d9d9")
    legend.get_frame().set_linewidth(1.0)
    legend.get_frame().set_facecolor("white")

    category = electre_tri_classify_housing(t30_res_by_curve)
    band_edges = [0.0] + HOUSING_BOUNDARIES + [SURFACE_AXIS_MAX]
    category_idx = HOUSING_CATEGORIES.index(category)
    ax.axvspan(band_edges[category_idx], band_edges[category_idx + 1], color="#4444CC", alpha=0.12, zorder=-1)

    for boundary in band_edges:  # includes 0 and SURFACE_AXIS_MAX, so Q1/Q5 are bounded too
        ax.axvline(boundary, color="#4444CC", linewidth=0.7, linestyle="-", alpha=0.6, zorder=0)

    for label, lo, hi in zip(HOUSING_CATEGORIES, band_edges[:-1], band_edges[1:]):
        is_assigned = label == category
        ax.text(
            (lo + hi) / 2,
            -0.14,
            label,
            transform=ax.get_xaxis_transform(),
            color="#4444CC",
            fontsize=12 if is_assigned else 9,
            fontweight="bold" if is_assigned else "normal",
            ha="center",
            va="top",
            bbox=(
                dict(facecolor="#4444CC", alpha=0.15, edgecolor="none", boxstyle="round,pad=0.3")
                if is_assigned
                else None
            ),
        )

    print("[Housing] Affordability classification (30% of income threshold, renting+buying combined):")
    for description, t30_res in t30_res_by_curve.items():
        print(f"[Housing]   {description}: t30_res={t30_res:.1f} sqm", flush=True)
    print(f"[Housing]   combined category -> {category}", flush=True)

    fig.tight_layout()
    return fig


if __name__ == "__main__":
    output_folder = Path(__file__).resolve().parent

    plot_form_of_access()
    plt.savefig(output_folder / "housing_affordability.png", dpi=300)
    plt.show()
    plt.close()
