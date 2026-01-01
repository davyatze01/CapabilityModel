from tqdm import tqdm
from shapely import wkt
from utils import graphml, route, decay
import math
import numpy as np
import os
import pickle
import networkx as nx  # <-- FIX: per catturare NetworkXNoPath / NodeNotFound

# ==========================
# CONFIG
# ==========================
CACHE_VERSION = 1
CACHE_FOLDER = "rra_cache"

os.makedirs(CACHE_FOLDER, exist_ok=True)


def accessibility(poi_type, origine):
    cache_file = os.path.join(
        CACHE_FOLDER,
        f"RRA_v{CACHE_VERSION}_{poi_type}_{origine[0]}_{origine[1]}.pkl"
    )

    # ---- Load from cache if exists ----
    if os.path.exists(cache_file):
        print("📦 Carico RRA dalla cache")
        RRA = load_rra(cache_file)
    else:
        grafo = graphml.get_graph()
        poi = graphml.get_poi(poi_type, True)
        poi_geometry = graphml.get_poi_geom(poi)

        RRA = []
        beta = math.log(2) / 20.0

        for geom_value in tqdm(poi_geometry, desc="POI", unit="POI"):
            geom = wkt.loads(geom_value)

            # Se non è un Point (es. Polygon), convertilo in un punto
            if geom.geom_type != "Point":
                geom = geom.representative_point()  # oppure geom.centroid

            destinazione = (geom.y, geom.x)


            decay_walk = decay_bike = decay_drive = decay_bus = None

            for mode in tqdm(["walk", "bike", "drive", "bus"], desc="Modalità", leave=False):
                try:
                    _, _, imp = route.get_route(
                        grafo,
                        mode,
                        origine,
                        destinazione,
                        impedance_flag=True,
                        ax=None
                    )
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    imp = None  # <-- FIX: nessun percorso / nodo non trovato
                except Exception:
                    # opzionale: se vuoi vedere altri errori, commenta il next e fai raise
                    imp = None

                # se imp è None o vuoto, saltiamo
                if not imp:
                    continue

                d = decay.distance_decay(beta, imp)

                if mode == "walk":
                    decay_walk = d
                elif mode == "bike":
                    decay_bike = d
                elif mode == "drive":
                    decay_drive = d
                elif mode == "bus":
                    decay_bus = d

            # Calcola RRA solo se HO TUTTO per questo POI
            if None not in (decay_walk, decay_bike, decay_drive, decay_bus):
                rra_poi = decay.calculate_rra(decay_walk, decay_bike, decay_drive, decay_bus)
                RRA.append(rra_poi)
            else:
                # se vuoi mantenere l'allineamento coi POI:
                # RRA.append(None)
                pass

        save_rra(cache_file, RRA)

    RRA_desc = sorted(RRA, reverse=True)
    print("RRA desc=", RRA_desc)
    
    def c_from_target(n_target: int, y_target: float) -> float:
        if n_target <= 0:
            raise ValueError("n_target must be > 0")
        if not (0.0 < y_target < 1.0):
            raise ValueError("y_target must be in (0, 1)")
        return round(math.log(1.0 - y_target) / n_target, 2)  # negative

    def g(x: int, c: float) -> float:
        return 1.0 - math.exp(c * x)

    def deltag(x: int, x2: int, c: float) -> float:
        return g(x, c) - g(x2, c)
    
    accessibility = 0
    c = c_from_target(n_target=2, y_target=0.6)

    for i,element in enumerate(RRA_desc):
        if i == 0:
            accessibility += (element * g(i+1, c))
        else:
            accessibility += (element * deltag(i+1, i,c))
    
    print("Accessibility= ", accessibility)
    return accessibility

def save_rra(path, RRA):
    with open(path, "wb") as f:
        pickle.dump(RRA, f)


def load_rra(path):
    with open(path, "rb") as f:
        return pickle.load(f)
