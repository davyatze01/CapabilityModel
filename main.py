from utils import graphml, route, decay, delta_g, get_impedance, capabilities as cap, services as serv
import argparse
import csv
import os
import osmnx as ox
import math
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import shutup
from tqdm import tqdm
import multiprocessing as mp
import threading
import time
import pickle
import random
import hashlib
import traceback
from shapely.geometry import Point
from shapely.ops import unary_union

# Set to None to use (cpu_count - 1)
CAP_WORKERS = None
CAP_OTP = None # None => usa tutti i thread bus disponibili
BUS_TIMEOUT_S = 15
NON_BUS_CACHE_DIR = os.path.join("cache", "non_bus")
_POI_COORDS_FILTER = None
_POI_SNAP_INFO = None
POI_SNAP_CACHE_DIR = os.path.join("cache", "poi_snap_cache")

_SNAP_PROJ_GRAPH = None
_SNAP_NODES_GDF = None

# Silenzia warning non critici per mantenere la console pulita
shutup.please()

def _non_bus_cache_path(node_id):
    return os.path.join(NON_BUS_CACHE_DIR, f"{node_id}.pkl")


def _write_non_bus_cache(path, payload):
    with open(path, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)


def _load_non_bus_cache(path):
    with open(path, "rb") as f:
        return pickle.load(f)


def _is_valid_non_bus_cache(payload):
    if not isinstance(payload, dict):
        return False
    if "origin" not in payload or "services" not in payload:
        return False
    if not isinstance(payload["services"], dict):
        return False
    return True


def _has_valid_non_bus_cache(path):
    if not os.path.exists(path):
        return False
    try:
        payload = _load_non_bus_cache(path)
    except Exception:
        return False
    return _is_valid_non_bus_cache(payload)


def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _coord_key(coord):
    return (round(coord[0], 6), round(coord[1], 6))


def _snap_to_graph(graph, coord, snap_cache, snap_lock):
    key = (round(coord[0], 6), round(coord[1], 6))
    if snap_lock:
        with snap_lock:
            cached = snap_cache.get(key)
        if cached is not None:
            return cached
    nodes = ox.distance.nearest_nodes(graph, [coord[1]], [coord[0]])
    try:
        nodes = list(nodes)
    except TypeError:
        nodes = [nodes]
    if not nodes:
        raise RuntimeError("nearest_nodes returned no snapped node")
    node = nodes[0]
    node_data = graph.nodes[node]
    snapped = (node_data["y"], node_data["x"])
    dist_m = _haversine_m(coord[0], coord[1], snapped[0], snapped[1])
    result = (snapped, dist_m)
    if snap_lock:
        with snap_lock:
            snap_cache[key] = result
    return result


def _get_snap_projection(graph):
    global _SNAP_PROJ_GRAPH, _SNAP_NODES_GDF
    if _SNAP_PROJ_GRAPH is None or _SNAP_NODES_GDF is None:
        _SNAP_PROJ_GRAPH = ox.project_graph(graph)
        _SNAP_NODES_GDF = ox.graph_to_gdfs(_SNAP_PROJ_GRAPH, nodes=True, edges=False)
    return _SNAP_PROJ_GRAPH, _SNAP_NODES_GDF

def _access_locus(geom):
    gtype = geom.geom_type
    if gtype == "Point":
        return geom
    if gtype in ("LineString", "MultiLineString"):
        return geom
    if gtype == "Polygon":
        return geom.exterior
    if gtype == "MultiPolygon":
        return unary_union([p.exterior for p in geom.geoms])
    return geom.representative_point()


def _extract_geom_vertices(geom):
    gtype = geom.geom_type
    if gtype == "Point":
        return [(geom.y, geom.x)]
    if gtype in ("LineString", "LinearRing"):
        return [(lat, lon) for lon, lat in geom.coords]
    if gtype == "MultiLineString":
        out = []
        for line in geom.geoms:
            out.extend([(lat, lon) for lon, lat in line.coords])
        return out
    if gtype == "Polygon":
        out = [(lat, lon) for lon, lat in geom.exterior.coords]
        for ring in geom.interiors:
            out.extend([(lat, lon) for lon, lat in ring.coords])
        return out
    if gtype == "MultiPolygon":
        out = []
        for poly in geom.geoms:
            out.extend([(lat, lon) for lon, lat in poly.exterior.coords])
            for ring in poly.interiors:
                out.extend([(lat, lon) for lon, lat in ring.coords])
        return out
    if gtype == "GeometryCollection":
        out = []
        for sub in geom.geoms:
            out.extend(_extract_geom_vertices(sub))
        return out
    rep = geom.representative_point()
    return [(rep.y, rep.x)]


def _snap_coords_batch(graph, coords):
    # batch snap all coordinates on a graph using one nearest_nodes call
    if not coords:
        return {}
    unique = {}
    ordered = []
    for coord in coords:
        key = _coord_key(coord)
        if key not in unique:
            unique[key] = (coord[0], coord[1])
            ordered.append(key)
    lons = [unique[k][1] for k in ordered]
    lats = [unique[k][0] for k in ordered]
    nodes = ox.distance.nearest_nodes(graph, lons, lats)
    if hasattr(nodes, "tolist"):
        nodes = nodes.tolist()
    elif not isinstance(nodes, (list, tuple)):
        nodes = [nodes]
    out = {}
    for idx, key in enumerate(ordered):
        node_id = nodes[idx]
        node_data = graph.nodes[node_id]
        snapped = (node_data["y"], node_data["x"])
        src = unique[key]
        dist_m = _haversine_m(src[0], src[1], snapped[0], snapped[1])
        out[key] = (snapped, dist_m)
    return out


