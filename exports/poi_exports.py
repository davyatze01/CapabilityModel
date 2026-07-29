import json
import os
import pickle
import shutil
from collections import OrderedDict
from typing import Any

import gc
import shapely
import geopandas as gpd
import pandas as pd
from shapely.geometry.base import BaseGeometry

from core.context import PipelineContext
from exports.hex_shard_writer import HexShardWriter
from core.pipeline_types import AccessibilityStageResult, NonBusRoutingStageResult, SnappingStageResult
from utils import capabilities as cap_mod, delta_g, graphml, services as serv
from utils.graphml import _ElapsedTimer
from utils.poi_identity import SOURCE_KEY_COLUMNS, build_poi_source_key
from stages.snapping_stage import select_best_snap_candidate_for_origin


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

    queries = list(serv.unique_query_keys())
    for q_idx, query in enumerate(queries, 1):
        poi = _query_frame(query)
        if poi is None or poi.empty:
            continue
        if "geometry" not in poi.columns:
            if "__snap_coord" not in poi.columns:
                continue
            # Cached GeoJSON is loaded as a plain DataFrame (no GEOS objects) to avoid
            # segfaults on complex Paris geometries. Build Point geometries from the
            # pre-extracted snap coordinates; Point(lon, lat) construction is safe.
            coords = poi["__snap_coord"]
            poi = poi.copy()
            poi["geometry"] = gpd.array.GeometryArray(
                shapely.points(
                    [c[0] for c in coords],
                    [c[1] for c in coords],
                )
            )
            poi = gpd.GeoDataFrame(poi, geometry="geometry", crs="EPSG:4326")
        print(
            f"[POI Export] Collecting records {q_idx}/{len(queries)}: "
            f"poi_type={query.poi_type} rows={len(poi)} unique_so_far={len(records)}",
            flush=True,
        )

        # Filter null/empty geometries up front on the whole column (vectorized).
        geom_col = poi.geometry
        valid_mask = geom_col.notna() & ~geom_col.is_empty & ~shapely.is_missing(geom_col.values)
        poi_valid = poi[valid_mask]

        if not poi_valid.empty:
            # Compute centroids for the whole filtered frame at once using the
            # shapely 2.x ufunc path — avoids the GEOS interior-point algorithm
            # used by representative_point(), which hard-crashes the interpreter
            # on certain complex Paris/large-city polygons.
            points_arr = shapely.centroid(poi_valid.geometry.values)

            # Compute the "missing centroid" mask once, vectorized over the whole
            # array (the supported shapely ufunc path). Calling shapely.is_missing()
            # on a single indexed scalar geometry per-row segfaults the interpreter
            # on this GEOS build, the same way representative_point() does above.
            points_missing = shapely.is_missing(points_arr)

            # Extract only the columns needed for source-key + address; avoids
            # materialising a full wide pd.Series per row (the iterrows() crash path).
            col_vals = {col: poi_valid[col].to_numpy() for col in SOURCE_KEY_COLUMNS if col in poi_valid.columns}
            geoms_arr = poi_valid.geometry.to_numpy()

            poi_type_str = str(query.poi_type)
            for i in range(len(poi_valid)):
                point = points_arr[i]
                if point is None or points_missing[i]:
                    continue
                try:
                    lat = float(point.y)
                    lon = float(point.x)
                except Exception:
                    continue

                row_dict = {col: vals[i] for col, vals in col_vals.items()}
                key = build_poi_source_key(row_dict, geoms_arr[i])

                record = records.get(key)
                if record is None:
                    records[key] = {
                        "source_key": key,
                        "geometry": point,
                        "poi_types": {poi_type_str},
                        "address": _compose_address(row_dict),
                        "lat": lat,
                        "lon": lon,
                    }
                else:
                    if not record.get("address"):
                        record["address"] = _compose_address(row_dict)
                    record["lat"] = lat
                    record["lon"] = lon
                    record["poi_types"].add(poi_type_str)

        # Explicitly release the GeoDataFrame and its GEOS objects before the next
        # poi_type is loaded — prevents GEOS heap accumulation across poi_types.
        del poi_valid, poi
        gc.collect()

    # Everything needed has already been extracted into `records` above. In shapefile
    # mode (Paris), utils.load_shapefile keeps the full merged POI GeoDataFrame cached
    # for the process's entire lifetime — on a 290k+ POI city that's a large permanent
    # cost sitting on top of `records` itself and every per-query .copy(). Release it
    # now; the next call (this runs once pre-routing and once post-routing) will just
    # re-read the shapefile from disk, trading a one-time I/O cost for not holding it
    # in RAM between calls.
    from utils.load_shapefile import clear_poi_shp_cache
    clear_poi_shp_cache()
    gc.collect()

    print(f"[POI Export] Collected {len(records)} unique POIs — sorting...", flush=True)
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

    print(f"[POI Export] Built {len(export_rows)} export rows.", flush=True)
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
    drop_map: dict[str, set[str]] | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    """Compute service_power and capability_power for one POI from one hexagon.

    Uses per-POI accessibility (accessibility_by_poi[source_key]) when available,
    falling back to per-poi-type accessibility (node_scores) otherwise.

    service_power[service]  = poi_accessibility × SERVICE_SINGLETON_M[service][poi_type]
                              summed over every poi_type this POI belongs to that
                              contributes to the service.
    capability_power[cap]   = same but also weighted by CAP_ELECTRE_W[cap][service].
    Only non-zero entries are included.

    Per-service ownership dedup: `drop_map` is `{poi_type: {source_keys to drop}}` (from
    utils.poi_dedup, the same map the accessibility stage applies). When a physical POI is
    owned by another poi_type of the same service, its non-owning poi_type is skipped here
    too, so service_power/capability_power never double-count it — keeping per-POI powers
    consistent with the deduplicated accessibility values.
    """
    poi_acc = accessibility_by_poi.get(source_key)
    drop_map = drop_map or {}

    service_power: dict[str, float] = {}
    for service in serv.SERVICE_KEYS:
        singletons = serv.SERVICE_SINGLETON_M.get(service, {})
        sp = 0.0
        for pt in poi_types:
            if pt not in singletons:
                continue
            # Skip the poi_type that does not own this physical POI in its service
            # (mirrors accessibility_stage zeroing dropped (poi_type, source_key) pairs).
            if source_key in drop_map.get(pt, ()):
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


