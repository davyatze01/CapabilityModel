from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running directly (python housing/housing_affordability.py) -- otherwise
# sys.path[0] is housing/, not the repo root, so `import housing...` below fails.
# Mirrors the bootstrap in ops/*.py, analysis/scenarios.py, and
# analysis/sensitivity_upstream.py.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.figure import Figure
import numpy as np

from housing.housing_capability import (
    INCOME,
    HOUSING_OPPORTUNITIES,
    CLASSIFICATION_RESIDUAL,
    classify_housing_opportunities,
)

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

# If true, main plots one figure per OMI zone (using that zone's buy/rent prices
# from Cagliari_OMI.gpkg instead of the hardcoded HOUSING_OPPORTUNITIES) -- each
# saved individually under plots/, plus one composite figure with all of them
# shown on screen. If false, the single hardcoded-opportunities plot (today's
# behavior) is shown/saved as before.
PLOT_ALL_OMI_ZONES = True


def _access_curves(opportunities: dict[str, float]) -> tuple[tuple[float, float, str, float, str], ...]:
    return (
        (INCOME, opportunities["renting"], "#0071B2", 1.0, "renting"),
        (INCOME, opportunities["buying"], "#FFB300", 1, "buying"),
    )


def plot_form_of_access(
    ax: Axes | None = None,
    opportunities: dict[str, float] = HOUSING_OPPORTUNITIES,
    title: str | None = None,
    compact: bool = False,
) -> Figure | None:
    fig = None
    if ax is None:
        fig, ax = plt.subplots(
            figsize=(10 * CM_TO_INCH, 10 * CM_TO_INCH),
            dpi=300,
        )

    for resources_intercept, slope, curve_color, curve_linewidth, description in _access_curves(opportunities):
        surface_intercept = resources_intercept / slope
        curve_surface = np.linspace(0.0, surface_intercept, N_POINTS)
        curve_resources = (resources_intercept - slope * curve_surface) / INCOME

        curve_label = (
            rf"$\iota_{{\mathrm{{res}}}} = {resources_intercept:g}"
            rf" - {slope:.2f}\varsigma$  {description}"
        )
        ax.plot(
            curve_surface,
            curve_resources,
            color=curve_color,
            linewidth=curve_linewidth,
            alpha=1.0,
            label=curve_label,
        )

        for threshold in (RESOURCES_THRESHOLD, UPPER_RESOURCES_THRESHOLD):
            intersection_surface = (resources_intercept - threshold) / slope
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
    ax.axhline(
        UPPER_RESOURCES_THRESHOLD / INCOME,
        color=THRESHOLD_COLOR,
        linewidth=0.5,
        linestyle="--",
        alpha=1,
        zorder=1,
    )
    ax.text(
        SURFACE_AXIS_MAX - 2.0,
        UPPER_RESOURCES_THRESHOLD / INCOME + 0.0125,
        r"$70\%\ \iota_{\mathrm{res}}$",
        color=THRESHOLD_COLOR,
        fontsize=9,
        ha="right",
        va="bottom",
    )

    ax.set_xlim(0.0, SURFACE_AXIS_MAX)
    ax.set_ylim(0.0, 1.05)
    ax.set_xlabel(r"Affordable surface ($ς_{\mathrm{aff}}$) [sqm]", fontsize=10)
    ylabel = r"$\iota_{\mathrm{res}}$ [frac. income]" if compact else r"Monthly residual resources ($\iota_{\mathrm{res}}$) [fraction of income]"
    ax.set_ylabel(ylabel, fontsize=10)

    ax.tick_params(axis="both", labelsize=10, colors="#404040")

    for spine in ax.spines.values():
        spine.set_color("#808080")
        spine.set_linewidth(0.9)

    legend = ax.legend(frameon=True, fontsize=10, loc="upper right")
    legend.get_frame().set_edgecolor("#d9d9d9")
    legend.get_frame().set_linewidth(1.0)
    legend.get_frame().set_facecolor("white")

    classification_surface_by_curve, category = classify_housing_opportunities(opportunities)

    label = title or "baseline"
    print(f"[Housing] {label}: affordability classification ({CLASSIFICATION_RESIDUAL:g}€ residual threshold, renting+buying combined):")
    for description, surface in classification_surface_by_curve.items():
        print(f"[Housing]   {description}: surface={surface:.1f} sqm", flush=True)
    print(f"[Housing]   combined category -> {category}", flush=True)

    if title is not None:
        ax.set_title(title, fontsize=10)

    if fig is not None:
        fig.tight_layout()
    return fig


if __name__ == "__main__":
    output_folder = Path(__file__).resolve().parent

    if not PLOT_ALL_OMI_ZONES:
        plot_form_of_access()
        plt.savefig(output_folder / "housing_affordability.png", dpi=300)
        plt.show()
        plt.close()
    else:
        import math
        import geopandas as gpd

        zones = gpd.read_file(output_folder / "Cagliari_OMI.gpkg")
        zones = zones[zones["omi_sale_monthly"].notna() & zones["omi_rent_final"].notna()]
        zones = zones.sort_values("CODZONA").reset_index(drop=True)

        plots_dir = output_folder / "plots"
        plots_dir.mkdir(exist_ok=True)

        n = len(zones)
        ncols = math.ceil(math.sqrt(n))
        nrows = math.ceil(n / ncols)
        composite_fig, composite_axes = plt.subplots(
            nrows, ncols, figsize=(ncols * 8 * CM_TO_INCH, nrows * 8 * CM_TO_INCH), dpi=150
        )
        composite_axes = np.atleast_1d(composite_axes).flatten()

        zone_codes: list[str] = [str(v) for v in zones["CODZONA"]]
        sale_monthly_values: list[float] = [float(v) for v in zones["omi_sale_monthly"]]
        rent_final_values: list[float] = [float(v) for v in zones["omi_rent_final"]]

        for i, (zone_code, sale_monthly, rent_final) in enumerate(
            zip(zone_codes, sale_monthly_values, rent_final_values)
        ):
            opportunities: dict[str, float] = {"buying": sale_monthly, "renting": rent_final}

            indiv_fig, indiv_ax = plt.subplots(figsize=(10 * CM_TO_INCH, 10 * CM_TO_INCH), dpi=300)
            plot_form_of_access(ax=indiv_ax, opportunities=opportunities, title=zone_code)
            indiv_fig.tight_layout()
            indiv_fig.savefig(plots_dir / f"housing_affordability_{zone_code}.png", dpi=300)
            plt.close(indiv_fig)

            plot_form_of_access(ax=composite_axes[i], opportunities=opportunities, title=zone_code, compact=True)

        for j in range(n, len(composite_axes)):
            composite_axes[j].axis("off")

        composite_fig.tight_layout()
        plt.show()
        plt.close(composite_fig)