def _normalize_cached_snap_entries(coords, cached):
    # supports old [(coord,snapped)] and new [(coord,snapped,dist)]
    if not isinstance(cached, list):
        return None
    normalized = []
    for idx, item in enumerate(cached):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        coord = item[0]
        if coord is None and idx < len(coords):
            coord = coords[idx]
        snapped = item[1]
        if coord is None or not isinstance(snapped, (list, tuple)) or len(snapped) < 2:
            continue
        if len(item) >= 3 and isinstance(item[2], (int, float)):
            snap_dist_m = float(item[2])
        else:
            snap_dist_m = _haversine_m(coord[0], coord[1], snapped[0], snapped[1])
        normalized.append((coord, (snapped[0], snapped[1]), snap_dist_m))
    if len(normalized) != len(coords):
        return None
    return normalized


def _build_poi_snap_map(graph, poi_coords_by_type, poi_geoms_by_type, enable_progress):
    poi_snap_map = {}
    total_snap = sum(len(coords) for coords in poi_coords_by_type.values())
    snap_pbar = tqdm(total=total_snap, desc="Snap POIs", mininterval=0) if enable_progress else None

    point_coords = []
    geom_vertex_map = {}  # (poi_key, coord_key) -> [vertex_coords]
    pending = []  # poi_key to compute

    for poi_key, coords in poi_coords_by_type.items():
        cached = _load_poi_snap_cache(poi_key)
        normalized = _normalize_cached_snap_entries(coords, cached)
        if normalized is not None:
            poi_snap_map[poi_key] = normalized
            if snap_pbar:
                snap_pbar.update(len(normalized))
            continue

        pending.append(poi_key)
        geom_list = poi_geoms_by_type.get(poi_key) or []
        for idx, coord in enumerate(coords):
            coord_k = _coord_key(coord)
            if geom_list and idx < len(geom_list) and geom_list[idx] is not None and geom_list[idx].geom_type != "Point":
                vertices = _extract_geom_vertices(geom_list[idx])
                if vertices:
                    geom_vertex_map[(poi_key, coord_k)] = vertices
                    point_coords.extend(vertices)
                    continue
            point_coords.append(coord)

    snapped_points = _snap_coords_batch(graph, point_coords)

    for poi_key in pending:
        coords = poi_coords_by_type.get(poi_key, [])
        snapped_list = []
        for coord in coords:
            coord_k = _coord_key(coord)
            geom_vertices = geom_vertex_map.get((poi_key, coord_k))
            if geom_vertices:
                best = None
                for vertex in geom_vertices:
                    snap_info = snapped_points.get(_coord_key(vertex))
                    if snap_info is None:
                        continue
                    if best is None or snap_info[1] < best[1]:
                        best = snap_info
                if best is None:
                    best = snapped_points.get(coord_k)
                snapped, snap_dist_m = best if best is not None else (coord, 0.0)
            else:
                best = snapped_points.get(coord_k)
                snapped, snap_dist_m = best if best is not None else (coord, 0.0)
            snapped_list.append((coord, snapped, snap_dist_m))
            if snap_pbar:
                snap_pbar.update(1)
        poi_snap_map[poi_key] = snapped_list
        _save_poi_snap_cache(poi_key, snapped_list)

    if snap_pbar:
        snap_pbar.close()
    return poi_snap_map

def _poi_snap_cache_path(poi_key):
    os.makedirs(POI_SNAP_CACHE_DIR, exist_ok=True)
    key_repr = repr(poi_key)
    key_hash = hashlib.sha1(key_repr.encode("utf-8")).hexdigest()
    return os.path.join(POI_SNAP_CACHE_DIR, f"{key_hash}.pkl")


def _load_poi_snap_cache(poi_key):
    path = _poi_snap_cache_path(poi_key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _save_poi_snap_cache(poi_key, payload):
    path = _poi_snap_cache_path(poi_key)
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp_path, path)


def _append_fail_fast_log(path, line, lock=None):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if lock is not None:
            with lock:
                with open(path, "a", encoding="utf-8") as ff:
                    ff.write(line.rstrip("\n") + "\n")
        else:
            with open(path, "a", encoding="utf-8") as ff:
                ff.write(line.rstrip("\n") + "\n")
    except Exception as log_exc:
        print(f"FAIL_FAST_LOG_WRITE_ERROR: {log_exc}")
        print(line)




def _init_worker(
    poi_coords_by_type,
    poi_snap_info_by_type=None,
    graph=None,
    mode_graphs=None,
    poi_geom_cache=None,
    poi_points_cache=None,
):
    global _POI_COORDS_FILTER, _POI_SNAP_INFO
    _POI_COORDS_FILTER = {}
    for poi_key, coords in poi_coords_by_type.items():
        _POI_COORDS_FILTER[poi_key] = {
            (round(lat, 6), round(lon, 6)) for lat, lon in coords
        }
    _POI_SNAP_INFO = poi_snap_info_by_type or {}
    if graph is not None and delta_g._G_CACHE is None:
        delta_g._G_CACHE = graph
    if mode_graphs:
        for mode, mode_graph in mode_graphs.items():
            if mode not in delta_g._MODE_GRAPH_CACHE:
                delta_g._MODE_GRAPH_CACHE[mode] = mode_graph
    if poi_geom_cache:
        delta_g._POI_GEOM_CACHE = poi_geom_cache
    if poi_points_cache:
        delta_g._POI_POINTS_CACHE = poi_points_cache


