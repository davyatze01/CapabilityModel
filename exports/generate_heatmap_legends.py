"""Generate standalone transparent PNG legends for the QGIS per-service heatmaps.

Outputs one cropped, transparent-background PNG per capability:
- service_legend_<capability>.png: a continuous color bar per capability
  (nutrition, care, restorativeness), using the same 5 white->color stops at
  t=0.2..1.0 baked into the QGIS per-service fill expression in
  pipeline_runner.py, values below 0.2 flat at the first stop, linear blending
  between stops.

For the *capability grid* legend (the 5 discrete ELECTRE-class shades with
per-level outlines, in square and hexagon variants), see
generate_capability_legend.py -- that is the grid's legend now that the diagonal
hatch has been removed from the pipeline.

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

from utils.capabilities import CAPABILITY_COLORS, CAPABILITY_SHADE_FRACTIONS

# Alias to the shared name used throughout this module.
STOP_FRACTIONS = CAPABILITY_SHADE_FRACTIONS


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
        ax.set_title(capability.capitalize(), fontsize=12, pad=10)
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
    generate_service_legends(args.output_dir)
