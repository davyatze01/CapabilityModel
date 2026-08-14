"""Build the robustness deliverables from robustness_analysis.py's outputs:

  1. a self-contained HTML report (outputs/sensitivity/robustness_report.html)
     -- opens in any browser and prints straight to PDF; and
  2. a styled point GeoPackage (outputs/sensitivity/capability_stability.gpkg)
     of per-node classification stability, for QGIS (drag it in; a graduated
     red->green style on the least-stable capability is embedded as the default).

Split out from sensitivity_report.py: robustness ("is the output trustworthy
given noisy inputs?") is a different question from parameter sensitivity ("do
the parameters matter?"), answered by a different method (Monte Carlo noise on
inputs, all parameters held fixed) -- so it gets its own script and doesn't need
a parameter sweep to be rerun to refresh its numbers, or vice versa.

Reads outputs/sensitivity/robustness_node_stability.csv (from robustness_analysis.py).
Reuses the chart/CSS infrastructure from sensitivity_report.py so both reports
look like one system.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.sensitivity_report import CHART_INK, CSS, VERDICT_HEX, _cap_colors, _df_html, _fig_html, _style_ax
from typing import cast
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.collections import PolyCollection
SENS_DIR = Path("outputs/sensitivity")



# —— Knobs for robustness report ——————————————————————————————————————————————————————————
SIGMA = 0.05
COORDS_CSV : Path | None = None
BUILD_QGIS = True

# ── Load ─────────────────────────────────────────────────────────────────────

def load_stability(out_dir : Path) -> pd.DataFrame:
    path = out_dir / "robustness_node_stability.csv"
    if not path.exists():
        raise RuntimeError(f"[Robustness] ERROR: expected {path}")
    return pd.read_csv(path)


# ── Chart ────────────────────────────────────────────────────────────────────

def chart_robustness(stability: pd.DataFrame, caps: list[str]) -> str:
    """Violin plot of the full per-node stability distribution, one violin per
    (capability, sigma). A bar chart of the mean or of '% below 0.8' hides
    exactly the thing that matters here -- whether a capability's nodes are
    uniformly so-so or split into a stable cluster and a coin-flip cluster
    (bimodal) -- so the full distribution shape is the point, not a summary
    statistic of it."""
    colors = _cap_colors(caps)
    sigmas = sorted(stability["sigma"].unique())
    cols = [c for c in caps if c in stability["capability"].unique()]
    n_series = len(cols)
    width = 0.8 / max(n_series, 1)
    fig, ax = plt.subplots(figsize=(9.5, 4.2))
    for i, cap in enumerate(cols):
        offset = (i - (n_series - 1) / 2) * width
        positions = [s_idx + offset for s_idx in range(len(sigmas))]
        data = [
            stability["stability"].loc[(stability["capability"] == cap) & (stability["sigma"] == s)].to_numpy()
            for s in sigmas
        ]
        parts = ax.violinplot(
            data, positions=positions, widths=width * 0.9, showmeans=True, showextrema=True
        )
        for body in cast(list[PolyCollection], parts["bodies"]):
            body.set_facecolor(colors[cap])
            body.set_edgecolor(colors[cap])
            body.set_alpha(0.65)
        for key in ("cbars", "cmins", "cmaxes", "cmeans"):
            if key in parts:
                parts[key].set_color(colors[cap])
                parts[key].set_linewidth(1.1)
    ax.axhline(0.8, color=VERDICT_HEX["warn"], linewidth=1.0, linestyle="--")
    ax.text(1.0, 0.8, " 0.8", color=VERDICT_HEX["warn"], fontsize=8.5, va="center", ha="left",
            transform=ax.get_yaxis_transform())
    ax.set_xticks(range(len(sigmas)))
    ax.set_xticklabels([f"σ={s:g}" for s in sigmas])
    ax.set_ylim(-0.02, 1.02)
    ax.set_ylabel("per-node stability")
    ax.set_title("Distribution of per-node class stability by σ", fontsize=10.5, color=CHART_INK, loc="left")
    _style_ax(ax)
    handles = [Rectangle((0, 0), 1, 1, color=colors[c], alpha=0.65) for c in cols]
    ax.legend(handles, cols, frameon=False, fontsize=9, ncols=n_series, loc="upper center", bbox_to_anchor=(0.5, -0.16))
    fig.tight_layout()
    return _fig_html(fig)


# ── HTML ─────────────────────────────────────────────────────────────────────

def build_html(stability: pd.DataFrame, sigma: float, out_path: Path) -> None:
    caps = sorted(stability["capability"].unique())
    n_nodes = int(stability["node_id"].nunique())
    sigmas = sorted(stability["sigma"].unique())

    rob = (
        stability.groupby(["capability", "sigma"])
        .agg(mean_stability=("stability", "mean"),
             pct_lt_08=("stability", lambda s: float((s < 0.8).mean() * 100)))
    )
    at_sigma = rob.xs(sigma, level="sigma") if sigma in sigmas else None
    fragile = at_sigma["pct_lt_08"].idxmax() if at_sigma is not None and len(at_sigma) else caps[0]
    rob_mean = (
        "; ".join(f"{c} {rob.loc[(c, sigma), 'mean_stability']:.2f}" for c in caps)
        if sigma in sigmas else ""
    )
    frag_row = at_sigma.loc[fragile] if at_sigma is not None and fragile in at_sigma.index else None

    rob_pivot = rob.round(3)
    chart_rob = chart_robustness(stability, caps)

    sigmas_str = ", ".join(f"{s:g}" for s in sigmas)

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Capability model — robustness to input noise (Cagliari)</title>
<style>{CSS}</style></head><body>
<h1>Capability model — robustness to input noise</h1>
<p class="sub">Cagliari · ELECTRE TRI capability classification · {n_nodes} nodes · all parameters held at
baseline (see the sensitivity report for parameter-sensitivity results) -- only the input service scores are
perturbed. This answers a different question from sensitivity: <b>given the parameters are right, is the
resulting map trustworthy, or is it noise-sensitive?</b></p>

<h2>1. How this analysis works</h2>
<p>Holds every parameter fixed at baseline (q indifference, p preference, &lambda; cut, ELECTRE weights, all upstream
config) and adds gaussian noise (&sigma;) to the service scores, clipped to [0,1], repeated many times per
&sigma; per node. <b>Stability</b> = share of repetitions that assign a node its single most common
("modal") class; 1.0 = solid, 0.5 = coin-flip. This is a structural question, independent of parameter
sensitivity: a model can be insensitive to its parameters yet still non-robust to noise, or vice versa.</p>

<h3>Parameters tested</h3>
<p>&sigma; &isin; {{{sigmas_str}}} (gaussian noise std-dev added to each service score, in the same [0,1]
units as the scores themselves), each repeated {int((stability.groupby(['capability','sigma']).size().iloc[0]) if len(stability) else 0)} times per capability.</p>

<h2>2. Results</h2>
<p>Mean class stability and share of "shaky" nodes (stability &lt; 0.8), per capability and &sigma;:</p>
{_df_html(rob_pivot)}
<p>Same data as a violin plot, showing the <b>full distribution</b> of per-node stability rather than just its
mean — a capability whose nodes split into a stable cluster and a coin-flip cluster looks very different from
one that is uniformly middling, even if their means match; the dashed line marks the 0.8 "shaky" threshold:</p>
{chart_rob}

<h2>3. Conclusions</h2>
<div class="callout">
<p>{("Mean class stability at &sigma;=" + f"{sigma:g}" + ": " + rob_mean + ". ") if rob_mean else ""}
{("<b>" + str(fragile) + "</b> is the least stable capability here" + (f" — <b>{frag_row['pct_lt_08']:.0f}%</b> of its nodes are unstable (fewer than 80% of noisy repetitions agree on a class)" if frag_row is not None else "") + ".") if rob_mean else ""}
So {fragile} classes should be presented with uncertainty, not as hard categories. The QGIS layer
(<code>capability_stability.gpkg</code>) maps this per node — red = coin-flip, green = solid — so a viewer
can see exactly where on the map to trust the classification and where not to.</p>
</div>
<p class="sub">Generated by <code>robustness_report.py</code> from <code>robustness_node_stability.csv</code>
(produced by <code>robustness_analysis.py</code>). Re-running both refreshes every figure.</p>
</body></html>"""
    out_path.write_text(html, encoding="utf-8")


