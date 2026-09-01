"""Merges the four scenarios/<key>/differences.gpkg files, plus each condition's own raw
per-hexagon capability grid, into one combined GeoPackage,
scenarios/all_scenarios_differences.gpkg -- so both the diffs and the underlying values they
were computed from are browsable/stylable in one QGIS file.

Uses the same SQL-based table-copy approach as exports/generate_experiment_shapefiles.py's
_merge_gpkg_table -- GDAL's own gpkg-append mode is unreliable in this environment ("NULL
pointer" errors from both the pyogrio and fiona engines). A GeoPackage geometry column is a
BLOB with a small header wrapping standard WKB, so a raw `CREATE TABLE ... AS SELECT *`
byte-copies valid geometries as-is; every source file shares the same CRS (EPSG:4326) and hex
grid, so no reprojection/alignment is needed.

The four differences.gpkg files already have unique layer names (the arm name is baked in,
e.g. diff_baseline_to_student__care vs. diff_baseline_to_new_metro__care). The six condition
capability grids all reuse the SAME three table names (capability_care/nutrition/
restorativeness) across conditions, so those need renaming on the way in (capability_<cap>__
<condition>) -- see _merge_gpkg_table_renamed.

Condition grids live at scenarios/_capability_grids/<condition>/. core/pipeline_runner.py's
gpkg output path (outputs/gpkg/Cagliari_<condition>/) is generic and shared by every pipeline
run, not just scenarios, so it isn't changed to write there directly. Instead,
relocate_condition_grids() (run automatically by merge_all(), every time) moves whatever
fresh outputs/gpkg/Cagliari_<condition>/ folder a scenarios.py run just produced into
scenarios/_capability_grids/<condition>/, replacing the previous copy -- so a future
`python analysis/scenarios.py` run followed by this script stays self-cleaning: nothing
manual to remember. A condition with no fresh outputs/gpkg/Cagliari_<condition>/ folder (not
rerun since last time) is left untouched.

Run with: python3 -m analysis.merge_scenario_differences
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import fiona
import geopandas as gpd

# ── Knobs ────────────────────────────────────────────────────────────────────
SCENARIO_KEYS: tuple[str, ...] = ("public-strike", "elder-student", "underservice-is-mirrionis", "new-metro")
CONDITION_KEYS: tuple[str, ...] = (
    "baseline", "student", "elderly", "public_strike", "underservice_is_mirrionis", "new_metro",
)
CAPABILITY_LAYERS: tuple[str, ...] = ("capability_care", "capability_nutrition", "capability_restorativeness")
SCENARIOS_DIR = Path("scenarios")
CAPABILITY_GRIDS_DIR = SCENARIOS_DIR / "_capability_grids"
OUT_GPKG = SCENARIOS_DIR / "all_scenarios_differences.gpkg"
# core/pipeline_runner.py:956 -- gpkg_output_path = outputs/gpkg/<artifact_slug>/, shared by
# every pipeline run. A scenario condition's artifact_slug is always "Cagliari_<condition>".
OUTPUTS_GPKG_DIR = Path("outputs/gpkg")

LAYER_STYLES_SCHEMA = """
CREATE TABLE IF NOT EXISTS "layer_styles" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "f_table_catalog" TEXT(256), "f_table_schema" TEXT(256), "f_table_name" TEXT(256),
    "f_geometry_column" TEXT(256), "styleName" TEXT(30), "styleQML" TEXT, "styleSLD" TEXT,
    "useAsDefault" BOOLEAN, "description" TEXT, "owner" TEXT(30), "ui" TEXT(30),
    "update_time" DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)
