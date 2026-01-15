from tqdm import tqdm
from utils import graphml, route, decay, get_impedance
import math
import os
import pickle
import networkx as nx  # per catturare NetworkXNoPath / NodeNotFound
import osmnx as ox

# ==========================
# CONFIG
# ==========================
CACHE_VERSION = 1
CACHE_FOLDER = "rra_cache"
os.makedirs(CACHE_FOLDER, exist_ok=True)

# ==========================
# CACHE (in-memory) per evitare reload ripetuti
# ==========================
_G_CACHE = None
_POI_GEOM_CACHE = {}  # chiave: (feature, value) -> list geom
_MODE_GRAPH_CACHE = {}  # chiave: network_type -> graph


def _resolve_feature(poi_type, feature):
    if feature is None:
        if poi_type in {"healthcare"}:
            return poi_type, True
        return "amenity", poi_type
    return feature, poi_type


def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

def _get_mode_graph(base_graph, origin, network_type, radius_m):
    if route._can_use_base_graph(base_graph, network_type):
        return base_graph
    if radius_m is None:
        if network_type not in _MODE_GRAPH_CACHE:
            _MODE_GRAPH_CACHE[network_type] = graphml.get_mode_graph(network_type)
        return _MODE_GRAPH_CACHE[network_type]
    dist = max(500, int(radius_m))
    return route._get_cached_graph(origin, dist, network_type)


def accessibility(poi_type, origine, feature=None, radius_m=None):
    feature, value = _resolve_feature(poi_type, feature)

    # nome file cache: arrotonda per evitare migliaia di file quasi identici
    lat_key = f"{origine[0]:.6f}"
    lon_key = f"{origine[1]:.6f}"
    radius_key = "all" if radius_m is None else f"r{int(radius_m)}"

    cache_file = os.path.join(
        CACHE_FOLDER,
        f"RRA_v{CACHE_VERSION}_{feature}_{poi_type}_{radius_key}_{lat_key}_{lon_key}.pkl"
    )

    # ---- Load from cache if exists ----
    if os.path.exists(cache_file):
        RRA = load_rra(cache_file)
    else:
        global _G_CACHE, _POI_GEOM_CACHE

        # carica il grafo una sola volta (in RAM)
        if _G_CACHE is None:
            _G_CACHE = graphml.get_graph()
        grafo = _G_CACHE

        # carica geometrie POI una sola volta per tipo (in RAM)
        cache_key = (feature, value)
        if cache_key not in _POI_GEOM_CACHE:
            poi = graphml.get_poi(feature, value)
            _POI_GEOM_CACHE[cache_key] = graphml.get_poi_geometries(poi)

        poi_geometry = _POI_GEOM_CACHE[cache_key]

        poi_points = []
        for geom in poi_geometry:
            if geom is None:
                continue
            if geom.geom_type != "Point":
                geom = geom.representative_point()
            poi_points.append(geom)

        if radius_m is not None:
            poi_points = [
                geom for geom in poi_points
                if _haversine_m(origine[0], origine[1], geom.y, geom.x) <= radius_m
            ]

        RRA = []
        beta = math.log(2) / 20.0

        mode_distances = {}
        xs = [geom.x for geom in poi_points]
        ys = [geom.y for geom in poi_points]

        for mode in ["walk", "bike", "drive"]:
            mode_graph = _get_mode_graph(grafo, origine, mode, radius_m)
            origin_node = ox.distance.nearest_nodes(mode_graph, origine[1], origine[0])
            poi_nodes = ox.distance.nearest_nodes(mode_graph, xs, ys)
            try:
                poi_nodes = list(poi_nodes)
            except TypeError:
                poi_nodes = [poi_nodes]

            lengths = nx.single_source_dijkstra_path_length(
                mode_graph,
                origin_node,
                weight="length"
            )
            mode_distances[mode] = [lengths.get(node) for node in poi_nodes]

        for i, geom in enumerate(poi_points):
            destinazione = (geom.y, geom.x)

            decay_walk = decay_bike = decay_drive = decay_bus = None

            for mode in ["walk", "bike", "drive", "bus"]:
                if mode == "bus":
                    try:
                        _, _, imp_bus = route.get_route(
                            grafo,
                            "bus",
                            origine,
                            destinazione,
                            impedance_flag=True,
                            ax=None,
                            distance_only=True
                        )
                    except (nx.NetworkXNoPath, nx.NodeNotFound):
                        imp_bus = None
                    except Exception:
                        imp_bus = None

                    if imp_bus:
                        decay_bus = decay.distance_decay(beta, imp_bus)
                    else:
                        decay_bus = 0.0
                    continue

                dist_m = mode_distances[mode][i]
                if dist_m is None:
                    continue
                imp = get_impedance.impedance_base(dist_m / 1000.0, mode)
                d = decay.distance_decay(beta, imp)
                if mode == "walk":
                    decay_walk = d
                elif mode == "bike":
                    decay_bike = d
                elif mode == "drive":
                    decay_drive = d

            # Calcola RRA solo se HO TUTTO per questo POI
            if None not in (decay_walk, decay_bike, decay_drive, decay_bus):
                rra_poi = decay.calculate_rra(decay_walk, decay_bike, decay_drive, decay_bus)
                RRA.append(rra_poi)

        save_rra(cache_file, RRA)

    RRA_desc = sorted(RRA, reverse=True)

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
