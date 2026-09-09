"""Housing affordability capability: ELECTRE TRI classification of a household's
housing situation (Q1..Q5) from renting/buying access curves, combining both into
one category instead of scoring each separately.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running directly -- see housing_affordability.py for why this is needed.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

INCOME = 1200.0
HOUSING_OPPORTUNITIES = {"buying": 15.0, "renting": 19.0}
CLASSIFICATION_RESIDUAL = 500.0  # €, residual resources used for classification (independent of any plot thresholds)
HOUSING_BOUNDARIES = [20.0, 28.0, 42.0, 56.0]  # sqm, affordable-surface cut points
HOUSING_CATEGORIES = ["Q1", "Q2", "Q3", "Q4", "Q5"]
HOUSING_Q = 2.0   # sqm, indifference threshold
HOUSING_P = 6.0   # sqm, preference threshold
HOUSING_LAMBDA_CUT = 0.75  # minimum outranking credibility to be assigned above a boundary
HOUSING_WEIGHTS = {"renting": 0.5, "buying": 0.5}  # uniform by default


def classification_surface(resources_intercept: float, slope: float) -> float:
    """Affordable surface (sqm) at CLASSIFICATION_RESIDUAL residual resources."""
    return (resources_intercept - CLASSIFICATION_RESIDUAL) / slope


def electre_tri_housing_details(surface_by_curve: dict[str, float]) -> dict:
    """Step-by-step ELECTRE TRI explanation for housing affordability, mirroring
    utils.capabilities.electre_tri_details's shape (no veto -- see
    electre_tri_classify_housing's docstring)."""
    boundaries = []
    assigned_idx = 0
    for k, boundary in enumerate(HOUSING_BOUNDARIES):
        concordance_terms = []
        weighted_concordance = 0.0
        weight_sum = 0.0
        for description, surface in surface_by_curve.items():
            w = HOUSING_WEIGHTS.get(description, 1.0 / len(surface_by_curve))
            d = surface - boundary
            if d >= -HOUSING_Q:
                c_j, rule = 1.0, "full"
            elif d <= -HOUSING_P:
                c_j, rule = 0.0, "none"
            else:
                c_j, rule = (d + HOUSING_P) / (HOUSING_P - HOUSING_Q), "partial"
            weighted_concordance += w * c_j
            weight_sum += w
            concordance_terms.append(
                {"criterion": description, "surface": surface, "difference_vs_boundary": d,
                 "partial_concordance": c_j, "weight": w, "rule": rule}
            )
        credibility = weighted_concordance / weight_sum
        outranks = credibility >= HOUSING_LAMBDA_CUT
        if outranks:
            assigned_idx = k + 1
        boundaries.append(
            {"boundary_index": k, "boundary_value": boundary, "concordance_terms": concordance_terms,
             "credibility": credibility, "outranks_boundary": outranks,
             "assigned_category_if_stopped_here": HOUSING_CATEGORIES[min(k + 1, len(HOUSING_CATEGORIES) - 1)] if outranks else HOUSING_CATEGORIES[assigned_idx]}
        )
    return {
        "criteria": list(surface_by_curve.keys()),
        "surface_by_curve": surface_by_curve,
        "weights": HOUSING_WEIGHTS,
        "q": HOUSING_Q, "p": HOUSING_P, "lambda_cut": HOUSING_LAMBDA_CUT,
        "boundaries": boundaries,
        "assigned_category": HOUSING_CATEGORIES[assigned_idx],
        "assigned_category_index": assigned_idx,
    }


def electre_tri_classify_housing(surface_by_curve: dict[str, float]) -> str:
    """ELECTRE TRI classification of housing affordability, combining renting and
    buying into one category instead of scoring each separately -- treats them as
    two criteria of the same household situation. No veto (assumes an infinite
    veto threshold): with only two criteria that represent the same household's two
    access routes, there isn't yet a case for one to veto the other.
    """
    return electre_tri_housing_details(surface_by_curve)["assigned_category"]


def classify_housing_opportunities(
    opportunities: dict[str, float] = HOUSING_OPPORTUNITIES, income: float = INCOME
) -> tuple[dict[str, float], str]:
    """Full pipeline: opportunities (€/sqm/mo slopes) -> per-curve classification
    surfaces -> combined category."""
    surfaces = {desc: classification_surface(income, slope) for desc, slope in opportunities.items()}
    return surfaces, electre_tri_classify_housing(surfaces)


# ── Grid export ────────────────────────────────────────────────────────────────

HOUSING_COLOR_LOW = "#FFFFFF"   # white, at t=0
HOUSING_COLOR_HIGH = "#15BB7E"  # at t=1
# Where each category sits along the white -> HOUSING_COLOR_HIGH interpolation.
HOUSING_CATEGORY_COLOR_STOPS = {"Q1": 0.2, "Q2": 0.4, "Q3": 0.6, "Q4": 0.8, "Q5": 1.0}


