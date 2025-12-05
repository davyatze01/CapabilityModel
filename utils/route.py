import requests
import json
import polyline
import osmnx as ox
import networkx as nx
from datetime import datetime
import matplotlib.pyplot as plt
from utils import get_impedance
from osmnx.routing import route_to_gdf

def format_time(timestamp_ms):
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%H:%M")

def get_route(graph, network_type, origin, destination, impedance_flag = False):
    # URL del server OTP2
    OTP_URL = "http://localhost:8080/otp/routers/default/index/graphql"

    # Data/ora da usare per la pianificazione (⚠︎ FORMATO CORRETTO)
    ROUTE_DATE = "2025-11-12"      # YYYY-MM-DD
    ROUTE_TIME = "19:30:00"        # hh:mm:ss

    if network_type == 'bus':
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
        try:
            # Esegui la richiesta al server OTP
            resp = requests.post(
                OTP_URL,
                headers={"Content-Type": "application/json"},
                data=json.dumps({"query": query})
            )
        except Exception:
            print("Non hai avviato correttamente il server di Open Trip Planner...")

        # Se e' tutto ok procedi
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

        # Unisci tutte le polilinee OTP in un’unica lista di coordinate (lat, lon)
        full_route_coords = []
        # Array che contiene tutte le distanze di ogni percorso in pullman e attesa prima di prenderlo
        full_route_distance = []
        full_route_waiting = []

        for leg in itinerary["legs"]:
            geometry = leg["legGeometry"]["points"]
            coords = polyline.decode(geometry)
            full_route_coords.extend(coords)

        #print("GEOMETRIA: ", geometry)
        #print("Numero totale di punti nel percorso:", len(full_route_coords))

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
            "CAR": "red",
            "IMPEDANCE": "violet",
        }
        prec_leg = None
        impedance = []
        # 🔹 2️⃣ Disegna ogni tratto (leg) con colore diverso
        for leg in itinerary["legs"]:
            geometry = leg["legGeometry"]["points"]
            coords = polyline.decode(geometry)
            lats, lons = zip(*coords)

            mode = leg["mode"]
            color = mode_colors.get(mode, "white")
            departure_t = format_time(leg.get("startTime", ""))
            arrive_t = format_time(leg.get("endTime", ""))
            leg_distance = leg["distance"] / 1000
            

            #print("Leg distance =", leg_distance)

            if impedance_flag and not(leg.get("route")):
                impedance.append(get_impedance.impedance_base(leg_distance,"walk"))

            # 🔸 Costruisci label più informativa
            label = ""
            if leg.get("route"):
                route = leg["route"]
                short = route.get("shortName", "")
                long = route.get("longName", "")

                waiting_time = None
                if prec_leg != None:
                    waiting_time = (leg["startTime"] - prec_leg)/60000
                else:
                    waiting_time = 0

                #Calcolo impedance in caso di bus
                impedance.append(get_impedance.impedance_bus(leg_distance,waiting_time))

                if short or long:
                    label += f"{short} Partenza: {departure_t} - Arrivo: {arrive_t}"
            else:
                label += mode + f" Partenza {departure_t} - Arrivo: {arrive_t}"
            ax.plot(
                lons,
                lats,
                color=color,
                linewidth=3,
                label=label,
                zorder=5
            )
            prec_leg = leg["endTime"]

        if impedance_flag:
            imp_label = "Impedance:\n" + "\n".join([f"{i}: {imp}" for i, imp in enumerate(impedance)])
            ax.scatter([], [], color="violet", label=imp_label)

        # 🔹 3️⃣ Aggiungi marker per partenza e arrivo
        ax.scatter(origin[1], origin[0], c='lime', s=100, marker='o', label='Origine', zorder=6)
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
        #print("DISTANCE = ", full_route_distance)
        #print("WAITING = ", full_route_waiting)
        fig.canvas.manager.set_window_title(f"{network_type}")

        plt.show()

        return impedance
            
    else:
        # Recupero punto mediano tra origine e destinazione e scarico la rete
        center_point = (
            (origin[0] + destination[0]) / 2,
            (origin[1] + destination[1]) / 2
        )

        print("Scarico grafo...")
        network_graph = ox.graph_from_point(center_point, dist=10000,network_type=network_type)
        print("Grafo scaricato!")

        origin_node = ox.distance.nearest_nodes(network_graph, origin[1], origin[0])
        destination_node = ox.distance.nearest_nodes(network_graph, destination[1], destination[0])

        # Usando il metodo 'shortest_path()' so trova la path piu breve basato sulla distanza tra origine e destinazione con dijkstra
        route = nx.shortest_path(network_graph, origin_node, destination_node, weight='length', method='dijkstra')

        mode_colors = {
            "walk": "cyan",
            "drive": "red",
            "bike": "yellow",
            "IMPEDANCE": "violet",
        }

        color = mode_colors.get(network_type, "white")


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
            route_color=color,
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

        if impedance_flag:
            # Ottieni GeoDataFrame del percorso
            gdf = route_to_gdf(network_graph, route)
            distance = gdf["length"].sum() / 1000

            imp_value = get_impedance.impedance_base(distance, network_type)
            # Aggiungi voce invisibile in legenda
            ax.scatter([], [], c='violet', label=f"Impedance: {imp_value}", marker='s')

        ax.legend(facecolor='black', labelcolor='white', loc="lower right")

        fig.canvas.manager.set_window_title(f"{network_type}")
        plt.show()

        if impedance_flag:
            return imp_value
        
    