def _stream_hexagon_export(
    poi_rows: list[dict[str, Any]],
    ctx: PipelineContext,
    non_bus: NonBusRoutingStageResult,
    writer: HexShardWriter,
) -> int:
    """Stream per-hexagon POI id lists straight into the shard writer.

    The previous implementation materialized the full hexagon->POIs dict for
    every node, then copied it again for compaction, before writing a single
    byte — O(all hexagons x POIs) memory, which OOMed on large cities. Here
    each hexagon is encoded and appended to its shard part file the moment it
    is built, so memory stays O(one hexagon) regardless of city size.

    Score computation (sp/cp) is still omitted at this stage: it is produced
    by the separate post-pipeline pass (score_report.py), which overwrites
    this id-only export with the powered one.
    """
    poi_index = _build_poi_index(poi_rows)
    non_bus_cache_dir = ctx.config.non_bus_cache_dir
    nodes = ctx.nodes_with_coords
    total = len(nodes) if hasattr(nodes, "__len__") else None

    for idx, (node_id, data) in enumerate(nodes, start=1):
        hexagon_id = data.get("hex_id") or _normalize_scalar(node_id)
        poi_seen: set[int] = set()

        # Real per-origin reachability data (which POIs this specific node can
        # actually reach), loaded on demand and discarded immediately so memory
        # never scales with node count.
        path = non_bus.cache_paths.get(node_id)
        if path is None and non_bus_cache_dir:
            path = os.path.join(non_bus_cache_dir, f"{node_id}.pkl")
        if path and os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    payload = pickle.load(f)
                service_payload = payload.get("services", {}) if isinstance(payload, dict) else {}
                for service in serv.SERVICE_KEYS:
                    for entry in service_payload.get(service, []):
                        if not isinstance(entry, dict):
                            continue
                        for source_key_raw in entry.get("source_keys", []):
                            poi_row = poi_index.get(str(source_key_raw))
                            if poi_row is not None:
                                poi_seen.add(int(poi_row["id"]))
            except Exception:
                pass

        if poi_seen:
            writer.add_hexagon(str(hexagon_id), [{"i": pid} for pid in sorted(poi_seen)])

        if idx % 500 == 0 or (total is not None and idx == total):
            print(
                f"[POI Export] hexagon stream: {idx}/{total if total is not None else '?'} nodes, "
                f"{writer.hex_count} hexagons written",
                flush=True,
            )

    return writer.hex_count


