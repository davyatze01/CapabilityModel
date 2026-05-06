import osmnx as ox
import os
import geopandas as gpd
import pandas as pd
import hashlib
import json
from typing import TypeAlias

from config import PipelineConfig
from utils.load_shapefile import graph_from_shapefile, feature_from_shapefile

TagValue: TypeAlias = bool | str | list[str]
TagsDict: TypeAlias = dict[str, TagValue]

# In-memory caches to avoid repeated disk loads
_GRAPH_CACHE = None
_MODE_GRAPH_CACHE = {}
_CITY_POI_UNIVERSE_CACHE: dict[str, gpd.GeoDataFrame] = {}


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
        print(f"Grafo non presente, scarico il grafo di {place_name}..")
        graph = ox.graph_from_place(place_name)
        ox.io.save_graphml(graph, filepath=name_file)
        print("Grafo scaricato e salvato correttamente!")

    _GRAPH_CACHE = graph
    return graph


def get_mode_graph(network_type):
    """Load or download graph for a specific travel mode.

    Inputs:
    - network_type: mode string (for example walk, bike, drive).

    Outputs:
    - graph object for that mode, cached in memory.
    """
    cfg = PipelineConfig()

    if network_type in _MODE_GRAPH_CACHE:
            return _MODE_GRAPH_CACHE[network_type]
    
    graph = None

    if cfg.use_shapefile:
        graph_path = f"graph/{cfg.artifact_slug}_{network_type}.graphml"
        try:
            print(
                f"[Graph] Carico grafo {network_type} da cache shapefile: "
                f"{graph_path}",
                flush=True,
            )
            graph = ox.io.load_graphml(graph_path)
        except Exception:
            print(
                f"[Graph] Grafo {network_type} da shapefile non presente. "
                f"Costruisco da '{cfg.name_shapefile}'...",
                flush=True,
            )
            _, graph = graph_from_shapefile(
                cfg.name_shapefile,
                network_type=network_type,
            )
            ox.io.save_graphml(graph, filepath=graph_path)
            print(
                f"[Graph] Grafo {network_type} da shapefile salvato in "
                f"{graph_path}",
                flush=True,
            )
    else:
        # Cache per-mode graphs on disk to avoid repeated Overpass downloads.
        place_name, city_slug = _get_city_settings()
        name_file = f"graph/{city_slug}_{network_type}.graphml"

        try:
            graph = ox.io.load_graphml(name_file)
        except Exception:
            print(f"Grafo {network_type} non presente, scarico da OSM..")
            graph = ox.graph_from_place(place_name, network_type=network_type)
            ox.io.save_graphml(graph, filepath=name_file)
            print("Grafo scaricato e salvato correttamente!")

    _MODE_GRAPH_CACHE[network_type] = graph
    return graph