def empty_cache():
    print("Starting cache cleanup...")
    for folder in ["route_cache", "rra_cache", "poi_geom_cache", NON_BUS_CACHE_DIR]:
        if not os.path.isdir(folder):
            print(f"Skip missing folder: {folder}")
            continue
        print(f"Cleaning folder: {folder}")
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                os.remove(path)
        print(f"Finished folder: {folder}")
    output_csvs = [
        os.path.join("outputs", "capability_restorativeness.csv"),
        os.path.join("outputs", "capability_nutrition.csv"),
        os.path.join("outputs", "capability_care.csv"),
    ]
    for output_csv in output_csvs:
        if os.path.isfile(output_csv):
            print(f"Removing file: {output_csv}")
            os.remove(output_csv)
        else:
            print(f"Skip missing file: {output_csv}")
    print("Cache cleanup completed.")


def _compute_capability_from_cache(item):
    node_id, cache_path = item
    if not os.path.exists(cache_path):
        return None
    try:
        state = _load_non_bus_cache(cache_path)
    except Exception:
        return None
    if not _is_valid_non_bus_cache(state):
        return None

    def _compute_entry_accessibility(origin, entry):
        if entry["cache_hit"]:
            return entry["accessibility_value"]

        beta = math.log(2) / 20.0
        decay_bus = []
        for coord in entry["poi_coords"]:
            if route.bus_cache_exists(origin, coord):
                try:
                    _, _, imp_bus = route.get_route(
                        None,
                        "bus",
                        origin,
                        coord,
                        impedance_flag=True,
                        ax=None,
                        distance_only=True,
                        quiet=True,
                    )
                except Exception:
                    imp_bus = None
            else:
                imp_bus = None

            if imp_bus:
                decay_bus.append(decay.distance_decay(beta, imp_bus))
            else:
                decay_bus.append(0.0)

        RRA, acc = delta_g.merge_rra_and_accessibility(
            entry["decay_walk"],
            entry["decay_bike"],
            entry["decay_drive"],
            decay_bus,
        )
        delta_g.save_rra(entry["cache_file"], RRA)
        return acc

    service_scores = {}
    services_state = state.get("services", {})
    for service in serv.SERVICE_KEYS:
        entries = services_state.get(service, [])
        values = []
        for entry in entries:
            values.append(_compute_entry_accessibility(state["origin"], entry))
        service_scores[service] = serv.choquet_integral(values, service) if values else 0.0

    rest_vals = [service_scores[s] for s in cap.CAP_RESTORATIVENESS_IDX]
    nutrition_vals = [service_scores[s] for s in cap.CAP_NUTRITION_IDX]
    care_vals = [service_scores[s] for s in cap.CAP_CARE_IDX]

    capability_restorativeness = cap.choquet_integral(rest_vals, "restorativeness") if rest_vals else 0.0
    capability_nutrition = cap.choquet_integral(nutrition_vals, "nutrition") if nutrition_vals else 0.0
    capability_care = cap.choquet_integral(care_vals, "care") if care_vals else 0.0

    return {
        "node_id": node_id,
        "lat": state["origin"][0],
        "lon": state["origin"][1],
        "capability_restorativeness": capability_restorativeness,
        "capability_nutrition": capability_nutrition,
        "capability_care": capability_care,
        "service_scores": service_scores,
    }

def _process_node(node_item):
    node_id, data = node_item
    if "y" not in data or "x" not in data:
        return None
    origin = (data["y"], data["x"])

    service_results = {}
    for service in serv.SERVICE_KEYS:
        entries = []
        for query in serv.get_service_queries(service):
            poi_key = serv.query_key(query)
            try:
                data = delta_g.accessibility_non_bus(
                    query.poi_type,
                    origin,
                    radius_m=query.radius_m,
                    tags=query.tags,
                )
            except Exception:
                # Keep worker alive on per-query failures.
                data = {
                    "cache_hit": True,
                    "cache_file": None,
                    "accessibility_value": 0.0,
                }
            if data["cache_hit"]:
                entries.append({
                    "poi_type": query.poi_type,
                    "cache_hit": True,
                    "cache_file": data["cache_file"],
                    "accessibility_value": data["accessibility_value"],
                })
            else:
                poi_points = data["poi_points"]
                decay_walk = data["decay_walk"]
                decay_bike = data["decay_bike"]
                decay_drive = data["decay_drive"]

                if _POI_COORDS_FILTER and poi_key in _POI_COORDS_FILTER:
                    keep_idx = []
                    for i, geom in enumerate(poi_points):
                        key = (round(geom.y, 6), round(geom.x, 6))
                        if key in _POI_COORDS_FILTER[poi_key]:
                            keep_idx.append(i)
                    poi_points = [poi_points[i] for i in keep_idx]
                    decay_walk = [decay_walk[i] for i in keep_idx]
                    decay_bike = [decay_bike[i] for i in keep_idx]
                    decay_drive = [decay_drive[i] for i in keep_idx]

                # use snapped bus destinations and add snap distance as extra walk impedance
                beta = math.log(2) / 20.0
                poi_coords = []
                adjusted_decay_walk = []
                snap_info_for_key = _POI_SNAP_INFO.get(poi_key, {}) if _POI_SNAP_INFO else {}
                for i, geom in enumerate(poi_points):
                    coord = (geom.y, geom.x)
                    snap_info = snap_info_for_key.get(_coord_key(coord))
                    if snap_info and isinstance(snap_info, (list, tuple)) and len(snap_info) >= 2:
                        snapped_coord = tuple(snap_info[0])
                        snap_dist_m = float(snap_info[1])
                    else:
                        snapped_coord = coord
                        snap_dist_m = 0.0
                    poi_coords.append(snapped_coord)
                    walk_imp_extra = get_impedance.impedance_base(snap_dist_m / 1000.0, "walk")
                    adjusted_decay_walk.append(decay_walk[i] * decay.distance_decay(beta, walk_imp_extra))

                entries.append({
                    "poi_type": query.poi_type,
                    "cache_hit": False,
                    "cache_file": data["cache_file"],
                    "decay_walk": adjusted_decay_walk,
                    "decay_bike": decay_bike,
                    "decay_drive": decay_drive,
                    "poi_coords": poi_coords,
                })
        service_results[service] = entries

    return {
        "node_id": node_id,
        "origin": origin,
        "services": service_results,
    }


