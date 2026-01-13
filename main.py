from utils import graphml, route, decay, delta_g
import osmnx as ox
import math
import matplotlib.pyplot as plt
import shutup
from tqdm import tqdm
import os
import pickle

shutup.please()

# (opzionale ma utile: riduce log/overhead OSMnx)
ox.settings.use_cache = True
ox.settings.log_console = False

grafo = graphml.get_graph()

origine = (39.22231439353061, 9.113848879825527)

print("Nodi: ", type(grafo))

G = grafo

first_node = next(iter(G.nodes))
print("Primo nodo:", first_node)

# ==========================
# CACHE accessibility array
# ==========================
ACCESS_CACHE_DIR = "access_cache"
os.makedirs(ACCESS_CACHE_DIR, exist_ok=True)

poi_type = "healthcare"
cache_path = os.path.join(ACCESS_CACHE_DIR, f"accessibility_{poi_type}.pkl")

if os.path.exists(cache_path):
    print("📦 Carico accessibility da cache")
    with open(cache_path, "rb") as f:
        payload = pickle.load(f)

    node_ids = payload["node_ids"]
    accessibility = payload["accessibility"]
else:
    accessibility = []
    node_ids = []

    for node, data in tqdm(
        G.nodes(data=True),
        total=G.number_of_nodes(),
        desc="Calcolo accessibility",
        mininterval=0.5
    ):
        lat = data.get("y")
        lon = data.get("x")
        origin = (lat, lon)

        # skip nodi senza coordinate (per sicurezza)
        if lat is None or lon is None:
            continue

        node_ids.append(node)
        accessibility.append(delta_g.accessibility(poi_type, origin))

    print("💾 Salvo accessibility in cache")
    with open(cache_path, "wb") as f:
        pickle.dump({"node_ids": node_ids, "accessibility": accessibility}, f)

print("✅ Lunghezza accessibility:", len(accessibility))


""" poi = graphml.get_poi('amenity', True)

# Sbloccare per stampare dei POI
# graphml.print_poi(poi, 0, 10) 

fig, axs = plt.subplots(2, 2, figsize=(16, 8), constrained_layout=True)


origine = (39.22231439353061, 9.113848879825527)
destinazione_poi = "Niu Nervi"
destinazione = ox.geocode(destinazione_poi)

fig_walk,ax_walk,impedance_walk = route.get_route(grafo,'walk',origine,destinazione, True, axs[0,0])
fig_drive,ax_drive,impedance_drive = route.get_route(grafo,'drive',origine,destinazione, True, axs[0,1])
fig_bike,ax_bike,impedance_bike = route.get_route(grafo,'bike',origine,destinazione, True, axs[1,0])
fig_bus,ax_bus,impedance_bus = route.get_route(grafo,'bus',origine,destinazione, True, axs[1,1])

for ax, title in zip(
    axs.ravel(), ["Walking", "Driving", "Biking", "Bus"]
):
    ax.set_title(title, color="black", fontsize=14, fontweight="bold")




# Valore per distance decay function
beta = math.log(2)/20.0

decay_walk = decay.distance_decay(beta,impedance_walk)
decay_drive = decay.distance_decay(beta,impedance_drive)
decay_bike = decay.distance_decay(beta, impedance_bike)
decay_bus = decay.distance_decay(beta, impedance_bus)

print("Decay walk = ", decay_walk)
print("Decay bike = ", decay_bike)
print("Decay drive = ", decay_drive)
print("Decay bus = ", decay_bus)

rra = decay.calculate_rra(decay_walk, decay_bike, decay_drive, decay_bike, decay_bus)

print("RRA = ", rra)

plt.show() """
