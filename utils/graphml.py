import osmnx as ox
import os
import math
import warnings
import geopandas as gpd
import pandas as pd
import hashlib
import json
from shapely.geometry import shape
from typing import TypeAlias

from utils.load_shapefile import poi_from_shp

from config import PipelineConfig
from utils.load_shapefile import graph_from_shapefile, feature_from_shapefile
from utils.poi_identity import build_poi_source_key

TagValue: TypeAlias = bool | str | list[str]
TagClause: TypeAlias = dict[str, TagValue]
TagQuery: TypeAlias = TagClause | list[TagClause]

# In-memory caches to avoid repeated disk loads
_GRAPH_CACHE = None
_MODE_GRAPH_CACHE = {}
_CITY_POI_UNIVERSE_CACHE: dict[str, gpd.GeoDataFrame] = {}
_POI_DOWNLOAD_LOGGED: set[tuple[str, str]] = set()


def _get_city_settings() -> tuple[str, str]:
    cfg = PipelineConfig()
    return cfg.city_name, cfg.city_slug


def _get_runtime_settings() -> tuple[str, str, str]:
    cfg = PipelineConfig()
    return cfg.city_name, cfg.city_slug, cfg.artifact_slug


def get_graph():
    """Load or download the base graph and cache it in memory.

    Inputs:
    - none.

    Outputs:
    - graph object representing the default study-area network.
    """

    # Nomi file
    place_name, city_slug = _get_city_settings()
    name_file = f"graph/{city_slug}.graphml"

    # Se esiste lo carico altrimenti creo e salvo

    global _GRAPH_CACHE
    if _GRAPH_CACHE is not None:
        return _GRAPH_CACHE

    graph = None
    try:
        graph = ox.io.load_graphml(name_file)
    except Exception:
        print(f"[Graph] Cached graph not found. Downloading graph for {place_name}...", flush=True)
        graph = ox.graph_from_place(place_name)
        ox.io.save_graphml(graph, filepath=name_file)
        print(f"[Graph] Graph downloaded and saved: {name_file}", flush=True)

    _GRAPH_CACHE = graph
    return graph


def _resolve_mode_graph_path(network_type, cfg: PipelineConfig) -> str:
    """Return the on-disk path of the full (unsimplified) mode graph cache file."""
    if cfg.use_shapefile:
        return f"graph/{cfg.artifact_slug}_{network_type}.graphml"

    city_slug = cfg.city_slug
    from utils.services import get_global_radius_m
    buffer_m = get_global_radius_m(cfg) or 0.0
    simplify_tag = "s" if cfg.osm_autobuild_simplify else "ns"
    retain_tag = "ra" if cfg.osm_autobuild_retain_all else ""
    graph_tag = f"_{simplify_tag}{retain_tag}" if retain_tag else f"_{simplify_tag}"
    if buffer_m > 0:
        return f"graph/{city_slug}_{network_type}_buf{buffer_m:.0f}m{graph_tag}.graphml"
    return f"graph/{city_slug}_{network_type}{graph_tag}.graphml"


def _get_simplified_mode_graph(network_type, cfg: PipelineConfig):
    """Load (or build once) a topology-simplified routing graph for a mode.

    Routing only needs correct network distances, which `ox.simplify_graph`
    preserves (it sums `length` over merged degree-2 chains). Collapsing those
    interstitial nodes cuts node count ~5x, so workers loading this instead of the
    full graph use a fraction of the RAM. Origins and POIs are still enumerated and
    snapped on the full graph upstream; their coordinates re-snap onto these nodes.

    Built once and cached to `<full>_simplified.graphml`; subsequent loads (e.g. in
    every worker) read that file directly without ever materializing the full graph.
    """
    cache_key = (network_type, "simplified")
    cached = _MODE_GRAPH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    full_path = _resolve_mode_graph_path(network_type, cfg)
    simp_path = full_path[: -len(".graphml")] + "_simplified.graphml"

    simp_stale = (
        os.path.isfile(simp_path)
        and os.path.isfile(full_path)
        and os.path.getmtime(full_path) > os.path.getmtime(simp_path)
    )
    if simp_stale:
        print(
            f"[Graph] Source graph is newer than simplified cache; rebuilding for '{network_type}'",
            flush=True,
        )
    if os.path.isfile(simp_path) and not simp_stale:
        graph = ox.io.load_graphml(simp_path)
    else:
        # Build from the full graph (loaded only here, normally in the parent).
        full_graph = get_mode_graph(network_type, cfg)
        if full_graph.graph.get("simplified"):
            graph = full_graph
        else:
            graph = ox.simplify_graph(full_graph.copy())
        tmp_path = simp_path + ".tmp"
        ox.io.save_graphml(graph, filepath=tmp_path)
        os.replace(tmp_path, simp_path)
        print(
            f"[Graph] Built simplified routing graph for '{network_type}': "
            f"{simp_path} nodes={graph.number_of_nodes()} edges={graph.number_of_edges()}",
            flush=True,
        )

    _MODE_GRAPH_CACHE[cache_key] = graph
    return graph


