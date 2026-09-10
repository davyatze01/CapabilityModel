import ctypes
import json
import multiprocessing as mp
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
                    [c[1] for c in coords],  # lon = x
                    [c[0] for c in coords],  # lat = y
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


def _build_poi_index(poi_rows: list[dict[str, Any]]) -> dict[str, int]:
    """source_key -> POI id only. `_stream_hexagon_export`'s hot loop never reads
    anything else off a POI row, so holding the full row (geometry, json strings,
    address...) per entry for the whole node loop would be pure waste at
    hundreds-of-thousands-of-POIs scale."""
    return {str(row["source_key"]): int(row["id"]) for row in poi_rows}


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


try:
    _LIBC = ctypes.CDLL("libc.so.6")
except OSError:
    _LIBC = None


def _trim_export_memory() -> None:
    """Return freed heap memory to the OS. See non_bus_routing_stage._trim_worker_memory
    for the incident this mirrors: gc.collect() clears reference cycles (glibc can't
    reclaim memory still reachable via one), then malloc_trim(0) asks glibc to release
    now-free top-of-heap arenas back to the kernel. No-op off glibc Linux."""
    gc.collect()
    if _LIBC is not None:
        try:
            _LIBC.malloc_trim(0)
        except Exception:
            pass


def _resolve_stream_tasks(
    ctx: PipelineContext,
    non_bus: NonBusRoutingStageResult,
) -> list[tuple[str, str | None]]:
    """Precompute (hex_id, non_bus_cache_path) per node in the main process (pure
    dict/path lookups, no I/O), so workers don't each need `non_bus.cache_paths`."""
    non_bus_cache_dir = ctx.config.non_bus_cache_dir
    tasks: list[tuple[str, str | None]] = []
    for node_id, data in ctx.nodes_with_coords:
        hexagon_id = str(data.get("hex_id") or _normalize_scalar(node_id))
        path = non_bus.cache_paths.get(node_id)
        if path is None and non_bus_cache_dir:
            path = os.path.join(non_bus_cache_dir, f"{node_id}.pkl")
        tasks.append((hexagon_id, path))
    return tasks


# Populated in each worker process by _init_stream_worker (via Pool initargs, pickled
# once per worker at pool startup, not per task).
_WORKER_POI_INDEX: dict[str, int] = {}
# None everywhere except Paris (see PipelineConfig.export_hex_radius_m) -- an export-only
# distance cutoff, independent of the real poi_radius_m used for accessibility/capability.
_WORKER_EXPORT_HEX_RADIUS_M: float | None = None
# Shared per-poi_type {src_keys, source_coords} catalog (see exports/artifact_bundle.py):
# origin-invariant, loaded once per worker. Each node entry's kept_idx indexes into it.
_WORKER_POI_CATALOG: dict[str, dict] = {}
_STREAM_TRIM_EVERY_N_NODES = 20  # mirrors non_bus_routing_stage._TRIM_EVERY_N_ORIGINS
_stream_trim_counter = 0  # per worker process


def _init_stream_worker(
    poi_index: dict[str, int],
    export_hex_radius_m: float | None = None,
    poi_catalog_path: str | None = None,
) -> None:
    global _WORKER_POI_INDEX, _WORKER_EXPORT_HEX_RADIUS_M, _WORKER_POI_CATALOG
    _WORKER_POI_INDEX = poi_index
    _WORKER_EXPORT_HEX_RADIUS_M = export_hex_radius_m
    if poi_catalog_path and os.path.exists(poi_catalog_path):
        with open(poi_catalog_path, "rb") as f:
            _WORKER_POI_CATALOG = pickle.load(f)
    else:
        _WORKER_POI_CATALOG = {}