def _request_bus_route(graph, origin, coord):
    transient_error_types = {"otp_unreachable", "otp_bad_json"}
    for attempt in (1, 2):
        try:
            if attempt > 1:
                time.sleep(0.35)
            _, _, result, status = route.get_route(
                graph,
                "bus",
                origin,
                coord,
                impedance_flag=True,
                ax=None,
                distance_only=True,
                return_geometry=False,
                quiet=True,
                timeout_s=BUS_TIMEOUT_S,
                save_geometry=False,
                return_status=True,
            )
        except Exception as exc:
            status = {
                "ok": False,
                "found_itinerary": False,
                "error_type": "unexpected",
                "error_detail": str(exc),
                "status_code": None,
            }
            result = None

        error_type = status.get("error_type")
        status_code = status.get("status_code")
        is_http_5xx = error_type == "otp_http_error" and isinstance(status_code, int) and status_code >= 500
        can_retry = (error_type in transient_error_types or is_http_5xx)

        if status.get("ok"):
            found_itinerary = status.get("found_itinerary")
            if found_itinerary is None:
                if isinstance(result, list):
                    found_itinerary = len(result) > 0
                else:
                    found_itinerary = bool(result)
            return {
                "ok": True,
                "found_itinerary": bool(found_itinerary),
                "error_type": error_type,
                "error_detail": status.get("error_detail"),
                "status_code": status_code,
                "attempts": attempt,
            }

        if attempt == 1 and can_retry:
            continue

        return {
            "ok": False,
            "found_itinerary": False,
            "error_type": error_type or "unexpected",
            "error_detail": status.get("error_detail"),
            "status_code": status_code,
            "attempts": attempt,
        }

    return {
        "ok": False,
        "found_itinerary": False,
        "error_type": "unexpected",
        "error_detail": "unreachable_retry_state",
        "status_code": None,
        "attempts": 2,
    }


def _precompute_bus_routes(
    graph,
    tasks,
    failed_coords=None,
    failed_lock=None,
    counts=None,
    counts_lock=None,
    progress_counter=None,
    progress_lock=None,
    abort_event=None,
    first_error=None,
    first_error_lock=None,
    fail_fast_log_path=None,
    fail_fast_log_lock=None,
    thread_errors=None,
    thread_errors_lock=None,
):
    try:
        for origin, snapped in tasks:
            if abort_event is not None and abort_event.is_set():
                break

            result = _request_bus_route(graph, origin, snapped)
            found = bool(result.get("found_itinerary"))
            ok = bool(result.get("ok"))

            if not ok and failed_coords is not None and failed_lock is not None:
                with failed_lock:
                    failed_coords.append((origin[0], origin[1], result.get("error_type") or "otp_error", snapped[0], snapped[1], None))
            elif not found and failed_coords is not None and failed_lock is not None:
                with failed_lock:
                    failed_coords.append((origin[0], origin[1], "otp_no_itinerary", snapped[0], snapped[1], None))
            if counts is not None and counts_lock is not None:
                with counts_lock:
                    counts[0] += 1  # analyzed
                    if found:
                        counts[1] += 1  # found
                    if not ok:
                        counts[2] += 1  # failed
            if progress_counter is not None and progress_lock is not None:
                with progress_lock:
                    progress_counter[0] += 1

            if not ok:
                if first_error is not None and first_error_lock is not None:
                    with first_error_lock:
                        if first_error.get("value") is None:
                            first_error["value"] = {
                                "origin": origin,
                                "destination": snapped,
                                "error_type": result.get("error_type"),
                                "error_detail": result.get("error_detail"),
                                "status_code": result.get("status_code"),
                                "attempts": result.get("attempts"),
                            }
                            if fail_fast_log_path:
                                _append_fail_fast_log(
                                    fail_fast_log_path,
                                    (
                                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                                        f"type={result.get('error_type')} status={result.get('status_code')} "
                                        f"attempts={result.get('attempts')} origin={origin} destination={snapped} "
                                        f"detail={result.get('error_detail')}"
                                    ),
                                    lock=fail_fast_log_lock,
                                )
                if abort_event is not None:
                    abort_event.set()
                break
    except Exception:
        exc_text = traceback.format_exc()
        if thread_errors is not None and thread_errors_lock is not None:
            with thread_errors_lock:
                thread_errors.append(exc_text)
        if first_error is not None and first_error_lock is not None:
            with first_error_lock:
                if first_error.get("value") is None:
                    first_error["value"] = {
                        "origin": None,
                        "destination": None,
                        "error_type": "worker_exception",
                        "error_detail": exc_text.strip(),
                        "status_code": None,
                        "attempts": 0,
                    }
                    if fail_fast_log_path:
                        _append_fail_fast_log(
                            fail_fast_log_path,
                            f"{time.strftime('%Y-%m-%d %H:%M:%S')} type=worker_exception detail={exc_text.strip()}",
                            lock=fail_fast_log_lock,
                        )
        if abort_event is not None:
            abort_event.set()


