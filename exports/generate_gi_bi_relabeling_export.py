"""Export real polygon/line shapes for every configured poi_type, actually used by the
pipeline, for the standalone blue/green infrastructure relabeling page.
"""

import csv
import json
import os
import sys
from collections import defaultdict

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

import fiona
import geopandas as gpd
import shapely
from shapely.strtree import STRtree

from core.config import PipelineConfig
from utils import graphml
from utils.poi_identity import SOURCE_KEY_COLUMNS, build_poi_source_key
from utils.services import get_global_radius_m, unique_query_keys

CITY = "paris"  # "cagliari" or "paris"

# Mirrors main.py: some helpers (e.g. utils.load_shapefile.feature_from_shapefile)
# build their own bare PipelineConfig() rather than taking the caller's, and that
# bare config reads the city from this env var — so it must match CITY above.
os.environ["CAP_STUDY_CITY"] = CITY

SHAPE_GEOM_TYPES = {"Polygon", "MultiPolygon", "LineString", "MultiLineString"}


def _used_source_keys(gpkg_path: str) -> set[str]:
    """Every source_key from pois_used.gpkg (POIs actually used by the pipeline)."""
    used = set()
    with fiona.open(gpkg_path, layer="pois_used") as src:
        for feat in src:
            used.add(feat["properties"]["source_key"])
    return used


def _cached_geojson_path(query) -> str:
    """Same per-query cache path graphml.get_poi() itself writes to."""
    cfg = PipelineConfig(study_city=CITY)
    buffer_m = get_global_radius_m(cfg) or 0.0
    poi_cache_slug = cfg.artifact_slug if cfg.use_shapefile else cfg.city_slug
    city_poi_dir = graphml._city_poi_cache_dir(poi_cache_slug)
    return os.path.join(city_poi_dir, graphml._tags_file_name(query.tags, buffer_m))


def _load_code_to_raw_tag() -> dict[str, str]:
    """Reverse of config/osm_raw_tag_codes.csv: short code -> plain "key=value" text."""
    path = os.path.join("config", "osm_raw_tag_codes.csv")
    mapping = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                mapping[row["code"]] = row["raw_tag"]
    return mapping


def _raw_fragment_rows_shapefile(cfg) -> list[dict]:
    """Shapefile-mode equivalent of _raw_fragment_rows: reads local MGP shapefile
    POIs per poi_type (same call get_poi() itself makes), no Overpass involved."""
    from utils.load_shapefile import feature_from_shapefile

    code_to_raw_tag = _load_code_to_raw_tag()
    rows = []
    seen_keys = set()
    for query in unique_query_keys():
        poi = feature_from_shapefile(cfg.name_shapefile, query_tags={}, poi_type=query.poi_type)
        if poi is None or poi.empty or "geometry" not in poi.columns:
            continue
        poi = graphml._stamp_poi_raw_tag(poi, None)
        shape_mask = poi.geometry.geom_type.isin(SHAPE_GEOM_TYPES)
        poi = poi[shape_mask]
        if poi.empty:
            continue

        col_vals = {col: poi[col].to_numpy() for col in SOURCE_KEY_COLUMNS if col in poi.columns}
        raw_tags = poi["poi_raw_tag"].to_numpy()
        geoms = poi.geometry.to_numpy()

        for i in range(len(poi)):
            row_dict = {col: vals[i] for col, vals in col_vals.items()}
            key = build_poi_source_key(row_dict, geoms[i])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append({
                "source_key": key,
                "poi_type": query.poi_type,
                "tag": code_to_raw_tag.get(str(raw_tags[i]), str(raw_tags[i])),
                "name": row_dict.get("name"),
                "geometry": geoms[i],
            })
    return rows