def _write_poi_type_accessibility(
    ctx: PipelineContext,
    acc: AccessibilityStageResult,
    output_path: str,
) -> int:
    """Write one small JSON(+JS) artifact: hex_id -> {poi_type: accessibility}.

    This is the per-poi_type aggregate accessibility (`A^i_k(x)` -- computed in
    accessibility_stage.py, before it's weighted by SERVICE_SINGLETON_M into
    service_power), one value per (hexagon, poi_type). Distinct from the
    per-POI sp/cp already in the hex_pois shards: this is "how accessible is
    this hexagon to POIs of this type overall", not any individual POI's
    contribution. Small (hexagons x poi_types -- tens of thousands of values,
    not millions), so a single flat file is enough; no sharding/compression
    needed the way the per-POI export requires.

    Only nonzero values are kept per hexagon, matching the sparse-by-default
    convention used elsewhere in this file (sp/cp, id-only POI records).
    """
    hex_id_by_node = {
        node_id: (data.get("hex_id") or _normalize_scalar(node_id))
        for node_id, data in ctx.nodes_with_coords
    }

    by_hex: dict[str, dict[str, float]] = {}
    for node_result in acc.node_results:
        hex_id = hex_id_by_node.get(node_result.node_id)
        if hex_id is None:
            continue
        poi_type_acc: dict[str, float] = {}
        for items in node_result.accessibility_by_service.values():
            for item in items:
                value = float(item.get("accessibility", 0.0))
                if value > 0.0:
                    poi_type_acc[item["poi_type"]] = round(value, 6)
        if poi_type_acc:
            by_hex[str(hex_id)] = poi_type_acc

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(by_hex, f, separators=(",", ":"))
    # Script-tag twin: the web interface runs over file://, where fetch() of
    # local JSON is blocked by the browser (same reason every other data file
    # it loads arrives as a .js global -- see interface_handoff/hex_shard_loader.js).
    js_path = os.path.splitext(output_path)[0] + ".js"
    with open(js_path, "w", encoding="utf-8") as f:
        f.write("window.POI_TYPE_ACCESSIBILITY = ")
        json.dump(by_hex, f, separators=(",", ":"))
        f.write(";\n")

    return len(by_hex)


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

    print(f"[POI Export] Building GeoDataFrame ({len(poi_rows)} rows)...", flush=True)
    poi_gdf = gpd.GeoDataFrame(poi_rows, geometry="geometry", crs="EPSG:4326")
    # The hexagon<->POI relationship is exported via shard files, so the per-POI
    # hexagon column is dropped from the GPKG.
    poi_gdf = poi_gdf[["id", "source_key", "lon", "lat", "angular_coords", "poi_types", "svc_map", "geometry"]].copy()
    poi_gdf = poi_gdf[poi_gdf.geometry.notna() & ~poi_gdf.geometry.is_empty]
    with _ElapsedTimer(f"POI Export GPKG write ({len(poi_gdf)} rows)"):
        poi_gdf.to_file(ctx.config.poi_export_geopackage_path, driver="GPKG", layer="pois_used")

    legacy_paths = (
        ctx.config.hexagon_service_pois_path,
        os.path.join(ctx.config.poi_export_dir, "hexagon_service_pois_scores.json"),
    )

    # Without non_bus there is no per-origin reachability data yet (that's the
    # pre-routing export, called before routing has run), and `snap` is only a
    # city-wide, node-independent map of POI type -> every instance of that type
    # anywhere in the city — it carries no per-origin relevance at all. Building a
    # per-hexagon list from it would give (approximately) the entire city's POIs to
    # every hexagon: wrong, and at Paris scale an OOM. The pre-routing export's
    # value is the id-only GeoPackage; the authoritative hexagon export is
    # regenerated post-routing once non_bus exists.
    hexagons_written = 0
    if non_bus is not None:
        writer = HexShardWriter(ctx.config.hex_pois_dir, ctx.config.artifact_slug, scale_powers=True)
        with _ElapsedTimer(f"POI Export hexagon stream ({len(poi_rows)} POIs)"):
            hexagons_written = _stream_hexagon_export(poi_rows, ctx, non_bus, writer)
        if hexagons_written:
            with _ElapsedTimer("POI Export shard finalize + zip"):
                hex_files_info = writer.finalize(zip_path=ctx.config.hex_pois_zip_path)

    if acc is not None and acc.node_results:
        with _ElapsedTimer("POI Export poi_type accessibility"):
            n_hexes = _write_poi_type_accessibility(ctx, acc, ctx.config.poi_type_accessibility_path)
        print(
            f"[POI Export] Wrote poi_type accessibility for {n_hexes} hexagons: "
            f"{ctx.config.poi_type_accessibility_path}",
            flush=True,
        )

    if not hexagons_written:
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

    for path in legacy_paths:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    print(
        "[Output] POI export: "
        f"{ctx.config.poi_export_geopackage_path} | {hex_files_info['hex_pois_dir']}"
        f" | {ctx.config.hex_pois_zip_path} ({hex_files_info['hex_pois_count']} hexagons"
        f" in {hex_files_info['shard_count']} shards)",
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
