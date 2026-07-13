"""Per-service POI ownership / de-duplication for OSM-downloaded POIs.

A single physical OSM element can be matched by several `poi_type`s of the *same*
service, because their tag clauses overlap (e.g. ``{"leisure": "sports_centre"}`` is
listed by ``organised_sport_indoor``, ``organized_sport_outdoor`` and
``informal_sport_indoor`` — all in ``sport_and_movement``). Routing/accessibility is
computed independently per poi_type, so without intervention that one POI is counted
two or three times in the service's Choquet aggregation.

This module decides, per service, which single poi_type "owns" each shared physical
POI. The ownership rule is:

    owner = the poi_type whose matched tag clause is the MOST SPECIFIC
            (largest number of key/value conditions);
            ties are broken by service config order (earliest poi_type wins).

It is the single source of truth for both the pipeline fix (``build_drop_map`` →
``accessibility_stage``) and the verification dashboard (``debug_poi_dedup.py``).

IMPORTANT: this only applies when POIs are downloaded from OSM. With a shapefile
source each feature already carries exactly one ``poi_type`` label, so there is no
tag-overlap conflict and the specificity logic (which needs OSM tag columns) does not
apply — see :func:`is_osm_mode`.
"""

from __future__ import annotations

import json
import os
from typing import Any

from utils import graphml, delta_g
from utils import services as serv
from utils.poi_identity import build_poi_source_key

# Identity of a physical POI within the model.
SourceKey = str


def is_osm_mode(cfg) -> bool:
    """Whether POIs are downloaded from OSM (the only mode dedup applies to).

    Mirrors the branch ``graphml._download_city_poi_universe`` uses: a shapefile
    source (``use_shapefile``) or a pre-built POI shapefile (``poi_from_shp``) means
    each feature is already labelled with a single poi_type, so there is nothing to
    de-duplicate.
    """
    return not bool(getattr(cfg, "use_shapefile", False)) and not bool(getattr(cfg, "poi_from_shp", False))


def _clauses_for_query(query) -> list[dict[str, Any]]:
    """Normalize a query's tags into a flat list of AND clauses (each a dict)."""
    tags = query.tags
    if isinstance(tags, dict):
        return [tags]
    if isinstance(tags, list):
        return [clause for clause in tags if isinstance(clause, dict) and clause]
    # Fallback to the feature/value form used when no explicit tags are configured.
    feature, value, _ = delta_g._resolve_query(query.poi_type, None, None)
    return [{feature: value}]


def _geom_lat_lon(geom: Any) -> tuple[float | None, float | None]:
    """Best-effort (lat, lon) for a POI geometry, for the dashboard's map links.

    `geom` is either the plain-dict cached form (``{"snap_coord": (lat, lon), ...}``,
    see graphml._read_geojson_without_gdal) or a real shapely geometry in EPSG:4326.
    """
    if isinstance(geom, dict):
        coord = geom.get("snap_coord")
        if isinstance(coord, (list, tuple)) and len(coord) >= 2:
            try:
                return float(coord[0]), float(coord[1])
            except (TypeError, ValueError):
                return None, None
        return None, None
    try:
        center = geom.centroid
        return float(center.y), float(center.x)
    except Exception:
        return None, None


def compute_specificity_by_type(cfg) -> dict[str, dict[SourceKey, dict[str, Any]]]:
    """Map each poi_type to the physical POIs it matches and how specifically.

    Returns ``{poi_type: {source_key: {"best_specificity": int,
    "matched_clauses": [clause, ...], "name": str|None, "osm_tags": {...},
    "lat": float|None, "lon": float|None}}}``.

    Source keys are built via the same :func:`graphml.get_poi_geometries` /
    :func:`build_poi_source_key` path used by the snapping stage, so they are directly
    comparable to the non-bus cache keys. Matching is done per clause with
    :func:`graphml._filter_by_clause` (the exact filter used to build each poi_type's
    POI set), which avoids any per-row pandas access over wide OSM frames.
    """
    result: dict[str, dict[SourceKey, dict[str, Any]]] = {}
    # One query per poi_type is enough (a poi_type's tags are identical wherever it
    # appears); de-duplicate so we fetch each POI set only once.
    seen_types: set[str] = set()
    for query in serv.all_queries():
        poi_type = str(query.poi_type)
        if poi_type in seen_types:
            continue
        seen_types.add(poi_type)

        gdf = graphml.get_poi(tags=query.tags, poi_type=poi_type)
        per_sk: dict[SourceKey, dict[str, Any]] = {}
        # The city-universe often comes back as a plain DataFrame (loaded via
        # _read_geojson_without_gdal to avoid GDAL/GEOS), which carries no "geometry"
        # column — it stores "__snap_coord"/"__geometry_token" instead. get_poi_geometries
        # handles both forms, so accept either here; requiring "geometry" made this bail on
        # every poi_type and silently emptied the dedup drop map.
        if gdf is None or gdf.empty or (
            "geometry" not in gdf.columns and "__snap_coord" not in gdf.columns
        ):
            result[poi_type] = per_sk
            continue

        for clause in _clauses_for_query(query):
            sub = graphml._filter_by_clause(gdf, clause)
            if sub is None or sub.empty:
                continue
            spec = len(clause)
            for geom, name, source_key in graphml.get_poi_geometries(sub):
                entry = per_sk.get(source_key)
                if entry is None:
                    lat, lon = _geom_lat_lon(geom)
                    per_sk[source_key] = {
                        "best_specificity": spec,
                        "matched_clauses": [clause],
                        "name": str(name) if name is not None else None,
                        "osm_tags": dict(clause),
                        "lat": lat,
                        "lon": lon,
                    }
                else:
                    if clause not in entry["matched_clauses"]:
                        entry["matched_clauses"].append(clause)
                    entry["best_specificity"] = max(entry["best_specificity"], spec)
                    entry["osm_tags"].update(clause)
                    if not entry.get("name") and name is not None:
                        entry["name"] = str(name)
        result[poi_type] = per_sk
    return result


