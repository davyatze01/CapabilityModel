import os
import pickle
import pyrosm
import gtfs_kit as gk
import networkx as nx
from shapely.geometry import Point
import folium
import numpy as np

# ----------------------------
# Percorsi file
# ----------------------------
CA_PATH = "pbf_files/cagliari-latest.osmv2.pbf"
GTFS_PATH = "gtfs/GTFS.zip"
GRAPH_BUS_FILE = "graph_bus.pkl"
GRAPH_WALK_FILE = "graph_walk.pkl"

ORIGIN = Point(9.114625009108789, 39.22294897283518)
DESTINATION = Point(9.098612322838331, 39.23998019279815)

# ----------------------------
# Parametri
# ----------------------------
WALK_SPEED = 1.39  # m/s (~5 km/h)
SEARCH_RADIUS = 0.005  # ~500 m in gradi

# ----------------------------
# 0️⃣ Carica GTFS (sempre)
# ----------------------------
gtfs = gk.read_feed(GTFS_PATH, dist_units="km")
stops = gtfs.stops
stop_times = gtfs.stop_times
trips = gtfs.trips

# ----------------------------
# 1️⃣ Grafo pedonale OSM
# ----------------------------
if os.path.exists(GRAPH_WALK_FILE):
    with open(GRAPH_WALK_FILE,"rb") as f:
        G_walk = pickle.load(f)
else:
    osm = pyrosm.OSM(CA_PATH)
    nodes, edges = osm.get_network(network_type="walking", nodes=True)
    G_walk = nx.DiGraph()
    for idx, row in nodes.iterrows():
        G_walk.add_node(row['id'], pos=(row['lon'], row['lat']))
    for idx, row in edges.iterrows():
        G_walk.add_edge(row['u'], row['v'], weight=row['length']/WALK_SPEED, mode='WALK')
    with open(GRAPH_WALK_FILE,"wb") as f:
        pickle.dump(G_walk,f)

# ----------------------------
# 2️⃣ Grafo bus GTFS (sequenza fermate)
# ----------------------------
if os.path.exists(GRAPH_BUS_FILE):
    with open(GRAPH_BUS_FILE,"rb") as f:
        G_bus = pickle.load(f)
else:
    G_bus = nx.DiGraph()
    for idx, row in stops.iterrows():
        G_bus.add_node(row['stop_id'], pos=(row['stop_lon'], row['stop_lat']))

    # Creiamo archi solo tra fermate consecutive della stessa corsa
    for trip_id in trips['trip_id']:
        trip_stops = stop_times[stop_times['trip_id']==trip_id].sort_values('stop_sequence')
        prev_stop = None
        prev_arrival = None
        for idx, stop in trip_stops.iterrows():
            curr_arrival = int(stop['arrival_time'].split(":")[0])*3600 + int(stop['arrival_time'].split(":")[1])*60 + int(stop['arrival_time'].split(":")[2])
            if prev_stop is not None:
                duration = curr_arrival-prev_arrival
                if duration<0:
                    duration+=24*3600
                G_bus.add_edge(prev_stop, stop['stop_id'], weight=duration, mode='BUS')
            prev_stop = stop['stop_id']
            prev_arrival = curr_arrival

    with open(GRAPH_BUS_FILE,"wb") as f:
        pickle.dump(G_bus,f)


# ----------------------------
# 3️⃣ Fermate vicine origine/destinazione
# ----------------------------
stops = gtfs.stops
def nearby_stops(coord, stops, radius=SEARCH_RADIUS):
    lon, lat = coord.x, coord.y
    mask = ((stops['stop_lat']-lat)**2 + (stops['stop_lon']-lon)**2) <= radius**2
    return stops[mask]

origin_candidates = nearby_stops(ORIGIN, stops)
dest_candidates = nearby_stops(DESTINATION, stops)

