import os
import json
import pickle
import hashlib
import requests
import polyline
import osmnx as ox
import networkx as nx
from datetime import datetime
import matplotlib.pyplot as plt
from osmnx.routing import route_to_gdf

from utils import get_impedance, graphml


# =========================
# Cache grafi (RAM + Disco)
# =========================
_GRAPH_CACHE = {}  # cache in memoria per il grafo osm
_MODE_GRAPH_CACHE = {}  # cache in memoria per grafi full-area per modalitÃ 
_ROUTE_CACHE = {}  # cache in memoria per distanze/impeance
_ROUTE_GEOM_CACHE = {}  # cache in memoria per geometrie
_ROUTE_CACHE_FOLDER = "route_cache"
_ROUTE_COORD_ROUND = 6  # arrotondamento coordinate per chiave cache

# Data/ora per pianificazione (OTP)
ROUTE_DATE = "2025-11-12"   # YYYY-MM-DD
ROUTE_TIME = "19:30:00"     # hh:mm:ss


def _round_coord(coord):
    # arrotonda coordinate per occupare meno spazio in memoria
    return (round(coord[0], _ROUTE_COORD_ROUND), round(coord[1], _ROUTE_COORD_ROUND))


def _route_cache_key(
    network_type,
    origin,
    destination,
    route_date=None,
    route_time=None,
    dist=None,
    center_point=None,
):
    # chiave minima per identificare un routing univoco -> questo è l'indice utilizzato in cache per recuperare i dati relativi a un routing già eseguito
    origin_key = _round_coord(origin)
    dest_key = _round_coord(destination)
    center_key = _round_coord(center_point) if center_point else None
    return (
        network_type,
        origin_key,
        dest_key,
        route_date,
        route_time,
        dist,
        center_key,
    )


def _route_cache_path(key, suffix):
    # crea il path su disco per il file che conterrà il routing. Il suffisso differenzia tra distanza ("dist") e geometria ("geom")
    os.makedirs(_ROUTE_CACHE_FOLDER, exist_ok=True)
    key_bytes = pickle.dumps(key)
    key_hash = hashlib.sha1(key_bytes).hexdigest() # hash per evitare nomi file troppo lunghi
    return os.path.join(_ROUTE_CACHE_FOLDER, f"{key_hash}.{suffix}.pkl") #ad esempio: route_cache/abc123.dist.pkl


def _route_cache_get(key):
    # cerca se il dato è già stato caricato e quindi si trova nel dictionary di cache in RAM, oppure sul file corrispondente su disco
    if key in _ROUTE_CACHE:
        return _ROUTE_CACHE[key]
    path = _route_cache_path(key, "dist")
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            _ROUTE_CACHE[key] = data
            return data
        except Exception:
            return None
    return None


def _route_cache_set(key, value):
    # si occupa di salvare il dato sia in RAM (dict di esecuzione) che su un file pickle su disco
    _ROUTE_CACHE[key] = value
    path = _route_cache_path(key, "dist")
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp_path, path)


def _route_geom_cache_get(key):
    # Cerca se la geometria del percorso breve (solo bus) è gia in cache (RAM) oppure la carica sul file corrispondente su disco.
    if key in _ROUTE_GEOM_CACHE:
        return _ROUTE_GEOM_CACHE[key]
    path = _route_cache_path(key, "geom")
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            _ROUTE_GEOM_CACHE[key] = data
            return data
        except Exception:
            return None
    return None


def _route_geom_cache_set(key, value):
    # Quando viene eseguito un bus routing, la geometria viene salvata in RAM e su disco (scrittura atomica)
    _ROUTE_GEOM_CACHE[key] = value
    path = _route_cache_path(key, "geom")
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp_path, path)


def bus_cache_exists(origin, destination, route_date=None, route_time=None):
    # verifica se cache bus (distanza + geometria) è già disponibile
    cache_key = _route_cache_key(
        "bus",
        origin,
        destination,
        route_date=route_date or ROUTE_DATE,
        route_time=route_time or ROUTE_TIME,
    )
    dist_cached = _route_cache_get(cache_key) is not None
    geom_cached = _route_geom_cache_get(cache_key) is not None
    return dist_cached and geom_cached