# ── QGIS layer ───────────────────────────────────────────────────────────────

def _graduated_qml(attr: str) -> str:
    """Minimal QGIS graduated-marker style: red (fragile) -> green (stable)."""
    breaks = [
        (0.0, 0.5, "215,25,28,255", "Coin-flip (under 0.5)"),
        (0.5, 0.8, "253,174,97,255", "Shaky (0.5 - 0.8)"),
        (0.8, 0.95, "166,217,106,255", "Mostly stable (0.8 - 0.95)"),
        (0.95, 1.0001, "26,150,65,255", "Stable (0.95+)"),
    ]
    ranges, symbols = [], []
    for i, (lo, hi, color, label) in enumerate(breaks):
        ranges.append(
            f'<range lower="{lo:.4f}" upper="{hi:.4f}" symbol="{i}" label="{label}" render="true"/>'
        )
        symbols.append(
            f'<symbol type="marker" name="{i}" force_rhr="0" alpha="1" clip_to_extent="1">'
            f'<layer class="SimpleMarker" enabled="1" pass="0" locked="0">'
            f'<Option type="Map">'
            f'<Option name="color" type="QString" value="{color}"/>'
            f'<Option name="outline_color" type="QString" value="35,35,35,255"/>'
            f'<Option name="outline_width" type="QString" value="0.2"/>'
            f'<Option name="size" type="QString" value="2"/>'
            f'<Option name="size_unit" type="QString" value="MM"/>'
            f'<Option name="name" type="QString" value="circle"/>'
            f'</Option></layer></symbol>'
        )
    return (
        '<!DOCTYPE qgis>\n'
        '<qgis version="3.34.0" styleCategories="Symbology">\n'
        f'<renderer-v2 type="graduatedSymbol" attr="{attr}" graduatedMethod="GraduatedColor" forceraster="0">\n'
        f'<ranges>{"".join(ranges)}</ranges>\n'
        f'<symbols>{"".join(symbols)}</symbols>\n'
        '</renderer-v2>\n'
        '<layerGeometryType>0</layerGeometryType>\n'
        '</qgis>\n'
    )


