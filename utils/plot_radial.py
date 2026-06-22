from __future__ import annotations

from typing import cast

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.axes import Axes
from matplotlib.patches import Rectangle

# ── fixed layout (inches) ─────────────────────────────────────────────────────
# Axes box: 0.72 × 10" = 7.2" wide, 0.80 × 9" = 7.2" tall → perfectly square
_FIG_W = 10.0
_FIG_H =  9.0
_AX_L  =  0.09   # axes left   (figure fraction)
_AX_B  =  0.10   # axes bottom
_AX_W  =  0.72   # axes width  → 7.2 in = 18.29 cm
_AX_H  =  0.80   # axes height → 7.2 in
_CB_L  = _AX_L + _AX_W + 0.025   # colorbar left
_CB_W  =  0.025                   # colorbar width fraction

_PADDING_CM = 1.0   # white space between grid boundary (0…n) and frame spines
_EDGE_PT    = 8.0   # visual edge width in points


def _pad_data(n: int) -> float:
    """
    Exact padding in data units so that it renders as _PADDING_CM centimetres.

    Derivation: the axes box spans ax_cm centimetres across n + 2·pad data
    units, so cm_per_unit = ax_cm / (n + 2·pad).  Setting pad · cm_per_unit
    = _PADDING_CM and solving gives the formula below.
    """
    ax_cm = min(_AX_W * _FIG_W, _AX_H * _FIG_H) * 2.54
    return _PADDING_CM * n / (ax_cm - 2.0 * _PADDING_CM)


def _edge_inset(n: int) -> float:
    """Half of _EDGE_PT in data units; used to inset white cell-interior patches."""
    lw_in = _EDGE_PT / 72.0                      # points → inches
    data_per_in = n / (_AX_W * _FIG_W)           # data units per inch
    return (lw_in / 2.0) * data_per_in


def plot_radial_cost(
    impedance: float = 1.0,
    n: int = 8,
    colormap: str = "YlOrRd",
    title: str = "Radial Cost Grid",
    ax: Axes | None = None,
) -> Axes:
    """
    Draw an n×n Manhattan grid where cell interiors are white and each
    edge strip is coloured by a continuous radial cost gradient from the
    bottom-left corner.

    Parameters
    ----------
    impedance : float
        Controls how quickly cost intensifies with distance.
        cost = (distance / max_distance) ** impedance.
        < 1 → sub-linear ramp; 1 → linear; > 1 → super-linear.
    n : int
        Grid size (cells per side). Default 8.
    colormap : str
        Matplotlib sequential colormap name.
    title : str
        Plot title.
    ax : Axes, optional
        Axes to draw on; a new figure is created when None.

    Returns
    -------
    Axes
    """
    cax: Axes | None

    if ax is None:
        fig = plt.figure(figsize=(_FIG_W, _FIG_H))
        fig.patch.set_facecolor("white")
        _ax: Axes = cast(Axes, fig.add_axes([_AX_L, _AX_B, _AX_W, _AX_H]))
        cax = cast(Axes, fig.add_axes([_CB_L, _AX_B, _CB_W, _AX_H]))
    else:
        _ax = ax
        _maybe_fig = ax.figure
        assert _maybe_fig is not None, "Axes has no associated Figure"
        fig = _maybe_fig
        cax = None

    _ax.set_facecolor("white")
    cmap = plt.get_cmap(colormap)
    norm = mcolors.Normalize(vmin=0, vmax=1)

    # ── continuous radial gradient (imshow) ───────────────────────────────────
    # Extend the image by one edge-half-width on every side so that outer edges
    # are the same visible thickness (2 × inset) as inner edges.
    inset = _edge_inset(n)
    res = 600
    xi = np.linspace(-inset, n + inset, res)
    yi = np.linspace(-inset, n + inset, res)
    Xi, Yi = np.meshgrid(xi, yi)
    cost_grid = np.clip(
        (np.sqrt(Xi**2 + Yi**2) / (np.sqrt(2) * n)) ** impedance, 0.0, 1.0
    )
    _ax.imshow(
        cost_grid,
        extent=(-inset, n + inset, -inset, n + inset),
        origin="lower",
        cmap=cmap,
        vmin=0,
        vmax=1,
        aspect="auto",
        interpolation="bilinear",
        zorder=0,
    )

    # ── white cell interiors ──────────────────────────────────────────────────
    # Inset from each cell boundary by half the edge width so that the imshow
    # gradient shows through as coloured strips of the correct thickness.
    for i in range(n):
        for j in range(n):
            _ax.add_patch(Rectangle(
                (i + inset, j + inset),
                1.0 - 2.0 * inset,
                1.0 - 2.0 * inset,
                facecolor="white",
                edgecolor="none",
                zorder=1,
            ))

    # ── frame: spines as border; xlim/ylim give exactly 1 cm white padding ───
    pad = _pad_data(n)
    _ax.set_xlim(-pad, n + pad)
    _ax.set_ylim(-pad, n + pad)

    for spine in _ax.spines.values():
        spine.set_visible(True)
        spine.set_linewidth(_EDGE_PT)

    # ── axis ticks: data positions 0…n labelled 0, 5, 10 … 40 ───────────────
    step = 40 // n
    tick_pos = list(range(n + 1))
    tick_labels = [str(i * step) for i in tick_pos]
    _ax.set_xticks(tick_pos)
    _ax.set_xticklabels(tick_labels, fontsize=9)
    _ax.set_yticks(tick_pos)
    _ax.set_yticklabels(tick_labels, fontsize=9)
    _ax.tick_params(length=4, direction="out")

    # ── colorbar ──────────────────────────────────────────────────────────────
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    if cax is not None:
        cb = fig.colorbar(sm, cax=cax)
    else:
        cb = fig.colorbar(sm, ax=_ax, fraction=0.046, pad=0.04)
    cb.set_label("Impedance", fontsize=10)
    cb.ax.tick_params(labelsize=8)

    _ax.set_title(f"{title}  (impedance = {impedance})", fontsize=11, pad=8)

    return _ax


if __name__ == "__main__":
    plot_radial_cost(impedance=1.0)
    plt.savefig("radial_cost.png", dpi=150, bbox_inches="tight", facecolor="white")
    plt.show()
