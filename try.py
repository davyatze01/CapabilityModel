import osmnx as ox
import os
import networkx as nx
import pandas as pd
import shutup
import matplotlib.pyplot as plt
import requests
import json
import polyline
from datetime import datetime

def format_time(timestamp_ms):
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%H:%M")

shutup.please()

# -----------------------------
# Config
# -----------------------------

PLACE_NAME = "Cagliari, Sardinia, Italy"
NAME_FILE = "graph/cagliari.graphml"

# URL del server OTP2 (GTFS GraphQL API)
OTP_URL = "http://localhost:8080/otp/routers/default/index/graphql"

# Data/ora da usare per la pianificazione (⚠︎ FORMATO CORRETTO)
ROUTE_DATE = "2025-11-12"      # YYYY-MM-DD
ROUTE_TIME = "19:30:00"        # hh:mm:ss

# Punto di partenza e destinazione
first_place = (39.22231439353061, 9.113848879825527)
destination_poi = "Niu Nervi"

# -----------------------------
# Carico / creo grafo OSM
# -----------------------------

if os.path.exists(NAME_FILE):
    graph = ox.io.load_graphml(NAME_FILE)
else:
    graph = ox.graph_from_place(PLACE_NAME)
    ox.io.save_graphml(graph, filepath=NAME_FILE)

# Matrice di adiacenza (non usata dopo ma la lascio perché magari ti serve)
matrix_graph = nx.adjacency_matrix(graph)

# Recupero i POI (non strettamente necessario per la route, ma lo lascio)
poi = ox.features_from_place(
    PLACE_NAME,
    {
        'amenity': True
    }
)

# Geocoding destinazione
destination = ox.geocode(destination_poi)
print("DESTINAZIONE GEOCODIFICATA:", destination)

# Scelgo rete da utilizzare
network_t = 'bus'

