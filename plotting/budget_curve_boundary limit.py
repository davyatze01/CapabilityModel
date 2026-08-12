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
mpl.rcParams["mathtext.cal"] = r"Garamond\-Math"
mpl.rcParams["mathtext.sf"] = r"Garamond\-Math"
mpl.rcParams["mathtext.tt"] = r"Garamond\-Math"

# -----------------------------
# SETUP
# -----------------------------

SURFACE_AXIS_MAX = 100.0
RESOURCES_INTERCEPT = 1.0
RESOURCES_AXIS_MAX = 1.0
RESOURCES_THRESHOLD = 0.3
N_POINTS = 500
CM_TO_INCH = 1 / 2.54
THRESHOLD_COLOR = "#0400D9"
BOUNDARY_COLOR =  "#0400D9"
BOUNDARY_THRESHOLD_INTERSECTIONS = (28.0, 40.0, 56.0, 66.0)
INTERMEDIATE_CURVES_PER_INTERVAL = 6
INTERVAL_COLORS = (
    "#D73027",  # red: below the first boundary
    "#F46D43",  # orange-red
    "#FEE08B",  # yellow
    "#A6D96A",  # yellow-green
    "#1A9850",  # green: beyond the last boundary
)

def plot_form_of_access() -> Figure:
    fig, ax = plt.subplots(
        figsize=(10 * CM_TO_INCH, 10 * CM_TO_INCH),
        dpi=300,
    )

    def draw_split_curve(
        threshold_surface: float,
        color: str,
        linewidth: float,
        upper_alpha: float,
        zorder: float,
        label: str = "_nolegend_",
    ) -> None:
        slope = (RESOURCES_INTERCEPT - RESOURCES_THRESHOLD) / threshold_surface
        surface_intercept = RESOURCES_INTERCEPT / slope
        upper_surface = np.linspace(0.0, threshold_surface, N_POINTS)
        lower_surface = np.linspace(threshold_surface, surface_intercept, N_POINTS)
        ax.plot(
            upper_surface,
            RESOURCES_INTERCEPT - slope * upper_surface,
            color=color,
            linewidth=linewidth,
            alpha=upper_alpha,
            label=label,
            zorder=zorder,
        )
        ax.plot(
            lower_surface,
            RESOURCES_INTERCEPT - slope * lower_surface,
            color=color,
            linewidth=linewidth,
            alpha=0.3,
            zorder=zorder,
        )

    for interval_index, (left_boundary, right_boundary) in enumerate(zip(
        BOUNDARY_THRESHOLD_INTERSECTIONS[:-1],
        BOUNDARY_THRESHOLD_INTERSECTIONS[1:],
    ), start=1):
        intermediate_intersections = np.linspace(
            left_boundary,
            right_boundary,
            INTERMEDIATE_CURVES_PER_INTERVAL + 2,
        )[1:-1]
        for threshold_surface in intermediate_intersections:
            draw_split_curve(
                threshold_surface,
                INTERVAL_COLORS[interval_index],
                0.5,
                0.75,
                1.5,
            )

    first_interval_width = (
        BOUNDARY_THRESHOLD_INTERSECTIONS[1]
        - BOUNDARY_THRESHOLD_INTERSECTIONS[0]
    )
    lower_intersections = np.linspace(
        BOUNDARY_THRESHOLD_INTERSECTIONS[0] - first_interval_width,
        BOUNDARY_THRESHOLD_INTERSECTIONS[0],
        INTERMEDIATE_CURVES_PER_INTERVAL + 2,
    )[1:-1]
    for threshold_surface in lower_intersections:
        draw_split_curve(threshold_surface, INTERVAL_COLORS[0], 0.5, 0.75, 1.5)

    final_interval_width = (
        BOUNDARY_THRESHOLD_INTERSECTIONS[-1]
        - BOUNDARY_THRESHOLD_INTERSECTIONS[-2]
    )
    outer_intersections = np.linspace(
        BOUNDARY_THRESHOLD_INTERSECTIONS[-1],
        BOUNDARY_THRESHOLD_INTERSECTIONS[-1] + final_interval_width,
        INTERMEDIATE_CURVES_PER_INTERVAL + 2,
    )[1:-1]
    for threshold_surface in outer_intersections:
        draw_split_curve(threshold_surface, INTERVAL_COLORS[-1], 0.5, 0.75, 1.5)

    for boundary_index, threshold_surface in enumerate(BOUNDARY_THRESHOLD_INTERSECTIONS):
        draw_split_curve(
            threshold_surface,
            BOUNDARY_COLOR,
            0.6,
            0.75,
            2,
            "boundary curves" if boundary_index == 0 else "_nolegend_",
        )
        ax.plot(
            [threshold_surface, threshold_surface],
            [0.0, RESOURCES_THRESHOLD],
            color=BOUNDARY_COLOR,
            linewidth=0.6,
            linestyle="--",
            alpha=0.75,
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
        RESOURCES_THRESHOLD - 0.015,
        r"$30\%\ \iota_{\mathrm{res}}$",
        color=THRESHOLD_COLOR,
        fontsize=9,
        ha="right",
        va="top",
    )
    ax.set_xlim(0.0, SURFACE_AXIS_MAX)
    ax.set_ylim(0.0, RESOURCES_AXIS_MAX)
    x_ticks = (0.0, *BOUNDARY_THRESHOLD_INTERSECTIONS, SURFACE_AXIS_MAX)
    ax.set_xticks(x_ticks)
    ax.set_xticklabels([f"{tick:g}" for tick in x_ticks])
    interval_bounds = (0.0, *BOUNDARY_THRESHOLD_INTERSECTIONS, SURFACE_AXIS_MAX)
    for interval_index, (left_bound, right_bound) in enumerate(
        zip(interval_bounds[:-1], interval_bounds[1:]),
        start=1,
    ):
        ax.text(
            (left_bound + right_bound) / 2,
            -0.09,
            rf"$Q_{{{interval_index}}}$",
            color=INTERVAL_COLORS[interval_index - 1],
            fontsize=9,
            ha="center",
            va="top",
            transform=ax.get_xaxis_transform(),
            clip_on=False,
        )
    ax.set_xlabel(
        r"Affordable surface ($ς_{\mathrm{aff}}$) [sqm]",
        fontsize=10,
        labelpad=28,
    )
    ax.set_ylabel(r"Normalized residual resources ($\hat{\iota}_{\mathrm{res}}$)", fontsize=10)

    ax.tick_params(axis="both", labelsize=10, colors="#404040")
    for tick_value, tick_label in zip(x_ticks, ax.get_xticklabels()):
        if tick_value in BOUNDARY_THRESHOLD_INTERSECTIONS:
            tick_label.set_color(BOUNDARY_COLOR)

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
    plt.savefig(output_folder / "budget_curve_boundary limit.png", dpi=300)
    plt.show()
    plt.close()

