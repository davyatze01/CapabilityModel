import osmnx as ox
import os
import geopandas as gpd


def get_graph():

    # Nomi file
    PLACE_NAME = "Cagliari, Sardinia, Italy"
    NAME_FILE = "graph/cagliari.graphml"

    # Se esiste lo carico altrimenti creo e salvo

    graph = None

    try:
        graph = ox.io.load_graphml(NAME_FILE)
        print(f"Grafo caricato correttamente da: {NAME_FILE}")
    except Exception as e:
        print("Grafo non presente, scarico il grafo di Cagliari..")
        graph = ox.graph_from_place(PLACE_NAME)
        ox.io.save_graphml(graph,filepath = NAME_FILE)
        print("Grafo scaricato e salvato correttamente!")

    return graph

def get_poi(feature,value):

    # Nomi file
    PLACE_NAME = "Cagliari, Sardinia, Italy"
    NAME_FILE = f"poi/{feature}_{value}.geojson"

    os.makedirs("poi", exist_ok=True)

    try:
        # Provo a caricare i POI gia salvati
        poi = gpd.read_file(NAME_FILE)
        print(f"POI caricati correttamente da: {NAME_FILE}")
    except Exception:
        # Scarico POI da OSM
        print("Scarico POI da OSM...")

        poi = ox.features_from_place(
            PLACE_NAME,
            {feature: value}
        )

        # Salvo tutti i POI
        poi.to_file(NAME_FILE, driver="GeoJSON")
        print("POI scaricati e salvati correttamente!")

    return poi

def print_poi(poi, print_start, print_end):
    if print_start == None or print_start < 0:
        print_start = 0
    
    if print_end == None or print_end > len(poi):
        print_end = len(poi)

    subset = poi.iloc[print_start:print_end]

    for i, row in subset.iterrows():
        print(f"{i+1}: {row.get('name', 'Senza nome')}")
