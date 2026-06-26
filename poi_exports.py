import json
import os
import pickle
import shutil
import zipfile
from collections import OrderedDict
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry.base import BaseGeometry

from context import PipelineContext
from pipeline_types import AccessibilityStageResult, NonBusRoutingStageResult, SnappingStageResult
from utils import capabilities as cap_mod, delta_g, graphml, services as serv
from utils.poi_identity import build_poi_source_key
from snapping_stage import select_best_snap_candidate_for_origin


_ADDRESS_FIELDS = {
    "street": ("addr:street", "street", "road", "name"),
    "neighbourhood": (
        "addr:neighbourhood",
        "neighbourhood",
        "suburb",
        "district",
        "quarter",
        "city_district",
        "locality",
    ),
    "cap": ("addr:postcode", "postcode", "postal_code", "CAP"),
}


def _first_text(row: pd.Series, candidates: tuple[str, ...]) -> str | None:
    for column in candidates:
        if column not in row:
            continue
        value = row.get(column)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() != "nan":
            return text
    return None


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


def _geometry_to_point(geom: BaseGeometry) -> BaseGeometry:
    if geom.geom_type == "Point":
        return geom
    return geom.representative_point()


def _format_angular_coords(lat: float, lon: float) -> str:
    lat_hemi = "N" if lat >= 0 else "S"
    lon_hemi = "E" if lon >= 0 else "W"
    return f"{abs(lat):.6f}°{lat_hemi}, {abs(lon):.6f}°{lon_hemi}"


def _compose_address(row: pd.Series) -> str:
    street = _first_text(row, _ADDRESS_FIELDS["street"])
    neighbourhood = _first_text(row, _ADDRESS_FIELDS["neighbourhood"])
    cap = _first_text(row, _ADDRESS_FIELDS["cap"])

    parts = []
    if street:
        parts.append(f"street={street}")
    if neighbourhood:
        parts.append(f"neighbourhood={neighbourhood}")
    if cap:
        parts.append(f"CAP={cap}")
    return "; ".join(parts)


def _build_poi_type_service_lookup() -> dict[str, list[str]]:
    poi_to_services: dict[str, set[str]] = {}
    for service, poi_types in serv.get_service_poi_types().items():
        for poi_type in poi_types:
            poi_to_services.setdefault(poi_type, set()).add(service)
    return {
        poi_type: sorted(services)
        for poi_type, services in poi_to_services.items()
    }


def _query_frame(query: serv.PoiQuery) -> gpd.GeoDataFrame:
    feature, value, tags = delta_g._resolve_query(query.poi_type, None, query.tags)
    return graphml.get_poi(feature=feature, value=value, tags=tags, poi_type=query.poi_type)


def _collect_poi_records() -> list[dict[str, Any]]:
    poi_type_to_services = _build_poi_type_service_lookup()
    records: dict[str, dict[str, Any]] = OrderedDict()

    for query in serv.unique_query_keys():
        poi = _query_frame(query)
        if poi is None or poi.empty:
            continue
        if "geometry" not in poi.columns:
            continue

        for _, row in poi.iterrows():
            geom = row.get("geometry")
            if geom is None or getattr(geom, "is_empty", False):
                continue
            point = _geometry_to_point(geom)
            key = build_poi_source_key(row, point)
            record = records.get(key)
            if record is None:
                record = {
                    "source_key": key,
                    "geometry": point,
                    "poi_types": set(),
                    "address": _compose_address(row),
                    "lat": float(point.y),
                    "lon": float(point.x),
                }
                records[key] = record
            else:
                if not record.get("address"):
                    record["address"] = _compose_address(row)
                record["lat"] = float(point.y)
                record["lon"] = float(point.x)

            record["poi_types"].add(str(query.poi_type))

    export_rows: list[dict[str, Any]] = []
    for idx, (key, record) in enumerate(
        sorted(
            records.items(),
            key=lambda item: (
                float(item[1]["lat"]),
                float(item[1]["lon"]),
                item[1]["address"] or "",
                item[0],
            ),
        ),
        start=1,
    ):
        poi_types = sorted(record["poi_types"])
        poi_type_services = {
            poi_type: poi_type_to_services.get(poi_type, [])
            for poi_type in poi_types
        }
        export_rows.append(
            {
                "id": idx,
                "source_key": str(key),
                "lon": float(record["lon"]),
                "lat": float(record["lat"]),
                "angular_coords": _format_angular_coords(float(record["lat"]), float(record["lon"])),
                "poi_types": json.dumps(poi_types, ensure_ascii=False, separators=(",", ":")),
                "svc_map": json.dumps(poi_type_services, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "geometry": record["geometry"],
            }
        )

    return export_rows


def _build_poi_index(poi_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row["source_key"]): row for row in poi_rows}