def _stream_node_worker(args: tuple[str, str | None]) -> tuple[str, list[int]] | None:
    """Load one node's non-bus routing payload and return its hexagon's POI ids.

    Each call unpickles one node's non-bus routing payload -- the same per-origin
    cache non_bus_routing_stage.py warns can hit ~1.8GB for a dense origin
    (2026-07-16 incident, see its _trim_worker_memory). That stage reclaims memory
    every few origins because glibc/pymalloc doesn't reliably hand freed arenas back
    to the OS mid-process, so RSS ratchets upward across iterations even though each
    payload is logically dropped; this worker walks the exact same per-node pickles,
    so it periodically reclaims the same way.
    """
    global _stream_trim_counter
    hexagon_id, path = args
    poi_seen: set[int] = set()

    if path and os.path.exists(path):
        try:
            with open(path, "rb") as f:
                payload = pickle.load(f)
            origin = payload.get("origin") if isinstance(payload, dict) else None
            service_payload = payload.get("services", {}) if isinstance(payload, dict) else {}
            for service in serv.SERVICE_KEYS:
                for entry in service_payload.get(service, []):
                    if not isinstance(entry, dict):
                        continue
                    # source_keys/source_coords are origin-invariant, so they live once in
                    # the shared _WORKER_POI_CATALOG instead of this entry -- kept_idx
                    # resolves this entry's POIs into it.
                    catalog = _WORKER_POI_CATALOG.get(str(entry.get("poi_type")), {})
                    catalog_keys = catalog.get("src_keys")
                    catalog_coords = catalog.get("source_coords")
                    kept_idx = entry.get("kept_idx", [])
                    if catalog_keys is None:
                        continue
                    for i in kept_idx:
                        source_key_raw = catalog_keys[i].decode("ascii")
                        if _WORKER_EXPORT_HEX_RADIUS_M is not None and origin is not None and catalog_coords is not None:
                            c = catalog_coords[i]
                            if delta_g._haversine_m(origin[0], origin[1], float(c[0]), float(c[1])) > _WORKER_EXPORT_HEX_RADIUS_M:
                                continue
                        poi_id = _WORKER_POI_INDEX.get(source_key_raw)
                        if poi_id is not None:
                            poi_seen.add(poi_id)
        except Exception:
            pass

    _stream_trim_counter += 1
    if _stream_trim_counter % _STREAM_TRIM_EVERY_N_NODES == 0:
        _trim_export_memory()

    if not poi_seen:
        return None
    return hexagon_id, sorted(poi_seen)


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

    Per-node work (unpickle one non-bus payload, pull out its source_keys) is
    independent across nodes, so it's farmed out to a worker pool the same way
    non_bus_routing_stage.py parallelizes the identical per-origin payload --
    this loop was previously single-threaded and, at large-city node counts, was
    the slow part of the export. Capped by non_bus_max_workers (not ctx.workers)
    since this reads the exact same payload class that cap was tuned for, and
    recycled via maxtasksperchild for the same reason. Only the main process
    touches `writer` (shard part-file appends aren't safe to parallelize).
    """
    poi_index = _build_poi_index(poi_rows)
    tasks = _resolve_stream_tasks(ctx, non_bus)
    total = len(tasks)

    pool_workers = max(1, min(ctx.workers, ctx.config.non_bus_max_workers, total or 1))
    # Kept small (not total // (pool_workers*4)) so maxtasksperchild=200 below
    # actually fires: maxtasksperchild counts chunks, not individual items, so
    # a large chunksize can make it never trigger a worker recycle for the
    # whole run -- see the 2026-08-07 OOM incident where chunksize=720 meant
    # each worker only ever received ~4 chunk-jobs total. Mirrors
    # accessibility_chunksize (core/config.py) for the same reason.
    chunksize = 4

    # Workers here only unpickle small non-bus payloads and look up ids in poi_index --
    # never a NetworkX graph. But this runs inside the same long-lived main.py process
    # that ran routing earlier, where the full mode graphs can still be cached. Forking
    # without releasing them first inherits that multi-GB baseline copy-on-write, which
    # becomes N private copies as workers touch pages -- see clear_mode_graph_cache's own
    # docstring and non_bus_routing_stage's "Released full mode graphs before forking
    # worker pool" for the incident this mirrors.
    graphml.clear_mode_graph_cache()

    with mp.Pool(
        processes=pool_workers,
        # Windows-only: worker recycling races the Pool's result-handler thread on the
        # same overlapped pipe there and raises "concurrent send_bytes() calls are not
        # supported" (see non_bus_routing_stage.py for detail).
        maxtasksperchild=200 if os.name != "nt" else None,
        initializer=_init_stream_worker,
        initargs=(poi_index, ctx.config.export_hex_radius_m, ctx.config.non_bus_poi_catalog_path),
    ) as pool:
        for idx, result in enumerate(pool.imap_unordered(_stream_node_worker, tasks, chunksize=chunksize), start=1):
            if result is not None:
                hexagon_id, poi_ids = result
                writer.add_hexagon(hexagon_id, [{"i": pid} for pid in poi_ids])

            if idx % 500 == 0 or idx == total:
                _trim_export_memory()
                print(
                    f"[POI Export] hexagon stream: {idx}/{total} nodes, "
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
        writer = HexShardWriter(ctx.config.hex_pois_dir, ctx.config.artifact_slug, scale_powers=True, resume=True)
        if writer.resumed:
            print(
                f"[POI Export] Resuming from a completed hexagon stream "
                f"({writer.hex_count} hexagons already streamed) -- skipping straight to finalize.",
                flush=True,
            )
            hexagons_written = writer.hex_count
        else:
            with _ElapsedTimer(f"POI Export hexagon stream ({len(poi_rows)} POIs)"):
                hexagons_written = _stream_hexagon_export(poi_rows, ctx, non_bus, writer)
            writer.mark_scoring_complete()
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
