from __future__ import annotations

import json
from typing import Any

import pandas as pd
from shapely.geometry.base import BaseGeometry


def _normalize_scalar(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (list, tuple)):
        return tuple(_normalize_scalar(item) for item in value)
    if isinstance(value, dict):
        return tuple((str(k), _normalize_scalar(v)) for k, v in sorted(value.items(), key=lambda kv: str(kv[0])))
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value


def build_poi_source_key(row: pd.Series, geom: BaseGeometry) -> str:
    """Build a stable identifier for one POI source row."""
    osmid = row.get("osmid")
    if osmid is not None:
        return json.dumps(
            {
                "kind": "osmid",
                "value": _normalize_scalar(osmid),
                "element_type": _normalize_scalar(row.get("element_type")),
            },
            sort_keys=True,
            ensure_ascii=True,
            default=str,
        )

    for column in ("id", "fid", "objectid", "OBJECTID", "osm_id"):
        if column in row and row.get(column) is not None:
            return json.dumps(
                {
                    "kind": column.lower(),
                    "value": _normalize_scalar(row.get(column)),
                },
                sort_keys=True,
                ensure_ascii=True,
                default=str,
            )

    signature = {
        "kind": "fallback",
        "name": _normalize_scalar(row.get("name")),
        "street": _normalize_scalar(row.get("addr:street") or row.get("street") or row.get("road") or row.get("name")),
        "neighbourhood": _normalize_scalar(
            row.get("addr:neighbourhood")
            or row.get("neighbourhood")
            or row.get("suburb")
            or row.get("district")
            or row.get("quarter")
            or row.get("city_district")
            or row.get("locality")
        ),
        "cap": _normalize_scalar(row.get("addr:postcode") or row.get("postcode") or row.get("postal_code") or row.get("CAP")),
        "geometry": getattr(geom, "wkb_hex", str(geom)),
    }
    return json.dumps(signature, sort_keys=True, ensure_ascii=True, default=str)
