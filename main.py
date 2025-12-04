from utils import graphml, route, decay
import osmnx as ox
import math
import shutup

shutup.please()

grafo = graphml.get_graph()

poi = graphml.get_poi('amenity', 'restaurant')

# Sbloccare per stampare dei POI
# graphml.print_poi(poi, 0, 10) 


origine = (39.22231439353061, 9.113848879825527)
destinazione_poi = "Niu Nervi"
destinazione = ox.geocode(destinazione_poi)

impedance_walk = route.get_route(grafo,'walk',origine,destinazione, True)
impedance_drive = route.get_route(grafo,'drive',origine,destinazione, True)
impedance_bike = route.get_route(grafo,'bike',origine,destinazione, True) 
impedance_bus = route.get_route(grafo,'bus',origine,destinazione, True)

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




