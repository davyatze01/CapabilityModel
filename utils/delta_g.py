from tqdm import tqdm
from shapely import wkt
from utils import graphml, route, decay
import math
import numpy as np
import os
import pickle
import networkx as nx  # per catturare NetworkXNoPath / NodeNotFound

# ==========================
# CONFIG
# ==========================
CACHE_VERSION = 1
CACHE_FOLDER = "rra_cache"
os.makedirs(CACHE_FOLDER, exist_ok=True)

# ==========================
# CACHE (in-memory) per evitare reload ripetuti
# (non cambia la logica: stessi dati, meno overhead)
# ==========================
_G_CACHE = None
_POI_GEOM_CACHE = {}  # chiave: poi_type -> list geom WKT


def accessibility(poi_type, origine):
    # nome file cache: meglio arrotondare per evitare migliaia di file quasi identici per float
    # (non cambia la logica del calcolo, solo il "nome" della cache)
    lat_key = f"{origine[0]:.6f}"
    lon_key = f"{origine[1]:.6f}"

    cache_file = os.path.join(
        CACHE_FOLDER,
        f"RRA_v{CACHE_VERSION}_{poi_type}_{lat_key}_{lon_key}.pkl"
    )

    # ---- Load from cache if exists ----
    if os.path.exists(cache_file):
        # print più leggero (puoi toglierlo se vuoi)
        # print("📦 Carico RRA dalla cache")
        RRA = load_rra(cache_file)
    else:
        global _G_CACHE, _POI_GEOM_CACHE

        # carica il grafo una sola volta (in RAM)
        if _G_CACHE is None:
            _G_CACHE = graphml.get_graph()
        grafo = _G_CACHE

        # carica geometrie POI una sola volta per tipo (in RAM)
        if poi_type not in _POI_GEOM_CACHE:
            poi = graphml.get_poi(poi_type, True)
            _POI_GEOM_CACHE[poi_type] = graphml.get_poi_geom(poi)
        poi_geometry = _POI_GEOM_CACHE[poi_type]

        RRA = []
        beta = math.log(2) / 20.0

        for geom_value in tqdm(poi_geometry, desc="POI", unit="POI", mininterval=0.5):
            geom = wkt.loads(geom_value)

            # Se non è un Point (es. Polygon), convertilo in un punto
            if geom.geom_type != "Point":
                geom = geom.representative_point()  # oppure geom.centroid

            destinazione = (geom.y, geom.x)

            decay_walk = decay_bike = decay_drive = decay_bus = None

            for mode in tqdm(
                ["walk", "bike", "drive", "bus"],
                desc="Modalità",
                leave=False,
                mininterval=0.5
            ):
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
                    imp = None  # nessun percorso / nodo non trovato
                except Exception:
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
                pass

        save_rra(cache_file, RRA)

    RRA_desc = sorted(RRA, reverse=True)
    # print("RRA desc=", RRA_desc)

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

    accessibility_value = 0
    c = c_from_target(n_target=2, y_target=0.6)

    for i, element in enumerate(RRA_desc):
        if i == 0:
            accessibility_value += (element * g(i + 1, c))
        else:
            accessibility_value += (element * deltag(i + 1, i, c))

    # print("Accessibility= ", accessibility_value)
    return accessibility_value


def save_rra(path, RRA):
    # scrittura atomica (evita file corrotti se interrompi il programma)
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        pickle.dump(RRA, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp_path, path)


def load_rra(path):
    with open(path, "rb") as f:
        return pickle.load(f)