# In-memory cache of compact CSR routing bundles, one per mode.
_MODE_CSR_CACHE: dict[str, object] = {}


def get_mode_csr(network_type, cfg: PipelineConfig | None = None):
    """Compact CSR routing bundle for a mode's FULL (unsimplified) graph.

    Routing on the full graph is exact; storing it as a scipy CSR adjacency
    (indptr/indices/length arrays, ~10-15 MB) instead of a NetworkX object (>1 GB)
    lets every worker hold the whole network for a fraction of the RAM. Built once
    per mode in the parent and cached to `<full>_csr.npz`; workers load that file.

    Returns a dict with:
    - `mat`: scipy.sparse.csr_matrix over the full routing topology, with
      additional virtual nodes when needed to preserve parallel edges.
    - `indptr`, `indices`, `length`: the raw CSR arrays (for fast path walkability).
    - `node_ids`: int64 array, row/col index -> OSM node id or synthetic virtual id.
    - `id_to_idx`: dict OSM node id -> index.
    - `tree`, `snap_indices`, `scale`: cKDTree over real node coords (m), the
      matching matrix row indices for those real nodes, and the projection scale.
    - `wscore`: float array aligned to `length` (walk only; else None).
    """
    import numpy as np
    from scipy.sparse import csr_matrix
    from scipy.spatial import cKDTree

    if cfg is None:
        cfg = PipelineConfig()

    cached = _MODE_CSR_CACHE.get(network_type)
    if cached is not None:
        return cached

    full_path = _resolve_mode_graph_path(network_type, cfg)
    csr_path = full_path[: -len(".graphml")] + "_csr_v2.npz"

    csr_stale = (
        os.path.isfile(csr_path)
        and os.path.isfile(full_path)
        and os.path.getmtime(full_path) > os.path.getmtime(csr_path)
    )
    if csr_stale:
        print(
            f"[Graph] Source graph is newer than CSR cache; rebuilding CSR for '{network_type}'",
            flush=True,
        )
    if os.path.isfile(csr_path) and not csr_stale:
        data = np.load(csr_path, allow_pickle=False)
        indptr = data["indptr"]
        indices = data["indices"]
        length = data["length"]
        node_ids = data["node_ids"]
        snap_x = data["snap_x"]
        snap_y = data["snap_y"]
        snap_indices = data["snap_indices"]
        m_per_deg_lon = float(data["m_per_deg_lon"])
        m_per_deg_lat = float(data["m_per_deg_lat"])
        wscore = data["wscore"] if "wscore" in data.files else None
    else:
        G = get_mode_graph(network_type, cfg, simplified=False)
        base_nodes = list(G.nodes())
        real_node_count = len(base_nodes)
        idx = {nid: i for i, nid in enumerate(base_nodes)}
        node_ids_list = [int(nid) for nid in base_nodes]
        next_virtual_id = -1
        rows_list: list[int] = []
        cols_list: list[int] = []
        length_list: list[float] = []

        need_scores = network_type == "walk"
        if need_scores:
            import walkability
            cache_obj = walkability.get_or_build_edge_walkability_index(
                cfg=cfg, G=G, force_rebuild=False, schema_version=1
            )
            escores = cache_obj.edge_scores
            score_list: list[float] = []

        edges_by_pair: dict[tuple[int, int], list[tuple[float, int, int, int]]] = {}
        for u, v, k, d in G.edges(keys=True, data=True):
            length_val = float(d.get("length", 0.0) or 0.0)
            pair = (idx[u], idx[v])
            edges_by_pair.setdefault(pair, []).append((length_val, int(u), int(v), int(k)))

        for (u_idx, v_idx), options in edges_by_pair.items():
            options.sort(key=lambda item: item[0])
            for option_idx, (edge_len, u_id, v_id, edge_key) in enumerate(options):
                edge_score = float(escores.get((u_id, v_id, edge_key), 5.0)) if need_scores else None
                if option_idx == 0:
                    rows_list.append(u_idx)
                    cols_list.append(v_idx)
                    length_list.append(edge_len)
                    if need_scores:
                        score_list.append(edge_score if edge_score is not None else 5.0)
                    continue

                virtual_idx = len(node_ids_list)
                node_ids_list.append(next_virtual_id)
                next_virtual_id -= 1

                rows_list.append(u_idx)
                cols_list.append(virtual_idx)
                length_list.append(edge_len)
                rows_list.append(virtual_idx)
                cols_list.append(v_idx)
                length_list.append(0.0)
                if need_scores:
                    score_list.append(edge_score if edge_score is not None else 5.0)
                    score_list.append(edge_score if edge_score is not None else 5.0)

        n = len(node_ids_list)
        rows = np.asarray(rows_list, dtype=np.int64)
        cols = np.asarray(cols_list, dtype=np.int64)
        length_vals = np.asarray(length_list, dtype=float)
        mat = csr_matrix((length_vals, (rows, cols)), shape=(n, n))
        mat.sort_indices()
        indptr = mat.indptr.astype(np.int64)
        indices = mat.indices.astype(np.int64)
        length = mat.data.astype(float)
        if need_scores:
            score_vals = np.asarray(score_list, dtype=float)
            smat = csr_matrix((score_vals, (rows, cols)), shape=(n, n))
            smat.sort_indices()
            wscore = smat.data.astype(float)
        else:
            wscore = None

        node_ids = np.asarray(node_ids_list, dtype=np.int64)
        snap_x = np.asarray([float(G.nodes[nid]["x"]) for nid in base_nodes], dtype=float)
        snap_y = np.asarray([float(G.nodes[nid]["y"]) for nid in base_nodes], dtype=float)
        snap_indices = np.arange(real_node_count, dtype=np.int64)
        mean_lat_rad = math.radians(float(snap_y.mean())) if snap_y.size else 0.0
        m_per_deg_lat = 111320.0
        m_per_deg_lon = 111320.0 * math.cos(mean_lat_rad)

        save_dict = dict(
            indptr=indptr,
            indices=indices,
            length=length,
            node_ids=node_ids,
            snap_x=snap_x,
            snap_y=snap_y,
            snap_indices=snap_indices,
            m_per_deg_lon=np.float64(m_per_deg_lon),
            m_per_deg_lat=np.float64(m_per_deg_lat),
        )
        if wscore is not None:
            save_dict["wscore"] = wscore
        tmp_path = csr_path + ".tmp.npz"
        np.savez(tmp_path, **save_dict)
        os.replace(tmp_path, csr_path)
        print(
            f"[Graph] Built CSR routing matrix for '{network_type}': "
            f"{csr_path} nodes={n} real_nodes={real_node_count} edges={len(rows_list)}",
            flush=True,
        )

    n_nodes = len(node_ids)
    mat = csr_matrix((length, indices, indptr), shape=(n_nodes, n_nodes))
    node_xy = np.column_stack([snap_x * m_per_deg_lon, snap_y * m_per_deg_lat])
    tree = cKDTree(node_xy)
    id_to_idx = {int(nid): i for i, nid in enumerate(node_ids)}
    bundle = {
        "mat": mat,
        "indptr": indptr,
        "indices": indices,
        "length": length,
        "node_ids": node_ids,
        "id_to_idx": id_to_idx,
        "tree": tree,
        "snap_indices": snap_indices,
        "scale": (m_per_deg_lon, m_per_deg_lat),
        "wscore": wscore,
    }
    _MODE_CSR_CACHE[network_type] = bundle
    return bundle



