from osmnx.routing import route_to_gdf

def impedance_bus(full_route_distance,full_route_waiting):
    full_route_impedance = []

    BUS_SPEED = 10
    # Se i due array sono di dimensioni diverse non si puo continuare
    if len(full_route_distance) == len(full_route_waiting):
        for i,distance in enumerate(full_route_distance):
            distance = distance/1000
            waiting = full_route_waiting[i]/60 # in ore
            impedance = ((distance/BUS_SPEED)+waiting) * 60 # in minuti
            full_route_impedance.append(impedance)
        return full_route_impedance
    else:
        print("Problema con il calcolo della impedance per il bus...")
        exit()

def impedance_base(graph,route,network_type):

    # Ottieni GeoDataFrame del percorso
    gdf = route_to_gdf(graph, route)
    route_length = gdf["length"].sum() / 1000

    speed = None

    if network_type == "walk":
        speed = 5
    elif network_type == "bike":
        speed = 15
    else: # network_type == "drive"
        speed = 25

    return (route_length / speed) * 60.0 # impedance in minuti