def build_service_ownership(cfg, specificity_by_type: dict | None = None) -> dict[str, Any]:
    """Resolve, per service, the single owning poi_type for each physical POI.

    Returns a structure carrying a full decision trace for the dashboard::

        {service: {
            "poi_types": [...],                 # config order
            "pois": {source_key: {
                "name": str|None,
                "osm_tags": {...},
                "lat": float|None, "lon": float|None,
                "candidates": [{"poi_type", "matched_clauses", "best_specificity",
                                "config_index"}],
                "owner": poi_type,
                "is_conflict": bool,            # matched by >1 poi_type
                "tie_broken_by_config_order": bool,
            }},
        }}

    Empty when not in OSM mode.
    """
    if not is_osm_mode(cfg):
        return {}
    if specificity_by_type is None:
        specificity_by_type = compute_specificity_by_type(cfg)

    service_poi_types = serv.get_service_poi_types()
    out: dict[str, Any] = {}

    for service, poi_types in service_poi_types.items():
        order_index = {pt: i for i, pt in enumerate(poi_types)}
        pois: dict[SourceKey, dict[str, Any]] = {}

        # Gather every (poi_type, source_key) candidate for this service.
        for pt in poi_types:
            for source_key, meta in specificity_by_type.get(pt, {}).items():
                entry = pois.setdefault(
                    source_key,
                    {
                        "name": meta.get("name"),
                        "osm_tags": meta.get("osm_tags", {}),
                        "lat": meta.get("lat"),
                        "lon": meta.get("lon"),
                        "candidates": [],
                    },
                )
                if entry.get("lat") is None and meta.get("lat") is not None:
                    entry["lat"] = meta.get("lat")
                    entry["lon"] = meta.get("lon")
                if not entry.get("name"):
                    entry["name"] = meta.get("name")
                if not entry.get("osm_tags"):
                    entry["osm_tags"] = meta.get("osm_tags", {})
                entry["candidates"].append(
                    {
                        "poi_type": pt,
                        "matched_clauses": meta.get("matched_clauses", []),
                        "best_specificity": int(meta.get("best_specificity", 0)),
                        "config_index": order_index[pt],
                    }
                )

        # Resolve owner per POI: max specificity, ties → earliest config index.
        tie_pairs: dict[tuple[str, ...], int] = {}
        for source_key, entry in pois.items():
            candidates = entry["candidates"]
            best_spec = max(c["best_specificity"] for c in candidates)
            top = [c for c in candidates if c["best_specificity"] == best_spec]
            owner = min(top, key=lambda c: c["config_index"])
            entry["owner"] = owner["poi_type"]
            entry["is_conflict"] = len(candidates) > 1
            entry["tie_broken_by_config_order"] = len(top) > 1
            if len(top) > 1:
                pair = tuple(sorted(c["poi_type"] for c in top))
                tie_pairs[pair] = tie_pairs.get(pair, 0) + 1

        # Equal-specificity ties mean ownership silently depends on config row order —
        # after the tag-clause disambiguation these should be rare (co-occurring tags on
        # one element), so surface every combination loudly instead of hiding it.
        for pair, count in sorted(tie_pairs.items(), key=lambda kv: -kv[1]):
            winner = min(pair, key=lambda pt: order_index[pt])
            print(
                f"[DEDUP][WARN] service={service}: {count} POI(s) tied at equal "
                f"specificity between {' / '.join(pair)}; config order gave them "
                f"to {winner}.",
                flush=True,
            )

        out[service] = {"poi_types": list(poi_types), "pois": pois}

    return out


def build_drop_map(cfg, ownership: dict | None = None) -> dict[str, list[SourceKey]]:
    """Per poi_type, the source_keys it must DROP (owned by another poi_type).

    This is what the accessibility stage consumes. Empty in non-OSM mode.
    """
    if not is_osm_mode(cfg):
        return {}
    if ownership is None:
        ownership = build_service_ownership(cfg)

    drop: dict[str, set[SourceKey]] = {}
    for service_info in ownership.values():
        for source_key, entry in service_info["pois"].items():
            owner = entry["owner"]
            for cand in entry["candidates"]:
                pt = cand["poi_type"]
                if pt != owner:
                    drop.setdefault(pt, set()).add(source_key)
    return {pt: sorted(keys) for pt, keys in drop.items()}


def write_drop_map(cfg, drop_map: dict | None = None) -> str:
    """Persist the drop map JSON to ``cfg.poi_ownership_drop_path``.

    Writes ``{}`` in non-OSM mode so the accessibility workers reliably no-op.
    Returns the path written.
    """
    if drop_map is None:
        drop_map = build_drop_map(cfg)
    path = cfg.poi_ownership_drop_path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(drop_map, f, ensure_ascii=True)
    os.replace(tmp_path, path)
    return path


def load_drop_map(path: str) -> dict[str, set[SourceKey]]:
    """Load a persisted drop map as ``{poi_type: set(source_keys)}`` (empty if absent)."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    return {str(pt): {str(sk) for sk in keys} for pt, keys in raw.items()}