def _embed_gpkg_style(gpkg: Path, table: str, attr: str) -> None:
    """Insert a default style into the GeoPackage layer_styles table (self-styling drag-in)."""
    qml = _graduated_qml(attr)
    with sqlite3.connect(gpkg) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS layer_styles ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, f_table_catalog TEXT, f_table_schema TEXT, "
            "f_table_name TEXT, f_geometry_column TEXT, styleName TEXT, styleQML TEXT, styleSLD TEXT, "
            "useAsDefault BOOLEAN, description TEXT, owner TEXT, ui TEXT, update_time DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("DELETE FROM layer_styles WHERE f_table_name = ?", (table,))
        conn.execute(
            "INSERT INTO layer_styles "
            "(f_table_catalog, f_table_schema, f_table_name, f_geometry_column, styleName, styleQML, useAsDefault, description) "
            "VALUES ('', '', ?, 'geom', 'default', ?, 1, ?)",
            (table, qml, f"Classification stability ({attr}); red=fragile, green=stable"),
        )


def build_qgis_layer(stability: pd.DataFrame, coords_csv: Path, sigma: float, out_gpkg: Path) -> None:
    import geopandas as gpd
    from shapely.geometry import Point

    coords = pd.read_csv(coords_csv)[["node_id", "lat", "lon"]].drop_duplicates("node_id")

    # Wide table: one stability column per capability at the chosen sigma, plus the
    # per-node minimum across capabilities (overall fragility) and modal classes.
    sdf = stability[np.isclose(stability["sigma"], sigma)]
    wide = coords.set_index("node_id")
    for capability, grp in sdf.groupby("capability"):
        g = grp.set_index("node_id")
        wide[f"stab_{capability}"] = g["stability"]
        wide[f"class_{capability}"] = g["modal_class"]
    stab_cols = [c for c in wide.columns if c.startswith("stab_")]
    wide["stab_min"] = wide[stab_cols].min(axis=1)
    wide = wide.dropna(subset=["lat", "lon"]).reset_index()

    gdf = gpd.GeoDataFrame(
        wide, geometry=[Point(xy) for xy in zip(wide["lon"], wide["lat"])], crs="EPSG:4326"
    )
    out_gpkg.parent.mkdir(parents=True, exist_ok=True)
    if out_gpkg.exists():
        out_gpkg.unlink()
    table = "capability_stability"
    gdf.to_file(out_gpkg, layer=table, driver="GPKG")

    # Default style on the least-stable capability's stability if present, else the min.
    mean_by_cap = sdf.groupby("capability")["stability"].mean()
    fragile = mean_by_cap.idxmin() if len(mean_by_cap) else None
    attr = f"stab_{fragile}" if fragile is not None and f"stab_{fragile}" in gdf.columns else "stab_min"
    _embed_gpkg_style(out_gpkg, table, attr)
    # Sidecar .qml too, in case a viewer prefers it.
    (out_gpkg.with_suffix(".qml")).write_text(_graduated_qml(attr), encoding="utf-8")
    print(f"[layer] {out_gpkg} ({len(gdf)} nodes) styled on {attr} (red=fragile)", flush=True)


# ── Main ─────────────────────────────────────────────────────────────────────

def generate_robustness_report(
        sigma : float = SIGMA,
        coords_csv : Path | None = COORDS_CSV,
        build_qgis : bool = BUILD_QGIS,
        out_dir : Path = SENS_DIR
) -> None:


    stability = load_stability(out_dir)

    html_path = out_dir / "robustness_report.html"
    build_html(stability, sigma, html_path)
    print(f"[Robustness] {html_path}")

    if build_qgis:
        coords_csv = coords_csv or (SENS_DIR / "upstream" / "baseline" / "service_scores.csv")
        if not coords_csv.exists():
            print(f"WARNING: {coords_csv} missing for coordinates; skipping QGIS layer. ")
        else:
            build_qgis_layer(stability, coords_csv, sigma, out_dir / "capability_stability.gpkg")


if __name__ == "__main__":
    generate_robustness_report()
