import osmnx as ox
import os
import networkx as nx
import pandas as pd
import shutup
import matplotlib.pyplot as plt
import requests
import json
import polyline

shutup.please()

# Nomi file

PLACE_NAME = "Cagliari, Sardinia, Italy"
NAME_FILE = "graph/cagliari.graphml"

# URL del server OTP2
OTP_URL = "http://localhost:8080/otp/routers/default/index/graphql"


# Se esiste lo carico altrimenti creo e salvo

if os.path.exists(NAME_FILE):
    graph = ox.io.load_graphml(NAME_FILE)

else:
    graph = ox.graph_from_place(PLACE_NAME)
    ox.io.save_graphml(graph,filepath = NAME_FILE)

# Genero la matrice del grafo
matrix_graph = nx.adjacency_matrix(graph)

# Recupero i POI (Point Of Interest) del luogo
poi = ox.features_from_place(
    PLACE_NAME,
    {
        'amenity' : True
    }
)

restaurant = ox.features_from_place(
    PLACE_NAME,
    {
        'amenity' : 'restaurant'
    }
)

print(restaurant["name"])
# Seleziono un POI casuale per calcolare 3 tipi di percorso
# 1. WALK
# 2. DRIVE
# 3. BUS

# Punto di partenza casuale: lat: 39.22294897283518, lon: 9.114625009108789

first_place = (39.22294897283518, 9.114625009108789)
destination_poi = "Niu Nervi"
destination = ox.geocode(destination_poi)
print(destination)

# Scelgo rete da utilizzare
network_t = 'drive'

if network_t == 'bus':

    query = f"""
    query {{
        plan(
            from: {{ lat: {first_place[0]}, lon: {first_place[1]} }}
            to: {{ lat: {destination[0]}, lon: {destination[1]} }}
            transportModes: [
                {{ mode: WALK }}
                {{ mode: BUS }}
            ]
            walkReluctance: 2.0
            walkSpeed: 1.3
            numItineraries: 3
            date: "2025-11-09T12:15:00+01:00"
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
        print("Errore nella richiesta:", resp.status_code)
        print(resp.text)
        exit()

    data = resp.json()

    # Estrai gli itinerari
    itineraries = data.get("data", {}).get("plan", {}).get("itineraries", [])
    if not itineraries:
        print("Nessun itinerario trovato.")
        exit()

    # 🔹 Seleziona l’itinerario più veloce
    itinerary = min(itineraries, key=lambda i: i["duration"])

    print(f"Itinerario selezionato: durata {itinerary['duration']} s, camminata {itinerary['walkDistance']} m")

    # Unisci tutte le polilinee OTP in un’unica lista di coordinate (lat, lon)
    full_route_coords = []
    for leg in itinerary["legs"]:
        geometry = leg["legGeometry"]["points"]
        coords = polyline.decode(geometry)
        full_route_coords.extend(coords)

    print("GEOMETRIA: ", geometry)
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

    # 🔹 2️⃣ Disegna ogni tratto (leg) con colore diverso
    for leg in itinerary["legs"]:
        geometry = leg["legGeometry"]["points"]
        coords = polyline.decode(geometry)
        lats, lons = zip(*coords)

        mode = leg["mode"]
        color = mode_colors.get(mode, "white")

        # 🔸 Costruisci label più informativa
        label = mode
        if leg.get("route"):
            route = leg["route"]
            short = route.get("shortName", "")
            long = route.get("longName", "")
            if short or long:
                label += f" ({short} - {long})"

        ax.plot(
            lons,
            lats,
            color=color,
            linewidth=3,
            label=label,
            zorder=5
        )

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

    plt.show()
else:
    # Recupero punto mediano tra origine e destinazione e scarico la rete
    center_point = (
        (first_place[0] + destination[0]) / 2,
        (first_place[1] + destination[1]) / 2
    )

    network_graph = ox.graph_from_point(center_point, dist=1000,network_type=network_t)

    origin_node = ox.distance.nearest_nodes(network_graph, first_place[1], first_place[0])
    destination_node = ox.distance.nearest_nodes(network_graph, destination[1], destination[0])

    # Usando il metodo 'shortest_path()' so trova la path piu breve basato sulla distanza tra origine e destinazione con dijkstra
    route = nx.shortest_path(network_graph, origin_node, destination_node, weight='length', method='dijkstra')

    # Faccio il plot
    # Plotta tutto il grafo
    fig, ax = ox.plot_graph(
        network_graph,
        bgcolor='black',
        edge_color='white',
        node_size=0,
        edge_linewidth=0.6,
        show=False,
        close=False
    )

    # Poi traccia il percorso
    fig, ax = ox.plot_graph_route(
        network_graph,
        route,
        route_color='yellow',
        route_linewidth=3,
        ax=ax,
        show=False,
        close=False
    )

    # Recupera coordinate dei nodi origine e destinazione
    origin_x, origin_y = network_graph.nodes[origin_node]['x'], network_graph.nodes[origin_node]['y']
    dest_x, dest_y     = network_graph.nodes[destination_node]['x'], network_graph.nodes[destination_node]['y']

    # Aggiungi marker personalizzati con matplotlib directly
    ax.scatter(origin_x, origin_y, c='lime', s=100, marker='o', label='Origine', zorder=5)
    ax.scatter(dest_x, dest_y,     c='red',  s=100, marker='o', label='Destinazione', zorder=5)

    ax.legend(facecolor='black', labelcolor='white')
    plt.show()
