from utils import graphml, route, decay, delta_g, capabilities as cap
import csv
import os
import osmnx as ox
import math
import matplotlib.pyplot as plt
import shutup
from tqdm import tqdm
import multiprocessing as mp
import threading
import time

# Set to None to use (cpu_count - 1)
CAP_WORKERS = 8

shutup.please()

# (opzionale ma utile: riduce log/overhead OSMnx)
ox.settings.use_cache = True
ox.settings.log_console = False

# (opzionale ma utile: riduce log/overhead OSMnx)
ox.settings.use_cache = True
ox.settings.log_console = False

def _process_node(node_item):
    node_id, data = node_item
    if "y" not in data or "x" not in data:
        return None
    origin = (data["y"], data["x"])

    dining_out_accessibility = []
    on_the_go_accessibility = []
    services = []

    for poi_type in cap.dining_out_list:
        dining_out_accessibility.append(delta_g.accessibility(poi_type, origin))
    services.append(cap.choquet_integral(dining_out_accessibility, cap.cap_dining_out))

    for poi_type in cap.on_the_go_list:
        on_the_go_accessibility.append(delta_g.accessibility(poi_type, origin))
    services.append(cap.choquet_integral(on_the_go_accessibility, cap.cap_on_the_go))

    capability_to_eat = cap.choquet_integral(services, cap.cap_eat)

    return [
        node_id,
        origin[0],
        origin[1],
        capability_to_eat,
        services[0],
        services[1],
    ]


def main():
    graph = graphml.get_graph()
    nodes = list(graph.nodes(data=True))

    os.makedirs("outputs", exist_ok=True)
    output_path = os.path.join("outputs", "capability_to_eat.csv")

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "node_id",
            "lat",
            "lon",
            "capability_to_eat",
            "dining_out_service",
            "on_the_go_service",
        ])

        workers = max(1, mp.cpu_count() - 1) if CAP_WORKERS is None else max(1, int(CAP_WORKERS))
        last_row_lock = threading.Lock()
        last_row_time = [time.time()]
        stop_event = threading.Event()

        pbar = tqdm(total=len(nodes), desc="Nodes", mininterval=0)

        def _monitor():
            while not stop_event.wait(60):
                pbar.refresh()
                with last_row_lock:
                    idle_s = time.time() - last_row_time[0]
                if idle_s >= 300:
                    print("Warning: no rows written in the last 5 minutes.")

        monitor_thread = threading.Thread(target=_monitor, daemon=True)
        monitor_thread.start()

        try:
            with mp.Pool(processes=workers) as pool:
                for row in pool.imap_unordered(_process_node, nodes, chunksize=20):
                    if row is None:
                        continue
                    writer.writerow(row)
                    f.flush()
                    pbar.update(1)
                    with last_row_lock:
                        last_row_time[0] = time.time()
        finally:
            stop_event.set()
            monitor_thread.join(timeout=2)
            pbar.close()

    print(f"Wrote results to: {output_path}")


if __name__ == "__main__":
    main()

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