def housing_category_colors() -> dict[str, str]:
    """Hex color per HOUSING_CATEGORIES, linearly interpolated between
    HOUSING_COLOR_LOW and HOUSING_COLOR_HIGH at the stops in
    HOUSING_CATEGORY_COLOR_STOPS."""
    from matplotlib.colors import LinearSegmentedColormap, rgb2hex

    cmap = LinearSegmentedColormap.from_list("housing_affordability", [HOUSING_COLOR_LOW, HOUSING_COLOR_HIGH])
    return {category: rgb2hex(cmap(t)) for category, t in HOUSING_CATEGORY_COLOR_STOPS.items()}


def _categorized_qml(attr: str, category_colors: dict[str, str]) -> str:
    """QGIS categorized polygon-fill style: one flat color per category value."""
    categories, symbols = [], []
    for i, (category, hex_color) in enumerate(category_colors.items()):
        r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
        categories.append(f'<category value="{category}" symbol="{i}" label="{category}" render="true"/>')
        symbols.append(
            f'<symbol type="fill" name="{i}" force_rhr="0" alpha="1" clip_to_extent="1">'
            f'<layer class="SimpleFill" enabled="1" pass="0" locked="0">'
            f'<Option type="Map">'
            f'<Option name="color" type="QString" value="{r},{g},{b},255"/>'
            f'<Option name="outline_color" type="QString" value="100,100,100,255"/>'
            f'<Option name="outline_width" type="QString" value="0.2"/>'
            f'<Option name="style" type="QString" value="solid"/>'
            f'</Option></layer></symbol>'
        )
    return (
        '<!DOCTYPE qgis>\n'
        '<qgis version="3.34.0" styleCategories="Symbology">\n'
        f'<renderer-v2 type="categorizedSymbol" attr="{attr}" symbollevels="0" forceraster="0">\n'
        f'<categories>{"".join(categories)}</categories>\n'
        f'<symbols>{"".join(symbols)}</symbols>\n'
        '</renderer-v2>\n'
        '<layerGeometryType>2</layerGeometryType>\n'
        '</qgis>\n'
    )


def _embed_gpkg_style(gpkg: Path, table: str, attr: str, category_colors: dict[str, str]) -> None:
    """Insert a default style into the GeoPackage layer_styles table (self-styling
    drag-in) -- same technique as analysis/robustness_report.py's stability layer."""
    import sqlite3

    qml = _categorized_qml(attr, category_colors)
    conn = sqlite3.connect(gpkg)
    try:
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
            (table, qml, f"Housing affordability category ({attr}); white=Q1, {HOUSING_COLOR_HIGH}=Q5"),
        )
        conn.commit()
    finally:
        conn.close()


def generate_housing_capability_grid(
    nodes_omi_csv: Path,
    origins_gpkg: Path,
    poi_export_dir: Path,
    out_gpkg: Path,
) -> None:
    """Build the same hexagonal grid main.py's Cagliari run uses (grid_params.json
    under poi_export_dir), one polygon per hex that has a housing-affordability
    node, colored by that node's housing_category (from nodes_omi_csv, joined to
    hex_id via origins_gpkg) using housing_category_colors().
    """
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import Polygon

    from tools.inspect_hex_pois import _hex_geometry, _load_grid_params

    nodes = pd.read_csv(nodes_omi_csv)[["node_id", "housing_category"]]
    origins = gpd.read_file(origins_gpkg)[["node_id", "hex_id"]]
    merged = nodes.merge(origins, on="node_id", how="inner")
    print(f"[grid] {len(merged)}/{len(nodes)} nodes matched to a hex_id", flush=True)

    grid_params = _load_grid_params(poi_export_dir)
    if grid_params is None:
        raise RuntimeError(f"grid_params.json not found under {poi_export_dir} -- run main.py for Cagliari first.")

    category_colors = housing_category_colors()

    rows = []
    for row in merged.itertuples(index=False):
        hex_id = str(row.hex_id)
        category = str(row.housing_category)
        vertices = _hex_geometry(hex_id, grid_params)
        rows.append(
            {
                "hex_id": hex_id,
                "node_id": row.node_id,
                "housing_category": category,
                "color": category_colors[category],
                "geometry": Polygon(vertices),
            }
        )

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    out_gpkg.parent.mkdir(parents=True, exist_ok=True)
    if out_gpkg.exists():
        out_gpkg.unlink()
    table = "housing_capability_grid"
    gdf.to_file(out_gpkg, layer=table, driver="GPKG")

    _embed_gpkg_style(out_gpkg, table, "housing_category", category_colors)
    print(f"[grid] wrote {len(gdf)} hexagons to {out_gpkg}, styled on housing_category", flush=True)


if __name__ == "__main__":
    _HOUSING_DIR = Path(__file__).resolve().parent
    generate_housing_capability_grid(
        nodes_omi_csv=_HOUSING_DIR / "cagliari_nodes_omi.csv",
        origins_gpkg=_HOUSING_DIR / "Cagliari_origins.gpkg",
        poi_export_dir=Path(_PROJECT_ROOT) / "outputs" / "poi_exports" / "Cagliari",
        out_gpkg=_HOUSING_DIR / "Cagliari_housing_capability_grid.gpkg",
    )
