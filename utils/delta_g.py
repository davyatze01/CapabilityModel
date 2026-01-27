from tqdm import tqdm
from utils import graphml, route, decay, get_impedance
import math
import os
import pickle
import networkx as nx
import osmnx as ox
import time
import numpy as np 

# Configurazione base per la cache su disco
CACHE_VERSION = 1
CACHE_FOLDER = "rra_cache"
os.makedirs(CACHE_FOLDER, exist_ok=True)

# Variabili globali per mantenere i dati in memoria (RAM) ed evitare caricamenti ripetuti
_G_CACHE = None
_POI_GEOM_CACHE = {}  
_MODE_GRAPH_CACHE = {} 


def _resolve_feature(poi_type, feature):
    # Determina la categoria corretta per OpenStreetMap (es. 'amenity' o 'healthcare')
    if feature is None:
        if poi_type in {"healthcare"}:
            return poi_type, True
        return "amenity", poi_type
    return feature, poi_type


def _haversine_m(lat1, lon1, lat2, lon2):
    # Calcola la distanza in metri tra due coordinate geografiche (formula dell'emisenoverso)
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

def _get_mode_graph(base_graph, origin, network_type, radius_m):
    # Recupera il grafo stradale specifico per il mezzo di trasporto (piedi, bici, auto)
    # Se esiste già in cache lo restituisce, altrimenti lo calcola.
    if route._can_use_base_graph(base_graph, network_type):
        return base_graph
    if radius_m is None:
        if network_type not in _MODE_GRAPH_CACHE:
            _MODE_GRAPH_CACHE[network_type] = graphml.get_mode_graph(network_type)
        return _MODE_GRAPH_CACHE[network_type]
    dist = max(500, int(radius_m))
    return route._get_cached_graph(origin, dist, network_type)

def precompute_distances(origin, radius_m=None):
    # Questa funzione è il cuore dell'ottimizzazione: calcola le distanze stradali
    # (Dijkstra) per walk/bike/drive UNA volta sola per il nodo di origine.
    # I risultati vengono poi riutilizzati per tutti i tipi di POI (ristoranti, bar, ecc.)
    global _G_CACHE
    if _G_CACHE is None:
        _G_CACHE = graphml.get_graph()
    grafo = _G_CACHE
    
    network_cache = {}
    
    for mode in ["walk", "bike", "drive"]:
        try:
            mode_graph = _get_mode_graph(grafo, origin, mode, radius_m)
            origin_node = ox.distance.nearest_nodes(mode_graph, origin[1], origin[0])
            
            # Calcola la distanza da 'origin_node' verso TUTTI gli altri nodi del grafo
            lengths = nx.single_source_dijkstra_path_length(
                mode_graph, origin_node, weight="length"
            )
            network_cache[mode] = {"graph": mode_graph, "lengths": lengths}
        except Exception as e:
            # Se un grafo non è disponibile (es. errore di rete o grafo disconnesso), proseguiamo senza
            network_cache[mode] = {"graph": None, "lengths": {}}
            
    return network_cache

def preload_all_pois(poi_list):
    # Scarica preventivamente tutti i Punti di Interesse (POI) richiesti.
    # È fondamentale chiamarla nel main prima di avviare i processi paralleli
    # per evitare che ogni worker cerchi di scaricare dati simultaneamente.
    global _POI_GEOM_CACHE
    print("Pre-loading POI geometries...")
    for poi_type in poi_list:
        feature, value = _resolve_feature(poi_type, None)
        cache_key = (feature, value)
        if cache_key not in _POI_GEOM_CACHE:
            try:
                print(f"  Fetching POI: {poi_type} ({feature}={value})...")
                poi = graphml.get_poi(feature, value)
                _POI_GEOM_CACHE[cache_key] = graphml.get_poi_geometries(poi)
            except Exception as e:
                print(f"  Error fetching {poi_type}: {e}")
                _POI_GEOM_CACHE[cache_key] = []
    print("POI pre-loading complete.")
    return _POI_GEOM_CACHE