"""
LAYER_STYLES_COLUMNS = (
    "f_table_catalog", "f_table_schema", "f_table_name", "f_geometry_column", "styleName",
    "styleQML", "styleSLD", "useAsDefault", "description", "owner", "ui", "update_time",
)


def _merge_gpkg_table(src_gpkg: Path, dest_gpkg: Path, table_name: str) -> None:
    """Copy one spatial layer's table from src_gpkg into dest_gpkg via plain SQLite,
    registering it in dest's gpkg_contents/gpkg_geometry_columns. dest must already be a
    valid GeoPackage (i.e. its first layer was written normally via geopandas)."""
    with sqlite3.connect(src_gpkg) as conn:
        conn.execute("ATTACH DATABASE ? AS dest", (str(dest_gpkg),))
        try:
            conn.execute(f'DROP TABLE IF EXISTS dest."{table_name}"')
            conn.execute(f'CREATE TABLE dest."{table_name}" AS SELECT * FROM "{table_name}"')
            conn.execute("DELETE FROM dest.gpkg_contents WHERE table_name = ?", (table_name,))
            conn.execute(
                "INSERT INTO dest.gpkg_contents SELECT * FROM gpkg_contents WHERE table_name = ?",
                (table_name,),
            )
            conn.execute("DELETE FROM dest.gpkg_geometry_columns WHERE table_name = ?", (table_name,))
            conn.execute(
                "INSERT INTO dest.gpkg_geometry_columns SELECT * FROM gpkg_geometry_columns WHERE table_name = ?",
                (table_name,),
            )
            conn.commit()
        finally:
            conn.execute("DETACH DATABASE dest")


def _merge_gpkg_table_renamed(src_gpkg: Path, dest_gpkg: Path, src_table: str, dest_table: str) -> None:
    """Like _merge_gpkg_table, but copies src_table into dest_gpkg under a different name.
    Used for per-condition capability layers, which share the same table name (e.g.
    capability_care) across all six conditions and must be disambiguated. identifier is set
    to dest_table (not copied from src) since gpkg_contents.identifier is UNIQUE and every
    condition's source row has the same identifier as its table_name."""
    with sqlite3.connect(src_gpkg) as conn:
        conn.execute("ATTACH DATABASE ? AS dest", (str(dest_gpkg),))
        try:
            conn.execute(f'DROP TABLE IF EXISTS dest."{dest_table}"')
            conn.execute(f'CREATE TABLE dest."{dest_table}" AS SELECT * FROM "{src_table}"')
            conn.execute("DELETE FROM dest.gpkg_contents WHERE table_name = ?", (dest_table,))
            conn.execute(
                "INSERT INTO dest.gpkg_contents "
                "(table_name, data_type, identifier, description, last_change, min_x, min_y, max_x, max_y, srs_id) "
                "SELECT ?, data_type, ?, description, last_change, min_x, min_y, max_x, max_y, srs_id "
                "FROM gpkg_contents WHERE table_name = ?",
                (dest_table, dest_table, src_table),
            )
            conn.execute("DELETE FROM dest.gpkg_geometry_columns WHERE table_name = ?", (dest_table,))
            conn.execute(
                "INSERT INTO dest.gpkg_geometry_columns "
                "(table_name, column_name, geometry_type_name, srs_id, z, m) "
                "SELECT ?, column_name, geometry_type_name, srs_id, z, m "
                "FROM gpkg_geometry_columns WHERE table_name = ?",
                (dest_table, src_table),
            )
            conn.commit()
        finally:
            conn.execute("DETACH DATABASE dest")

    with sqlite3.connect(dest_gpkg) as conn:
        conn.execute(LAYER_STYLES_SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier) "
            "VALUES ('layer_styles', 'attributes', 'layer_styles')"
        )
        conn.commit()
    style_cols = [c for c in LAYER_STYLES_COLUMNS if c != "f_table_name"]
    cols_sql = ", ".join(f'"{c}"' for c in style_cols)
    with sqlite3.connect(src_gpkg) as conn:
        conn.execute("ATTACH DATABASE ? AS dest", (str(dest_gpkg),))
        try:
            conn.execute(
                f'INSERT INTO dest.layer_styles (f_table_name, {cols_sql}) '
                f'SELECT ?, {cols_sql} FROM layer_styles WHERE f_table_name = ?',
                (dest_table, src_table),
            )
            conn.commit()
        finally:
            conn.execute("DETACH DATABASE dest")