def run_pipeline(max_nodes=None, max_pois=None, seed=42, enable_progress=True, skip_routing=False):
    graph = graphml.get_graph()
    nodes = list(graph.nodes(data=True))
    nodes_with_coords = [item for item in nodes if "y" in item[1] and "x" in item[1]]
    if max_nodes is not None:
        rng = random.Random(seed)
        nodes_with_coords = rng.sample(nodes_with_coords, min(max_nodes, len(nodes_with_coords)))

    os.makedirs("outputs", exist_ok=True)
    os.makedirs(NON_BUS_CACHE_DIR, exist_ok=True)
    output_paths = {
        "restorativeness": os.path.join("outputs", "capability_restorativeness.csv"),
        "nutrition": os.path.join("outputs", "capability_nutrition.csv"),
        "care": os.path.join("outputs", "capability_care.csv"),
    }

    with (
        open(output_paths["restorativeness"], "w", newline="", encoding="utf-8") as f_rest,
        open(output_paths["nutrition"], "w", newline="", encoding="utf-8") as f_nut,
        open(output_paths["care"], "w", newline="", encoding="utf-8") as f_care,
    ):
        writer_rest = csv.writer(f_rest)
        writer_nut = csv.writer(f_nut)
        writer_care = csv.writer(f_care)

        # Intestazioni dei CSV per capability
        rest_services = cap.CAPABILITY_SERVICES["restorativeness"]
        nut_services = cap.CAPABILITY_SERVICES["nutrition"]
        care_services = cap.CAPABILITY_SERVICES["care"]

        header_rest = ["node_id", "lat", "lon", "capability_restorativeness"]
        header_rest.extend([f"service_{service}" for service in rest_services])
        writer_rest.writerow(header_rest)

        header_nut = ["node_id", "lat", "lon", "capability_nutrition"]
        header_nut.extend([f"service_{service}" for service in nut_services])
        writer_nut.writerow(header_nut)

        header_care = ["node_id", "lat", "lon", "capability_care"]
        header_care.extend([f"service_{service}" for service in care_services])
        writer_care.writerow(header_care)

        # Determina il numero di worker da usare (default: tutti i core disponibili)
        workers = max(1, mp.cpu_count()) if CAP_WORKERS is None else max(1, int(CAP_WORKERS))
        
        # Variabili per il monitoraggio dello stato
        last_row_lock = threading.Lock()
        last_row_time = [time.time()]
        stop_event = threading.Event()
        routing_running = [False]

        pbar = None

        # Thread separato che controlla se il processo si è bloccato
        def _monitor():
            while not stop_event.wait(60):
                if pbar:
                    pbar.refresh()
                with last_row_lock:
                    idle_s = time.time() - last_row_time[0]
                if idle_s >= 300 and not routing_running[0]:
                    print("Warning: no rows written in the last 5 minutes.")

        monitor_thread = threading.Thread(target=_monitor, daemon=True)
        monitor_thread.start()

        try:
            poi_coords_by_type = {}
            poi_geoms_by_type = {}
            poi_points_by_type = {}
            non_bus_poi_counts = {}
            rng = random.Random(seed)
            unique_queries = serv.unique_query_keys()
            # Preload POI geometries on disk to avoid parallel downloads.
            delta_g.preload_all_pois(unique_queries)
            # Load graphs once in parent.
            shared_graph = graph
            shared_mode_graphs = {
                "walk": graphml.get_mode_graph("walk"),
                "bike": graphml.get_mode_graph("bike"),
                "drive": graphml.get_mode_graph("drive"),
            }
            shared_poi_geoms = dict(delta_g._POI_GEOM_CACHE)
            for query in unique_queries:
                poi_key = serv.query_key(query)
                feature, value, _ = delta_g._resolve_query(query.poi_type, None, query.tags)
                points_cache_key = (feature, value)
                try:
                    poi_points = delta_g.get_poi_points(
                        query.poi_type,
                        (0.0, 0.0),
                        radius_m=query.radius_m,
                        tags=query.tags,
                    )
                except Exception as e:
                    raise RuntimeError(
                        f"Entrance inference failed for poi_type={query.poi_type}, tags={query.tags}. {e}"
                    ) from e
                if points_cache_key not in poi_points_by_type:
                    poi_points_by_type[points_cache_key] = poi_points
                coords = [(geom.y, geom.x) for geom in poi_points]
                raw_geoms = [g for g, _ in (delta_g._POI_GEOM_CACHE.get(points_cache_key) or [])]
                if max_pois is not None:
                    count = min(max_pois, len(coords))
                    idx = rng.sample(range(len(coords)), count)
                    coords = [coords[i] for i in idx]
                    raw_geoms = [raw_geoms[i] for i in idx] if raw_geoms else []
                poi_coords_by_type[poi_key] = coords
                poi_geoms_by_type[poi_key] = raw_geoms if raw_geoms else []
                non_bus_poi_counts[poi_key] = len(coords)


            total_pois = 0
            for service in serv.SERVICE_KEYS:
                for query in serv.get_service_queries(service):
                    total_pois += len(poi_coords_by_type[serv.query_key(query)])
            # Build snapped POIs (unique) for bus routing
            poi_snap_map = _build_poi_snap_map(graph, poi_coords_by_type, poi_geoms_by_type, enable_progress)
            poi_snap_info_by_type = {}
            unique_snapped_coords = set()
            for poi_key, snapped_list in poi_snap_map.items():
                info = {}
                for item in snapped_list:
                    if not isinstance(item, (list, tuple)) or len(item) < 3:
                        continue
                    coord, snapped, snap_dist_m = item
                    info[_coord_key(coord)] = (snapped, snap_dist_m)
                    unique_snapped_coords.add(snapped)
                poi_snap_info_by_type[poi_key] = info
            unique_snapped_coords = sorted(unique_snapped_coords)
            total_bus_tasks = len(nodes_with_coords) * len(unique_snapped_coords)

            def _flush_failed_coords():
                with failed_coords_lock:
                    start = failed_flush_index[0]
                    if start >= len(failed_coords):
                        return
                    batch = failed_coords[start:]
                    failed_flush_index[0] = len(failed_coords)
                with failed_flush_lock:
                    for lat, lon, reason, snap_lat, snap_lon, snap_dist_m in batch:
                        failed_csv_writer.writerow([lat, lon, reason, snap_lat, snap_lon, snap_dist_m])
                    failed_csv_file.flush()

            def _failed_coords_flusher():
                while not failed_flush_stop.wait(60):
                    _flush_failed_coords()
                    with failed_coords_lock:
                        snapshot = [(lat, lon) for lat, lon, *_ in failed_coords]
                    if snapshot:
                        plot_graph_with_pois(
                            snapshot,
                            output_path=os.path.join("outputs", "graph_failed_pois.png"),
                            show=False,
                        )

            if skip_routing:
                print("Skipping bus routing precompute: using existing route cache.")
            else:
                bus_pbar = None
                itins_pbar = None
                bus_progress_counter = None
                bus_progress_lock = None
                itins_counts = [0, 0, 0]  # analyzed, found, failed
                itins_counts_lock = threading.Lock()
                failed_coords = []
                failed_coords_lock = threading.Lock()
                failed_csv_path = os.path.join("outputs", "failed_pois.csv")
                failed_csv_file = open(failed_csv_path, "w", newline="", encoding="utf-8")
                failed_csv_writer = csv.writer(failed_csv_file)
                failed_csv_writer.writerow(["lat", "lon", "reason", "snap_lat", "snap_lon", "snap_dist_m"])
                failed_csv_file.flush()
                fail_fast_log_path = os.path.join("outputs", "bus_fail_fast.log")
                fail_fast_log_lock = threading.Lock()
                failed_flush_stop = threading.Event()
                failed_flush_lock = threading.Lock()
                failed_flush_index = [0]
                abort_event = threading.Event()
                first_error: dict[str, dict[str, object] | None] = {"value": None}
                first_error_lock = threading.Lock()
                thread_errors = []
                thread_errors_lock = threading.Lock()
                routing_running[0] = True
                bus_progress_thread = None

                def _flush_bus_progress():
                    if not bus_pbar or bus_progress_counter is None or bus_progress_lock is None:
                        return
                    with bus_progress_lock:
                        delta = bus_progress_counter[0]
                        bus_progress_counter[0] = 0
                    if delta:
                        bus_pbar.update(delta)

                def _refresh_bus_progress():
                    while routing_running[0] and not stop_event.wait(1):
                        _flush_bus_progress()
                        if bus_pbar:
                            bus_pbar.refresh()
                        if itins_pbar:
                            with itins_counts_lock:
                                analyzed = itins_counts[0]
                                found = itins_counts[1]
                            itins_pbar.total = max(1, analyzed)
                            itins_pbar.n = found
                            itins_pbar.set_description_str("Itineraries found")
                            itins_pbar.refresh()

                # Build all routing tasks directly (no precompute scan)
                bus_tasks = []
                for node_id, data in nodes_with_coords:
                    origin = (data["y"], data["x"])
                    for snapped in unique_snapped_coords:
                        bus_tasks.append((origin, snapped))

                bus_workers = workers if CAP_OTP is None else max(1, int(CAP_OTP))
                bus_threads_count = min(bus_workers, len(bus_tasks)) if bus_tasks else 0

                bus_pbar = tqdm(total=total_bus_tasks, desc="Bus routes", mininterval=1, miniters=1) if enable_progress else None
                bus_progress_counter = [0] if bus_pbar else None
                bus_progress_lock = threading.Lock() if bus_pbar else None

                if bus_pbar:
                    bus_progress_thread = threading.Thread(target=_refresh_bus_progress, daemon=True)
                    bus_progress_thread.start()
                    itins_pbar = tqdm(
                        total=1,
                        desc="Itineraries found",
                        bar_format="{desc}: {n}/{total}",
                        mininterval=0,
                        position=1,
                    )

                bus_threads = []
                failed_flush_thread = threading.Thread(target=_failed_coords_flusher, daemon=True)
                failed_flush_thread.start()
                for i in range(bus_threads_count):
                    shard = bus_tasks[i::bus_threads_count]
                    if not shard:
                        continue
                    bus_threads.append(
                        threading.Thread(
                            target=_precompute_bus_routes,
                            args=(
                                graph,
                                shard,
                                failed_coords,
                                failed_coords_lock,
                                itins_counts,
                                itins_counts_lock,
                                bus_progress_counter,
                                bus_progress_lock,
                                abort_event,
                                first_error,
                                first_error_lock,
                                fail_fast_log_path,
                                fail_fast_log_lock,
                                thread_errors,
                                thread_errors_lock,
                            ),
                            daemon=True
                        )
                    )
                for t in bus_threads:
                    t.start()
                stall_deadline_s = max(120, BUS_TIMEOUT_S * 4)
                last_progress_change_s = time.time()
                last_analyzed_seen = 0
                while True:
                    alive = False
                    for t in bus_threads:
                        t.join(timeout=1)
                        if t.is_alive():
                            alive = True
                    with itins_counts_lock:
                        analyzed_now = itins_counts[0]
                    if analyzed_now != last_analyzed_seen:
                        last_analyzed_seen = analyzed_now
                        last_progress_change_s = time.time()
                    if not alive:
                        break
                    if abort_event.is_set():
                        break
                    if time.time() - last_progress_change_s > stall_deadline_s:
                        with first_error_lock:
                            if first_error.get("value") is None:
                                first_error["value"] = {
                                    "origin": None,
                                    "destination": None,
                                    "error_type": "worker_stall",
                                    "error_detail": (
                                        f"No bus progress for {int(time.time() - last_progress_change_s)}s "
                                        f"with {sum(1 for bt in bus_threads if bt.is_alive())} active threads"
                                    ),
                                    "status_code": None,
                                    "attempts": 0,
                                }
                                _append_fail_fast_log(
                                    fail_fast_log_path,
                                    (
                                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                                        "type=worker_stall status=None attempts=0 origin=None destination=None "
                                        f"detail={first_error['value'].get('error_detail')}"
                                    ),
                                    lock=fail_fast_log_lock,
                                )
                        abort_event.set()
                        break
                if abort_event.is_set():
                    for t in bus_threads:
                        t.join(timeout=2)
                with itins_counts_lock:
                    analyzed_after_join = itins_counts[0]
                if not abort_event.is_set() and thread_errors:
                    with first_error_lock:
                        if first_error.get("value") is None:
                            first_error["value"] = {
                                "origin": None,
                                "destination": None,
                                "error_type": "worker_exception",
                                "error_detail": thread_errors[0].strip(),
                                "status_code": None,
                                "attempts": 0,
                            }
                    _append_fail_fast_log(
                        fail_fast_log_path,
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} type=worker_exception detail={thread_errors[0].strip()}",
                        lock=fail_fast_log_lock,
                    )
                    abort_event.set()
                if not abort_event.is_set() and analyzed_after_join < total_bus_tasks:
                    missing = total_bus_tasks - analyzed_after_join
                    with first_error_lock:
                        if first_error.get("value") is None:
                            first_error["value"] = {
                                "origin": None,
                                "destination": None,
                                "error_type": "worker_incomplete",
                                "error_detail": f"Bus workers exited early: analyzed={analyzed_after_join}, total={total_bus_tasks}, missing={missing}",
                                "status_code": None,
                                "attempts": 0,
                            }
                    _append_fail_fast_log(
                        fail_fast_log_path,
                        (
                            f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                            f"type=worker_incomplete analyzed={analyzed_after_join} total={total_bus_tasks} missing={missing}"
                        ),
                        lock=fail_fast_log_lock,
                    )
                    abort_event.set()
                routing_running[0] = False
                if bus_progress_thread:
                    bus_progress_thread.join(timeout=2)
                if bus_pbar:
                    _flush_bus_progress()
                    bus_pbar.refresh()
                    bus_pbar.close()
                if itins_pbar:
                    itins_pbar.close()
                failed_flush_stop.set()
                failed_flush_thread.join(timeout=2)
                _flush_failed_coords()
                failed_csv_file.close()
                if failed_coords:
                    plot_graph_with_pois(
                        [(lat, lon) for lat, lon, *_ in failed_coords],
                        output_path=os.path.join("outputs", "graph_failed_pois.png"),
                        show=False,
                    )
                if abort_event.is_set():
                    with itins_counts_lock:
                        analyzed = itins_counts[0]
                        found = itins_counts[1]
                        failed = itins_counts[2]
                    err = first_error.get("value") or {}
                    _append_fail_fast_log(
                        fail_fast_log_path,
                        (
                            f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                            f"analyzed={analyzed} found={found} failed={failed} "
                            f"type={err.get('error_type')} status={err.get('status_code')} "
                            f"attempts={err.get('attempts')} origin={err.get('origin')} "
                            f"destination={err.get('destination')} detail={err.get('error_detail')}"
                        ),
                        lock=fail_fast_log_lock,
                    )
                    print(
                        "Bus routing aborted due to OTP failure "
                        f"(analyzed={analyzed}, found={found}, failed={failed})."
                    )
                    raise RuntimeError(
                        "OTP bus precompute failed: "
                        f"type={err.get('error_type')} "
                        f"status={err.get('status_code')} "
                        f"attempts={err.get('attempts')} "
                        f"origin={err.get('origin')} "
                        f"destination={err.get('destination')} "
                        f"detail={err.get('error_detail')}"
                    )

            cache_paths = {}
            nodes_to_compute = []
            cached_nodes = 0
            for node_id, data in nodes_with_coords:
                cache_path = _non_bus_cache_path(node_id)
                cache_paths[node_id] = cache_path
                if _has_valid_non_bus_cache(cache_path):
                    cached_nodes += 1
                    continue
                nodes_to_compute.append((node_id, data))

            with mp.Pool(
                processes=workers,
                initializer=_init_worker,
                initargs=(
                    poi_coords_by_type,
                    poi_snap_info_by_type,
                    shared_graph,
                    shared_mode_graphs,
                    shared_poi_geoms,
                    poi_points_by_type,
                ),
            ) as pool:
                total_non_bus_pois = 0
                for service in serv.SERVICE_KEYS:
                    for query in serv.get_service_queries(service):
                        total_non_bus_pois += non_bus_poi_counts[serv.query_key(query)]
                total_non_bus_tasks = len(nodes_with_coords) * total_non_bus_pois
                pbar_non_bus = tqdm(total=total_non_bus_tasks, desc="Non-bus routes", mininterval=0) if enable_progress else None
                if pbar_non_bus and cached_nodes:
                    pbar_non_bus.update(cached_nodes * total_non_bus_pois)
                for partial in pool.imap_unordered(_process_node, nodes_to_compute, chunksize=20):
                    if partial is None:
                        continue
                    cache_path = _non_bus_cache_path(partial["node_id"])
                    _write_non_bus_cache(cache_path, partial)
                    if pbar_non_bus:
                        pbar_non_bus.update(total_non_bus_pois)
                if pbar_non_bus:
                    pbar_non_bus.close()

            if enable_progress:
                pbar = tqdm(total=len(nodes_with_coords), desc="Nodes", mininterval=0)

            items = [(node_id, cache_paths[node_id]) for node_id, _ in nodes_with_coords]
            with mp.Pool(processes=workers) as pool:
                for data in pool.imap_unordered(_compute_capability_from_cache, items, chunksize=20):
                    if data is None:
                        continue
                    service_scores = data["service_scores"]

                    row_rest = [
                        data["node_id"],
                        data["lat"],
                        data["lon"],
                        data["capability_restorativeness"],
                    ]
                    row_rest.extend(service_scores[s] for s in rest_services)
                    writer_rest.writerow(row_rest)

                    row_nut = [
                        data["node_id"],
                        data["lat"],
                        data["lon"],
                        data["capability_nutrition"],
                    ]
                    row_nut.extend(service_scores[s] for s in nut_services)
                    writer_nut.writerow(row_nut)

                    row_care = [
                        data["node_id"],
                        data["lat"],
                        data["lon"],
                        data["capability_care"],
                    ]
                    row_care.extend(service_scores[s] for s in care_services)
                    writer_care.writerow(row_care)

                    f_rest.flush()
                    f_nut.flush()
                    f_care.flush()
                    if pbar:
                        pbar.update(1)
                    with last_row_lock:
                        last_row_time[0] = time.time()
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
        finally:
            # Pulizia finale
            stop_event.set()
            monitor_thread.join(timeout=2)
            if pbar:
                pbar.close()

    print("Wrote results to:")
    for path in output_paths.values():
        print(f"- {path}")


