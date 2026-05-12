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

# 

# servizi
""" sport_and_movement,poi_type,choquet_capacity
[amenity_supermarket,]....[]


restorativeness,False,[servizi...],[singleton servizi]
[sport_and_movement,...] [1.0,...] """


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
    """Return the capability measure for a subset of services.

    Inputs:
    - S: ordered/list-like subset of services.
    - capability: target capability key.

    Outputs:
    - float: fuzzy measure value used by Choquet aggregation.
    """
    if len(S) == 0:
        return 0
    elif len(S) == 1:
        return CAP_SINGLETON_M[capability][S[0]]
    else:
        singletons =  [CAP_SINGLETON_M[capability][k] for k in S]
        m = max(singletons)
        return min(1, m + 0.2 * (1 - m))
    

def choquet_integral(x, capability):
    """Aggregate service scores into one capability score via Choquet integral.

    Inputs:
    - x: service score list aligned to capability service order.
    - capability: target capability key.

    Outputs:
    - float: aggregated capability score.
    """
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


rows = []

for capability,services in CAPABILITY_SERVICES.items():
    singleton_values = [
        CAP_SINGLETON_M[capability][service]
        for service in services
    ]


    rows.append({
        "capability": capability,
        "services":services,
        "choquet_singleton": singleton_values,
        "enabled": True
    })


df = pd.DataFrame(rows)

df.to_csv("capability_new.csv", index=False)