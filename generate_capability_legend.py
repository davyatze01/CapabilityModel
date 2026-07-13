"""Generate a standalone PNG legend for the capability grid's hatch levels.

Mirrors the five ELECTRE TRI classes and diagonal-hatch densities used for the
QGIS "Capability grid" layer in pipeline_runner.py (Very Low -> Very High,
sparse -> dense lines). Kept as a separate image since the grid itself no
longer carries color, only a labeled reference for what each hatch density
means.
"""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# Must stay in sync with electre_labels / electre_bounds in pipeline_runner.py.
LABELS = ["Very Low", "Low", "Medium", "High", "Very High"]
BOUNDS = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
# Density increases from Very Low to Very High, same ordering as
# hatch_line_distances_mm in pipeline_runner.py (sparse -> tight).
HATCHES = ["/", "//", "///", "////", "/////"]


def main(output_path: Path, line_width: float) -> None:
    plt.rcParams["hatch.linewidth"] = line_width
    fig, ax = plt.subplots(figsize=(2.6, 6))

    n = len(LABELS)
    for i, (label, hatch) in enumerate(zip(LABELS, HATCHES)):
        # Draw bottom-to-top so Very Low sits at the bottom, Very High at the top.
        y0 = i / n
        ax.add_patch(
            Rectangle(
                (0, y0),
                1,
                1 / n,
                facecolor="white",
                edgecolor="black",
                hatch=hatch,
                linewidth=1.0,
            )
        )
        lo, hi = BOUNDS[i], BOUNDS[i + 1]
        ax.text(
            1.15,
            y0 + 0.5 / n,
            f"{label}\n({lo:.1f}–{hi:.1f})",
            va="center",
            ha="left",
            fontsize=11,
        )

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_title("Capability Level", fontsize=13, pad=12)

    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/capability_legend.png"),
        help="Path to write the PNG legend to.",
    )
    parser.add_argument(
        "--line-width",
        type=float,
        default=1.5,
        help="Hatch stroke width, matched to config.qgis_grid_hatch_line_width intent.",
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    main(args.output, args.line_width)