def plot_graph(output_path=None, show=True):
    graph = graphml.get_graph()
    fig, _ = ox.plot_graph(
        graph,
        bgcolor="black",
        edge_color="white",
        node_size=0,
        edge_linewidth=0.5,
        show=False,
        close=False,
    )
    if output_path:
        fig.savefig(output_path, dpi=200, bbox_inches="tight")
    if show:
        plt.show()


def plot_graph_with_pois(poi_coords, output_path=None, show=True):
    if not poi_coords:
        return
    graph = graphml.get_graph()
    fig, ax = ox.plot_graph(
        graph,
        bgcolor="black",
        edge_color="white",
        node_size=0,
        edge_linewidth=0.5,
        show=False,
        close=False,
    )
    lats = [c[0] for c in poi_coords]
    lons = [c[1] for c in poi_coords]
    ax.scatter(lons, lats, c="red", s=8, marker="o", label="Unsnapped POIs", zorder=5)
    ax.legend(facecolor="black", labelcolor="white", loc="lower left", fontsize=8, framealpha=0.9)
    if output_path:
        fig.savefig(output_path, dpi=200, bbox_inches="tight")
    if show:
        plt.show()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-routing",
        action="store_true",
        help="Skip bus routing precompute and rely only on existing route cache.",
    )
    args = parser.parse_args()

    # empty_cache()
    run_pipeline(skip_routing=args.skip_routing)


if __name__ == "__main__":
    main()

