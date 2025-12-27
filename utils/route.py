import os
import json
import requests
import polyline
import osmnx as ox
import networkx as nx
from datetime import datetime
import matplotlib.pyplot as plt
from osmnx.routing import route_to_gdf

from utils import get_impedance


# =========================
# Cache grafi (RAM + Disco)
# =========================
_GRAPH_CACHE = {}  # cache in memoria


def _format_time(timestamp_ms):
    """OTP ritorna epoch in millisecondi."""
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%H:%M")


def _graph_cache_filename(center_point, dist, network_type, folder="graph_cache"):
    os.makedirs(folder, exist_ok=True)
    lat = round(center_point[0], 4)
    lon = round(center_point[1], 4)
    return os.path.join(folder, f"{network_type}_{dist}_{lat}_{lon}.graphml")


def _get_cached_graph(center_point, dist, network_type):
    """
    Restituisce un grafo OSMnx con:
    - cache RAM
    - cache su disco (graph_cache/)
    - download da Overpass solo se necessario
    """
    key = (network_type, dist, round(center_point[0], 4), round(center_point[1], 4))
    if key in _GRAPH_CACHE:
        return _GRAPH_CACHE[key]

    fname = _graph_cache_filename(center_point, dist, network_type)

    if os.path.exists(fname):
        G = ox.load_graphml(fname)
        _GRAPH_CACHE[key] = G
        return G

    # Download solo la prima volta
    G = ox.graph_from_point(center_point, dist=dist, network_type=network_type)
    ox.save_graphml(G, fname)
    _GRAPH_CACHE[key] = G
    return G


def _can_use_base_graph(base_graph, network_type):
    """
    Heuristica: se il grafo base ha network_type compatibile, usalo direttamente.
    Non è perfetta, ma evita download inutili.
    """
    if base_graph is None:
        return False

    # Molti grafi OSMnx hanno info in base_graph.graph
    gtype = None
    try:
        gtype = base_graph.graph.get("network_type", None)
    except Exception:
        gtype = None

    # Se non sappiamo il tipo, lo usiamo comunque per "walk" (caso più comune)
    if gtype is None:
        return network_type == "walk"

    return gtype == network_type


def get_route(graph, network_type, origin, destination, impedance_flag=False, ax=None):
    """
    origin/destination: tuple (lat, lon)

    network_type:
      - 'bus' usa OTP2
      - 'walk' / 'drive' / 'bike' usa OSMnx + NetworkX (con cache per non riscaricare)
    """

    # URL del server OTP2
    OTP_URL = "http://localhost:8080/otp/routers/default/index/graphql"

    # Data/ora per pianificazione (OTP)
    ROUTE_DATE = "2025-11-12"   # YYYY-MM-DD
    ROUTE_TIME = "19:30:00"     # hh:mm:ss

    # Se ax non è creato lo creo
    if ax is None:
        fig, ax = plt.subplots(figsize=(8, 8))
    else:
        fig = ax.figure

    # ==========================
    # CASO BUS (OTP2)
    # ==========================
    if network_type == "bus":
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
            print("Nessun itinerario trovato (OTP).")
            return fig, ax, ([] if impedance_flag else None)

        # itinerario più veloce
        itinerary = min(itineraries, key=lambda i: i["duration"])

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

        for leg in itinerary["legs"]:
            geometry = leg["legGeometry"]["points"]
            coords = polyline.decode(geometry)  # [(lat, lon), ...]
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

            ax.plot(lons, lats, color=color, linewidth=3, label=label, zorder=5)
            prec_leg_end_time = leg["endTime"]

        if impedance_flag:
            imp_label = "Impedance:\n" + "\n".join([f"{i}: {imp:.3f}" if isinstance(imp, (int, float)) else f"{i}: {imp}"
                                                    for i, imp in enumerate(impedance)])
            ax.scatter([], [], color="violet", label=imp_label)

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
        return fig, ax, (impedance if impedance_flag else None)

    # ==========================
    # CASO WALK/DRIVE/BIKE
    # ==========================
    # 1) se il grafo passato è compatibile, lo uso direttamente
    if _can_use_base_graph(graph, network_type):
        network_graph = graph
    else:
        # 2) altrimenti: cache su disco + RAM con center_point
        center_point = ((origin[0] + destination[0]) / 2.0, (origin[1] + destination[1]) / 2.0)

        # dist più piccolo = molto più veloce
        # (se vuoi puoi aumentarlo, ma 10km è spesso troppo)
        dist = 3000

        network_graph = _get_cached_graph(center_point, dist, network_type)

    origin_node = ox.distance.nearest_nodes(network_graph, origin[1], origin[0])
    destination_node = ox.distance.nearest_nodes(network_graph, destination[1], destination[0])

    route_nodes = nx.shortest_path(
        network_graph,
        origin_node,
        destination_node,
        weight="length",
        method="dijkstra"
    )

    mode_colors = {
        "walk": "cyan",
        "drive": "red",
        "bike": "yellow",
        "IMPEDANCE": "violet",
    }
    color = mode_colors.get(network_type, "white")

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
        gdf = route_to_gdf(network_graph, route_nodes)
        distance_km = gdf["length"].sum() / 1000.0
        imp_value = get_impedance.impedance_base(distance_km, network_type)
        ax.scatter([], [], c="violet", label=f"Impedance: {imp_value}", marker="s")

    ax.legend(facecolor="black", labelcolor="white", loc="lower right", fontsize=8, framealpha=0.9)
    ax.set_title(network_type, color="white")

    return fig, ax, imp_value if impedance_flag else None