if network_t == 'bus':
    # -----------------------------
    # QUERY GRAPHQL CORRETTA PER OTP 2.8.x
    # -----------------------------
    query = f"""
    query {{
        plan(
            from: {{ lat: {first_place[0]}, lon: {first_place[1]} }}
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
                    from {{
                        name
                        lat
                        lon
                    }}
                    to {{
                        name
                        lat
                        lon
                    }}
                    route {{
                        shortName
                        longName
                    }}
                    distance
                    legGeometry {{
                        points
                    }}
                }}
            }}
        }}
    }}
    """

    # Esegui la richiesta al server OTP
    resp = requests.post(
        OTP_URL,
        headers={"Content-Type": "application/json"},
        data=json.dumps({"query": query})
    )

    if resp.status_code != 200:
        print("Errore nella richiesta HTTP:", resp.status_code)
        print(resp.text)
        exit()

    data = resp.json()
    # Debug: se OTP torna errori GraphQL li vogliamo vedere
    if "errors" in data:
        print("ERRORI GRAPHQL RITORNATI DA OTP:")
        print(json.dumps(data["errors"], indent=2, ensure_ascii=False))

    plan = data.get("data", {}).get("plan")
    if plan is None:
        print("La chiave 'plan' è None. Controlla gli errori sopra (es. NO_TRANSIT_CONNECTION).")
        exit()

    itineraries = plan.get("itineraries", [])
    if not itineraries:
        print("Nessun itinerario trovato. Probabili cause:")
        print("- Nessun servizio bus alla data/ora specificata")
        print("- GTFS non valido per quella data (feed fuori range)")
        print("- Coordinate troppo lontane dalla rete di trasporto")
        exit()

    # 🔹 Seleziona l’itinerario più veloce
    itinerary = min(itineraries, key=lambda i: i["duration"])

    print(f"Itinerario selezionato: durata {itinerary['duration']} s, camminata {itinerary['walkDistance']} m")

    # Unisci tutte le polilinee OTP in un’unica lista di coordinate (lat, lon)
    full_route_coords = []
    # Array che contiene tutte le distanze di ogni percorso in bus
    # e il tempo di attesa prima di prenderlo
    full_route_distance = []
    full_route_waiting = []

    for leg in itinerary["legs"]:
        geometry = leg["legGeometry"]["points"]
        coords = polyline.decode(geometry)
        full_route_coords.extend(coords)

    print("GEOMETRIA ULTIMA LEG:", geometry)
    print("Numero totale di punti nel percorso:", len(full_route_coords))

    # 🔹 1️⃣ Plotta il grafo di OSM come sfondo
    fig, ax = ox.plot_graph(
        graph,
        bgcolor="black",
        edge_color="white",
        node_size=0,
        edge_linewidth=0.5,
        show=False,
        close=False
    )

    # 🔹 Mappa colori per ogni tipo di trasporto
    mode_colors = {
        "WALK": "cyan",
        "BUS": "orange",
        "TRAM": "cyan",
        "RAIL": "blue",
        "CAR": "red"
    }
    prec_leg_end_time = None

    # 🔹 2️⃣ Disegna ogni tratto (leg) con colore diverso
    for leg in itinerary["legs"]:
        geometry = leg["legGeometry"]["points"]
        coords = polyline.decode(geometry)
        lats, lons = zip(*coords)

        mode = leg["mode"]
        color = mode_colors.get(mode, "white")
        departure_t = format_time(leg.get("startTime", 0))
        arrive_t = format_time(leg.get("endTime", 0))

        waiting_time = None
        label = ""

        # Se è una leg di trasporto pubblico, ha "route"
        if leg.get("route"):
            # distanza del tratto in metri
            if leg.get("distance"):
                full_route_distance.append(leg["distance"])

            # tempo di attesa prima di questo mezzo (in minuti)
            if prec_leg_end_time is not None:
                waiting_time = (leg["startTime"] - prec_leg_end_time) / 60000.0
            else:
                waiting_time = 0.0
            full_route_waiting.append(waiting_time)

            route = leg["route"]
            short = route.get("shortName", "")
            long = route.get("longName", "")

            if short or long:
                label += f"{short} Partenza: {departure_t} - Arrivo: {arrive_t}"
        else:
            # tratto a piedi ecc.
            label += mode + f" Partenza {departure_t} - Arrivo: {arrive_t}"

        ax.plot(
            lons,
            lats,
            color=color,
            linewidth=3,
            label=label,
            zorder=5
        )
        prec_leg_end_time = leg["endTime"]

    # 🔹 3️⃣ Aggiungi marker per partenza e arrivo
    ax.scatter(first_place[1], first_place[0], c='lime', s=100, marker='o', label='Origine', zorder=6)
    ax.scatter(destination[1], destination[0], c='red', s=100, marker='o', label='Destinazione', zorder=6)

    # 🔹 4️⃣ Legenda pulita e leggibile
    handles, labels = ax.get_legend_handles_labels()
    by_label = dict(zip(labels, handles))  # elimina duplicati
    ax.legend(
        by_label.values(),
        by_label.keys(),
        facecolor='black',
        labelcolor='white',
        loc='lower left'
    )

    print("DISTANCE (m) = ", full_route_distance)
    print("WAITING (min) = ", full_route_waiting)
    plt.show()

    #                  DISTANZA DA PUNTO X A Y IN BUS
    # IMPEDANCE BUS =  ------------------------------  + (tempo di attesa alla fermata in ORE)
    #                             10 km/h

    full_route_impedance = []
    BUS_SPEED_KMH = 10

    if len(full_route_distance) == len(full_route_waiting):
        for i, distance_m in enumerate(full_route_distance):
            distance_km = distance_m / 1000.0
            waiting_min = full_route_waiting[i]
            # porto tutto in ore: distanza/velocità (ore) + attesa (min -> ore)
            impedance_hours = (distance_km / BUS_SPEED_KMH) + (waiting_min / 60.0)
            full_route_impedance.append(impedance_hours)

    print("ALL IMPEDANCE (ore):", full_route_impedance)

else:
    # -----------------------------
    # Parte per walk/drive ecc. – invariata
    # -----------------------------
    center_point = (
        (first_place[0] + destination[0]) / 2,
        (first_place[1] + destination[1]) / 2
    )

    print("Scarico grafo...")
    network_graph = ox.graph_from_point(center_point, dist=10000, network_type=network_t)
    print("Grafo scaricato!")

    origin_node = ox.distance.nearest_nodes(network_graph, first_place[1], first_place[0])
    destination_node = ox.distance.nearest_nodes(network_graph, destination[1], destination[0])

    route = nx.shortest_path(network_graph, origin_node, destination_node, weight='length', method='dijkstra')

    fig, ax = ox.plot_graph(
        network_graph,
        bgcolor='black',
        edge_color='white',
        node_size=0,
        edge_linewidth=0.6,
        show=False,
        close=False
    )

    fig, ax = ox.plot_graph_route(
        network_graph,
        route,
        route_color='yellow',
        route_linewidth=3,
        ax=ax,
        show=False,
        close=False
    )

    origin_x, origin_y = network_graph.nodes[origin_node]['x'], network_graph.nodes[origin_node]['y']
    dest_x, dest_y = network_graph.nodes[destination_node]['x'], network_graph.nodes[destination_node]['y']

    ax.scatter(origin_x, origin_y, c='lime', s=100, marker='o', label='Origine', zorder=5)
    ax.scatter(dest_x, dest_y, c='red', s=100, marker='o', label='Destinazione', zorder=5)

    ax.legend(facecolor='black', labelcolor='white')
    plt.show()