# ----------------------------
# 4️⃣ Grafo combinato (camminata + bus)
# ----------------------------
G_combined = nx.DiGraph()
# Aggiungi archi bus (già sequenza rispettata)
for u,v,data in G_bus.edges(data=True):
    G_combined.add_edge(u,v, weight=data['weight'], mode='BUS')

# Aggiungi archi pedonali origine → fermate
G_combined.add_node('origin', pos=(ORIGIN.x, ORIGIN.y))
for o_stop_id in origin_candidates['stop_id']:
    stop_row = origin_candidates[origin_candidates['stop_id']==o_stop_id].iloc[0]
    stop_coord = Point(stop_row['stop_lon'], stop_row['stop_lat'])
    dist = np.sqrt((stop_coord.x - ORIGIN.x)**2 + (stop_coord.y - ORIGIN.y)**2)*111000
    time_sec = dist/WALK_SPEED
    G_combined.add_edge('origin', o_stop_id, weight=time_sec, mode='WALK')

# Archi pedonali fermate finali → destinazione
G_combined.add_node('destination', pos=(DESTINATION.x, DESTINATION.y))
for d_stop_id in dest_candidates['stop_id']:
    stop_row = dest_candidates[dest_candidates['stop_id']==d_stop_id].iloc[0]
    stop_coord = Point(stop_row['stop_lon'], stop_row['stop_lat'])
    dist = np.sqrt((stop_coord.x - DESTINATION.x)**2 + (stop_coord.y - DESTINATION.y)**2)*111000
    time_sec = dist/WALK_SPEED
    G_combined.add_edge(d_stop_id, 'destination', weight=time_sec, mode='WALK')

# ----------------------------
# 5️⃣ Routing Dijkstra porta a porta
# ----------------------------
best_time = float('inf')
best_path = None
for o_stop in origin_candidates['stop_id']:
    for d_stop in dest_candidates['stop_id']:
        try:
            path = nx.shortest_path(G_combined, source='origin', target='destination', weight='weight')
            total_time = sum(G_combined[path[i]][path[i+1]]['weight'] for i in range(len(path)-1))
            if total_time<best_time:
                best_time = total_time
                best_path = path
        except nx.NetworkXNoPath:
            continue

print("Percorso ottimale:", best_path)
print("Tempo stimato (min):", best_time/60)

# ----------------------------
# 6️⃣ Visualizzazione mappa
# ----------------------------
m = folium.Map(location=[ORIGIN.y, ORIGIN.x], zoom_start=14)
folium.Marker([ORIGIN.y, ORIGIN.x], tooltip="Origine", icon=folium.Icon(color='green')).add_to(m)
folium.Marker([DESTINATION.y, DESTINATION.x], tooltip="Destinazione", icon=folium.Icon(color='red')).add_to(m)

for i in range(len(best_path)-1):
    n1 = best_path[i]
    n2 = best_path[i+1]
    
    if n1=='origin':
        lat1, lon1 = ORIGIN.y, ORIGIN.x
    elif n1=='destination':
        lat1, lon1 = DESTINATION.y, DESTINATION.x
    else:
        stop1 = stops[stops['stop_id']==n1].iloc[0]
        lat1, lon1 = stop1['stop_lat'], stop1['stop_lon']
    
    if n2=='origin':
        lat2, lon2 = ORIGIN.y, ORIGIN.x
    elif n2=='destination':
        lat2, lon2 = DESTINATION.y, DESTINATION.x
    else:
        stop2 = stops[stops['stop_id']==n2].iloc[0]
        lat2, lon2 = stop2['stop_lat'], stop2['stop_lon']
    
    mode = G_combined[n1][n2]['mode']
    color = 'orange' if mode=='WALK' else 'blue'
    
    folium.PolyLine([[lat1, lon1],[lat2, lon2]], color=color, weight=4, opacity=0.8).add_to(m)

m.save("percorso_bus_sequenza.html")
print("Mappa salvata in percorso_bus_sequenza.html")
