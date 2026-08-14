"""Visual debug pipeline for capability scores.

This debug pipeline assumes that main.py pipeline has already
completed a full run for the current study_city, since grid_params.json and the capability
exports and non-bus/bus/accessibility/service caches must already be on disk.
Otherwise, a runtime error will be thrown.

Reads all intermediate pipeline artifacts for a sample of hexagons (4 near the
grid centre + 1 near each bounding-box corner) and writes a single self-contained
HTML report that walks through the full computation chain:

  raw impedances → accessibility decay → Choquet service aggregation → ELECTRE TRI
"""

from __future__ import annotations

STUDY_CITY = "paris"

# ── Report size knobs ──────────────────────────────────────────────────────────────────

# Steps 1/2 list every POI a hex's node reaches per poi_type. That's fine for a sparse
# city (Cagliari) but a dense one (Paris/mgp_boundary) can have thousands of POIs per
# type per hex, which is what blew the report up to ~2GB. Lists are already sorted
# best-first (nearest / highest accessibility), so truncating just drops the long tail
# that nobody scrolls to anyway.
MAX_POIS_PER_TYPE = 30

# Cap on rows carried into the step-5 export-verification table (also sorted, mismatches
# first). Kept as a knob alongside MAX_POIS_PER_TYPE rather than a hardcoded constant.
MAX_VERIFY_ROWS = 150

# How many hexagons to sample (kept as a knob for symmetry with the caps above; the
# selection logic below assumes up to 4 centre + 1 per corner).
MAX_CENTER_HEXAGONS = 4

# The Δg aggregation table only ever renders the top 10 ranks per poi_type client-side
# (see `agg.steps.slice(0, 10)`); keep at most this many in the exported JSON too. The
# running `total` is still summed over every POI regardless of this cap.
MAX_AGGREGATION_STEPS = 10

import csv
import json
import math
import os
import pickle
from pathlib import Path
from typing import Any

import numpy as np

from core.config import PipelineConfig
from utils import services as serv
from utils.capabilities import electre_tri_details, CAPABILITY_SERVICES
from utils.services import choquet_integral_details, get_decay_coefficient
from utils.decay import calculate_rra
from utils.delta_g import accessibility_from_rra

# Re-use geometry helpers already implemented in inspect_hex_pois.
from tools.inspect_hex_pois import (
    _hex_geometry,
    _hex_centroid,
    _list_hex_ids,
    _nearest_capability_point,
    _haversine_m,
    _load_grid_params,
    _default_paths,
    _fetch_capability_points,
    _load_hex_items,
    _fetch_poi_rows,
)


# ---------------------------------------------------------------------------
# Hexagon selection
# ---------------------------------------------------------------------------

def _load_poi_names_by_source_key(cfg: PipelineConfig) -> dict[str, str]:
    """Build source_key -> name for every raw POI, so the debug UI can show a human
    name instead of the opaque source_key JSON blob. Reuses get_poi_geometries so the
    source_key is computed identically to how the pipeline built it originally, and
    reads from the same on-disk POI cache the pipeline itself uses (shapefile for
    cities configured with poi_from_shp, otherwise the cached OSMnx city-universe
    GeoJSON) so this never triggers a live network download."""
    from utils import graphml
    from utils.services import get_global_radius_m

    if cfg.poi_from_shp:
        from utils.load_shapefile import poi_from_shp
        pois = poi_from_shp(None)
    else:
        poi_cache_slug = cfg.artifact_slug if cfg.use_shapefile else cfg.city_slug
        city_poi_dir = graphml._city_poi_cache_dir(poi_cache_slug)
        buffer_m = get_global_radius_m(cfg) or 0.0
        pois = graphml._get_city_poi_universe(cfg.city_name, poi_cache_slug, city_poi_dir, buffer_m=buffer_m)

    if pois is None or pois.empty:
        return {}
    names: dict[str, str] = {}
    for _geom, name, source_key in graphml.get_poi_geometries(pois):
        if name:
            names[str(source_key)] = str(name)
    return names


def _grid_extent(grid_params: dict) -> tuple[float, float, float, float]:
    """Return (min_lon, min_lat, max_lon, max_lat) of the grid bounding box."""
    min_x = float(grid_params["min_x"])
    min_y = float(grid_params["min_y"])
    m_per_deg_lon = float(grid_params["m_per_deg_lon"])
    m_per_deg_lat = 111320.0
    cell_size_m = float(grid_params["cell_size_m"])
    hex_r = cell_size_m / math.cos(math.pi / 6.0)

    # Walk through all known hex_ids to find max col/row
    return (min_x / m_per_deg_lon, min_y / m_per_deg_lat,
            min_x / m_per_deg_lon, min_y / m_per_deg_lat)


def _select_hexagons(
    grid_params: dict,
    hex_source: Path,
    capability_points: list[dict],
) -> list[dict]:
    """Select 4 centre + 4 corner hexagons and resolve their representative nodes."""
    hex_ids = _list_hex_ids(hex_source)
    if not hex_ids:
        raise RuntimeError(f"No hex files found in {hex_source}")

    # Compute centroid for every hex (fast — pure arithmetic).
    centroids: dict[str, tuple[float, float]] = {}
    for hid in hex_ids:
        try:
            centroids[hid] = _hex_centroid(hid, grid_params)
        except Exception:
            pass

    lats = [lat for lat, _ in centroids.values()]
    lons = [lon for _, lon in centroids.values()]
    mid_lat = (min(lats) + max(lats)) / 2.0
    mid_lon = (min(lons) + max(lons)) / 2.0

    # Sort by distance to centre; take up to 4, deduplicating by resolved node_id.
    by_dist = sorted(centroids.items(), key=lambda kv: _haversine_m(kv[1][0], kv[1][1], mid_lat, mid_lon))
    centre_group: list[str] = []
    seen_nodes: set[str] = set()
    for hid, (clat, clon) in by_dist:
        node = _nearest_capability_point(clat, clon, capability_points)
        nid = str(node["node_id"]) if node else None
        if nid and nid not in seen_nodes:
            centre_group.append(hid)
            seen_nodes.add(nid)
        if len(centre_group) >= MAX_CENTER_HEXAGONS:
            break

    # One hex per corner.
    corners = {
        "NW": (max(lats), min(lons)),
        "NE": (max(lats), max(lons)),
        "SW": (min(lats), min(lons)),
        "SE": (min(lats), max(lons)),
    }
    corner_group: dict[str, str] = {}
    for label, (clat, clon) in corners.items():
        for candidate_hid, _ in sorted(
            centroids.items(), key=lambda kv: _haversine_m(kv[1][0], kv[1][1], clat, clon)
        ):
            if candidate_hid in centre_group or candidate_hid in corner_group.values():
                continue
            node = _nearest_capability_point(
                centroids[candidate_hid][0], centroids[candidate_hid][1], capability_points
            )
            nid = str(node["node_id"]) if node else None
            if nid and nid not in seen_nodes:
                corner_group[label] = candidate_hid
                seen_nodes.add(nid)
                break

    selected: list[dict] = []
    for i, hid in enumerate(centre_group):
        clat, clon = centroids[hid]
        node = _nearest_capability_point(clat, clon, capability_points)
        selected.append({
            "hex_id": hid,
            "group": f"center_{i + 1}",
            "group_type": "center",
            "hex_centroid": (clat, clon),
            "node_id": str(node["node_id"]) if node else None,
            "node_lat": float(node["lat"]) if node else None,
            "node_lon": float(node["lon"]) if node else None,
            "hex_vertices": _hex_geometry(hid, grid_params),
        })
    for label, hid in sorted(corner_group.items()):
        clat, clon = centroids[hid]
        node = _nearest_capability_point(clat, clon, capability_points)
        selected.append({
            "hex_id": hid,
            "group": f"corner_{label}",
            "group_type": "corner",
            "hex_centroid": (clat, clon),
            "node_id": str(node["node_id"]) if node else None,
            "node_lat": float(node["lat"]) if node else None,
            "node_lon": float(node["lon"]) if node else None,
            "hex_vertices": _hex_geometry(hid, grid_params),
        })
    return selected


# ---------------------------------------------------------------------------
# Artifact context loaders
# ---------------------------------------------------------------------------