def _raw_fragment_rows() -> list[dict]:
    """[{source_key, poi_type, tag, name, geometry (real shapely)}] per pre-merge fragment.

    Shared by _load_raw_fragments() (serializes these for the browser) and
    _load_all_shapes() (uses the real geometry to compute each merged POI's
    member_keys via geometric containment) so the universe filter/stamp work — the
    expensive part — only happens once.
    """
    cfg = PipelineConfig(study_city=CITY)
    if cfg.use_shapefile:
        return _raw_fragment_rows_shapefile(cfg)

    code_to_raw_tag = _load_code_to_raw_tag()
    buffer_m = get_global_radius_m(cfg) or 0.0
    poi_cache_slug = cfg.artifact_slug if cfg.use_shapefile else cfg.city_slug
    city_poi_dir = graphml._city_poi_cache_dir(poi_cache_slug)

    # graphml._get_city_poi_universe() can return the fast, GEOS-avoiding cache form
    # (only a "__snap_coord" token per row, no real geometry) once the universe is
    # already cached on disk — fine for the main pipeline, useless here since merge
    # candidates need real polygon/line shapes. Read the same cache file directly
    # with geopandas instead, which always yields real geometry.
    universe_path = os.path.join(city_poi_dir, graphml._all_tags_file_name(graphml._build_city_universe_tags(), buffer_m))
    if not os.path.exists(universe_path):
        graphml._get_city_poi_universe(cfg.city_name, poi_cache_slug, city_poi_dir, buffer_m=buffer_m)
    universe = gpd.read_file(universe_path) if os.path.exists(universe_path) else None
    if universe is None or universe.empty:
        return []

    rows = []
    seen_keys = set()
    for query in unique_query_keys():
        if not query.tags:
            continue
        filtered = graphml._filter_by_tags(universe, query.tags)
        if filtered is None or filtered.empty or "geometry" not in filtered.columns:
            continue
        stamped = graphml._stamp_poi_raw_tag(filtered, query.tags)
        if stamped is None or "geometry" not in stamped.columns:
            continue
        shape_mask = stamped.geometry.geom_type.isin(SHAPE_GEOM_TYPES)
        stamped = stamped[shape_mask]
        if stamped.empty:
            continue

        col_vals = {col: stamped[col].to_numpy() for col in SOURCE_KEY_COLUMNS if col in stamped.columns}
        raw_tags = stamped["poi_raw_tag"].to_numpy() if "poi_raw_tag" in stamped.columns else None
        geoms = stamped.geometry.to_numpy()

        for i in range(len(stamped)):
            row_dict = {col: vals[i] for col, vals in col_vals.items()}
            key = build_poi_source_key(row_dict, geoms[i])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append({
                "source_key": key,
                "poi_type": query.poi_type,
                "tag": code_to_raw_tag.get(str(raw_tags[i]), str(raw_tags[i])) if raw_tags is not None else "unknown",
                "name": row_dict.get("name"),
                "geometry": geoms[i],
            })
    return rows


def _load_raw_fragments(raw_rows: list[dict]) -> list[dict]:
    """Every pre-merge polygon/line fragment, across all configured poi_types.

    Deliberately NOT filtered to "used by pipeline" POIs, unlike _load_all_shapes: the
    whole point of Split/Merge is to let a colleague pull in a nearby fragment the
    automatic merge excluded, which by definition may not be part of any used POI.
    """
    return [
        {
            "type": "Feature",
            "geometry": shapely.geometry.mapping(r["geometry"]),
            "properties": {k: v for k, v in r.items() if k != "geometry"},
        }
        for r in raw_rows
    ]