def _load_non_bus_payloads(ctx: PipelineContext, non_bus: NonBusRoutingStageResult) -> dict[Any, dict[str, Any]]:
    """Load cached non-bus payloads for all known node ids.

    Paths come from `non_bus.cache_paths` when available, otherwise they are derived directly
    from `config.non_bus_cache_dir`. We avoid `_non_bus_cache_path`, whose module-global cache
    dir is only initialized while `run_non_bus_routing_stage` runs — when the impedance bundle
    is loaded that stage is skipped, but the per-node `.pkl` files written by the run that
    created the bundle still exist on disk.
    """
    cache_dir = ctx.config.non_bus_cache_dir
    out: dict[Any, dict[str, Any]] = {}
    for node_id, _ in ctx.nodes_with_coords:
        path = non_bus.cache_paths.get(node_id)
        if path is None:
            if not cache_dir:
                continue
            path = os.path.join(cache_dir, f"{node_id}.pkl")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as f:
                payload = pickle.load(f)
        except Exception:
            continue
        if isinstance(payload, dict):
            out[node_id] = payload
    return out


def _poi_powers(
    source_key: str,
    poi_types: list[str],
    accessibility_by_poi: dict[str, float],
    node_scores: dict[str, dict[str, float]],
) -> tuple[dict[str, float], dict[str, float]]:
    """Compute service_power and capability_power for one POI from one hexagon.

    Uses per-POI accessibility (accessibility_by_poi[source_key]) when available,
    falling back to per-poi-type accessibility (node_scores) otherwise.

    service_power[service]  = poi_accessibility × SERVICE_SINGLETON_M[service][poi_type]
                              summed over every poi_type this POI belongs to that
                              contributes to the service.
    capability_power[cap]   = same but also weighted by CAP_ELECTRE_W[cap][service].
    Only non-zero entries are included.
    """
    poi_acc = accessibility_by_poi.get(source_key)

    service_power: dict[str, float] = {}
    for service in serv.SERVICE_KEYS:
        singletons = serv.SERVICE_SINGLETON_M.get(service, {})
        sp = 0.0
        for pt in poi_types:
            if pt not in singletons:
                continue
            if poi_acc is not None:
                acc_val = poi_acc
            else:
                # Fallback: per-poi-type accessibility from node_scores
                type_scores = node_scores.get(service, {})
                acc_val = float(type_scores.get(pt, 0.0))
            sp += acc_val * float(singletons[pt])
        if sp > 0.0:
            service_power[service] = round(sp, 8)

    capability_power: dict[str, float] = {}
    for capability, cap_services in cap_mod.CAPABILITY_SERVICES.items():
        weights = cap_mod.CAP_ELECTRE_W[capability]
        cp = 0.0
        for service in cap_services:
            sp = service_power.get(service, 0.0)
            cp += sp * float(weights.get(service, 0.0))
        if cp > 0.0:
            capability_power[capability] = round(cp, 8)

    return service_power, capability_power