def accessibility(poi_type, origine, feature=None, radius_m=None, network_cache=None):
    # Calcola l'indice di accessibilità per un dato tipo di POI partendo dall'origine.
    feature, value = _resolve_feature(poi_type, feature)

    # Crea un nome file univoco per la cache basato su posizione e tipo di POI
    lat_key = f"{origine[0]:.6f}"
    lon_key = f"{origine[1]:.6f}"
    radius_key = "all" if radius_m is None else f"r{int(radius_m)}"

    cache_file = os.path.join(
        CACHE_FOLDER,
        f"RRA_v{CACHE_VERSION}_{feature}_{poi_type}_{radius_key}_{lat_key}_{lon_key}.pkl"
    )

    RRA = []

    # Controlla se abbiamo già calcolato questo risultato in passato
    if os.path.exists(cache_file):
        RRA = load_rra(cache_file)
    else:
        # Se non è in cache, procediamo al calcolo
        global _G_CACHE, _POI_GEOM_CACHE

        if _G_CACHE is None:
            _G_CACHE = graphml.get_graph()
        grafo = _G_CACHE

        cache_key = (feature, value)
        
        # Recupera i POI dalla memoria o li scarica se mancano
        if cache_key not in _POI_GEOM_CACHE:
            poi = graphml.get_poi(feature, value)
            _POI_GEOM_CACHE[cache_key] = graphml.get_poi_geometries(poi)

        poi_geometry = _POI_GEOM_CACHE[cache_key]

        # Converte le geometrie in punti semplici
        poi_points = []
        for geom in poi_geometry:
            if geom is None: continue
            if geom.geom_type != "Point":
                geom = geom.representative_point()
            poi_points.append(geom)

        # Filtra i POI entro il raggio specificato (se presente)
        if radius_m is not None:
            poi_points = [
                geom for geom in poi_points
                if _haversine_m(origine[0], origine[1], geom.y, geom.x) <= radius_m
            ]

        # Se non ci sono POI vicini, l'accessibilità è 0
        if not poi_points:
            save_rra(cache_file, [])
            return 0

        beta = math.log(2) / 20.0
        mode_distances = {}
        xs = [geom.x for geom in poi_points]
        ys = [geom.y for geom in poi_points]

        # Fase 1: Otteniamo le distanze stradali per piedi, bici e auto.
        # Se 'network_cache' è fornita (dalla funzione precompute_distances), usiamo quella
        # per evitare calcoli ridondanti, altrimenti calcoliamo da zero.
        for mode in ["walk", "bike", "drive"]:
            lengths = None
            mode_graph = None
            
            if network_cache and mode in network_cache and network_cache[mode]["graph"] is not None:
                mode_graph = network_cache[mode]["graph"]
                lengths = network_cache[mode]["lengths"]
            else:
                try:
                    mode_graph = _get_mode_graph(grafo, origine, mode, radius_m)
                    origin_node = ox.distance.nearest_nodes(mode_graph, origine[1], origine[0])
                    lengths = nx.single_source_dijkstra_path_length(mode_graph, origin_node, weight="length")
                except Exception:
                    lengths = {}

            if mode_graph and lengths:
                # Troviamo i nodi della rete stradale più vicini ai POI
                poi_nodes = ox.distance.nearest_nodes(mode_graph, xs, ys)
                
                # Gestiamo il caso in cui osmnx restituisca un array numpy o un singolo intero
                # convertendo tutto in una lista standard di Python
                if hasattr(poi_nodes, 'tolist'):
                    poi_nodes = poi_nodes.tolist()
                elif not isinstance(poi_nodes, list):
                    poi_nodes = [poi_nodes]

                # Estraiamo le distanze pre-calcolate per ogni POI
                mode_distances[mode] = [lengths.get(node) for node in poi_nodes]
            else:
                mode_distances[mode] = [None] * len(poi_points)

        # Fase 2: Calcoliamo l'impedenza (costo di viaggio) per ogni POI e ogni mezzo
        for i, geom in enumerate(poi_points):
            destinazione = (geom.y, geom.x)
            decay_vals = {}

            for mode in ["walk", "bike", "drive", "bus"]:
                # Il BUS richiede un calcolo specifico punto-punto tramite un servizio di routing esterno
                if mode == "bus":
                    imp_bus = None
                    try:
                        _, _, imp_bus = route.get_route(
                            grafo, "bus", origine, destinazione,
                            impedance_flag=True, ax=None, distance_only=True
                        )
                    except Exception:
                        imp_bus = None
                    
                    if imp_bus:
                        decay_vals["bus"] = decay.distance_decay(beta, imp_bus)
                    else:
                        decay_vals["bus"] = 0.0
                    continue

                # Per gli altri mezzi usiamo la distanza stradale che abbiamo già
                dist_m = mode_distances[mode][i]
                if dist_m is not None:
                    imp = get_impedance.impedance_base(dist_m / 1000.0, mode)
                    decay_vals[mode] = decay.distance_decay(beta, imp)
                else:
                    decay_vals[mode] = None

            # Calcoliamo il punteggio RRA finale solo se abbiamo dati validi per i mezzi privati
            if None not in (decay_vals.get("walk"), decay_vals.get("bike"), decay_vals.get("drive")):
                rra_poi = decay.calculate_rra(
                    decay_vals["walk"], decay_vals["bike"], decay_vals["drive"], decay_vals["bus"]
                )
                RRA.append(rra_poi)

        # Salviamo i risultati grezzi (lista di punteggi) nella cache su disco
        save_rra(cache_file, RRA)
    
    # Fase 3: Aggregazione finale
    # Trasformiamo la lista di punteggi RRA dei vari POI in un unico numero (indice di accessibilità)
    # usando una funzione di decadimento logaritmico (Choquet/Delta G).
    if not RRA:
        return 0

    RRA_desc = sorted(RRA, reverse=True)
    
    def c_from_target(n_target: int, y_target: float) -> float:
        return round(math.log(1.0 - y_target) / n_target, 2)
    def g(x: int, c: float) -> float:
        return 1.0 - math.exp(c * x)
    def deltag(x: int, x2: int, c: float) -> float:
        return g(x, c) - g(x2, c)

    accessibility_value = 0
    c = c_from_target(n_target=2, y_target=0.6)
    
    for i, element in enumerate(RRA_desc):
        if i == 0: accessibility_value += (element * g(i + 1, c))
        else: accessibility_value += (element * deltag(i + 1, i, c))

    return accessibility_value

def save_rra(path, RRA):
    # Salva i dati usando un file temporaneo per evitare corruzione in caso di crash
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        pickle.dump(RRA, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp_path, path)

def load_rra(path):
    with open(path, "rb") as f:
        return pickle.load(f)