def _load_all_shapes(used_keys: set[str] | None, raw_rows: list[dict]) -> list[dict]:
    """Real polygon/line geometry + poi_type/tag for every used, shape-typed POI, any poi_type.

    used_keys=None skips the "actually used by the pipeline" filter entirely and
    includes every shape-typed POI matched by the configured queries instead — used
    for shapefile-mode cities (Paris), where pois_used.gpkg predates the switch to
    shapefile-sourced POIs and its source_keys (all "fid"-kind, from the old OSM-era
    cache) can no longer match anything the current shapefile source produces.

    Also computes member_keys: which raw fragments (from raw_rows) the automatic
    merge actually combined into this POI, via real geometric containment (a
    contributing fragment's centroid must fall inside the merged union) — this is
    what Split/Merge pre-checks when you open it on a POI. Candidates are grouped by
    `tag` alone, not `(poi_type, tag)`: the actual automatic merge
    (merge_nearby_polygon_pois in graphml.py) groups by poi_raw_tag only, and a
    physical element's raw tag is intrinsic to it regardless of which poi_type's
    query happened to surface it in raw_rows' own independent dedup.
    """
    code_to_raw_tag = _load_code_to_raw_tag()
    fragments_by_group = defaultdict(list)
    for r in raw_rows:
        fragments_by_group[r["tag"]].append(r)

    # member_keys needs, per POI, which raw fragments' centroids fall inside its
    # buffered geometry. A naive per-row linear scan over every same-tag fragment is
    # O(rows_in_poi_type x candidates_for_tag) — with tens of thousands of real,
    # distinct fragments per common tag (e.g. landuse=grass) after the identity fix
    # above, that's billions of checks and multi-minute hangs. Build one spatial index
    # per tag group instead, so each row does an indexed lookup.
    centroid_trees: dict[str, tuple[STRtree, list, list[dict]]] = {}
    for tag, frags in fragments_by_group.items():
        centroids = [f["geometry"].centroid for f in frags]
        centroid_trees[tag] = (STRtree(centroids), centroids, frags)

    records = []
    seen_keys = set()
    for query in unique_query_keys():
        # get_poi()'s cache-hit path returns a fast point-token form (fine for the main
        # pipeline's hot loop, useless here) — call it only to ensure the cache file
        # exists, then read that file directly for real polygon/line geometry.
        graphml.get_poi(tags=query.tags, poi_type=query.poi_type)
        cache_path = _cached_geojson_path(query)
        if not os.path.exists(cache_path):
            continue
        poi = gpd.read_file(cache_path)
        if poi is None or poi.empty or "geometry" not in poi.columns:
            continue
        shape_mask = poi.geometry.geom_type.isin(SHAPE_GEOM_TYPES)
        poi = poi[shape_mask]
        if poi.empty:
            continue

        col_vals = {col: poi[col].to_numpy() for col in SOURCE_KEY_COLUMNS if col in poi.columns}
        raw_tags = poi["poi_raw_tag"].to_numpy() if "poi_raw_tag" in poi.columns else None
        geoms = poi.geometry.to_numpy()

        for i in range(len(poi)):
            row_dict = {col: vals[i] for col, vals in col_vals.items()}
            key = build_poi_source_key(row_dict, geoms[i])
            if (used_keys is not None and key not in used_keys) or key in seen_keys:
                continue
            seen_keys.add(key)
            tag_value = code_to_raw_tag.get(str(raw_tags[i]), str(raw_tags[i])) if raw_tags is not None else "unknown"

            tolerance = geoms[i].buffer(1e-7)
            tree_entry = centroid_trees.get(tag_value)
            if tree_entry is not None:
                tree, centroids, frags = tree_entry
                candidate_idxs = tree.query(tolerance)
                member_keys = [frags[j]["source_key"] for j in candidate_idxs if tolerance.contains(centroids[j])]
            else:
                member_keys = []
            if key not in member_keys:
                member_keys.append(key)  # a merged POI is always its own member

            records.append({
                "type": "Feature",
                "geometry": shapely.geometry.mapping(geoms[i]),
                "properties": {
                    "source_key": key,
                    "poi_type": query.poi_type,
                    "tag": tag_value,
                    "name": row_dict.get("name"),
                    "member_keys": member_keys,
                },
            })
    return records


def _poi_type_order() -> list[str]:
    """poi_type values in the order they first appear in config/poi_types.csv."""
    order = []
    seen = set()
    with open(os.path.join("config", "poi_types.csv"), encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pt = row["poi_type"]
            if pt not in seen:
                seen.add(pt)
                order.append(pt)
    return order


OUTPUT_DIR = "tools/gi_bi_relabeling" if CITY == "cagliari" else "tools/gi_bi_relabeling_paris"
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "data.geojson")
POI_TYPE_ORDER_PATH = os.path.join(OUTPUT_DIR, "poi_type_order.json")
RAW_FRAGMENTS_PATH = os.path.join(OUTPUT_DIR, "raw_fragments.geojson")


def main() -> None:
    cfg = PipelineConfig(study_city=CITY)
    if cfg.use_shapefile:
        used_keys = None  # see _load_all_shapes' docstring: pois_used.gpkg is stale for Paris
    else:
        pois_used_gpkg_path = os.path.join("outputs", "poi_exports", cfg.artifact_slug, "pois_used.gpkg")
        used_keys = _used_source_keys(pois_used_gpkg_path)
    raw_rows = _raw_fragment_rows()

    features = _load_all_shapes(used_keys, raw_rows)
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f, ensure_ascii=False, separators=(",", ":"))
    with open(POI_TYPE_ORDER_PATH, "w", encoding="utf-8") as f:
        json.dump(_poi_type_order(), f, ensure_ascii=False, separators=(",", ":"))

    raw_fragments = _load_raw_fragments(raw_rows)
    with open(RAW_FRAGMENTS_PATH, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": raw_fragments}, f, ensure_ascii=False, separators=(",", ":"))

    used_desc = "all (no used-filter)" if used_keys is None else str(len(used_keys))
    print(f"Wrote {OUTPUT_PATH} ({len(features)} shapes, {used_desc} used POIs total)")
    print(f"Wrote {RAW_FRAGMENTS_PATH} ({len(raw_fragments)} raw fragments)")


if __name__ == "__main__":
    main()