def _load_pt_context(cfg: PipelineConfig, mode: str = "bus") -> dict | None:
    """Load public transport (bus or subway, depending on mode) impedance matrix and destination mapping."""
    paths = cfg.public_transport_paths(mode)
    if not all(os.path.exists(paths[k]) for k in ("source_id_to_row", "dest_id_to_col", "routing_destinations_input", "impedance_matrix")):
        return None
    try:
        with open(paths["source_id_to_row"], encoding="utf-8") as f:
            source_to_row: dict[str, int] = {str(k): int(v) for k, v in json.load(f).items()}
        with open(paths["dest_id_to_col"], encoding="utf-8") as f:
            dest_id_to_col: dict[str, int] = {str(k): int(v) for k, v in json.load(f).items()}
        coord_to_col: dict[tuple, int] = {}
        with open(paths["routing_destinations_input"], newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                coord = (round(float(row["lat"]), 6), round(float(row["lon"]), 6))
                col = dest_id_to_col.get(row["id"])
                if col is not None:
                    coord_to_col[coord] = int(col)
        n_src = len(source_to_row)
        n_dst = len(dest_id_to_col)
        mat = np.memmap(paths["impedance_matrix"], dtype=np.float32, mode="r", shape=(n_src, n_dst))
        return {"mat": mat, "source_to_row": source_to_row, "coord_to_col": coord_to_col}
    except Exception as exc:
        print(f"[Debug] {mode} context load failed: {exc}")
        return None


def _load_accessibility_context(cfg: PipelineConfig) -> dict | None:
    """Load accessibility matrix and row/column mappings."""
    paths = [cfg.accessibility_matrix_path, cfg.accessibility_node_to_row_path, cfg.accessibility_poi_to_col_path, cfg.accessibility_meta_path]
    if not all(os.path.exists(p) for p in paths):
        return None
    try:
        with open(cfg.accessibility_meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        n_rows, n_cols = int(meta["shape"][0]), int(meta["shape"][1])
        with open(cfg.accessibility_node_to_row_path, encoding="utf-8") as f:
            node_to_row: dict[str, int] = {str(k): int(v) for k, v in json.load(f).items()}
        with open(cfg.accessibility_poi_to_col_path, encoding="utf-8") as f:
            poi_to_col: dict[str, int] = {str(k): int(v) for k, v in json.load(f).items()}
        mat = np.memmap(cfg.accessibility_matrix_path, dtype=np.float32, mode="r", shape=(n_rows, n_cols))
        return {"mat": mat, "node_to_row": node_to_row, "poi_to_col": poi_to_col}
    except Exception as exc:
        print(f"[Debug] Accessibility context load failed: {exc}")
        return None


def _load_service_context(cfg: PipelineConfig) -> dict | None:
    """Load service matrix and row/column mappings."""
    paths = [cfg.service_matrix_path, cfg.service_node_to_row_path, cfg.service_to_col_path, cfg.service_meta_path]
    if not all(os.path.exists(p) for p in paths):
        return None
    try:
        with open(cfg.service_meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        n_rows, n_cols = int(meta["shape"][0]), int(meta["shape"][1])
        with open(cfg.service_node_to_row_path, encoding="utf-8") as f:
            node_to_row: dict[str, int] = {str(k): int(v) for k, v in json.load(f).items()}
        with open(cfg.service_to_col_path, encoding="utf-8") as f:
            svc_to_col: dict[str, int] = {str(k): int(v) for k, v in json.load(f).items()}
        mat = np.memmap(cfg.service_matrix_path, dtype=np.float32, mode="r", shape=(n_rows, n_cols))
        return {"mat": mat, "node_to_row": node_to_row, "svc_to_col": svc_to_col}
    except Exception as exc:
        print(f"[Debug] Service context load failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Chain computation for one hexagon
# ---------------------------------------------------------------------------

def _bus_time(poi_coord: tuple, source_row: int | None, bus_ctx: dict | None) -> float | None:
    if bus_ctx is None or source_row is None:
        return None
    coord_key = (round(float(poi_coord[0]), 6), round(float(poi_coord[1]), 6))
    col = bus_ctx["coord_to_col"].get(coord_key)
    if col is None:
        return None
    v = float(bus_ctx["mat"][source_row, col])
    return v if v > 0 else None


def _build_step1(non_bus: dict, source_row: int | None, bus_ctx: dict | None, subway_source_row: int | None, subway_ctx: dict | None, poi_names: dict[str, str] | None = None) -> dict:
    """Step 1 — raw impedances from the non-bus pickle."""
    services_out = []
    for svc in serv.SERVICE_KEYS:
        entries_out = []
        for entry in non_bus.get("services", {}).get(svc, []):
            poi_type = str(entry["poi_type"])
            try:
                decay_coeff = float(get_decay_coefficient(poi_type))
            except Exception:
                decay_coeff = None

            imp_walk_list = entry.get("imp_walk", []) or []
            imp_bike_list = entry.get("imp_bike", []) or []
            imp_drive_list = entry.get("imp_drive", []) or []
            poi_coords = entry.get("poi_coords", []) or []
            source_coords = entry.get("source_coords", []) or []
            walk_scores = entry.get("walk_path_scores", []) or []
            source_keys = entry.get("source_keys", []) or []
            n = len(poi_coords)

            pois_out = []
            for i in range(n):
                src_coord = source_coords[i] if i < len(source_coords) else (None, None)
                poi_coord = poi_coords[i] if i < len(poi_coords) else (None, None)
                src_key = str(source_keys[i]) if i < len(source_keys) else ""
                pois_out.append({
                    "source_key": src_key,
                    "name": (poi_names or {}).get(src_key),
                    "src_lat": float(src_coord[0]) if src_coord[0] is not None else None,
                    "src_lon": float(src_coord[1]) if src_coord[1] is not None else None,
                    "walk_min": float(imp_walk_list[i]) if i < len(imp_walk_list) and imp_walk_list[i] is not None else None,
                    "bike_min": float(imp_bike_list[i]) if i < len(imp_bike_list) and imp_bike_list[i] is not None else None,
                    "drive_min": float(imp_drive_list[i]) if i < len(imp_drive_list) and imp_drive_list[i] is not None else None,
                    "bus_min": _bus_time(poi_coord, source_row, bus_ctx) if poi_coord[0] is not None else None,
                    "subway_min": _bus_time(poi_coord, subway_source_row, subway_ctx) if poi_coord[0] is not None else None,
                    "walk_score": float(walk_scores[i]) if i < len(walk_scores) and walk_scores[i] is not None else None,
                })
            pois_out.sort(key=lambda p: p["walk_min"] if p["walk_min"] is not None else float("inf"))
            n_total = len(pois_out)
            if pois_out:
                entries_out.append({
                    "poi_type": poi_type,
                    "decay_coeff": decay_coeff,
                    "pois": pois_out[:MAX_POIS_PER_TYPE],
                    "n_total": n_total,
                    "truncated": n_total > MAX_POIS_PER_TYPE,
                })
        services_out.append({"service": svc, "entries": entries_out})
    return {"services": services_out}


def _delta_g_details(rra: list[float], poi_type: str) -> dict:
    """Step-by-step breakdown of the Delta-g POI aggregation for one poi_type.

    Mirrors ``accessibility_from_rra``: per-POI RRAs are sorted descending and each
    is weighted by the marginal saturation increment Delta g_k(j) = g(j+1) − g(j),
    with g(x) = 1 − exp(c·x). Returns the inputs, the resolved contribution
    coefficient/c, and a per-rank step list (value, g, delta_g, term, running total).
    """
    try:
        contribution_coefficient = float(serv.get_contribution_coefficient(poi_type))
    except Exception:
        contribution_coefficient = 2.0

    y_target = 0.9
    c = round(math.log(1.0 - y_target) / contribution_coefficient, 2) if contribution_coefficient > 0 else 0.0

    def g(x: int) -> float:
        return 1.0 - math.exp(c * x)

    rra_desc = sorted((float(v) for v in rra), reverse=True)
    steps = []
    total = 0.0
    prev_g = 0.0
    for i, element in enumerate(rra_desc):
        g_cur = g(i + 1)
        delta_g = g_cur - prev_g  # g(1) - g(0)=0 for i==0, so this matches element*g(1)
        term = element * delta_g
        total += term
        steps.append({
            "rank": i + 1,
            "value": round(element, 4),
            "g": round(g_cur, 4),
            "delta_g": round(delta_g, 4),
            "term": round(term, 4),
            "running_total": round(total, 4),
        })
        prev_g = g_cur

    return {
        "contribution_coefficient": contribution_coefficient,
        "c": c,
        "y_target": y_target,
        "n_pois": len(rra_desc),
        "steps": steps[:MAX_AGGREGATION_STEPS],
        "total": round(total, 4),
    }


def _build_step2(
    non_bus: dict,
    source_row: int | None,
    subway_source_row: int | None,
    bus_ctx: dict | None,
    subway_ctx: dict | None,
    acc_ctx: dict | None,
    acc_node_row: int | None,
    cfg: PipelineConfig,
    drop_map: dict[str, set[str]] | None = None,
    poi_names: dict[str, str] | None = None,
) -> tuple[dict, dict[str, float]]:
    """Step 2 — accessibility decay applied per mode and merged into poi_type scores."""
    # Deduplicate POI entries by poi_type across services.
    entry_by_poi_type: dict[str, dict] = {}
    for svc in serv.SERVICE_KEYS:
        for entry in non_bus.get("services", {}).get(svc, []):
            pt = str(entry["poi_type"])
            if pt not in entry_by_poi_type:
                entry_by_poi_type[pt] = entry

    poi_types_out = []
    poi_to_col = acc_ctx["poi_to_col"] if acc_ctx else {}
    acc_mat = acc_ctx["mat"] if acc_ctx else None
    # Recomputed per-POI accessibility keyed by source_key, mirroring how the pipeline
    # stores accessibility_by_poi (last non-zero value wins across poi_types). Used by the
    # export-verification step.
    acc_by_source_key: dict[str, float] = {}

    for pt, entry in entry_by_poi_type.items():
        try:
            decay_coeff = float(get_decay_coefficient(pt))
        except Exception:
            decay_coeff = 16.0
        beta = math.log(2) / decay_coeff

        imp_walk_list = entry.get("imp_walk", []) or []
        imp_bike_list = entry.get("imp_bike", []) or []
        imp_drive_list = entry.get("imp_drive", []) or []
        poi_coords = entry.get("poi_coords", []) or []
        source_coords = entry.get("source_coords", []) or []
        source_keys = entry.get("source_keys", []) or []
        n = len(poi_coords)

        pois_out = []
        poi_accs: list[float] = []  # per-POI accessibility (single-element RRA transform)
        for i in range(n):
            wm = float(imp_walk_list[i]) if i < len(imp_walk_list) and imp_walk_list[i] is not None else None
            bm = float(imp_bike_list[i]) if i < len(imp_bike_list) and imp_bike_list[i] is not None else None
            dm = float(imp_drive_list[i]) if i < len(imp_drive_list) and imp_drive_list[i] is not None else None
            poi_coord = poi_coords[i] if i < len(poi_coords) else (None, None)
            bus_m = _bus_time(poi_coord, source_row, bus_ctx) if poi_coord[0] is not None else None
            subway_m = _bus_time(poi_coord, subway_source_row, subway_ctx) if poi_coord[0] is not None else None
            src_coord = source_coords[i] if i < len(source_coords) else (None, None)
            src_key = str(source_keys[i]) if i < len(source_keys) else ""

            dw = math.exp(-beta * wm) if wm is not None else 0.0
            db = math.exp(-beta * bm) if bm is not None else 0.0
            dd = math.exp(-beta * dm) if dm is not None else 0.0
            dbus = math.exp(-beta * bus_m) if bus_m is not None else 0.0
            if subway_ctx is None:
                dsub = None
            else:
                dsub = math.exp(-beta * subway_m) if subway_m is not None else 0.0

            # Per-POI accessibility A^i_k(x, y) is exactly the RRA over modes; the
            # Delta-g aggregation happens only at the POI-type level downstream.
            rra = calculate_rra(dw, db, dd, dbus, dsub)
            poi_acc = rra
            poi_accs.append(poi_acc)

            pois_out.append({
                "source_key": src_key,
                "name": (poi_names or {}).get(src_key),
                "src_lat": float(src_coord[0]) if src_coord[0] is not None else None,
                "src_lon": float(src_coord[1]) if src_coord[1] is not None else None,
                "walk_min": round(wm, 2) if wm is not None else None,
                "bike_min": round(bm, 2) if bm is not None else None,
                "drive_min": round(dm, 2) if dm is not None else None,
                "bus_min": round(bus_m, 2) if bus_m is not None else None,
                "subway_min": round(subway_m, 2) if subway_m is not None else None,
                "d_walk": round(dw, 4),
                "d_bike": round(db, 4),
                "d_drive": round(dd, 4),
                "d_bus": round(dbus, 4),
                "d_subway": round(dsub, 4) if dsub is not None else None,
                "rra": round(rra, 4),
                "poi_acc": round(poi_acc, 4),
            })

        # Aggregate: pipeline treats per-POI accessibilities as decay_walk inputs.
        computed = float(accessibility_from_rra(poi_accs, poi_type=pt)) if poi_accs else 0.0

        # Value from the matrix.
        from_matrix: float | None = None
        if acc_mat is not None and acc_node_row is not None:
            col = poi_to_col.get(pt)
            if col is not None:
                v = float(acc_mat[acc_node_row, col])
                from_matrix = None if math.isnan(v) else v

        # Map source_key -> per-POI accessibility (same overwrite/>0 rule the pipeline
        # uses when building accessibility_by_poi). source_keys are aligned to poi_coords.
        # Apply the same per-service ownership drop so a non-owning poi_type does not
        # overwrite the owning value (mirrors accessibility_stage zeroing dropped pairs).
        drop_for_pt = (drop_map or {}).get(pt, ())
        src_keys = entry.get("source_keys", []) or []
        for i, a in enumerate(poi_accs):
            if i < len(src_keys) and src_keys[i] and a > 0.0:
                if str(src_keys[i]) in drop_for_pt:
                    continue
                acc_by_source_key[str(src_keys[i])] = float(a)

        pois_out.sort(key=lambda p: p.get("poi_acc", 0), reverse=True)
        n_total = len(pois_out)
        poi_types_out.append({
            "poi_type": pt,
            "decay_coeff": decay_coeff,
            "beta": round(beta, 6),
            "pois": pois_out[:MAX_POIS_PER_TYPE],
            "n_total": n_total,
            "truncated": n_total > MAX_POIS_PER_TYPE,
            "poi_type_acc_computed": round(computed, 4),
            "poi_type_acc_matrix": round(from_matrix, 4) if from_matrix is not None else None,
            # `computed` is the exact accessibility_from_rra() aggregate over every POI
            # (poi_accs is never truncated); only the display rows/steps below are capped.
            "aggregation": _delta_g_details(poi_accs, pt),
        })

    return {"poi_types": poi_types_out}, acc_by_source_key


def _build_step3(
    acc_ctx: dict | None,
    acc_node_row: int | None,
    svc_ctx: dict | None,
    svc_node_row: int | None,
) -> dict:
    """Step 3 — Choquet service aggregation."""
    acc_mat = acc_ctx["mat"] if acc_ctx else None
    poi_to_col = acc_ctx["poi_to_col"] if acc_ctx else {}
    svc_mat = svc_ctx["mat"] if svc_ctx else None
    svc_to_col = svc_ctx["svc_to_col"] if svc_ctx else {}

    services_out = []
    for svc in serv.SERVICE_KEYS:
        queries = serv.get_service_queries(svc)
        poi_types = [q.poi_type for q in queries]

        # Collect accessibility values for this service's POI types.
        acc_vals: list[float] = []
        for pt in poi_types:
            v = 0.0
            if acc_mat is not None and acc_node_row is not None:
                col = poi_to_col.get(pt)
                if col is not None:
                    raw = float(acc_mat[acc_node_row, col])
                    v = 0.0 if math.isnan(raw) else raw
            acc_vals.append(v)

        choquet = choquet_integral_details(acc_vals, svc)

        # Service score from matrix.
        from_matrix: float | None = None
        if svc_mat is not None and svc_node_row is not None:
            col = svc_to_col.get(svc)
            if col is not None:
                from_matrix = float(svc_mat[svc_node_row, col])

        services_out.append({
            "service": svc,
            "poi_types_order": poi_types,
            "accessibility_inputs": [round(v, 4) for v in acc_vals],
            "score_from_matrix": round(from_matrix, 4) if from_matrix is not None else None,
            "choquet": choquet,
        })

    return {"services": services_out}


def _build_step_verify(
    hex_id: str,
    node_id: Any,
    poi_by_node_dir: str,
    acc_by_source_key: dict[str, float],
    node_scores: dict[str, dict[str, float]],
    hex_source: Path,
    gpkg_path: Path | None,
    drop_map: dict[str, set[str]] | None = None,
    poi_names: dict[str, str] | None = None,
) -> dict | None:
    """Cross-check recomputed per-POI accessibility against the pipeline's EXPORTED values.

    For each POI the pipeline exported under this hexagon (hex_pois files → {id, sp, cp}):
      • read the pipeline's stored per-POI accessibility straight from the per-node
        accessibility cache (accessibility/poi_by_node/{node_id}.npz → {poi_id: acc}),
        i.e. the exact value score_report.py fed into the export — NOT backed out from
        the stored sp, because the shard writer quantile-rescales sp/cp per key on export
        (utils/power_scaling.py), so the stored sp is a rank, not acc × Σ singleton, and
        inverting it would spuriously disagree with the recomputation, and
      • recompute service_power/capability_power from both the cached exported acc and our
        own recomputed acc using the exact pipeline routine (poi_exports._poi_powers),
    then flag any mismatch. This is the end-to-end check that "accessibility to the POI" is
    both computed and exported consistently. (The quantile rescaling of the stored sp/cp is
    a display-only transform validated separately by the power-scaling dashboard.)
    """
    if gpkg_path is None or not Path(gpkg_path).exists():
        return {"available": False, "reason": "pois_used.gpkg not found — run spatial export."}
    try:
        hex_items = _load_hex_items(hex_id, hex_source)
    except FileNotFoundError:
        return {"available": False, "reason": f"No exported POIs for hex {hex_id} (hex_pois missing)."}
    except Exception as exc:
        return {"available": False, "reason": f"Could not read hex export: {exc}"}

    # hex_pois objects use compact keys: "i" = POI id, "sp"/"cp" = power maps.
    poi_ids = [int(it["i"]) for it in hex_items if "i" in it]
    try:
        poi_rows = _fetch_poi_rows(Path(gpkg_path), poi_ids)
    except Exception as exc:
        return {"available": False, "reason": f"Could not read pois_used.gpkg: {exc}"}

    from exports.poi_exports import _poi_powers  # exact pipeline power formula (lazy import)
    from analysis.score_report import _load_node_sparse  # exact per-node acc cache reader (lazy import)

    # The pipeline's per-POI accessibility for this hexagon's representative node, keyed by
    # POI id — the same {poi_id: acc} score_report.py reads to build the export. This is the
    # ground truth for "exported accessibility"; hex_pois' stored sp/cp are quantile-scaled.
    exported_acc_by_pid = _load_node_sparse(node_id, poi_by_node_dir)

    rows = []
    mism = 0
    for it in hex_items:
        pid = int(it.get("i", -1))
        row_info = poi_rows.get(pid)
        if row_info is None:
            continue
        source_key = row_info["source_key"]
        try:
            poi_types = json.loads(row_info.get("poi_types", "[]")) or []
        except Exception:
            poi_types = []
        poi_types = [str(p) for p in poi_types]

        # Exported accessibility read straight from the pipeline's per-node cache (the value
        # score_report.py used), not inverted from the quantile-scaled stored sp.
        exported_acc = exported_acc_by_pid.get(pid)
        recomputed_acc = acc_by_source_key.get(source_key)

        # Recompute powers with the exact pipeline routine from both accessibilities, so the
        # sp/cp columns compare like with like (both pre-scale). The stored, quantile-scaled
        # sp/cp in hex_pois are display-only and covered by the power-scaling dashboard.
        rec_sp, rec_cp = ({}, {})
        if recomputed_acc is not None:
            rec_sp, rec_cp = _poi_powers(source_key, poi_types, {source_key: recomputed_acc}, node_scores, drop_map)
        exp_sp, exp_cp = ({}, {})
        if exported_acc is not None:
            exp_sp, exp_cp = _poi_powers(source_key, poi_types, {source_key: exported_acc}, node_scores, drop_map)

        acc_match = (
            recomputed_acc is not None
            and exported_acc is not None
            and abs(recomputed_acc - exported_acc) < 1e-4
        )
        sp_match = all(
            abs(float(rec_sp.get(s, 0.0)) - float(exp_sp.get(s, 0.0))) < 1e-4
            for s in set(rec_sp) | set(exp_sp)
        )
        cp_match = all(
            abs(float(rec_cp.get(c, 0.0)) - float(exp_cp.get(c, 0.0))) < 1e-4
            for c in set(rec_cp) | set(exp_cp)
        )
        ok = bool(acc_match and sp_match and cp_match)
        if not ok:
            mism += 1
        rows.append({
            "id": pid,
            "source_key": source_key[:28],
            "name": (poi_names or {}).get(source_key),
            "poi_types": poi_types,
            "recomputed_acc": round(recomputed_acc, 4) if recomputed_acc is not None else None,
            "exported_acc": round(exported_acc, 4) if exported_acc is not None else None,
            "acc_match": acc_match,
            "sp_recomputed": {s: round(v, 4) for s, v in rec_sp.items()},
            "sp_exported": {s: round(float(v), 4) for s, v in exp_sp.items()},
            "cp_recomputed": {c: round(v, 4) for c, v in rec_cp.items()},
            "cp_exported": {c: round(float(v), 4) for c, v in exp_cp.items()},
            "match": ok,
        })

    # Mismatches first, then by recomputed accessibility. Cap the rows carried into the
    # HTML (a hexagon can see thousands of POIs); the totals below stay exact.
    rows.sort(key=lambda r: (r["match"], -(r["recomputed_acc"] or 0)))
    return {
        "available": True,
        "n_pois": len(rows),
        "n_mismatch": mism,
        "rows": rows[:MAX_VERIFY_ROWS],
        "truncated": len(rows) > MAX_VERIFY_ROWS,
    }


def _build_node_scores(acc_ctx: dict | None, acc_node_row: int | None) -> dict[str, dict[str, float]]:
    """Build {service: {poi_type: accessibility}} for one node from the accessibility matrix.

    Used as the fallback table inside poi_exports._poi_powers (only consulted when a POI has
    no per-POI accessibility); recomputation here passes an explicit per-POI value, so this is
    mostly a safety net kept faithful to the pipeline.
    """
    if acc_ctx is None or acc_node_row is None:
        return {}
    acc_mat = acc_ctx["mat"]
    poi_to_col = acc_ctx["poi_to_col"]
    out: dict[str, dict[str, float]] = {}
    for service in serv.SERVICE_KEYS:
        type_scores: dict[str, float] = {}
        for query in serv.get_service_queries(service):
            pt = str(query.poi_type)
            col = poi_to_col.get(pt)
            if col is not None:
                v = float(acc_mat[acc_node_row, col])
                type_scores[pt] = 0.0 if math.isnan(v) else v
        out[service] = type_scores
    return out


def _nan_safe(v: Any) -> Any:
    """Replace float inf/nan with None for JSON serialisation."""
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
    return v


def _sanitise(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sanitise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitise(i) for i in obj]
    return _nan_safe(obj)


def _build_step4(svc_ctx: dict | None, svc_node_row: int | None) -> dict:
    """Step 4 — ELECTRE TRI capability aggregation."""
    svc_mat = svc_ctx["mat"] if svc_ctx else None
    svc_to_col = svc_ctx["svc_to_col"] if svc_ctx else {}

    capabilities_out = []
    for cap, services in CAPABILITY_SERVICES.items():
        svc_scores: list[float] = []
        for svc in services:
            v = 0.0
            if svc_mat is not None and svc_node_row is not None:
                col = svc_to_col.get(svc)
                if col is not None:
                    raw = float(svc_mat[svc_node_row, col])
                    v = 0.0 if math.isnan(raw) else raw
            svc_scores.append(v)

        details = _sanitise(electre_tri_details(svc_scores, cap))
        capabilities_out.append({"capability": cap, "electre": details})

    return {"capabilities": capabilities_out}


def _build_chain(
    hex_entry: dict,
    cfg: PipelineConfig,
    bus_ctx: dict | None,
    acc_ctx: dict | None,
    svc_ctx: dict | None,
    subway_ctx : dict | None,
    hex_source: Path,
    gpkg_path: Path | None,
    drop_map: dict[str, set[str]] | None = None,
    poi_names: dict[str, str] | None = None,
) -> dict:
    node_id = hex_entry.get("node_id")
    vertices_latlng = [[lat, lon] for lon, lat in hex_entry["hex_vertices"]]
    clat, clon = hex_entry["hex_centroid"]

    chain: dict[str, Any] = {
        "hex_id": hex_entry["hex_id"],
        "group": hex_entry["group"],
        "group_type": hex_entry["group_type"],
        "node_id": node_id,
        "node_lat": hex_entry.get("node_lat"),
        "node_lon": hex_entry.get("node_lon"),
        "centroid_lat": clat,
        "centroid_lon": clon,
        "hex_vertices": vertices_latlng,
        "has_data": False,
        "step1": None,
        "step2": None,
        "step3": None,
        "step4": None,
        "verify": None,
    }

    if not node_id:
        return chain

    # Non-bus cache
    non_bus_path = os.path.join(cfg.non_bus_cache_dir, f"{node_id}.pkl")
    if not os.path.exists(non_bus_path):
        print(f"[Debug] Non-bus cache missing for node {node_id}")
        return chain
    try:
        with open(non_bus_path, "rb") as f:
            non_bus = pickle.load(f)
    except Exception as exc:
        print(f"[Debug] Failed to load non-bus cache for node {node_id}: {exc}")
        return chain

    bus_source_row: int | None = None
    if bus_ctx:
        bus_source_row = bus_ctx["source_to_row"].get(str(node_id))

    subway_source_row: int | None = None
    if subway_ctx:
        subway_source_row = subway_ctx["source_to_row"].get(str(node_id))

    acc_node_row: int | None = None
    if acc_ctx:
        acc_node_row = acc_ctx["node_to_row"].get(str(node_id))

    svc_node_row: int | None = None
    if svc_ctx:
        svc_node_row = svc_ctx["node_to_row"].get(str(node_id))

    chain["has_data"] = True
    chain["step1"] = _build_step1(non_bus, bus_source_row, bus_ctx, subway_source_row, subway_ctx, poi_names)
    chain["step2"], acc_by_source_key = _build_step2(non_bus, bus_source_row, subway_source_row, bus_ctx, subway_ctx, acc_ctx, acc_node_row, cfg, drop_map, poi_names)
    chain["step3"] = _build_step3(acc_ctx, acc_node_row, svc_ctx, svc_node_row)
    chain["step4"] = _build_step4(svc_ctx, svc_node_row)
    chain["verify"] = _build_step_verify(
        hex_entry["hex_id"],
        node_id,
        cfg.accessibility_poi_by_node_dir,
        acc_by_source_key,
        _build_node_scores(acc_ctx, acc_node_row),
        hex_source,
        gpkg_path,
        drop_map,
        poi_names,
    )
    return chain


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Capability Debug Pipeline</title>
  <link rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
  <style>
    *{box-sizing:border-box}
    body{margin:0;font-family:Arial,sans-serif;background:#f3f1ea;color:#1f2933;font-size:13px}
    .layout{display:flex;height:100vh;overflow:hidden}
    .left{width:42%;min-width:340px;display:flex;flex-direction:column;border-right:1px solid #d9d3c7}
    .right{flex:1;overflow-y:auto;padding:14px 16px;background:#f8f4ec}
    #map{flex:1}
    .hex-list{padding:8px 10px;background:#fffdf8;border-top:1px solid #d9d3c7;overflow-y:auto;max-height:160px}
    .hex-btn{display:inline-block;margin:3px;padding:4px 10px;border:1px solid #bbb;border-radius:20px;cursor:pointer;font-size:12px;background:#fff}
    .hex-btn.center{border-color:#1d4ed8;color:#1d4ed8}
    .hex-btn.corner{border-color:#c26a00;color:#c26a00}
    .hex-btn.active{font-weight:bold;background:#1d4ed8;color:#fff;border-color:#1d4ed8}
    .hex-btn.active.corner{background:#c26a00;border-color:#c26a00}
    h2{margin:0 0 10px;font-size:17px}
    h3{margin:6px 0 4px;font-size:14px;color:#374151}
    .panel{background:#fffdf8;border:1px solid #d9d3c7;border-radius:10px;margin-bottom:12px;overflow:hidden}
    .panel-header{padding:10px 14px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;background:#f1ede4;user-select:none}
    .panel-header:hover{background:#e8e3d8}
    .panel-body{padding:12px 14px;display:none}
    .panel-body.open{display:block}
    .arrow{transition:transform .2s}
    .arrow.open{transform:rotate(90deg)}
    table{width:100%;border-collapse:collapse;font-size:12px;margin-top:6px}
    th,td{text-align:left;padding:5px 7px;border-bottom:1px solid #e8e3d8;vertical-align:top}
    th{background:#f1ede4;font-weight:600;white-space:nowrap}
    .null{color:#aaa;font-style:italic}
    .fast{background:#d1fae5}
    .medium{background:#fef3c7}
    .slow{background:#fde8d8}
    .veryslow{background:#fee2e2}
    .acc-bar-wrap{display:inline-block;width:80px;height:10px;background:#e5e7eb;border-radius:4px;vertical-align:middle;margin-left:4px}
    .acc-bar{height:10px;border-radius:4px;background:#0f766e}
    .score-chip{display:inline-block;padding:2px 8px;border-radius:10px;font-weight:700;font-size:11px}
    .cat-verylow{background:#fee2e2;color:#991b1b}
    .cat-low{background:#fde8d8;color:#9a3412}
    .cat-medium{background:#fef3c7;color:#92400e}
    .cat-high{background:#d1fae5;color:#065f46}
    .cat-veryhigh{background:#dbeafe;color:#1e3a8a}
    .outranks-yes{color:#065f46;font-weight:700}
    .outranks-no{color:#6b7280}
    .muted{color:#6b7280;font-size:11px}
    .toggle-btn{font-size:11px;color:#1d4ed8;background:none;border:none;cursor:pointer;padding:2px 0;margin:2px 0 6px;display:block}
    .toggle-btn:hover{text-decoration:underline}
    .maps-link{font-size:10px;color:#1d4ed8;text-decoration:none;margin-left:3px;opacity:0.7}
    .maps-link:hover{opacity:1}
    .section-label{font-size:11px;font-weight:700;text-transform:uppercase;color:#52606d;margin:10px 0 4px}
    .formula{font-family:monospace;font-size:11px;background:#f1ede4;padding:2px 6px;border-radius:4px;color:#374151}
    .match-ok{color:#065f46}
    .match-warn{color:#b45309;font-weight:700}
    #no-data{color:#6b7280;padding:20px 0;text-align:center}
    .meta-row{display:flex;gap:16px;margin-bottom:8px;font-size:12px;color:#52606d}
    .meta-val{font-weight:700;color:#1f2933}
    .step-title{font-size:13px;font-weight:700}
    .step-badge{display:inline-block;width:22px;height:22px;border-radius:50%;background:#0f766e;color:#fff;font-size:11px;font-weight:700;text-align:center;line-height:22px;margin-right:6px}
    .service-block{margin-bottom:12px}
    .poi-type-label{font-size:12px;font-weight:600;color:#374151;margin:8px 0 3px}
    .choquet-step{display:flex;gap:6px;font-size:11px;padding:3px 0;border-bottom:1px solid #ece6d8}
    .boundary-block{margin:8px 0;border:1px solid #ddd;border-radius:6px;overflow:hidden}
    .boundary-header{padding:7px 12px;background:#f4f0e6;font-weight:700;font-size:12px}
    .b-step{padding:5px 12px;font-size:11px;border-top:1px solid #ece6d8;display:flex;align-items:baseline;gap:6px}
    .b-num{display:inline-flex;align-items:center;justify-content:center;width:17px;height:17px;border-radius:50%;background:#0f766e;color:#fff;font-size:10px;font-weight:700;flex-shrink:0}
    .b-calc{font-family:monospace;font-size:11px;color:#374151}
    .b-table{padding:0 12px 6px;border-top:1px solid #ece6d8}
    .b-decision{padding:6px 12px;font-size:12px;font-weight:700;border-top:2px solid #ddd;background:#f9f7f2}
    .elec-summary{background:#eef4ff;border:1px solid #c3d9f5;border-radius:6px;padding:8px 12px;margin:10px 0 4px;font-size:12px}
    .d-pos{color:#065f46}
    .d-neg{color:#b91c1c}
    .rule-full{color:#065f46;font-weight:600}
    .rule-partial{color:#92400e;font-weight:600}
    .rule-none{color:#b91c1c;font-weight:600}
  </style>
</head>
<body>
<div class="layout">
  <div class="left">
    <div id="map"></div>
    <div class="hex-list" id="hex-list"></div>
  </div>
  <div class="right" id="right-panel">
    <div id="no-data" style="display:block">Click a hexagon on the map to explore its computation chain.</div>
    <div id="chain-content" style="display:none"></div>
  </div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
  integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script>
const CHAIN_DATA = __CHAIN_DATA__;
const SUBWAY_ENABLED = __SUBWAY_ENABLED__;

// ---- Map ----
const map = L.map("map");
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{
  maxZoom:19,attribution:"&copy; OpenStreetMap contributors"
}).addTo(map);

let activeHexId = null;
let poiLayer = null;
const hexLayers = {};
const allBounds = [];

for(const [hexId, chain] of Object.entries(CHAIN_DATA)){
  const isCenter = chain.group_type === "center";
  const color = isCenter ? "#1d4ed8" : "#c26a00";
  const fillColor = isCenter ? "#60a5fa" : "#f1a340";
  const poly = L.polygon(chain.hex_vertices, {
    color, weight:2, fillColor, fillOpacity:0.15
  }).addTo(map);
  poly.bindTooltip(`<b>${hexId}</b><br>${chain.group}`);
  poly.on("click", () => selectHex(hexId));
  hexLayers[hexId] = poly;
  chain.hex_vertices.forEach(ll => allBounds.push(ll));
  if(chain.node_lat != null)
    L.circleMarker([chain.node_lat, chain.node_lon],{radius:5,color:color,weight:2,fillColor:"#fff",fillOpacity:0.9}).addTo(map);
}
if(allBounds.length) map.fitBounds(allBounds, {padding:[20,20]});

// ---- Hex list buttons ----
const hexList = document.getElementById("hex-list");
for(const [hexId, chain] of Object.entries(CHAIN_DATA)){
  const btn = document.createElement("span");
  btn.className = "hex-btn " + chain.group_type;
  btn.textContent = hexId + " (" + chain.group + ")";
  btn.dataset.hexId = hexId;
  btn.onclick = () => selectHex(hexId);
  hexList.appendChild(btn);
}

function selectHex(hexId){
  if(activeHexId){
    const prev = hexLayers[activeHexId];
    if(prev) prev.setStyle({weight:2, fillOpacity:0.15});
    document.querySelector(`.hex-btn[data-hex-id="${activeHexId}"]`)?.classList.remove("active");
  }
  activeHexId = hexId;
  const poly = hexLayers[hexId];
  if(poly) poly.setStyle({weight:3, fillOpacity:0.35});
  document.querySelector(`.hex-btn[data-hex-id="${hexId}"]`)?.classList.add("active");

  const chain = CHAIN_DATA[hexId];

  // Draw POI markers for step 1 pois
  if(poiLayer){ poiLayer.remove(); poiLayer = null; }
  if(chain.step1){
    const markers = [];
    chain.step1.services.forEach(s => s.entries.forEach(e => e.pois.forEach(p => {
      if(p.src_lat != null)
        markers.push(L.circleMarker([p.src_lat, p.src_lon],{radius:4,color:"#0f766e",weight:1,fillColor:"#14b8a6",fillOpacity:0.9})
          .bindTooltip(`${e.poi_type}<br>${poiLabel(p)}`));
    })));
    if(markers.length){ poiLayer = L.layerGroup(markers).addTo(map); }
  }
  map.fitBounds(chain.hex_vertices, {padding:[30,30]});

  renderChain(chain);
  document.getElementById("no-data").style.display = "none";
  document.getElementById("chain-content").style.display = "block";
}

// ---- Rendering ----
function renderChain(chain){
  const el = document.getElementById("chain-content");
  el.innerHTML = "";

  const catClass = {"Very Low":"cat-verylow","Low":"cat-low","Medium":"cat-medium","High":"cat-high","Very High":"cat-veryhigh"};

  // Header
  el.insertAdjacentHTML("beforeend", `
    <h2>${chain.hex_id} <small style="font-weight:400;color:#6b7280">${chain.group}</small></h2>
    <div class="meta-row">
      <span>Node: <span class="meta-val">${chain.node_id ?? "—"}</span></span>
      <span>Lat: <span class="meta-val">${chain.node_lat?.toFixed(5) ?? "—"}</span></span>
      <span>Lon: <span class="meta-val">${chain.node_lon?.toFixed(5) ?? "—"}</span></span>
    </div>
  `);

  if(!chain.has_data){
    el.insertAdjacentHTML("beforeend","<p style='color:#b45309'>No routing cache found for this hexagon's representative node.</p>");
    return;
  }

  // Step 1
  const p1 = makePanel("1","Impedances","Travel time (minutes) to each POI by mode");
  if(chain.step1){
    chain.step1.services.forEach(s => {
      if(!s.entries.length) return;
      p1.body.insertAdjacentHTML("beforeend",`<div class="section-label">${s.service}</div>`);
      s.entries.forEach(e => {
        const countLabel1 = e.truncated ? `${e.pois.length} of ${e.n_total} POIs, nearest-first` : `${e.pois.length} POIs`;
        p1.body.insertAdjacentHTML("beforeend",`<div class="poi-type-label">${e.poi_type} <span class="muted">(T½ = ${e.decay_coeff} min, ${countLabel1})</span></div>`);
        const rows = e.pois.map(p => `
          <tr>
            <td>${poiLabel(p)}${gMapsLink(chain.node_lat, chain.node_lon, p.src_lat, p.src_lon)}</td>
            <td class="${timeClass(p.walk_min)}">${fmt(p.walk_min)} ${p.walk_score != null ? '<span class="muted">ws='+p.walk_score.toFixed(1)+'</span>' : ''}</td>
            <td class="${timeClass(p.bike_min)}">${fmt(p.bike_min)}</td>
            <td class="${timeClass(p.drive_min)}">${fmt(p.drive_min)}</td>
            <td class="${timeClass(p.bus_min)}">${fmt(p.bus_min)}</td>
            ${SUBWAY_ENABLED? `<td class="${timeClass(p.subway_min)}">${fmt(p.subway_min)}</td>` : ""}
          </tr>`);
        p1.body.insertAdjacentHTML("beforeend", makeToggleTable(
          `<thead><tr><th>POI</th><th>Walk</th><th>Bike</th><th>Drive</th><th>Bus</th>${SUBWAY_ENABLED? "<th>Subway</th>" : ""}</tr></thead>`,
          rows, 10));
      });
    });
  }
  el.appendChild(p1.panel);

  // Step 2
  const p2 = makePanel("2","Accessibility","Decay d = exp(−β×t) per mode → RRA merge → Δg POI aggregation → poi_type score");
  if(chain.step2){
    const aggSummaryRows = [];
    let aggMaxRanks = 0;
    chain.step2.poi_types.forEach(pt => {
      const match = pt.poi_type_acc_matrix != null
        ? (Math.abs(pt.poi_type_acc_computed - pt.poi_type_acc_matrix) < 0.01
           ? `<span class="match-ok">✓ matrix=${pt.poi_type_acc_matrix.toFixed(3)}</span>`
           : `<span class="match-warn">⚠ matrix=${pt.poi_type_acc_matrix.toFixed(3)} computed=${pt.poi_type_acc_computed.toFixed(3)}</span>`)
        : `<span class="muted">matrix N/A</span>`;
      const countLabel2 = pt.truncated ? `<span class="muted">(top ${pt.pois.length} of ${pt.n_total}, by poi_acc)</span>` : "";
      p2.body.insertAdjacentHTML("beforeend",`
        <div class="poi-type-label">${pt.poi_type}
          <span class="muted">β=${pt.beta.toFixed(4)}</span>
          ${match}
          ${countLabel2}
        </div>
      `);
      const rows = pt.pois.map(p => {
        const acc = p.poi_acc ?? 0;
        return `<tr>
          <td>${poiLabel(p)}${gMapsLink(chain.node_lat, chain.node_lon, p.src_lat, p.src_lon)}</td>
          <td>${fmtD(p.d_walk)}</td>
          <td>${fmtD(p.d_bike)}</td>
          <td>${fmtD(p.d_drive)}</td>
          <td>${fmtD(p.d_bus)}</td>
          ${SUBWAY_ENABLED? `<td>${fmtD(p.d_subway)}</td>` : ""}
          <td>${fmtD(p.rra)}</td>
          <td>${fmtD(acc)}<div class="acc-bar-wrap"><div class="acc-bar" style="width:${(acc*100).toFixed(0)}%"></div></div></td>
        </tr>`;
      });
      p2.body.insertAdjacentHTML("beforeend", makeToggleTable(
        `<thead><tr><th>POI</th><th>d_walk</th><th>d_bike</th><th>d_drive</th><th>d_bus</th>${SUBWAY_ENABLED? "<th>d_subway</th>" : ""} <th>RRA</th><th>poi_acc</th></tr></thead>`,
        rows, 10));
      p2.body.insertAdjacentHTML("beforeend",`
        <div class="muted" style="margin:2px 0 4px">
          <span class="formula">RRA = 1−∏(1−λ_i·d_i), λ=[1,½,⅓,¼${SUBWAY_ENABLED? ",⅕]" : "]"}</span>
        </div>`);

      // --- POI aggregation (Delta-g marginal saturation): collect one summary row per poi_type ---
      const agg = pt.aggregation;
      if(agg){
        const match2 = pt.poi_type_acc_matrix != null
          ? (Math.abs(pt.poi_type_acc_computed - pt.poi_type_acc_matrix) < 0.01
             ? `<span class="match-ok">✓</span>`
             : `<span class="match-warn">⚠ matrix=${pt.poi_type_acc_matrix.toFixed(3)}</span>`)
          : `<span class="muted">N/A</span>`;
        const top = agg.steps.slice(0, 10);
        aggMaxRanks = Math.max(aggMaxRanks, top.length);
        const rankCells = top.map(st => `<td>${st.value.toFixed(3)}<div class="muted" style="font-size:10px">+${st.term.toFixed(3)}</div></td>`);
        aggSummaryRows.push({
          label: `<td>${pt.poi_type}<div class="muted" style="font-size:10px">${agg.n_pois} POIs, cc=${agg.contribution_coefficient}</div></td>`,
          cells: rankCells,
          output: `<td><b>${pt.poi_type_acc_computed.toFixed(4)}</b> ${match2}</td>`,
        });
      }
    });
    if(aggSummaryRows.length){
      p2.body.insertAdjacentHTML("beforeend",`
        <div class="section-label" style="margin-top:6px">POI aggregation (Δg) — per poi_type</div>
        <div class="muted" style="margin:0 0 4px">
          Sort per-POI RRAs descending, weight each by marginal increment Δg: <span class="formula">g(x)=1−exp(c·x)</span>.
          Each cell = RRA at that rank, with its term contribution (+RRA·Δg) below it.
        </div>`);
      const rankHeaders = Array.from({length: aggMaxRanks}, (_, i) => `<th>#${i+1}</th>`).join("");
      const bodyRows = aggSummaryRows.map(r => {
        const padded = r.cells.concat(Array.from({length: aggMaxRanks - r.cells.length}, () => `<td class="muted">—</td>`));
        return `<tr>${r.label}${padded.join("")}${r.output}</tr>`;
      });
      p2.body.insertAdjacentHTML("beforeend",
        `<table><thead><tr><th>POI type</th>${rankHeaders}<th>Output (Σ terms)</th></tr></thead><tbody>${bodyRows.join("")}</tbody></table>`);
    }
  }
  el.appendChild(p2.panel);

  // Step 3
  const p3 = makePanel("3","Service Scores","Choquet integral over POI-type accessibility values");
  if(chain.step3){
    chain.step3.services.forEach(s => {
      const sc = s.choquet;
      const svcScore = s.score_from_matrix ?? sc.normalized;
      p3.body.insertAdjacentHTML("beforeend",`
        <div class="section-label">${s.service}
          <span style="margin-left:8px">Score: <b>${svcScore.toFixed(4)}</b>
          <div class="acc-bar-wrap"><div class="acc-bar" style="width:${(svcScore*100).toFixed(0)}%"></div></div>
          </span>
        </div>
        <div class="muted">μ(full set)=${sc.mu_full.toFixed(3)} | Σ(raw)=${sc.total.toFixed(4)} | normalised=${sc.normalized.toFixed(4)}</div>
      `);
      const inputRows = sc.poi_types.map((pt,i) => `
        <tr><td>${pt}</td>
          <td>${(sc.input[i] ?? 0).toFixed(4)}<div class="acc-bar-wrap"><div class="acc-bar" style="width:${((sc.input[i]??0)*100).toFixed(0)}%"></div></div></td>
        </tr>`).join("");
      p3.body.insertAdjacentHTML("beforeend",`<table><thead><tr><th>POI type</th><th>Accessibility input</th></tr></thead><tbody>${inputRows}</tbody></table>`);

      const stepRows = sc.steps.map(step => `
        <div class="choquet-step">
          <span style="width:22px;color:#6b7280">${step.j}</span>
          <span style="width:120px"><b>${sc.poi_types[step.index]}</b></span>
          <span style="width:60px">val=${step.value.toFixed(3)}</span>
          <span style="width:50px">Δ=${step.delta.toFixed(3)}</span>
          <span style="width:60px">μ(tail)=${step.capacity.toFixed(3)}</span>
          <span style="width:60px">term=${step.term.toFixed(4)}</span>
          <span style="color:#6b7280">tail=[${step.tail.join(",")}]</span>
        </div>`).join("");
      p3.body.insertAdjacentHTML("beforeend",`<div style="margin:8px 0">${stepRows}</div>`);
    });
  }
  el.appendChild(p3.panel);

  // Step 4
  const p4 = makePanel("4","Capability Scores","ELECTRE TRI — pessimistic (descending) rule: assign to the highest category whose lower profile is outranked. Profiles are listed low→high for readability; the rule itself is descending.");
  if(chain.step4){
    chain.step4.capabilities.forEach(c => {
      const e = c.electre;
      const chipCls = catClass[e.assigned_category] || "";
      const n = (e.services||[]).length;

      // --- Capability header ---
      p4.body.insertAdjacentHTML("beforeend",`
        <div class="section-label">${c.capability}
          <span style="margin-left:8px"><span class="score-chip ${chipCls}">${e.assigned_category} (score = ${e.score?.toFixed(2)})</span></span>
        </div>`);

      // --- Service score inputs ---
      const svcRows = (e.services||[]).map(svc => {
        const sc = e.service_scores?.[svc] ?? 0;
        return `<tr><td>${svc}</td>
          <td>${sc.toFixed(4)}<div class="acc-bar-wrap"><div class="acc-bar" style="width:${(sc*100).toFixed(0)}%"></div></div></td>
        </tr>`;
      }).join("");
      p4.body.insertAdjacentHTML("beforeend",`
        <table><thead><tr><th>Service input</th><th>Score</th></tr></thead><tbody>${svcRows}</tbody></table>`);

      // --- Thresholds block ---
      if(e.mode === "constant_scores"){
        p4.body.insertAdjacentHTML("beforeend",`<div class="muted" style="margin:4px 0 10px">${e.rule}</div>`);
        return;
      }
      const vStr = e.veto_threshold == null ? "∞" : e.veto_threshold.toFixed(3);
      p4.body.insertAdjacentHTML("beforeend",`
        <div class="muted" style="margin:5px 0 2px">
          std(x) = <b>${e.std?.toFixed(4)}</b>
          &nbsp;|&nbsp; q (indifference) = <b>${e.q?.toFixed(4)}</b>
          &nbsp;|&nbsp; p (preference) = <b>${e.p?.toFixed(4)}</b>
          &nbsp;|&nbsp; v = <b>${vStr}</b>
          &nbsp;|&nbsp; λ-cut = <b>${e.lambda_cut}</b>
        </div>
        <div class="formula" style="margin:0 0 10px">
          c_j = 1 if x_j−b ≥ −q &nbsp;|&nbsp; (x_j−b+p)/(p−q) if −p &lt; x_j−b &lt; −q &nbsp;|&nbsp; 0 if x_j−b ≤ −p
        </div>`);

      // --- Per-boundary blocks ---
      // Render high→low to follow the pessimistic/descending procedure: check the
      // top profile first and walk down. (The stored array is low→high.)
      (e.boundaries||[]).slice().reverse().forEach(b => {
        const bv = b.boundary_value;
        const terms = b.concordance_terms || [];
        const C = b.global_concordance;
        const cred = b.credibility;
        const outranks = b.outranks_boundary;

        // ① Concordance table
        const concRows = terms.map(t => {
          const d = t.difference_vs_boundary ?? 0;
          const dStr = (d>=0?"+":"")+d.toFixed(3);
          const ruleCls = t.rule==="full"?"rule-full":t.rule==="none"?"rule-none":"rule-partial";
          return `<tr>
            <td>${t.service}</td>
            <td>${(t.score??0).toFixed(3)}</td>
            <td>${bv.toFixed(3)}</td>
            <td class="${d>=0?"d-pos":"d-neg"}">${dStr}</td>
            <td><b>${(t.partial_concordance??0).toFixed(3)}</b></td>
            <td class="${ruleCls}">${t.rule}</td>
          </tr>`;
        }).join("");

        // ② Global concordance formula
        const cTerms = terms.map(t=>(t.partial_concordance??0).toFixed(3)).join(" + ");
        const cFormula = `(${cTerms}) / ${n} = <b>${C?.toFixed(3)}</b>`;

        // ③ Veto / credibility
        let vetoHtml;
        if(e.veto_threshold == null){
          vetoHtml = `v = ∞ → no veto check &nbsp;→ σ = C = <b>${cred?.toFixed(3)}</b>`;
        } else if(!b.veto_triggered){
          vetoHtml = `v = ${vStr}, no service exceeded veto &nbsp;→ σ = C = <b>${cred?.toFixed(3)}</b>`;
        } else {
          const vetoSvc = (b.discordance_terms||[]).filter(d=>d.rule==="full_veto").map(d=>d.service).join(", ");
          vetoHtml = `<span style="color:#dc2626">⛔ veto triggered by [${vetoSvc}] (gap > v=${vStr}) → σ = <b>0.000</b></span>`;
        }

        // ④ Decision
        const decisionCls = outranks ? "outranks-yes" : "outranks-no";
        const decisionTxt = outranks
          ? `σ = ${cred?.toFixed(3)} ≥ ${e.lambda_cut} → <span class="outranks-yes">✓ OUTRANKS b = ${bv}</span>`
          : `σ = ${cred?.toFixed(3)} < ${e.lambda_cut} → <span class="outranks-no">✗ does not outrank b = ${bv}</span>`;

        p4.body.insertAdjacentHTML("beforeend",`
          <div class="boundary-block">
            <div class="boundary-header">Boundary b = ${bv}</div>
            <div class="b-step">
              <span class="b-num">①</span>
              <span class="b-calc">Partial concordance c_j per service</span>
            </div>
            <div class="b-table">
              <table><thead><tr><th>Service</th><th>x_j</th><th>b</th><th>d = x−b</th><th>c_j</th><th>Rule</th></tr></thead>
              <tbody>${concRows}</tbody></table>
            </div>
            <div class="b-step">
              <span class="b-num">②</span>
              <span class="b-calc">C = ${cFormula}</span>
            </div>
            <div class="b-step">
              <span class="b-num">③</span>
              <span class="b-calc">${vetoHtml}</span>
            </div>
            <div class="b-decision ${decisionCls}">④ ${decisionTxt}</div>
          </div>`);
      });

      // --- Final assignment summary ---
      const ob = (e.boundaries||[]).filter(b=>b.outranks_boundary).map(b=>b.boundary_value);
      const nb2 = (e.boundaries||[]).filter(b=>!b.outranks_boundary).map(b=>b.boundary_value);
      const summaryParts = ob.length
        ? `Outranks [${ob.join(", ")}] — stopped at [${nb2.join(", ")}]. Pessimistic/descending: take the highest outranked profile (${Math.max(...ob)}) → category just above it`
        : `Does not outrank any boundary → lowest category`;
      p4.body.insertAdjacentHTML("beforeend",`
        <div class="elec-summary">
          <b>Assignment:</b> ${summaryParts}
          &nbsp;→&nbsp; category = <span class="score-chip ${chipCls}">${e.assigned_category}</span>
          &nbsp;(score = ${e.score?.toFixed(2)})
        </div>`);
    });
  }
  el.appendChild(p4.panel);

  // Step 5 — Per-POI export verification
  const p5 = makePanel("5","Per-POI Export Check","Recomputed per-POI accessibility & powers vs the pipeline's cached exported accessibility (accessibility/poi_by_node cache; POI set from hex_pois + pois_used.gpkg). Confirms accessibility-to-POI is computed and exported consistently.");
  const vr = chain.verify;
  if(!vr || !vr.available){
    p5.body.insertAdjacentHTML("beforeend",
      `<div class="muted">${vr && vr.reason ? vr.reason : "No export available for this hexagon."}</div>`);
  } else {
    const summaryCls = vr.n_mismatch === 0 ? "match-ok" : "match-warn";
    const summaryTxt = vr.n_mismatch === 0
      ? `✓ all ${vr.n_pois} exported POIs match the recomputation`
      : `⚠ ${vr.n_mismatch} of ${vr.n_pois} POIs differ`;
    const staleHint = vr.n_mismatch > 0
      ? `<div class="muted" style="margin-top:4px">Widespread differences usually mean the stored export/matrix are <b>stale</b> — generated by an earlier version of the accessibility/decay code. Re-run the accessibility + POI-export stages to refresh, then this should turn green.</div>`
      : "";
    p5.body.insertAdjacentHTML("beforeend",
      `<div class="elec-summary"><b class="${summaryCls}">${summaryTxt}</b>
       <div class="muted" style="margin-top:4px">exported acc is read from the pipeline's per-node accessibility cache (accessibility/poi_by_node/{node}.npz) — the exact value score_report used; powers on both sides are recomputed from it via the exact pipeline routine (poi_exports._poi_powers). The stored sp/cp in hex_pois are quantile-rescaled for display (see the power-scaling dashboard) and are not what's compared here.</div>${staleHint}
       <div class="muted" style="margin-top:6px">
         <b>How the powers are calculated</b> (per POI, per hexagon):<br>
         <span class="formula">service_power[svc] = Σ_poi_type acc(POI) × SERVICE_SINGLETON_M[svc][poi_type]</span><br>
         — summed over every poi_type this POI belongs to that contributes to that service (a poi_type owned by a
         different POI for dedup purposes is skipped).<br>
         <span class="formula">capability_power[cap] = Σ_svc service_power[svc] × CAP_ELECTRE_W[cap][svc]</span><br>
         — each capability's power is its member services' powers, weighted by that capability's ELECTRE weights.
         A POI can contribute to several services/capabilities at once; each cell above shows recomputed/exported.
       </div></div>`);
    // Each POI's service_power / capability_power is a dict keyed by service/capability —
    // there's no single combined "power" per POI, so sorting offers one option per
    // service/capability actually present among these POIs, not a summed total.
    const svcKeys = Array.from(new Set(vr.rows.flatMap(r => Object.keys(r.sp_exported)))).sort();
    const capKeys = Array.from(new Set(vr.rows.flatMap(r => Object.keys(r.cp_exported)))).sort();
    const sortWrap = document.createElement("div");
    sortWrap.style.margin = "6px 0";
    sortWrap.style.fontSize = "12px";
    const svcOpts = svcKeys.map(s => `<option value="svc:${s}">${s} ↓</option>`).join("");
    const capOpts = capKeys.map(c => `<option value="cap:${c}">${c} ↓</option>`).join("");
    sortWrap.innerHTML = `<label class="muted">Sort by:
      <select>
        <option value="default">Mismatches first (default)</option>
        <option value="acc_desc">Accessibility ↓</option>
        <optgroup label="Service power">${svcOpts}</optgroup>
        <optgroup label="Capability power">${capOpts}</optgroup>
      </select></label>`;
    p5.body.appendChild(sortWrap);
    const tableContainer = document.createElement("div");
    p5.body.appendChild(tableContainer);

    const rowHtml = (r) => {
      const accCls = r.acc_match ? "match-ok" : "match-warn";
      const rowCls = r.match ? "" : "match-warn";
      const spStr = Object.keys(r.sp_exported).length
        ? Object.keys(r.sp_exported).map(s => `${s}: ${(r.sp_recomputed[s]??0).toFixed(3)}/${(r.sp_exported[s]??0).toFixed(3)}`).join("<br>")
        : "—";
      const cpStr = Object.keys(r.cp_exported).length
        ? Object.keys(r.cp_exported).map(c => `${c}: ${(r.cp_recomputed[c]??0).toFixed(3)}/${(r.cp_exported[c]??0).toFixed(3)}`).join("<br>")
        : "—";
      return `<tr class="${rowCls}">
        <td>${r.name || r.source_key || "—"}<div class="muted">${(r.poi_types||[]).join(", ")}</div></td>
        <td>${r.recomputed_acc==null?'<span class="null">—</span>':r.recomputed_acc.toFixed(4)}</td>
        <td>${r.exported_acc==null?'<span class="null">—</span>':r.exported_acc.toFixed(4)}</td>
        <td class="${accCls}">${r.acc_match?"✓":"⚠"}</td>
        <td style="font-size:11px">${spStr}</td>
        <td style="font-size:11px">${cpStr}</td>
      </tr>`;
    };
    const sortRows = (rows, key) => {
      const out = rows.slice();
      if(key === "acc_desc") out.sort((a,b) => (b.exported_acc??-Infinity) - (a.exported_acc??-Infinity));
      else if(key.startsWith("svc:")) { const s = key.slice(4); out.sort((a,b) => (b.sp_exported[s]??0) - (a.sp_exported[s]??0)); }
      else if(key.startsWith("cap:")) { const c = key.slice(4); out.sort((a,b) => (b.cp_exported[c]??0) - (a.cp_exported[c]??0)); }
      else out.sort((a,b) => (Number(a.match) - Number(b.match)) || ((b.recomputed_acc??0) - (a.recomputed_acc??0)));
      return out;
    };
    const renderTable = (key) => {
      const vrows = sortRows(vr.rows, key).map(rowHtml);
      tableContainer.innerHTML = makeToggleTable(
        `<thead><tr><th>POI (source_key / types)</th><th>acc (recomputed)</th><th>acc (exported)</th><th>✓</th><th>service_power<br><span class="muted">recomp/export</span></th><th>capability_power<br><span class="muted">recomp/export</span></th></tr></thead>`,
        vrows, 12);
    };
    renderTable("default");
    sortWrap.querySelector("select").addEventListener("change", (ev) => renderTable(ev.target.value));
    if(vr.truncated)
      p5.body.insertAdjacentHTML("beforeend",
        `<div class="muted" style="margin-top:4px">Showing ${vr.rows.length} of ${vr.n_pois} POIs.</div>`);
  }
  el.appendChild(p5.panel);
}

let _tgSeq = 0;
function makeToggleTable(headerHtml, allRows, limit=10){
  const shown = allRows.slice(0, limit);
  const hidden = allRows.slice(limit);
  let out = `<table>${headerHtml}<tbody>${shown.join("")}</tbody>`;
  if(hidden.length){
    const id = "tg_"+(++_tgSeq);
    out += `<tbody id="${id}" style="display:none">${hidden.join("")}</tbody>`;
    out += `</table><button class="toggle-btn" onclick="(function(btn){const b=document.getElementById('${id}');const show=b.style.display==='none';b.style.display=show?'':'none';btn.textContent=show?'Collapse ↑':'Show ${hidden.length} more ↓'})(this)">Show ${hidden.length} more ↓</button>`;
  } else {
    out += `</table>`;
  }
  return out;
}
function gMapsLink(fromLat, fromLon, toLat, toLon){
  if(fromLat==null||toLat==null) return "";
  return ` <a href="https://www.google.com/maps/dir/${fromLat},${fromLon}/${toLat},${toLon}" target="_blank" class="maps-link" title="Verify in Google Maps">🗺</a>`;
}

function makePanel(num, title, subtitle){
  const panel = document.createElement("div");
  panel.className = "panel";
  const header = document.createElement("div");
  header.className = "panel-header";
  header.innerHTML = `<span><span class="step-badge">${num}</span><span class="step-title">${title}</span><br><span class="muted">${subtitle}</span></span><span class="arrow">▶</span>`;
  const body = document.createElement("div");
  body.className = "panel-body open";
  header.onclick = () => { body.classList.toggle("open"); header.querySelector(".arrow").classList.toggle("open"); };
  header.querySelector(".arrow").classList.add("open");
  panel.appendChild(header);
  panel.appendChild(body);
  return {panel, body};
}

function fmt(v){ return v == null ? '<span class="null">—</span>' : v.toFixed(1)+" min"; }
function fmtD(v){ return v == null ? '<span class="null">—</span>' : v.toFixed(3); }
function fmtLatLon(lat, lon){ return lat == null ? "—" : lat.toFixed(4)+", "+lon.toFixed(4); }
function poiLabel(p){
  if(p.name) return p.name;
  if(p.source_key) return p.source_key.slice(0,24);
  return fmtLatLon(p.src_lat, p.src_lon);
}
function timeClass(v){
  if(v == null) return "null";
  if(v < 5) return "fast";
  if(v < 15) return "medium";
  if(v < 30) return "slow";
  return "veryslow";
}
</script>
</body>
</html>
"""


def _build_html(chain_data: dict, slug: str, subway_enabled: bool) -> str:
    payload = json.dumps(chain_data, ensure_ascii=False, separators=(",", ":"))
    return _HTML_TEMPLATE.replace("__SUBWAY_ENABLED__", json.dumps(subway_enabled)).replace("__CHAIN_DATA__", payload)

# ---------------------------------------------------------------------------
# This function is called by main.py to generate the debug pipeline 
# immediately after the run is done
# ---------------------------------------------------------------------------

def run_debug_pipeline(study_city = STUDY_CITY, output = None) -> None:

    # Build the config straight from the chosen city so __post_init__ derives the city_slug,
    # artifact_slug and every *_path consistently. (Constructing a default config and then
    # calling apply_study_city() afterwards does NOT re-derive those paths, which is why the
    # report used to load the wrong city's artifacts.)
    # Several lower-level functions (utils/graphml.py, utils/poi_dedup.py,
    # utils/load_shapefile.py) construct their own bare PipelineConfig() instead of using
    # the cfg built here, which falls back to CAP_STUDY_CITY from the environment. If a
    # previous run (e.g. main.py for Paris) already set that env var in this shell, it
    # persists and those functions silently resolve the *other* city even though this
    # script's own cfg correctly says `study_city`, causing a Frankenstein mix of this
    # city's paths/slug with the other city's data. Set it explicitly here too (mirroring
    # main.py) so every bare PipelineConfig() reconstruction agrees with this one.

    os.environ["CAP_STUDY_CITY"] = study_city
    cfg = PipelineConfig(study_city=study_city)

    slug = cfg.artifact_slug
    print(f"[Debug] study_city={cfg.study_city} city={cfg.city_name} slug={slug}")

    export_dir, gpkg_path, hex_source = _default_paths(None)
    grid_params = _load_grid_params(export_dir)
    if grid_params is None:
        raise RuntimeError("grid_params.json not found — run the pipeline first.")

    # Find the spatial export gpkg that has capability_points.
    from tools.inspect_hex_pois import _find_spatial_export_gpkg
    spatial_gpkg = _find_spatial_export_gpkg(export_dir)
    capability_points = _fetch_capability_points(spatial_gpkg) if spatial_gpkg else []
    if not capability_points:
        raise RuntimeError("No capability points found — run the pipeline and generate spatial outputs first.")

    print(f"[Debug] Found {len(capability_points)} capability points.")

    # Limit hexagon selection to nodes that actually have non-bus cache data.
    cached_nodes: set[str] = set()
    if os.path.isdir(cfg.non_bus_cache_dir):
        cached_nodes = {f[:-4] for f in os.listdir(cfg.non_bus_cache_dir) if f.endswith(".pkl")}
    if cached_nodes:
        capability_points = [p for p in capability_points if str(p["node_id"]) in cached_nodes]
    print(f"[Debug] Capability points with non-bus cache: {len(capability_points)}")

    selected = _select_hexagons(grid_params, hex_source, capability_points)
    print(f"[Debug] Selected {len(selected)} hexagons: {[h['hex_id'] for h in selected]}")

    bus_ctx = _load_pt_context(cfg, "bus")
    subway_ctx = _load_pt_context(cfg, "metro") if cfg.enable_subway else None
    acc_ctx = _load_accessibility_context(cfg)
    svc_ctx = _load_service_context(cfg)
    print(f"[Debug] bus={'ok' if bus_ctx else 'missing'} acc={'ok' if acc_ctx else 'missing'} subway={'ok' if subway_ctx else 'missing'} svc={'ok' if svc_ctx else 'missing'}")
    if cfg.enable_subway and subway_ctx is None:
        raise RuntimeError(f"Subway is none even if enabled, please check the subway artifacts in artifacts/{cfg.artifact_slug}/subway and the paths in" \
        f" {cfg.public_transport_paths("metro")}")
        
    # Same per-service ownership drop map the accessibility + export stages apply, so the
    # debug recomputation/verification matches the pipeline's deduplicated values.
    from utils import poi_dedup
    poi_drop_map = poi_dedup.load_drop_map(cfg.poi_ownership_drop_path)
    print(f"[Debug] poi_drop_map: {len(poi_drop_map)} poi_types with drops"
          + ("" if poi_drop_map else " (none — file absent or dedup off)"))

    poi_names = _load_poi_names_by_source_key(cfg)
    print(f"[Debug] poi_names: {len(poi_names)} named POIs")

    chain_data: dict[str, Any] = {}
    for hex_entry in selected:
        hid = hex_entry["hex_id"]
        print(f"[Debug] Building chain for {hid} ({hex_entry['group']}) node={hex_entry['node_id']}")
        chain_data[hid] = _build_chain(hex_entry, cfg, bus_ctx, acc_ctx, svc_ctx, subway_ctx, hex_source, gpkg_path, poi_drop_map, poi_names)

    html = _build_html(chain_data, slug, cfg.enable_subway)

    out_path = os.path.join("outputs", f"debug/{slug}/debug_pipeline.html") if output is None else output 
    # I haven't used " output or ..." because output = "" would count as False
    
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(html, encoding="utf-8")
    print(f"[Debug] Written: {out_path}")


if __name__ == "__main__":
    run_debug_pipeline()
