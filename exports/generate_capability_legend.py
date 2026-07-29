"""Generate standalone PNG legends for the per-capability colored grids.

Each legend is a vertical stack of 5 swatches (one per ELECTRE TRI class, Very
Low at the bottom -> Very High at the top). Each swatch is filled with that
level's shade of the capability's signature color (the exact same 5 discrete
shades the QGIS capability grid uses, from
utils.capabilities.capability_shade_hexes) and outlined in black at that level's
iso-band stroke width (0.3 -> 1.8 mm, thin at Very Low, thick at Very High) so
the legend also encodes the iso-band line weight.

Two swatch shapes are produced: squares and hexagons (the map grid cells are
hexagonal). By default every capability x both shapes is written to
outputs/legends/.

All styling constants are imported from utils.capabilities so this legend can
never drift from the map renderer in pipeline_runner.py.
"""

import argparse
import math
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, RegularPolygon

from utils.capabilities import (
    CAPABILITY_COLORS,
    ELECTRE_BOUNDS,
    ELECTRE_SHORT_LABELS,
    ISO_BAND_WIDTHS_MM,
    capability_shade_hexes,
)

# QGIS symbol widths are in millimetres; matplotlib linewidths are in points.
_MM_TO_PT = 72.0 / 25.4
# Swatch geometry, in data units (the axes uses an equal aspect ratio so these
# render undistorted). Rows are stacked with a fixed vertical pitch.
_SWATCH = 1.0
_ROW_PITCH = 1.45
_LABEL_X = 1.0


def render_legend(capability: str, shape: str, output_path: Path) -> None:
    """Render one capability's legend (5 stacked swatches) in the given shape.

    shape: "square" -> square swatches; "hexagon" -> flat-top hexagon swatches
    (matching the hexagonal map grid cells).
    """
    color_hex = CAPABILITY_COLORS[capability]
    shades = capability_shade_hexes(color_hex)
    n = len(ELECTRE_SHORT_LABELS)

    fig, ax = plt.subplots(figsize=(3.0, 6))
    ax.set_aspect("equal")

    for i, label in enumerate(ELECTRE_SHORT_LABELS):
        # Draw bottom-to-top so Very Low sits at the bottom, Very High at the top.
        cy = i * _ROW_PITCH
        edge_pt = ISO_BAND_WIDTHS_MM[i] * _MM_TO_PT
        if shape == "hexagon":
            # Flat-top hexagon (orientation pi/6): width 2R, so R = _SWATCH/2 makes
            # its width match the square swatch. Vertex-up is the default at
            # orientation 0, so rotate 30 degrees for a flat top like the map cells.
            ax.add_patch(
                RegularPolygon(
                    (0.0, cy),
                    numVertices=6,
                    radius=_SWATCH / 2,
                    orientation=math.pi / 6,
                    facecolor=shades[i],
                    edgecolor="black",
                    linewidth=edge_pt,
                )
            )
        else:
            ax.add_patch(
                Rectangle(
                    (-_SWATCH / 2, cy - _SWATCH / 2),
                    _SWATCH,
                    _SWATCH,
                    facecolor=shades[i],
                    edgecolor="black",
                    linewidth=edge_pt,
                )
            )
        lo, hi = ELECTRE_BOUNDS[i], ELECTRE_BOUNDS[i + 1]
        ax.text(
            _LABEL_X,
            cy,
            f"{label}\n({lo:.1f}–{hi:.1f})",
            va="center",
            ha="left",
            fontsize=11,
        )

    ax.set_xlim(-_SWATCH, _LABEL_X + 2.2)
    ax.set_ylim(-_ROW_PITCH * 0.6, (n - 1) * _ROW_PITCH + _ROW_PITCH * 0.6)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_title(f"{capability.capitalize()} capability", fontsize=13, pad=12)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {output_path}")


def main(output_dir: Path, capabilities: list[str], shapes: list[str]) -> None:
    for capability in capabilities:
        for shape in shapes:
            out = output_dir / f"capability_legend_{capability}_{shape}.png"
            render_legend(capability, shape, out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/legends"),
        help="Directory to write the legend PNGs to.",
    )
    parser.add_argument(
        "--capability",
        choices=sorted(CAPABILITY_COLORS.keys()),
        action="append",
        help="Capability to render (repeatable). Default: all.",
    )
    parser.add_argument(
        "--shape",
        choices=["square", "hexagon"],
        action="append",
        help="Swatch shape (repeatable). Default: both.",
    )
    args = parser.parse_args()
    capabilities = args.capability or sorted(CAPABILITY_COLORS.keys())
    shapes = args.shape or ["square", "hexagon"]
    main(args.output_dir, capabilities, shapes)
