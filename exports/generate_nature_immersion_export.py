"""Export high_nature_immersion POIs (with their matched raw OSM tag) for the
standalone nature-immersion browsing page.
"""

import glob
import json
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

import fiona

from utils.services import all_queries

POI_GPKG_PATH = "outputs/poi_exports/Cagliari/pois_used.gpkg"
RAW_TAGS_GLOB = "poi/Cagliari/all_tags_*.geojson"


def _high_nature_immersion_clauses() -> list[dict]:
    for q in all_queries():
        if q.poi_type == "high_nature_immersion":
            return q.tags if isinstance(q.tags, list) else [q.tags]
    raise RuntimeError("high_nature_immersion not found in config/poi_types.csv")


def _match_label(raw_tags: dict, clauses: list[dict]) -> str | None:
    """First clause (in CSV order) whose key/value pairs all match raw_tags, as 'key=value'."""
    for clause in clauses:
        if all(_tag_matches(raw_tags.get(k), v) for k, v in clause.items()):
            return ", ".join(f"{k}={raw_tags.get(k)}" for k in clause)
    return None


def _tag_matches(raw_value, expected) -> bool:
    if raw_value in (None, ""):
        return False
    if expected is True:
        return True
    if isinstance(expected, list):
        return raw_value in expected
    return raw_value == expected


def _load_raw_tags() -> dict[tuple[str, int], dict]:
    """(element_type, osmid) -> raw OSM tag dict, merged across all universe shards."""
    raw_tags: dict[tuple[str, int], dict] = {}
    for path in glob.glob(RAW_TAGS_GLOB):
        with fiona.open(path) as src:
            for feat in src:
                props = dict(feat["properties"])
                element_type = props.pop("element_type")
                osmid = props.pop("osmid")
                tags = {k: v for k, v in props.items() if v not in (None, "")}
                raw_tags[(element_type, osmid)] = tags
    return raw_tags


def _load_nature_immersion_pois(raw_tags: dict, clauses: list[dict]) -> list[dict]:
    """Filter pois_used.gpkg to high_nature_immersion POIs, each tagged with its matched raw tag."""
    records = []
    with fiona.open(POI_GPKG_PATH, layer="pois_used") as src:
        for feat in src:
            props = feat["properties"]
            poi_types = json.loads(props["poi_types"])
            if "high_nature_immersion" not in poi_types:
                continue
            source_key = json.loads(props["source_key"])
            key = (source_key["element_type"], source_key["value"])
            tags = raw_tags.get(key, {})
            label = _match_label(tags, clauses) or "unknown"
            records.append({
                "id": props["id"],
                "lon": props["lon"],
                "lat": props["lat"],
                "tag": label,
            })
    return records


OUTPUT_PATH = "tools/nature_immersion/data.json"


def main() -> None:
    clauses = _high_nature_immersion_clauses()
    raw_tags = _load_raw_tags()
    records = _load_nature_immersion_pois(raw_tags, clauses)
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {OUTPUT_PATH} ({len(records)} POIs)")


if __name__ == "__main__":
    main()