def _build_hexagon_report(
    poi_rows: list[dict[str, Any]],
    ctx: PipelineContext,
    snap: SnappingStageResult | None,
    non_bus: NonBusRoutingStageResult | None,
    acc: AccessibilityStageResult | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    poi_index = _build_poi_index(poi_rows)
    pid_to_source_key: dict[int, str] = {int(row["id"]): str(row["source_key"]) for row in poi_rows}
    non_bus_payloads = _load_non_bus_payloads(ctx, non_bus) if non_bus is not None else {}

    # Main payload: hexagon id -> list of POI objects.
    # Each object has at minimum {"id": int}; when acc is provided it also carries
    # service_power and capability_power (non-zero entries only).
    # Full POI attributes live in the GeoPackage (keyed by the same `id`).
    hexagon_pois: dict[str, list[dict[str, Any]]] = {}
    score_hexagons: list[dict[str, Any]] | None = [] if acc is not None else None
    acc_by_node = {
        str(node.node_id): {
            service: {str(item.get("poi_type")): float(item.get("accessibility", 0.0)) for item in node.accessibility_by_service.get(service, [])}
            for service in serv.SERVICE_KEYS
        }
        for node in acc.node_results
    } if acc is not None else {}

    # Per-POI accessibility keyed by node_id then source_key.
    acc_by_poi_by_node: dict[str, dict[str, float]] = {
        str(node.node_id): node.accessibility_by_poi
        for node in acc.node_results
    } if acc is not None else {}

    for node_id, data in ctx.nodes_with_coords:
        # Use the stable grid hex_id if available; fall back to raw node_id.
        hexagon_id = data.get("hex_id") or _normalize_scalar(node_id)
        # poi_seen: poi_id -> list of poi_types (populated once per poi_id)
        poi_seen: dict[int, list[str]] = {}
        global_score_items: dict[str, dict[str, Any]] = {}
        node_scores = acc_by_node.get(str(node_id), {})
        node_poi_acc = acc_by_poi_by_node.get(str(node_id), {})
        origin = (float(data["y"]), float(data["x"]))

        def _upsert_item(source_key: str, service: str, snapped_coord: tuple[float, float] | None, snap_distance_m: float | None) -> None:
            poi_row = poi_index.get(source_key)
            if poi_row is None:
                return
            pid = int(poi_row["id"])
            if pid not in poi_seen:
                try:
                    pt_list = json.loads(str(poi_row.get("poi_types", "[]")))
                except Exception:
                    pt_list = []
                poi_seen[pid] = pt_list if isinstance(pt_list, list) else []
            if acc is None:
                return
            poi_types = poi_seen[pid]
            type_scores = node_scores.get(service, {})
            score_item = global_score_items.setdefault(
                source_key,
                {
                    "id": int(poi_row["id"]),
                    "source_key": source_key,
                    "hexagon_id": hexagon_id,
                    "lat": float(poi_row["lat"]),
                    "lon": float(poi_row["lon"]),
                    "address": str(poi_row.get("address", "")),
                    "poi_types": list(poi_types),
                    "services": set(),
                    "snap_lat": None,
                    "snap_lon": None,
                    "snap_distance_m": None,
                    "poi_type_scores": {},
                },
            )
            score_item["services"].add(service)
            if snapped_coord is not None:
                score_item["snap_lat"] = float(snapped_coord[0])
                score_item["snap_lon"] = float(snapped_coord[1])
            if snap_distance_m is not None:
                score_item["snap_distance_m"] = float(snap_distance_m)
            for poi_type in poi_types:
                score_item["poi_type_scores"][str(poi_type)] = float(type_scores.get(str(poi_type), 0.0))

        if snap is not None:
            for service in serv.SERVICE_KEYS:
                for query in serv.get_service_queries(service):
                    poi_key = serv.query_key(query)
                    snap_info_for_key = snap.poi_bus_snap_info_by_type.get(poi_key, {})
                    for source_key_raw, snap_info in snap_info_for_key.items():
                        snapped_coord, snap_distance_m = select_best_snap_candidate_for_origin(origin, (0.0, 0.0), snap_info)
                        _upsert_item(str(source_key_raw), service, snapped_coord, snap_distance_m)
        else:
            payload = non_bus_payloads.get(node_id, {})
            service_payload = payload.get("services", {}) if isinstance(payload, dict) else {}
            for service in serv.SERVICE_KEYS:
                service_entries = service_payload.get(service, []) if isinstance(service_payload, dict) else []
                if not isinstance(service_entries, list):
                    service_entries = []
                for entry in service_entries:
                    if not isinstance(entry, dict):
                        continue
                    entry_source_keys = entry.get("source_keys", [])
                    if not isinstance(entry_source_keys, list):
                        continue
                    for source_key_raw in entry_source_keys:
                        _upsert_item(str(source_key_raw), service, None, None)

        if poi_seen:
            poi_objects: list[dict[str, Any]] = []
            for pid in sorted(poi_seen.keys()):
                obj: dict[str, Any] = {"id": pid}
                if acc is not None:
                    _sk = pid_to_source_key.get(pid, "")
                    sp, cp = _poi_powers(_sk, poi_seen[pid], node_poi_acc, node_scores)
                    if sp:
                        obj["service_power"] = sp
                    if cp:
                        obj["capability_power"] = cp
                poi_objects.append(obj)
            hexagon_pois[str(hexagon_id)] = poi_objects

        if score_hexagons is not None:
            sorted_score_items = [
                {**item, "services": sorted(item["services"])}
                for item in sorted(global_score_items.values(), key=lambda row: (int(row["id"]), row["source_key"]))
            ]
            score_hexagons.append(
                {
                    "hexagon_id": _normalize_scalar(node_id),
                    "lat": float(data["y"]),
                    "lon": float(data["x"]),
                    "poi_count": len(sorted_score_items),
                    "pois": sorted_score_items,
                }
            )

    return {
        "schema": "hexagon_poi_powers_v1",
        "hexagons": hexagon_pois,
    }, (
        {
            "schema": "hexagon_poi_report_v1_scores",
            "hexagons": score_hexagons,
        }
        if score_hexagons is not None
        else None
    )


def _compact_hex_payload(hexagons: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Shrink the hexagon->POI map using short keys for browser delivery.

    Full POI attributes live in the pois_used GeoPackage (keyed by `id`), so each object keeps
    only the POI id plus its non-empty power maps: id -> "i", service_power -> "sp",
    capability_power -> "cp". `sp`/`cp` are omitted when absent/empty (e.g. the pre-routing,
    acc-less export, where objects are id-only).
    """
    compact: dict[str, list[dict[str, Any]]] = {}
    for hex_id, poi_objs in hexagons.items():
        items: list[dict[str, Any]] = []
        for obj in poi_objs:
            item: dict[str, Any] = {"i": obj["id"]}
            sp = obj.get("service_power")
            cp = obj.get("capability_power")
            if sp:
                item["sp"] = sp
            if cp:
                item["cp"] = cp
            items.append(item)
        compact[hex_id] = items
    return compact


def _write_hex_poi_files(
    compact: dict[str, list[dict[str, Any]]],
    out_dir: str,
    slug: str,
    zip_path: str,
) -> dict[str, Any]:
    """Write one JSONP `.js` file per hexagon plus a manifest, then zip the directory.

    JSONP (not `.json`) so the offline OpenLayers interface can load a single hexagon on demand
    via `<script>` injection — browsers block fetch()/XHR of local files over file://, but not
    script tags. The zip is a convenience artifact to hand to the interface developer.
    """
    # Regenerate from scratch so hexagons removed since a prior run don't linger.
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    for hex_id, items in compact.items():
        body = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(out_dir, f"{hex_id}.js"), "w", encoding="utf-8") as f:
            f.write(f'__onHexPois("{hex_id}",{body});')

    hex_ids = sorted(compact.keys())
    manifest = {
        "schema": "hexagon_poi_powers_v1",
        "slug": slug,
        "count": len(hex_ids),
        "hex_ids": hex_ids,
    }
    manifest_body = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(out_dir, "index.js"), "w", encoding="utf-8") as f:
        f.write(f"__onHexPoisManifest({manifest_body});")

    # Zip every generated file (per-hexagon + manifest), flat inside the archive.
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(os.listdir(out_dir)):
            if name.endswith(".js"):
                zf.write(os.path.join(out_dir, name), arcname=name)

    return {
        "hex_pois_dir": out_dir,
        "hex_pois_zip_path": zip_path,
        "hex_pois_count": len(hex_ids),
    }


def generate_poi_exports(
    ctx: PipelineContext,
    snap: SnappingStageResult | None = None,
    non_bus: NonBusRoutingStageResult | None = None,
    acc: AccessibilityStageResult | None = None,
) -> dict[str, str]:
    """Export POIs used by the current run and optionally the hexagon/service maps."""
    os.makedirs(ctx.config.poi_export_dir, exist_ok=True)

    poi_rows = _collect_poi_records()
    if not poi_rows:
        raise ValueError("No POIs were collected for export.")

    poi_gdf = gpd.GeoDataFrame(poi_rows, geometry="geometry", crs="EPSG:4326")
    hex_payload, _hex_scores_payload = _build_hexagon_report(poi_rows, ctx, snap, non_bus, acc)
    # The hexagon<->POI relationship is exported via per-hex files, so the per-POI
    # hexagon column is dropped from the GPKG.
    poi_gdf = poi_gdf[["id", "source_key", "lon", "lat", "angular_coords", "poi_types", "svc_map", "geometry"]].copy()
    poi_gdf.to_file(ctx.config.poi_export_geopackage_path, driver="GPKG", layer="pois_used")

    legacy_paths = (
        ctx.config.hexagon_service_pois_path,
        os.path.join(ctx.config.poi_export_dir, "hexagon_service_pois_scores.json"),
    )
    has_hexagon_payload = bool(hex_payload.get("hexagons"))

    if not has_hexagon_payload:
        for path in (*legacy_paths, ctx.config.hex_pois_zip_path):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        if os.path.isdir(ctx.config.hex_pois_dir):
            try:
                shutil.rmtree(ctx.config.hex_pois_dir)
            except OSError:
                pass
        print(
            "[Output] POI export: "
            f"{ctx.config.poi_export_geopackage_path} (hexagon/service update deferred)",
            flush=True,
        )
        print(f"[Output] POI export rows: {len(poi_rows)}", flush=True)
        return {
            "poi_geopackage_path": ctx.config.poi_export_geopackage_path,
            "poi_shapefile_path": ctx.config.poi_export_geopackage_path,
        }

    # Export the compact hexagon->POI map as one JSONP `.js` file per hexagon
    # (+ manifest), then zip that directory for the offline interface.
    compact_hexagons = _compact_hex_payload(hex_payload["hexagons"])
    hex_files_info = _write_hex_poi_files(
        compact_hexagons,
        ctx.config.hex_pois_dir,
        ctx.config.artifact_slug,
        ctx.config.hex_pois_zip_path,
    )

    for path in legacy_paths:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    print(
        "[Output] POI export: "
        f"{ctx.config.poi_export_geopackage_path} | {hex_files_info['hex_pois_dir']}"
        f" | {ctx.config.hex_pois_zip_path} ({hex_files_info['hex_pois_count']} hex files)",
        flush=True,
    )
    print(f"[Output] POI export rows: {len(poi_rows)}", flush=True)
    return {
        "poi_geopackage_path": ctx.config.poi_export_geopackage_path,
        "poi_shapefile_path": ctx.config.poi_export_geopackage_path,
        "hex_pois_dir": hex_files_info["hex_pois_dir"],
        "hex_pois_zip_path": hex_files_info["hex_pois_zip_path"],
        "hex_pois_count": str(hex_files_info["hex_pois_count"]),
    }
