import json
import math
import multiprocessing as mp
import os

from config import PipelineConfig
from pipeline_types import PipelineContext
from utils import capabilities as cap
from utils import graphml

GRID_PARAMS_PATH = os.path.join("outputs", "grid_params.json")


def _hex_grid_sample_nodes(nodes_with_coords, cell_size_m: float):
    """Return one representative node per hex cell (nearest to centroid).

    Inputs:
    - nodes_with_coords: list of (node_id, data_dict) with 'x' (lon) and 'y' (lat).
    - cell_size_m: inscribed-circle radius of each hex cell in metres.

    Outputs:
    - Filtered list of (node_id, data_dict) with one entry per occupied hex cell.
    """
    import numpy as np
    from scipy.spatial import cKDTree

    if not nodes_with_coords:
        return nodes_with_coords

    lats = np.array([d["y"] for _, d in nodes_with_coords], dtype=float)
    lons = np.array([d["x"] for _, d in nodes_with_coords], dtype=float)

    mean_lat_rad = math.radians(float(np.mean(lats)))
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(mean_lat_rad)

    xs = lons * m_per_deg_lon
    ys = lats * m_per_deg_lat

    hex_radius_m = cell_size_m / math.cos(math.pi / 6.0)
    x_step = 1.5 * hex_radius_m
    y_step = math.sqrt(3.0) * hex_radius_m

    min_x, max_x = float(xs.min()), float(xs.max())
    min_y, max_y = float(ys.min()), float(ys.max())

    node_xy = np.column_stack([xs, ys])
    tree = cKDTree(node_xy)

    # Store (cx, cy, col_idx, row_idx) so each selected node can be given the
    # hex_id of the centroid that claimed it.
    centroid_items: list[tuple[float, float, int, int]] = []
    col_idx = 0
    cx = min_x - hex_radius_m
    while cx <= max_x + hex_radius_m:
        y_offset = 0.0 if col_idx % 2 == 0 else y_step / 2.0
        cy = min_y - y_step + y_offset
        row_idx = 0
        while cy <= max_y + y_step:
            centroid_items.append((cx, cy, col_idx, row_idx))
            cy += y_step
            row_idx += 1
        cx += x_step
        col_idx += 1

    if not centroid_items:
        return nodes_with_coords

    centroid_xy = np.array([(cx, cy) for cx, cy, _, _ in centroid_items], dtype=float)
    dists, indices = tree.query(centroid_xy)

    # First centroid to claim a node wins; build node_idx → (col, row) mapping.
    node_to_hex: dict[int, tuple[int, int]] = {}
    for (_, _, c_i, r_i), dist, idx in zip(centroid_items, dists, indices):
        if dist <= hex_radius_m:
            node_idx = int(idx)
            if node_idx not in node_to_hex:
                node_to_hex[node_idx] = (c_i, r_i)

    # Copy node data dicts so we don't mutate the underlying graph; inject hex_id.
    result = []
    for node_idx in sorted(node_to_hex.keys()):
        node_id, data = nodes_with_coords[node_idx]
        c_i, r_i = node_to_hex[node_idx]
        new_data = dict(data)
        new_data["hex_id"] = f"H{c_i:04d}_{r_i:04d}"
        result.append((node_id, new_data))
    print(
        f"[Context] Hex-grid sampling: cell_size={cell_size_m:.0f} m  "
        f"hexagons={len(centroid_items)}  selected={len(result)}  (from {len(nodes_with_coords)} nodes)",
        flush=True,
    )

    # Persist the exact grid parameters so the display grid can reconstruct the
    # identical tiling without needing the full node set.
    os.makedirs("outputs", exist_ok=True)
    with open(GRID_PARAMS_PATH, "w", encoding="utf-8") as _f:
        json.dump(
            {
                "min_x": min_x, "max_x": max_x,
                "min_y": min_y, "max_y": max_y,
                "m_per_deg_lon": m_per_deg_lon,
                "cell_size_m": cell_size_m,
            },
            _f,
        )

    return result


