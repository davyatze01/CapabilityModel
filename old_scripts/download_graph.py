import osmnx as ox
import os
import networkx as nx
import pandas as pd
import shutup
import matplotlib.pyplot as plt

shutup.please()

# Nomi file

PLACE_NAME = "Cagliari, Sardinia, Italy"
NAME_FILE = "cagliari.graphml"

# Se esiste lo carico altrimenti creo e salvo

if os.path.exists(NAME_FILE):
    graph = ox.io.load_graphml(NAME_FILE)

else:
    graph = ox.graph_from_place(PLACE_NAME)
    ox.io.save_graphml(graph,filepath = NAME_FILE)

figure, ax = ox.plot_graph(graph)

# Genero matrice del grafo

m = nx.adjacency_matrix(graph)

# Stampo matrice
print(m.shape[0])

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

# Stampo la quantità di POI di ogni categoria

len(poi)
pd.set_option('display.max_rows',None)
#print(poi["amenity"])


print(restaurant['name'])


#print(poi["amenity"].value_counts())

# Seleziono due POI ristoranti per visualizzarne il miglior percorso

first_place_name = "Namastè"
snd_place_name = "La Balena"

# Li trasformo in geocode

first_place_geocode = ox.geocode(first_place_name)
snd_place_geocode = ox.geocode(snd_place_name)

# Prendo il punto centrale e tramite 'graph from point' vado a prender tutte le strade percorribili in 1500 metri dalla
# posizione

center_point = (
    (first_place_geocode[0] + snd_place_geocode[0]) / 2,
    (first_place_geocode[1] + snd_place_geocode[1]) / 2
)

grafo_distanza = ox.graph_from_point(center_point, dist=1000, network_type='walk')

""" ox.plot_graph(
    grafo_distanza,
    bgcolor='yellow',
    edge_color='black',
    node_color = 'black',
    edge_linewidth=0.6,
    node_size=50,
    figsize=(10, 10)
) """

nodo_origine = ox.distance.nearest_nodes(grafo_distanza, first_place_geocode[1], first_place_geocode[0])
nodo_destinazione = ox.distance.nearest_nodes(grafo_distanza, snd_place_geocode[1], snd_place_geocode[0])

fig, ax = ox.plot_graph(
    grafo_distanza,
    bgcolor="black",
    edge_color="white",
    edge_linewidth=0.6,
    node_size=10,
    show=False,
    close=False
)

print(grafo_distanza.nodes[nodo_origine]['x'])

# Get (x, y) coordinates of the nodes
origin_xy = (grafo_distanza.nodes[nodo_origine]['x'], graph.nodes[nodo_origine]['y'])
destination_xy = (grafo_distanza.nodes[nodo_destinazione]['x'], graph.nodes[nodo_destinazione]['y'])

# Plot origin (in red) and destination (in lime)
ax.scatter(*origin_xy, s=80, c='red', label='Origin', zorder=3)
ax.scatter(*destination_xy, s=80, c='green', label='Destination', zorder=3)

# Optional: add legend and title
ax.legend(facecolor='white')
plt.title("Graph with Origin (red) and Destination (green) nodes")
#plt.show()

# Usando il metodo 'shortest_path()' so trova la path piu breve basato sulla distanza tra origine e destinazione con dijkstra
route = nx.shortest_path(grafo_distanza, nodo_origine, nodo_destinazione, weight='length', method='dijkstra')
route_length_m = nx.shortest_path_length(grafo_distanza, nodo_origine, nodo_destinazione, weight='length', method='dijkstra')
print(f"Shortest road distance: {route_length_m/1000:.2f} km")

ox.plot_graph_route(grafo_distanza, route, bgcolor='black', edge_color='white', route_color='yellow', node_size=0, edge_linewidth=0.6, route_linewidth=3)