def _tags_file_name(tags):
    """Build stable geojson filename for a tags-based POI query.

    Inputs:
    - tags: dictionary used in OSM features query.

    Outputs:
    - str: deterministic filename for cache reuse.
    """
    tags_json = json.dumps(tags, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    key_hash = hashlib.sha1(tags_json.encode("utf-8")).hexdigest()
    return f"tags_{key_hash}.geojson"

def _feature_value_file_name(feature: str, value: TagValue) -> str:
    """Build stable geojson filename for a feature/value POI query."""
    payload = json.dumps(
        {"feature": feature, "value": value},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    key_hash = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"fv_{key_hash}.geojson"

def _city_poi_cache_dir(cache_slug: str) -> str:
    """Return scenario-scoped directory path for POI cache files."""
    return os.path.join("poi", cache_slug)

def _all_tags_file_name(query_tags: TagsDict) -> str:
    payload = json.dumps(query_tags, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    key_hash = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"all_tags_{key_hash}.geojson"

def _normalize_value_list(values: set[TagValue]) -> list[TagValue]:
    return sorted(list(values), key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=True))

def _build_city_query_batches() -> dict[str, list[TagValue]]:
    """Group configured POI queries by tag key to enable batched extraction."""
    from utils import services as serv

    by_key: dict[str, set[TagValue]] = {}
    for q in serv.unique_query_keys():
        tags = q.tags or {}
        for key, value in tags.items():
            if isinstance(value, list):
                for item in value:
                    by_key.setdefault(str(key), set()).add(str(item))
            else:
                by_key.setdefault(str(key), set()).add(value)
    return {k: _normalize_value_list(v) for k, v in by_key.items()}

def _build_city_universe_tags() -> TagsDict:
    """Build one combined tags payload covering all configured POI queries."""
    key_batches = _build_city_query_batches()
    out: TagsDict = {}
    for key, values in key_batches.items():
        if any(v is True for v in values):
            out[key] = True
        else:
            out[key] = [str(v) for v in values]
    return out

def _download_city_poi_universe(place_name: str, query_tags: TagsDict) -> gpd.GeoDataFrame:
    """Download one city-wide POI dataset that covers all configured tags."""
    print(f"[POI] OSMnx city-universe download: keys={len(query_tags)}")
    cfg = PipelineConfig()

    if cfg.use_shapefile:
        return feature_from_shapefile(cfg.name_shapefile, query_tags)
    else:
        return _download_poi_for_place(place_name, query_tags)

def _values_match(series: pd.Series, value: TagValue) -> pd.Series:
    if value is True:
        return series.notna()
    if isinstance(value, list):
        wanted = {str(v) for v in value}
        return series.astype(str).isin(wanted)
    return series.astype(str) == str(value)

def _filter_by_tags(gdf: gpd.GeoDataFrame, tags: TagsDict) -> gpd.GeoDataFrame:
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

def _get_city_poi_universe(place_name: str, cache_slug: str, city_poi_dir: str) -> gpd.GeoDataFrame:
    cached = _CITY_POI_UNIVERSE_CACHE.get(cache_slug)
    if cached is not None:
        return cached

    universe_tags = _build_city_universe_tags()
    path = os.path.join(city_poi_dir, _all_tags_file_name(universe_tags))
    gdf = _load_cached_poi_if_nonempty(path)
    if gdf is None:
        try:
            gdf = _download_city_poi_universe(place_name, universe_tags)
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
    _CITY_POI_UNIVERSE_CACHE[cache_slug] = gdf
    return gdf

def _download_poi_for_place(place_name: str, query_tags: TagsDict):
    """Download POIs for one place with polygon fallback when place lookup fails."""
    try:
        return ox.features_from_place(place_name, query_tags)
    except Exception as place_exc:
        print(
            f"features_from_place failed for '{place_name}' ({place_exc}); "
            "retrying with geocoded polygon..."
        )
        place_gdf = ox.geocode_to_gdf(place_name)
        if place_gdf.empty:
            raise RuntimeError(f"geocode_to_gdf returned no geometry for place '{place_name}'")
        polygon = place_gdf.geometry.iloc[0]
        return ox.features_from_polygon(polygon, query_tags)

def _load_cached_poi_if_nonempty(path: str):
    """Load cached POI file and return None when missing/invalid/empty."""
    try:
        poi = gpd.read_file(path)
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
    tags: TagsDict | None = None,
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

        # Fallback: scarica tutti gli amenity se non ci sono file locali
        feature = "amenity"
        value = True

    if tags:
        NAME_FILE = os.path.join(city_poi_dir, _tags_file_name(tags))
    else:
        feature_key = feature if feature is not None else "amenity"
        value_key: TagValue = value if value is not None else True
        NAME_FILE = os.path.join(city_poi_dir, _feature_value_file_name(feature_key, value_key))

    # Provo a caricare i POI gia salvati.
    poi = _load_cached_poi_if_nonempty(NAME_FILE)
    if poi is None:
        if tags:
            try:
                universe = _get_city_poi_universe(place_name, poi_cache_slug, city_poi_dir)
                if universe is not None and not universe.empty:
                    poi = _filter_by_tags(universe, tags)
                    if poi is not None and not poi.empty:
                        poi.to_file(NAME_FILE, driver="GeoJSON")
                        print(f"POI built from city-universe cache. count={len(poi)} file={NAME_FILE}")
                        return poi
            except Exception as batch_exc:
                print(f"[POI] Batch resolution failed for tags={tags}: {batch_exc}")
            # Avoid duplicate OSMnx work: tags queries rely only on the city-universe dataset.
            print(f"[POI] No matches after city-universe filtering for tags={tags}. Skipping per-query OSMnx fallback.")
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        # Scarico POI da OSM
        print(f"Scarico POI da OSM per '{place_name}'...")

        try:
            if tags:
                query_tags: TagsDict = tags
            else:
                query_tags = {feature_key: value_key}
            if cfg.use_shapefile:
                print(f"[POI] Scarico POI da shapefile...")
                poi = feature_from_shapefile(cfg.name_shapefile, query_tags=query_tags)
            else:
                print(f"[POI] Scarico POI da OSM per '{place_name}'...")
                poi = _download_poi_for_place(place_name, query_tags)
        except Exception as e:
            # Nessun POI disponibile per questa query: ritorna GDF vuoto
            print(f"Nessun POI trovato per city={place_name} feature={feature} value={value} tags={tags}: {e}")
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        if poi.empty:
            # Do not persist empty payloads: they can mask transient Overpass/geocoding issues.
            try:
                if os.path.exists(NAME_FILE):
                    os.remove(NAME_FILE)
            except Exception:
                pass
            print(f"POI download returned 0 features. Cache not persisted for {NAME_FILE}")
            return poi
        if "geometry" not in poi.columns or poi["geometry"].dropna().empty:
            try:
                if os.path.exists(NAME_FILE):
                    os.remove(NAME_FILE)
            except Exception:
                pass
            print(f"POI download has no usable geometry. Cache not persisted for {NAME_FILE}")
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        # Salvo tutti i POI
        poi.to_file(NAME_FILE, driver="GeoJSON")
        print(f"POI scaricati e salvati correttamente! count={len(poi)} file={NAME_FILE}")

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
        print(f"{i+1}: {row.get('name', 'Senza nome')}")


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
    """Extract geometry objects with optional names from POI table.

    Inputs:
    - poi: POI GeoDataFrame.

    Outputs:
    - list[tuple[geometry, name]]: geometry/name pairs for downstream snapping.
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
        out.append((geometry, name))
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