def _filter_nodes_to_boundary(nodes_with_coords, city_name: str):
    """Keep only nodes that fall within the administrative boundary of city_name."""
    import osmnx as ox
    import numpy as np
    import shapely

    try:
        place_gdf = ox.geocode_to_gdf(city_name)
        if place_gdf.empty:
            return nodes_with_coords
        boundary = place_gdf.geometry.iloc[0]
    except Exception as exc:
        print(f"[Context] Could not fetch city boundary for node filtering: {exc}", flush=True)
        return nodes_with_coords

    if not nodes_with_coords:
        return nodes_with_coords

    # Guard against an invalid Nominatim polygon (self-intersections etc.) that could
    # crash GEOS during containment tests.
    boundary = shapely.make_valid(boundary)

    # Single vectorized GEOS call instead of one Point(...).within(...) per node. This
    # avoids hundreds of thousands of Python->GEOS crossings (each releasing/re-acquiring
    # the GIL), which is both far faster and far less prone to native heap faults.
    xs = np.fromiter((data["x"] for _, data in nodes_with_coords), dtype=float, count=len(nodes_with_coords))
    ys = np.fromiter((data["y"] for _, data in nodes_with_coords), dtype=float, count=len(nodes_with_coords))
    mask = shapely.contains_xy(boundary, xs, ys)

    filtered = [nc for nc, keep in zip(nodes_with_coords, mask) if keep]
    print(
        f"[Context] Boundary filter: {len(filtered)}/{len(nodes_with_coords)} nodes within '{city_name}'",
        flush=True,
    )
    return filtered


def build_context(config: PipelineConfig) -> PipelineContext:
    """Build shared runtime context from configuration.

    Inputs:
    - config: pipeline configuration with paths, debug options, and worker settings.

    Outputs:
    - PipelineContext: graph, node list, output paths, worker count, and service groupings.
    """
    graph = graphml.get_mode_graph("walk", config)
    nodes = list(graph.nodes(data=True))
    nodes_with_coords = [item for item in nodes if "y" in item[1] and "x" in item[1]]

    if config.debug_max_nodes is not None:
        import random
        rng = random.Random(config.seed)
        nodes_with_coords = rng.sample(
            nodes_with_coords,
            min(config.debug_max_nodes, len(nodes_with_coords)),
        )

    from utils import services as serv
    _boundary_filtered = False
    if serv.get_global_radius_m(config) is not None and not config.use_shapefile:
        nodes_with_coords = _filter_nodes_to_boundary(nodes_with_coords, config.city_name)
        _boundary_filtered = True

    if config.origin_hex_enabled:
        # Hex-grid sampling must always work from study-area nodes so the saved
        # bbox covers the case study, not the surrounding routing buffer.
        if not _boundary_filtered:
            nodes_with_coords = _filter_nodes_to_boundary(nodes_with_coords, config.city_name)
        nodes_with_coords = _hex_grid_sample_nodes(nodes_with_coords, config.qgis_grid_cell_size_m)

    os.makedirs("outputs", exist_ok=True)
    os.makedirs(config.non_bus_cache_dir, exist_ok=True)
    os.makedirs(config.poi_snap_cache_dir, exist_ok=True)
    os.makedirs(os.path.dirname(config.bus_impedance_matrix_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(config.accessibility_matrix_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(config.service_matrix_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(config.impedance_artifact_path) or ".", exist_ok=True)

    output_paths = {
        "capabilities": os.path.join("outputs", "capability.csv"),
    }

    if config.worker_count is None:
        cpu = mp.cpu_count()
        # Per-origin cache growth is now bounded (delta_g.reset_origin_caches), so the
        # dominant per-worker cost is the resident mode graphs (~mem_per_worker_gb each,
        # since every spawned worker loads its own copy). Derive the worker count from
        # available RAM rather than a fixed cap so we don't re-saturate memory: more
        # cores are only useful while the graph copies still fit.
        import psutil

        total_gb = psutil.virtual_memory().total / (1024 ** 3)
        mem_per_worker_gb = max(0.5, float(getattr(config, "mem_per_worker_gb", 3.0)))
        reserve_gb = float(getattr(config, "worker_mem_reserve_gb", 10.0))
        mem_workers = max(1, int((total_gb - reserve_gb) / mem_per_worker_gb))
        workers = max(1, min(cpu - 2, mem_workers))
        print(
            f"[Context] workers={workers} "
            f"(cpu={cpu}, total_ram={total_gb:.0f}GB, reserve={reserve_gb:.0f}GB, "
            f"~{mem_per_worker_gb:.1f}GB/worker -> mem_cap={mem_workers})",
            flush=True,
        )
    else:
        workers = max(1, int(config.worker_count))

    return PipelineContext(
        config=config,
        graph=graph,
        nodes_with_coords=nodes_with_coords,
        workers=workers,
        output_paths=output_paths,
        rest_services=cap.CAPABILITY_SERVICES["restorativeness"],
        nut_services=cap.CAPABILITY_SERVICES["nutrition"],
        care_services=cap.CAPABILITY_SERVICES["care"],
    )