def get_mode_graph(network_type, cfg: PipelineConfig | None = None, simplified: bool = False):
    """Load or download graph for a specific travel mode.

    Inputs:
    - network_type: mode string (for example walk, bike, drive).
    - cfg: optional PipelineConfig. If not provided, creates a new one.
    - simplified: when True, return the topology-simplified routing graph (smaller,
      same network distances) instead of the full enumeration graph.

    Outputs:
    - graph object for that mode, cached in memory.
    """
    if cfg is None:
        cfg = PipelineConfig()

    if simplified:
        return _get_simplified_mode_graph(network_type, cfg)

    if network_type in _MODE_GRAPH_CACHE:
            return _MODE_GRAPH_CACHE[network_type]

    graph = None

    if cfg.use_shapefile:
        graph_path = _resolve_mode_graph_path(network_type, cfg)
        try:
            print(
                f"[Graph] Loading {network_type} graph from shapefile cache: "
                f"{graph_path}",
                flush=True,
            )
            graph = ox.io.load_graphml(graph_path)
        except Exception:
            print(
                f"[Graph] Cached shapefile graph not found for mode '{network_type}'. "
                f"Building from '{cfg.name_shapefile}'...",
                flush=True,
            )
            _, graph = graph_from_shapefile(
                cfg.name_shapefile,
                network_type=network_type,
            )
            ox.io.save_graphml(graph, filepath=graph_path)
            print(
                f"[Graph] Shapefile graph for mode '{network_type}' saved to "
                f"{graph_path}",
                flush=True,
            )
    else:
        # Cache per-mode graphs on disk to avoid repeated Overpass downloads.
        place_name = cfg.city_name
        name_file = _resolve_mode_graph_path(network_type, cfg)
        from utils.services import get_global_radius_m
        buffer_m = get_global_radius_m(cfg) or 0.0

        try:
            graph = ox.io.load_graphml(name_file)
        except Exception:
            print(f"[Graph] Cached graph not found for mode '{network_type}'. Downloading from OSM...", flush=True)
            if buffer_m > 0:
                place_gdf = ox.geocode_to_gdf(place_name)
                utm_crs = place_gdf.estimate_utm_crs()
                gdf_metric = place_gdf.to_crs(utm_crs)
                gdf_buffered = gdf_metric.copy()
                gdf_buffered["geometry"] = gdf_metric.geometry.buffer(buffer_m)
                polygon = gdf_buffered.to_crs("EPSG:4326").geometry.iloc[0]
                print(f"[Graph] Expanding graph area by {buffer_m/1000:.1f} km buffer", flush=True)
                # clean_periphery=True (osmnx default) runs stats.count_streets_per_node over
                # the buffered graph. That call hard-crashes the interpreter (SIGSEGV) on large
                # graphs in this environment — verified to still crash even on networkx 3.3 — and
                # only computes the `street_count` attribute, which is unused anywhere in this repo.
                # We pass clean_periphery=False to skip it entirely. The caller already applied its
                # own buffer, so osmnx's extra ~500 m peripheral buffer adds nothing here.
                # osmnx 1.9 emits a FutureWarning for this arg (removed in v2.0); suppress it so the
                # warning isn't mistaken for the cause of a crash it actually prevents.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", FutureWarning)
                    graph = ox.graph_from_polygon(
                        polygon,
                        network_type=network_type,
                        simplify=cfg.osm_autobuild_simplify,
                        retain_all=cfg.osm_autobuild_retain_all,
                        clean_periphery=False,
                    )
            else:
                graph = ox.graph_from_place(
                    place_name,
                    network_type=network_type,
                    simplify=cfg.osm_autobuild_simplify,
                    retain_all=cfg.osm_autobuild_retain_all,
                )
            ox.io.save_graphml(graph, filepath=name_file)
            print(f"[Graph] Graph downloaded and saved: {name_file}", flush=True)

    _MODE_GRAPH_CACHE[network_type] = graph
    return graph

