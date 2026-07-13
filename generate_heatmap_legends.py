"""Generate standalone transparent PNG legends for the QGIS heatmap layers.

Outputs one cropped, transparent-background PNG per legend:
- capability_hatch_legend.png: the capability grid's five ELECTRE TRI classes.
  The swatches replicate the actual QGIS symbology from pipeline_runner.py
  (QgsLinePatternFillSymbolLayer: 45-degree black lines, no fill, per-class
  line spacing AND stroke thickness as fractions of the grid cell size),
  not matplotlib's stylized hatch patterns.
- service_legend_<capability>.png: one color bar per capability (nutrition,
  care, restorativeness), using the same 5 white->color stops at t=0.2..1.0
  baked into the QGIS fill expression, values below 0.2 flat at the first
  stop, linear blending between stops.

NOTE on between-stop blending: pipeline_runner.py blends stops with
color_mix(c1, c2, ratio * 100), but QGIS documents color_mix's ratio as 0-1.
These bars draw the *intended* smooth blend; verify in QGIS whether the
rendered grid actually matches (see discussion in pipeline_runner.py).
"""

import argparse
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, ListedColormap, to_rgb
from matplotlib.patches import Rectangle

# Must stay in sync with electre_labels / electre_bounds in pipeline_runner.py.
HATCH_LABELS = ["Very Low", "Low", "Medium", "High", "Very High"]
# Must stay in sync with hatch_distance_fractions / hatch_line_width_fractions
# in pipeline_runner.py: line spacing and stroke width per class, as fractions
# of the grid cell size (~2 -> 5 lines per cell, ink coverage ~8% -> ~62%).
HATCH_DISTANCE_FRACTIONS = [1 / 2, 1 / 2.5, 1 / 3, 1 / 4, 1 / 5]
HATCH_WIDTH_FRACTIONS = [1 / 25, 1 / 17, 1 / 12, 1 / 10, 1 / 8]

# Must stay in sync with capability_grid_colors / _STOP_FRACTIONS in
# pipeline_runner.py.
CAPABILITY_COLORS = {
    "Nutrition": "#FFA200",
    "Care": "#EB4CCC",
    "Restorativeness": "#006BFF",
}
STOP_FRACTIONS = [0.2, 0.4, 0.6, 0.8, 1.0]


def _hatch_swatch_image(cls: int, npx: int = 400) -> np.ndarray:
    """One grid cell's worth of the QGIS line pattern as an RGBA raster:
    45-degree black stripes, per-class spacing/width, transparent elsewhere."""
    spacing = HATCH_DISTANCE_FRACTIONS[cls]
    width = HATCH_WIDTH_FRACTIONS[cls]
    xs = (np.arange(npx) + 0.5) / npx
    xx, yy = np.meshgrid(xs, xs)
    # Perpendicular coordinate of each pixel w.r.t. the 45-degree stripe
    # direction (n = (-1, 1)/sqrt2), phased so a stripe crosses the center.
    d = (yy - xx) / np.sqrt(2)
    frac = np.mod(d + width / 2, spacing)
    ink = frac < width
    rgba = np.zeros((npx, npx, 4))
    rgba[..., 3] = ink.astype(float)
    return rgba


def _draw_hatch_swatch(ax, x0: float, y0: float, size: float, cls: int) -> None:
    ax.imshow(
        _hatch_swatch_image(cls),
        extent=[x0, x0 + size, y0, y0 + size],
        origin="lower",
        interpolation="antialiased",
        zorder=2,
    )
    ax.add_patch(
        Rectangle(
            (x0, y0),
            size,
            size,
            facecolor="none",
            edgecolor="black",
            linewidth=0.8,
            zorder=3,
        )
    )


def _service_cmap(color_hex: str, n: int = 256) -> ListedColormap:
    """Reproduce the QGIS stop expression as a matplotlib colormap."""
    shade = LinearSegmentedColormap.from_list("shade", ["white", color_hex])
    stops = np.array([to_rgb(shade(f)) for f in STOP_FRACTIONS])
    values = np.linspace(0.0, 1.0, n)
    colors = np.empty((n, 3))
    for i, v in enumerate(values):
        if v <= STOP_FRACTIONS[0]:
            colors[i] = stops[0]
            continue
        for j in range(1, len(STOP_FRACTIONS)):
            lo, hi = STOP_FRACTIONS[j - 1], STOP_FRACTIONS[j]
            if v <= hi:
                t = (v - lo) / (hi - lo)
                colors[i] = stops[j - 1] * (1 - t) + stops[j] * t
                break
        else:
            colors[i] = stops[-1]
    return ListedColormap(colors)


def _save(fig, path: Path) -> None:
    fig.savefig(path, dpi=200, transparent=True, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    print(f"Wrote {path}")


def generate_hatch_legend(output_dir: Path) -> None:
    n = len(HATCH_LABELS)
    swatch = 0.9 / n  # square side; the remaining 0.1 is inter-row gaps
    fig, ax = plt.subplots(figsize=(2.6, 3.2))
    for i, label in enumerate(HATCH_LABELS):
        y0 = i * (1.0 / n)
        _draw_hatch_swatch(ax, 0.0, y0, swatch, i)
        ax.text(
            swatch * 1.25,
            y0 + swatch / 2,
            label,
            va="center",
            ha="left",
            fontsize=11,
        )
    ax.set_xlim(-0.01, 0.85)
    ax.set_ylim(-0.02, 1.0)
    ax.set_aspect("equal")
    ax.set_axis_off()
    ax.set_title("Capability Level", fontsize=13, pad=12)
    _save(fig, output_dir / "capability_hatch_legend.png")


def generate_service_legends(output_dir: Path) -> None:
    gradient = np.linspace(0, 1, 256).reshape(-1, 1)
    for capability, color_hex in CAPABILITY_COLORS.items():
        fig, ax = plt.subplots(figsize=(1.3, 4.6))
        ax.imshow(
            gradient,
            aspect="auto",
            cmap=_service_cmap(color_hex),
            origin="lower",
            extent=[0, 1, 0, 1],
        )
        ax.set_xticks([])
        ax.set_yticks([0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
        ax.tick_params(labelsize=9)
        for spine in ax.spines.values():
            spine.set_linewidth(0.8)
        ax.set_title(capability, fontsize=12, pad=10)
        _save(fig, output_dir / f"service_legend_{capability.lower()}.png")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/legends"),
        help="Directory to write the legend PNGs to.",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    generate_hatch_legend(args.output_dir)
    generate_service_legends(args.output_dir)
