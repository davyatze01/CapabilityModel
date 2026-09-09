"""Generate a standalone transparent PNG legend for the QGIS per-service heatmaps.

Outputs one cropped, transparent-background PNG:
- service_legend.png: a stepped color bar using the shared 10-color
  SERVICE_COLOR_STOPS scale baked into the QGIS per-service fill expression in
  pipeline_runner.py -- one flat color per 0.1-wide bucket, no blending between
  stops. Every service uses this exact bar now (no per-capability hue any more).

For the *capability grid* legend (the 5 discrete ELECTRE-class shades with
per-level outlines, in square and hexagon variants), see
generate_capability_legend.py -- that is the grid's legend now that the diagonal
hatch has been removed from the pipeline.
"""

import argparse
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap, to_rgb

from utils.capabilities import service_step_color


def _service_cmap(n: int = 256) -> ListedColormap:
    """Reproduce the QGIS step expression (utils.capabilities.service_step_color)
    as a matplotlib colormap: one flat color per 0.1-wide bucket, no interpolation."""
    colors = [to_rgb(service_step_color(v)) for v in np.linspace(0.0, 1.0, n)]
    return ListedColormap(colors)


def _save(fig, path: Path) -> None:
    fig.savefig(path, dpi=200, transparent=True, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    print(f"Wrote {path}")


def generate_service_legend(output_dir: Path) -> None:
    gradient = np.linspace(0, 1, 256).reshape(-1, 1)
    fig, ax = plt.subplots(figsize=(1.3, 4.6))
    ax.imshow(gradient, aspect="auto", cmap=_service_cmap(), origin="lower", extent=[0, 1, 0, 1])
    ax.set_xticks([])
    ax.set_yticks([0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
    ax.tick_params(labelsize=9)
    for spine in ax.spines.values():
        spine.set_linewidth(0.8)
    ax.set_title("Service score", fontsize=12, pad=10)
    _save(fig, output_dir / "service_legend.png")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/legends"),
        help="Directory to write the legend PNG to.",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    generate_service_legend(args.output_dir)