def _tags_file_name(tags, buffer_m: float = 0.0) -> str:
    """Build stable geojson filename for a tags-based POI query.

    Inputs:
    - tags: dictionary used in OSM features query.

    Outputs:
    - str: deterministic filename for cache reuse.
    """
    tags_json = json.dumps(tags, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if buffer_m > 0:
        tags_json += f"|buffer={buffer_m:.1f}"
    key_hash = hashlib.sha1(tags_json.encode("utf-8")).hexdigest()
    return f"tags_{key_hash}.geojson"

def _feature_value_file_name(feature: str, value: TagValue, buffer_m: float = 0.0) -> str:
    """Build stable geojson filename for a feature/value POI query."""
    payload = json.dumps(
        {"feature": feature, "value": value},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    if buffer_m > 0:
        payload += f"|buffer={buffer_m:.1f}"
    key_hash = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"fv_{key_hash}.geojson"

def _city_poi_cache_dir(cache_slug: str) -> str:
    """Return scenario-scoped directory path for POI cache files."""
    return os.path.join("poi", cache_slug)

def _all_tags_file_name(query_tags: TagClause, buffer_m: float = 0.0) -> str:
    payload = json.dumps(query_tags, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if buffer_m > 0:
        payload += f"|buffer={buffer_m:.1f}"
    key_hash = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"all_tags_{key_hash}.geojson"

def _normalize_value_list(values: set[TagValue]) -> list[TagValue]:
    return sorted(list(values), key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=True))

def _iter_tag_clauses(tags: TagQuery | None) -> list[TagClause]:
    if tags is None:
        return []
    if isinstance(tags, list):
        return [clause for clause in tags if isinstance(clause, dict)]
    if isinstance(tags, dict):
        return [tags]
    return []


def _build_city_query_batches() -> dict[str, list[TagValue]]:
    """Group configured POI queries by tag key to enable batched extraction."""
    from utils import services as serv

    by_key: dict[str, set[TagValue]] = {}
    for q in serv.unique_query_keys():
        for clause in _iter_tag_clauses(q.tags):
            for key, value in clause.items():
                if isinstance(value, list):
                    for item in value:
                        by_key.setdefault(str(key), set()).add(str(item))
                else:
                    by_key.setdefault(str(key), set()).add(value)
    return {k: _normalize_value_list(v) for k, v in by_key.items()}

def _build_city_universe_tags() -> TagClause:
    """Build one combined tags payload covering all configured POI queries."""
    key_batches = _build_city_query_batches()
    out: TagClause = {}
    for key, values in key_batches.items():
        if any(v is True for v in values):
            out[key] = True
        else:
            out[key] = [str(v) for v in values]
    return out

def _download_city_poi_universe(place_name: str, query_tags: TagClause, buffer_m: float = 0.0) -> gpd.GeoDataFrame:
    """Download one city-wide POI dataset that covers all configured tags."""
    cfg = PipelineConfig()

    if cfg.use_shapefile:
        return feature_from_shapefile(cfg.name_shapefile, query_tags)
    elif cfg.poi_from_shp:
        return poi_from_shp(query_tags=query_tags)
    else:
        print(f"[POI] OSMnx city-universe download: keys={len(query_tags)}")
        return _download_poi_for_place(place_name, query_tags, buffer_m=buffer_m)

def _values_match(series: pd.Series, value: TagValue) -> pd.Series:
    if value is True:
        return series.notna()
    if isinstance(value, list):
        wanted = {str(v) for v in value}
        return series.astype(str).isin(wanted)
    return series.astype(str) == str(value)

def _filter_by_clause(gdf: gpd.GeoDataFrame, tags: TagClause) -> gpd.GeoDataFrame:
    if gdf.empty:
        return gdf
    mask = pd.Series(True, index=gdf.index)
    for key, value in tags.items():
        if key not in gdf.columns:
            return gpd.GeoDataFrame(geometry=[], crs=gdf.crs)
        mask = mask & _values_match(gdf[key], value)
    out = gdf.loc[mask].copy()
    if "geometry" in out.columns:
        out = out[out["geometry"].notna()].copy()
    return out

def _filter_by_tags(gdf: gpd.GeoDataFrame, tags: TagQuery) -> gpd.GeoDataFrame:
    if isinstance(tags, dict):
        return _filter_by_clause(gdf, tags)
    if not isinstance(tags, list) or not tags:
        return gpd.GeoDataFrame(geometry=[], crs=gdf.crs)
    parts = []
    for clause in tags:
        if not isinstance(clause, dict):
            continue
        part = _filter_by_clause(gdf, clause)
        if part is not None and not part.empty:
            parts.append(part)
    if not parts:
        return gpd.GeoDataFrame(geometry=[], crs=gdf.crs)
    out = gpd.GeoDataFrame(pd.concat(parts, ignore_index=False), crs=parts[0].crs)
    out = out[~out.index.duplicated(keep="first")].copy()
    if "geometry" in out.columns:
        out = out[out["geometry"].notna()].copy()
    return out

def _get_city_poi_universe(place_name: str, cache_slug: str, city_poi_dir: str, buffer_m: float = 0.0) -> gpd.GeoDataFrame:
    cache_key = f"{cache_slug}|buffer={buffer_m:.1f}"
    cached = _CITY_POI_UNIVERSE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    universe_tags = _build_city_universe_tags()
    path = os.path.join(city_poi_dir, _all_tags_file_name(universe_tags, buffer_m))
    gdf = _load_cached_poi_if_nonempty(path)
    if gdf is None:
        try:
            gdf = _download_city_poi_universe(place_name, universe_tags, buffer_m=buffer_m)
        except Exception as exc:
            print(f"[POI] City-universe download failed: {exc}")
            gdf = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        if gdf is not None and not gdf.empty and "geometry" in gdf.columns and not gdf["geometry"].dropna().empty:
            try:
                gdf.to_file(path, driver="GeoJSON")
            except Exception as exc:
                print(f"[POI] Failed to persist city-universe cache {path}: {exc}")
    if gdf is None:
        gdf = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    _CITY_POI_UNIVERSE_CACHE[cache_key] = gdf
    return gdf

def _download_poi_for_place(place_name: str, query_tags: TagClause, buffer_m: float = 0.0):
    """Download POIs for one place, optionally expanding the query area by buffer_m metres."""
    if buffer_m > 0:
        place_gdf = ox.geocode_to_gdf(place_name)
        if place_gdf.empty:
            raise RuntimeError(f"geocode_to_gdf returned no geometry for '{place_name}'")
        utm_crs = place_gdf.estimate_utm_crs()
        gdf_metric = place_gdf.to_crs(utm_crs)
        gdf_buffered = gdf_metric.copy()
        gdf_buffered["geometry"] = gdf_metric.geometry.buffer(buffer_m)
        polygon = gdf_buffered.to_crs("EPSG:4326").geometry.iloc[0]
        print(f"[POI] Query area expanded by {buffer_m/1000:.1f} km buffer", flush=True)
        return ox.features_from_polygon(polygon, query_tags)

    try:
        return ox.features_from_place(place_name, query_tags)
    except Exception as place_exc:
        print(
            f"[POI] features_from_place failed for '{place_name}' ({place_exc}); "
            "retrying with geocoded polygon..."
        )
        place_gdf = ox.geocode_to_gdf(place_name)
        if place_gdf.empty:
            raise RuntimeError(f"geocode_to_gdf returned no geometry for place '{place_name}'")
        polygon = place_gdf.geometry.iloc[0]
        return ox.features_from_polygon(polygon, query_tags)

def _read_geojson_without_gdal(path: str) -> gpd.GeoDataFrame:
    """Read a GeoJSON file using only json + shapely, never GDAL.

    geopandas' normal read path (fiona or pyogrio) goes through GDAL, which bundles its own
    GEOS. shapely bundles a *different* GEOS. Once shapely's GEOS has been heavily exercised
    in the process (building the walk/bike/drive graphs in the snapping stage), any subsequent
    GDAL geometry read segfaults the interpreter with no traceback — reproducible with both
    fiona and pyogrio. Parsing the GeoJSON as plain JSON and building geometries with
    shapely.geometry.shape uses shapely's GEOS exclusively, sidestepping the conflict entirely.
    GeoJSON is always EPSG:4326 per RFC 7946, which matches what GDAL returned for these files.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    features = data.get("features", []) if isinstance(data, dict) else []
    geometries = [shape(ft["geometry"]) if ft.get("geometry") else None for ft in features]
    properties = [ft.get("properties", {}) or {} for ft in features]
    return gpd.GeoDataFrame(pd.DataFrame(properties), geometry=geometries, crs="EPSG:4326")


def _load_cached_poi_if_nonempty(path: str):
    """Load cached POI file and return None when missing/invalid/empty."""
    try:
        poi = _read_geojson_without_gdal(path)
    except Exception:
        return None
    if poi is None or poi.empty:
        return None
    if "geometry" not in poi.columns:
        return None
    if poi["geometry"].dropna().empty:
        return None
    return poi


def get_poi(
    feature: str | None = None,
    value: TagValue | None = None,
    tags: TagQuery | None = None,
    poi_type: str | None = None,
):
    """Load POIs from local cache or download them from OSM.

    Inputs:
    - feature/value: optional key-value OSM filter.
    - tags: optional tags dictionary filter (preferred when provided).

    Outputs:
    - GeoDataFrame: POI features matching requested filters.
    """

    cfg = PipelineConfig()

    # Nomi file
    place_name = cfg.city_name
    city_slug = cfg.city_slug
    poi_cache_slug = cfg.artifact_slug if cfg.use_shapefile else city_slug

    from utils.services import get_global_radius_m
    buffer_m: float = get_global_radius_m(cfg) or 0.0

    os.makedirs("poi", exist_ok=True)
    city_poi_dir = _city_poi_cache_dir(poi_cache_slug)
    os.makedirs(city_poi_dir, exist_ok=True)

    if (feature is None or value is None) and not tags:
        poi_files = [
            os.path.join(city_poi_dir, name)
            for name in os.listdir(city_poi_dir)
            if name.lower().endswith(".geojson")
        ]

        if poi_files:
            frames = []
            for path in poi_files:
                cached = _load_cached_poi_if_nonempty(path)
                if cached is not None:
                    frames.append(cached)

            if frames:
                poi = gpd.GeoDataFrame(
                    pd.concat(frames, ignore_index=True),
                    crs=frames[0].crs
                )
                return poi

        # Fallback: download all amenities when no local cache is available.
        feature = "amenity"
        value = True

    if tags:
        NAME_FILE = os.path.join(city_poi_dir, _tags_file_name(tags, buffer_m))
    else:
        feature_key = feature if feature is not None else "amenity"
        value_key: TagValue = value if value is not None else True
        NAME_FILE = os.path.join(city_poi_dir, _feature_value_file_name(feature_key, value_key, buffer_m))

    # Provo a caricare i POI gia salvati.
    poi = _load_cached_poi_if_nonempty(NAME_FILE)
    if poi is None:
        if tags and not cfg.poi_from_shp:
            try:
                universe = _get_city_poi_universe(place_name, poi_cache_slug, city_poi_dir, buffer_m=buffer_m)
                if universe is not None and not universe.empty:
                    poi = _filter_by_tags(universe, tags)
                    if poi is not None and not poi.empty:
                        poi.to_file(NAME_FILE, driver="GeoJSON")
                        print(
                            f"[POI] Built poi_type={poi_type} from city-universe cache. "
                            f"count={len(poi)} file={NAME_FILE}",
                            flush=True,
                        )
                        return poi
            except Exception as batch_exc:
                print(f"[POI] Batch resolution failed for tags={tags}: {batch_exc}")
            # Avoid duplicate OSMnx work: tags queries rely only on the city-universe dataset.
            print(f"[POI] No matches after city-universe filtering for tags={tags}. Skipping per-query OSMnx fallback.")
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        download_key = (poi_cache_slug, place_name)
        if download_key not in _POI_DOWNLOAD_LOGGED:
            print(f"[POI] Downloading POIs for '{place_name}'...", flush=True)
            _POI_DOWNLOAD_LOGGED.add(download_key)

        try:
            if cfg.use_shapefile:
                print(
                    f"[POI] Loading poi_type={poi_type} from shapefile source...",
                    flush=True,
                )
                # In shapefile mode, selection is based on poi_type -> labels mapping.
                # OSM tags are irrelevant for source filtering.
                poi = feature_from_shapefile(cfg.name_shapefile, query_tags={}, poi_type=poi_type)
            else:
                if tags:
                    if not isinstance(tags, dict):
                        # OR-queries are resolved from the city-universe dataset only.
                        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
                    query_tags: TagClause = tags
                else:
                    query_tags = {feature_key: value_key}
                print(
                    f"[POI] Downloading poi_type={poi_type} from OSM for '{place_name}'...",
                    flush=True,
                )
                poi = _download_poi_for_place(place_name, query_tags, buffer_m=buffer_m)
        except Exception as e:
            print(
                f"[POI] No POIs found for city={place_name}, poi_type={poi_type}, feature={feature}, value={value}, tags={tags}: {e}",
                flush=True,
            )
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        source_name = "shapefile" if cfg.use_shapefile else "OSM"
        if poi.empty:
            # Do not persist empty payloads: they can mask transient Overpass/geocoding issues.
            try:
                if os.path.exists(NAME_FILE):
                    os.remove(NAME_FILE)
            except Exception:
                pass
            print(
                f"[POI] {source_name.capitalize()} query returned 0 features "
                f"for poi_type={poi_type}. Cache not persisted for {NAME_FILE}",
                flush=True,
            )
            return poi
        if "geometry" not in poi.columns or poi["geometry"].dropna().empty:
            try:
                if os.path.exists(NAME_FILE):
                    os.remove(NAME_FILE)
            except Exception:
                pass
            print(
                f"[POI] {source_name.capitalize()} query has no usable geometry "
                f"for poi_type={poi_type}. Cache not persisted for {NAME_FILE}",
                flush=True,
            )
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        poi.to_file(NAME_FILE, driver="GeoJSON")
        print(
            f"[POI] Cached poi_type={poi_type}. count={len(poi)} file={NAME_FILE}",
            flush=True,
        )

    return poi

def print_poi(poi, print_start, print_end):
    """Print POI names in a selected index window.

    Inputs:
    - poi: POI GeoDataFrame.
    - print_start: start index (inclusive).
    - print_end: end index (exclusive).

    Outputs:
    - None. Prints names to stdout.
    """
    if print_start == None or print_start < 0:
        print_start = 0
    
    if print_end == None or print_end > len(poi):
        print_end = len(poi)

    subset = poi.iloc[print_start:print_end]

    for i, row in subset.iterrows():
        print(f"{i+1}: {row.get('name', 'Unnamed')}")


def get_poi_names(poi):
    """Extract POI names from a GeoDataFrame.

    Inputs:
    - poi: POI GeoDataFrame.

    Outputs:
    - list[str]: non-null POI names.
    """
    return [
        name for name in poi["name"].dropna().astype(str).values
    ]

def get_poi_geom(poi):
    """Extract geometry column values as strings.

    Inputs:
    - poi: POI GeoDataFrame.

    Outputs:
    - list[str]: non-null geometry values converted to strings.
    """
    return [
        geometry for geometry in poi["geometry"].dropna().astype(str).values
    ]

def get_poi_geometries(poi):
    """Extract geometry objects, optional names, and stable source keys from POI table.

    Inputs:
    - poi: POI GeoDataFrame.

    Outputs:
    - list[tuple[geometry, name, source_key]]: geometry/name/key triples for downstream snapping.
    """
    out = []
    if "geometry" not in poi.columns:
        return out
    names = poi["name"] if "name" in poi.columns else None
    for idx, geometry in poi["geometry"].dropna().items():
        name = None
        if names is not None:
            try:
                name = names.loc[idx]
            except Exception:
                name = None
        row = poi.loc[idx]
        source_key = build_poi_source_key(row, geometry)
        out.append((geometry, name, source_key))
    return out

def get_poi_amenity_types(poi):
    """List unique amenity values present in POI data.

    Inputs:
    - poi: POI GeoDataFrame.

    Outputs:
    - list[str]: sorted unique amenity labels.
    """
    if "amenity" not in poi.columns:
        return []
    return sorted(poi["amenity"].dropna().astype(str).unique().tolist())
