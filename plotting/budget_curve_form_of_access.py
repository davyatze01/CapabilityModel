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
RESOURCES_AXIS_MAX = 1400.0
RESOURCES_THRESHOLD = 360.0
UPPER_RESOURCES_THRESHOLD = 840.0
N_POINTS = 500
CM_TO_INCH = 1 / 2.54
THRESHOLD_COLOR = "#C000D9"
INCOME = 1200.0
HOUSING_OPPORTUNITIES = {"buying": 15.0, "renting": 19.0}

ACCESS_CURVES = (
    # (resources intercept, slope, color, line width, description)
    (INCOME, HOUSING_OPPORTUNITIES["renting"], "#0071B2", 1.0, "renting"),
    (INCOME, HOUSING_OPPORTUNITIES["buying"], "#FFB300", 1, "buying"),
)


def plot_form_of_access() -> Figure:
    fig, ax = plt.subplots(
        figsize=(10 * CM_TO_INCH, 10 * CM_TO_INCH),
        dpi=300,
    )

    for resources_intercept, slope, curve_color, curve_linewidth, description in ACCESS_CURVES:
        surface_intercept = resources_intercept / slope
        curve_surface = np.linspace(0.0, surface_intercept, N_POINTS)
        curve_resources = resources_intercept - slope * curve_surface

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

        for threshold in (RESOURCES_THRESHOLD, UPPER_RESOURCES_THRESHOLD):
            intersection_surface = (resources_intercept - threshold) / slope
            ax.plot(
                [intersection_surface, intersection_surface],
                [0.0, threshold],
                color=curve_color,
                linewidth=0.8,
                linestyle="--",
                alpha=1,
                zorder=1,
            )

    ax.axhline(
        RESOURCES_THRESHOLD,
        color=THRESHOLD_COLOR,
        linewidth=0.5,
        linestyle="--",
        alpha=1,
        zorder=1,
    )
    ax.text(
        SURFACE_AXIS_MAX - 2.0,
        RESOURCES_THRESHOLD - 15.0,
        r"$30\%\ \iota_{\mathrm{res}}$",
        color=THRESHOLD_COLOR,
        fontsize=9,
        ha="right",
        va="top",
    )
    ax.axhline(
        UPPER_RESOURCES_THRESHOLD,
        color=THRESHOLD_COLOR,
        linewidth=0.5,
        linestyle="--",
        alpha=1,
        zorder=1,
    )
    ax.text(
        SURFACE_AXIS_MAX - 2.0,
        UPPER_RESOURCES_THRESHOLD + 15.0,
        r"$70\%\ \iota_{\mathrm{res}}$",
        color=THRESHOLD_COLOR,
        fontsize=9,
        ha="right",
        va="bottom",
    )

    ax.set_xlim(0.0, SURFACE_AXIS_MAX)
    ax.set_ylim(0.0, RESOURCES_AXIS_MAX)
    ax.set_xlabel(r"Affordable surface ($ς_{\mathrm{aff}}$) [sqm]", fontsize=10)
    ax.set_ylabel(r"Monthly residual resources ($\iota_{\mathrm{res}}$) [€]", fontsize=10)

    ax.grid(True, color="#d9d9d9", linewidth=0.8, linestyle="--", alpha=0.75)
    ax.tick_params(axis="both", labelsize=10, colors="#404040")

    for spine in ax.spines.values():
        spine.set_color("#808080")
        spine.set_linewidth(0.9)

    legend = ax.legend(frameon=True, fontsize=10, loc="upper right")
    legend.get_frame().set_edgecolor("#d9d9d9")
    legend.get_frame().set_linewidth(1.0)
    legend.get_frame().set_facecolor("white")

    fig.tight_layout()
    return fig


if __name__ == "__main__":
    output_folder = Path(__file__).resolve().parent

    plot_form_of_access()
    plt.savefig(output_folder / "budget_curve_form_of_access.png", dpi=300)
    plt.show()
    plt.close()