def _format_time(timestamp_ms):
    """OTP ritorna epoch in millisecondi."""
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%H:%M")


def _get_mode_graph(network_type):
    # carica un grafo full-area per modalita e salva in RAM
    if network_type not in _MODE_GRAPH_CACHE:
        _MODE_GRAPH_CACHE[network_type] = graphml.get_mode_graph(network_type)
    return _MODE_GRAPH_CACHE[network_type]



def get_route(
    graph,
    network_type,
    origin,
    destination,
    impedance_flag=False,
    ax=None,
    distance_only=False,
    return_geometry=False,
    quiet=False,
):
    """
    origin/destination: tuple (lat, lon)

    network_type:
      - 'bus' usa OTP2
      - 'walk' / 'drive' / 'bike' usa OSMnx + NetworkX (con cache per non riscaricare)

    distance_only:
      - True: non disegna nulla, ritorna solo impedenza/distanza

    return_geometry:
      - True: ritorna anche la geometria della strada (gdf) quando disponibile
    """

    # URL del server OTP2
    OTP_URL = "http://localhost:8080/otp/routers/default/index/graphql"

    fig = None
    if not distance_only:
        # Se ax non è creato lo creo
        if ax is None:
            fig, ax = plt.subplots(figsize=(8, 8))
        else:
            fig = ax.figure

    # ==========================
    # CASO BUS (OTP2)
    # ==========================
    if network_type == "bus":
        # cache solo per richieste bus distance_only
        cache_key = _route_cache_key(
            network_type,
            origin,
            destination,
            route_date=ROUTE_DATE,
            route_time=ROUTE_TIME,
        )
        cached = _route_cache_get(cache_key)
        if cached is not None and distance_only:
            cached_value = cached.get("result_value")
            if return_geometry:
                return fig, ax, cached_value, None
            return fig, ax, cached_value

        query = f"""
        query {{
          plan(
            from: {{ lat: {origin[0]}, lon: {origin[1]} }}
            to: {{ lat: {destination[0]}, lon: {destination[1]} }}
            date: "{ROUTE_DATE}"
            time: "{ROUTE_TIME}"
            transportModes: [
              {{ mode: WALK }}
              {{ mode: BUS }}
            ]
            walkReluctance: 2.0
            walkSpeed: 1.3
            numItineraries: 3
            boardSlack: 180
          ) {{
            itineraries {{
              duration
              walkDistance
              legs {{
                mode
                startTime
                endTime
                from {{ name lat lon }}
                to   {{ name lat lon }}
                route {{ shortName longName }}
                distance
                legGeometry {{ points }}
              }}
            }}
          }}
        }}
        """

        try:
            resp = requests.post(
                OTP_URL,
                headers={"Content-Type": "application/json"},
                data=json.dumps({"query": query}),
                timeout=60
            )
        except Exception as e:
            print("Non hai avviato correttamente OpenTripPlanner oppure OTP non è raggiungibile.")
            print("Dettaglio:", e)
            return fig, ax, ([] if impedance_flag else None)

        if resp.status_code != 200:
            if not quiet:
                print("Errore nella richiesta OTP:", resp.status_code)
                print(resp.text)
            return fig, ax, ([] if impedance_flag else None)

        try:
            data = resp.json()
        except Exception:
            print("OTP ha risposto ma la risposta non è JSON valido.")
            print(resp.text)
            return fig, ax, ([] if impedance_flag else None)

        itineraries = data.get("data", {}).get("plan", {}).get("itineraries", [])
        if not itineraries:
            if not quiet:
                print("Nessun itinerario trovato (OTP).")
            return fig, ax, ([] if impedance_flag else None)

        # itinerario più veloce
        itinerary = min(itineraries, key=lambda i: i["duration"])
        total_distance_m = sum(leg.get("distance", 0) for leg in itinerary["legs"])

        if not distance_only:
            # sfondo: grafo passato in input (es. il tuo graphml di Cagliari)
            ox.plot_graph(
                graph,
                ax=ax,
                bgcolor="black",
                edge_color="black",
                node_size=0,
                edge_linewidth=0.5,
                show=False,
                close=False
            )

        mode_colors = {
            "WALK": "cyan",
            "BUS": "orange",
            "TRAM": "cyan",
            "RAIL": "blue",
            "CAR": "red",
            "IMPEDANCE": "violet",
        }

        prec_leg_end_time = None
        impedance = []
        bus_geom = []

        for leg in itinerary["legs"]:
            geometry = leg["legGeometry"]["points"]
            coords = polyline.decode(geometry)  # [(lat, lon), ...]
            bus_geom.append(coords)
            lats, lons = zip(*coords)

            mode = leg["mode"]
            color = mode_colors.get(mode, "white")

            departure_t = _format_time(leg.get("startTime", 0))
            arrive_t = _format_time(leg.get("endTime", 0))
            leg_distance_km = leg["distance"] / 1000.0

            # impedance WALK (leg senza route)
            if impedance_flag and not leg.get("route"):
                impedance.append(get_impedance.impedance_base(leg_distance_km, "walk"))

            label = ""
            if leg.get("route"):
                route_info = leg["route"]
                short = route_info.get("shortName", "")
                long = route_info.get("longName", "")

                # waiting time tra mezzi (minuti)
                if prec_leg_end_time is None:
                    waiting_time_min = 0
                else:
                    waiting_time_min = (leg["startTime"] - prec_leg_end_time) / 60000.0

                if impedance_flag:
                    impedance.append(get_impedance.impedance_bus(leg_distance_km, waiting_time_min))

                if short or long:
                    label = f"{short} Partenza: {departure_t} - Arrivo: {arrive_t}"
                else:
                    label = f"BUS Partenza: {departure_t} - Arrivo: {arrive_t}"
            else:
                label = f"{mode} Partenza: {departure_t} - Arrivo: {arrive_t}"

            if not distance_only:
                ax.plot(lons, lats, color=color, linewidth=3, label=label, zorder=5)
            prec_leg_end_time = leg["endTime"]

        if impedance_flag and not distance_only:
            imp_label = "Impedance:\n" + "\n".join([f"{i}: {imp:.3f}" if isinstance(imp, (int, float)) else f"{i}: {imp}"
                                                    for i, imp in enumerate(impedance)])
            ax.scatter([], [], color="violet", label=imp_label)

        if not distance_only:
            ax.scatter(origin[1], origin[0], c="lime", s=100, marker="o", label="Origine", zorder=6)
            ax.scatter(destination[1], destination[0], c="red", s=100, marker="o", label="Destinazione", zorder=6)

            handles, labels = ax.get_legend_handles_labels()
            by_label = dict(zip(labels, handles))
            ax.legend(
                by_label.values(),
                by_label.keys(),
                facecolor="black",
                labelcolor="white",
                loc="lower left",
                fontsize=7,
                framealpha=0.9
            )

            ax.set_title("bus", color="white")

        result_value = impedance if impedance_flag else (total_distance_m if distance_only else None)
        # salva sempre geometria bus per riuso futuro
        _route_geom_cache_set(cache_key, bus_geom)
        if distance_only:
            # salva risultato bus per riuso futuro
            _route_cache_set(cache_key, {"result_value": result_value})
        if return_geometry:
            return fig, ax, result_value, bus_geom
        return fig, ax, result_value

    # ==========================
    # CASO WALK/DRIVE/BIKE
    # ==========================
    # usa sempre il grafo full-area per la modalita richiesta
    center_point = None
    dist = None
    network_graph = _get_mode_graph(network_type)

    # colori per il plotting
    mode_colors = {
        "walk": "cyan",
        "drive": "red",
        "bike": "yellow",
        "IMPEDANCE": "violet",
    }
    color = mode_colors.get(network_type, "white")

    # chiave cache per walk/bike/drive
    cache_key = _route_cache_key(
        network_type,
        origin,
        destination,
        dist=dist,
        center_point=center_point,
    )
    # tenta cache del routing già calcolato
    cached = _route_cache_get(cache_key)
    if cached is not None:
        route_nodes = cached.get("route_nodes")
        distance_m = cached.get("distance_m")
        imp_value = cached.get("imp_value")
        if return_geometry:
            route_gdf = None
            if route_nodes is not None:
                route_gdf = route_to_gdf(network_graph, route_nodes)
            result_value = imp_value if impedance_flag else (distance_m if distance_only else None)
            return fig, ax, result_value, route_gdf
        if not distance_only and route_nodes is not None:
            # in caso di cache hit, plottiamo direttamente la route salvata
            # plot grafo + route
            ox.plot_graph(
                network_graph,
                ax=ax,
                bgcolor="black",
                edge_color="black",
                node_size=0,
                edge_linewidth=0.6,
                show=False,
                close=False
            )
            ox.plot_graph_route(
                network_graph,
                route_nodes,
                ax=ax,
                route_color=color,
                route_linewidth=3,
                show=False,
                close=False
            )
        result_value = imp_value if impedance_flag else (distance_m if distance_only else None)
        return fig, ax, result_value

    origin_node = ox.distance.nearest_nodes(network_graph, origin[1], origin[0])
    destination_node = ox.distance.nearest_nodes(network_graph, destination[1], destination[0])

    route_nodes = None
    route_gdf = None
    distance_m = None

    if distance_only:
        distance_m = nx.shortest_path_length(
            network_graph,
            origin_node,
            destination_node,
            weight="length",
            method="dijkstra"
        )
        if return_geometry:
            route_nodes = nx.shortest_path(
                network_graph,
                origin_node,
                destination_node,
                weight="length",
                method="dijkstra"
            )
            route_gdf = route_to_gdf(network_graph, route_nodes)
    else:
        route_nodes = nx.shortest_path(
            network_graph,
            origin_node,
            destination_node,
            weight="length",
            method="dijkstra"
        )

    if not distance_only:
        # plot grafo + route
        ox.plot_graph(
            network_graph,
            ax=ax,
            bgcolor="black",
            edge_color="black",
            node_size=0,
            edge_linewidth=0.6,
            show=False,
            close=False
        )

        ox.plot_graph_route(
            network_graph,
            route_nodes,
            ax=ax,
            route_color=color,
            route_linewidth=3,
            show=False,
            close=False
        )

        # marker origine/destinazione
        origin_x, origin_y = network_graph.nodes[origin_node]["x"], network_graph.nodes[origin_node]["y"]
        dest_x, dest_y = network_graph.nodes[destination_node]["x"], network_graph.nodes[destination_node]["y"]

        ax.scatter(origin_x, origin_y, c="lime", s=100, marker="o", label="Origine", zorder=5)
        ax.scatter(dest_x, dest_y, c="red", s=100, marker="o", label="Destinazione", zorder=5)

    imp_value = None
    if impedance_flag:
        if distance_m is None:
            gdf = route_to_gdf(network_graph, route_nodes)
            distance_km = gdf["length"].sum() / 1000.0
        else:
            distance_km = distance_m / 1000.0
        imp_value = get_impedance.impedance_base(distance_km, network_type)
        if not distance_only:
            ax.scatter([], [], c="violet", label=f"Impedance: {imp_value}", marker="s")

    if not distance_only:
        ax.legend(facecolor="black", labelcolor="white", loc="lower right", fontsize=8, framealpha=0.9)
        ax.set_title(network_type, color="white")

    # valore finale del routing (impedenza o distanza)
    result_value = imp_value if impedance_flag else (distance_m if distance_only else None)
    # salva distanza/nodi/impedenza per riuso futuro
    _route_cache_set(cache_key, {
        "distance_m": distance_m,
        "route_nodes": route_nodes,
        "imp_value": imp_value,
    })

    if return_geometry:
        if route_gdf is None and route_nodes is not None:
            route_gdf = route_to_gdf(network_graph, route_nodes)
        return fig, ax, result_value, route_gdf
    return fig, ax, result_value
