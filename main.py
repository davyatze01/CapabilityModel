from utils import graphml, route
import osmnx as ox
import shutup

shutup.please()

grafo = graphml.get_graph()

poi = graphml.get_poi('amenity', 'restaurant')

# Sbloccare per stampare dei POI
# graphml.print_poi(poi, 0, 10) 


origine = (39.22231439353061, 9.113848879825527)
destinazione_poi = "Niu Nervi"
destinazione = ox.geocode(destinazione_poi)

impedance = route.get_route(grafo,'bus',origine,destinazione, True)

print("Impedance = ",impedance)
