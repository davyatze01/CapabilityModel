from utils import graphml, route, decay, delta_g
import osmnx as ox
import math
import matplotlib.pyplot as plt
import shutup

shutup.please()

grafo = graphml.get_graph()

origine = (39.22231439353061, 9.113848879825527)

delta_g.accessibility("healthcare", origine)

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

rra = decay.calculate_rra(decay_walk, decay_bike, decay_drive, decay_bus)

print("RRA = ", rra)

plt.show() """




