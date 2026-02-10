import os
from collections import Counter, OrderedDict
import geopandas as gpd
import pandas as pd
import osmnx as ox

CAP_RESTORATIVENESS_IDX = {
    "sport_and_movement": 0,
    "scenic_views": 1,
    "quietness": 2,
    "cultural_activities": 3,
    "nature_contact": 4,
}

CAP_NUTRITION_IDX = {
    "eating_out": 0,
    "fresh_food_access": 1,
    "ready_food_access": 2,
}

CAP_CARE_IDX = {
    "medicines_and_supplies": 0,
    "impatient_and_care": 1,
    "rehabilitation_services": 2,
    "diagnosis_and_prevention": 3,
    "emergency_services": 4,
    "care_services": 5,
}

CAP_SINGLETON_M = {
    "restorativeness": {
        "sport_and_movement": 1.0,     # direct restorative mechanism (activity, stress relief)
        "scenic_views": 0.8,           # strong contributor
        "quietness": 1.0,              # core restorative condition
        "cultural_activities": 0.6,    # restorative but more context dependent
        "nature_contact": 1.0,         # core restorative mechanism
    },

    "nutrition": {
        "eating_out": 0.6,             # provides nutrition but quality/control varies
        "fresh_food_access": 1.0,      # core for adequate nutrition
        "ready_food_access": 0.8,      # strong contributor (availability), less ideal than fresh
    },

    "care": {
        "medicines_and_supplies": 0.8,       # strong contributor
        "impatient_and_care": 1.0,           # core
        "rehabilitation_services": 0.8,      # strong contributor
        "diagnosis_and_prevention": 0.8,     # strong contributor
        "emergency_services": 1.0,           # core
        "care_services": 1.0,                # core (ongoing care)
    },
}

def cap(S, capability):
    if len(S) == 0:
        return 0
    elif len(S) == 1:
        return CAP_SINGLETON_M[capability][S[0]]
    else:
        singletons =  [CAP_SINGLETON_M[capability][k] for k in S]
        m = max(singletons)
        return min(1, m + 0.2 * (1 - m))
    

def choquet_integral(x, capability):
    n = len(x)
    order = sorted(range(n), key=lambda i: x[i])
    x_sorted = [x[i] for i in order]
    services = CAPABILITY_SERVICES[capability]

    total = 0.0
    prev = 0.0
    for j in range(n):
        tail = [services[i] for i in order[j:]]
        total += (x_sorted[j] - prev) * cap(tail, capability)
        prev = x_sorted[j]
    return total


CAPABILITY_SERVICES = {
    "restorativeness": list(CAP_RESTORATIVENESS_IDX.keys()),
    "nutrition": list(CAP_NUTRITION_IDX.keys()),
    "care": list(CAP_CARE_IDX.keys()),
}


def print_poi_entrances(poi_gdf, entrance_tags=None, search_radius_m=50):
    """
    Print entrances for each POI to verify availability.
    - entrance_tags: OSM tags to identify entrances (default: {"entrance": True})
    - search_radius_m: used for point POIs to find nearest entrance within radius
    """
    if poi_gdf is None or poi_gdf.empty:
        print("No POIs provided.")
        return

    tags = entrance_tags or {"entrance": True}
    minx, miny, maxx, maxy = poi_gdf.total_bounds
    entrances = ox.features_from_bbox((maxy, miny, maxx, minx), tags=tags)
    if entrances is None or entrances.empty:
        print("No entrance features found in POI bounds.")
        return

    entrances = entrances[entrances.geometry.notna()].copy()
    entrances["geometry"] = entrances.geometry.apply(
        lambda g: g if g.geom_type == "Point" else g.centroid
    )

    for idx, row in poi_gdf.iterrows():
        geom = row.geometry
        if geom is None:
            continue

        name = row.get("name", "")
        if geom.geom_type in ("Polygon", "MultiPolygon"):
            candidates = entrances[entrances.within(geom)]
        else:
            # for points/lines: find entrances within buffer
            try:
                buffer_geom = geom.buffer(search_radius_m / 111_000.0)
                candidates = entrances[entrances.within(buffer_geom)]
            except Exception:
                candidates = entrances.iloc[0:0]

        coords = [(g.y, g.x) for g in candidates.geometry]
        print(f"POI {idx} {name}: {len(coords)} entrances -> {coords[:5]}")


def print_all_service_poi_entrances(entrance_tags=None, search_radius_m=50, limit=None):
    """
    Print entrances for all POIs defined in services/capabilities.
    - limit: optional max POIs per query to print (for quick inspection)
    """
    from utils import services, graphml, delta_g

    queries = services.unique_query_keys()
    for q in queries:
        feature, value, tags = delta_g._resolve_query(q.poi_type, None, q.tags)
        if tags:
            poi = graphml.get_poi(tags=tags)
        else:
            poi = graphml.get_poi(feature, value)
        if limit is not None:
            try:
                poi = poi.head(int(limit))
            except Exception:
                pass
        print(f"\n=== {q.service} | {q.poi_type} | tags={q.tags} ===")
        print_poi_entrances(poi, entrance_tags=entrance_tags, search_radius_m=search_radius_m)