def _merge_layer_styles(src_gpkg: Path, dest_gpkg: Path) -> None:
    """Append src's layer_styles rows into dest's (creating dest's table + gpkg_contents
    registration on first use, as a non-spatial 'attributes' table). Explicit column list
    (excluding `id`) so autoincrement ids never collide across the four source files."""
    with sqlite3.connect(dest_gpkg) as conn:
        conn.execute(LAYER_STYLES_SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier) "
            "VALUES ('layer_styles', 'attributes', 'layer_styles')"
        )
        conn.commit()
    cols = ", ".join(f'"{c}"' for c in LAYER_STYLES_COLUMNS)
    with sqlite3.connect(src_gpkg) as conn:
        conn.execute("ATTACH DATABASE ? AS dest", (str(dest_gpkg),))
        try:
            conn.execute(f'INSERT INTO dest.layer_styles ({cols}) SELECT {cols} FROM layer_styles')
            conn.commit()
        finally:
            conn.execute("DETACH DATABASE dest")


def relocate_condition_grids() -> None:
    """Move each condition's freshly-written outputs/gpkg/Cagliari_<condition>/ folder into
    scenarios/_capability_grids/<condition>/, replacing any previous copy there. A condition
    not rerun since the last merge (no outputs/gpkg/Cagliari_<condition>/ folder) is left
    untouched -- its existing scenarios/_capability_grids/<condition>/ copy is still used."""
    CAPABILITY_GRIDS_DIR.mkdir(parents=True, exist_ok=True)
    for condition in CONDITION_KEYS:
        src_dir = OUTPUTS_GPKG_DIR / f"Cagliari_{condition}"
        if not src_dir.exists():
            continue
        dest_dir = CAPABILITY_GRIDS_DIR / condition
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.move(str(src_dir), str(dest_dir))
        print(f"[relocate] {src_dir} -> {dest_dir}")


def merge_all() -> Path:
    relocate_condition_grids()

    if OUT_GPKG.exists():
        OUT_GPKG.unlink()

    first_layer_written = False
    for key in SCENARIO_KEYS:
        src = SCENARIOS_DIR / key / "differences.gpkg"
        if not src.exists():
            print(f"[skip] {key}: {src} not found")
            continue
        layers = [l for l in fiona.listlayers(src) if l != "layer_styles"]
        for layer in layers:
            if not first_layer_written:
                gdf = gpd.read_file(src, layer=layer)
                gdf.to_file(OUT_GPKG, layer=layer, driver="GPKG", mode="w")
                first_layer_written = True
            else:
                _merge_gpkg_table(src, OUT_GPKG, layer)
            print(f"[merge] {key}/{layer}")
        _merge_layer_styles(src, OUT_GPKG)

    for condition in CONDITION_KEYS:
        cond_dir = CAPABILITY_GRIDS_DIR / condition
        gpkgs = list(cond_dir.glob("*.gpkg"))
        if not gpkgs:
            print(f"[skip] {condition}: no gpkg found under {cond_dir}")
            continue
        src = gpkgs[0]
        available = set(fiona.listlayers(src))
        for layer in CAPABILITY_LAYERS:
            if layer not in available:
                print(f"[skip] {condition}/{layer}: not found in {src}")
                continue
            dest_layer = f"{layer}__{condition}"
            if not first_layer_written:
                gdf = gpd.read_file(src, layer=layer)
                gdf.to_file(OUT_GPKG, layer=dest_layer, driver="GPKG", mode="w")
                first_layer_written = True
            else:
                _merge_gpkg_table_renamed(src, OUT_GPKG, layer, dest_layer)
            print(f"[merge] {condition}/{layer} -> {dest_layer}")

    print(f"[done] wrote {OUT_GPKG}")
    return OUT_GPKG


if __name__ == "__main__":
    merge_all()
