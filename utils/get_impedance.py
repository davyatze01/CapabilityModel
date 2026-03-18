from osmnx.routing import route_to_gdf

def impedance_bus(route_distance,route_waiting):
    """Compute bus impedance in minutes from route distance and waiting time.

    Inputs:
    - route_distance: route distance value expected in kilometers.
    - route_waiting: waiting time expected in minutes.

    Outputs:
    - float: estimated bus impedance in minutes.
    """
    impedance = None

    BUS_SPEED = 10
    route_waiting = route_waiting/60 # in ore
    if route_distance != None and route_waiting != None:
        #print(f"({route_distance}/{BUS_SPEED})*{route_waiting}={(route_distance/BUS_SPEED) * route_waiting}")
        return ((route_distance/BUS_SPEED) * route_waiting)*60 # in minuti
    else:
        print("Problema con il calcolo della impedance...")
        exit()

def impedance_base(distance,network_type):
    """Convert distance to travel-time impedance for a given transport mode.

    Inputs:
    - distance: route distance in kilometers.
    - network_type: one of walk, bike, or drive.

    Outputs:
    - float: impedance in minutes using fixed mode speed.
    """

    speed = None

    if network_type == "walk":
        speed = 5
    elif network_type == "bike":
        speed = 15
    else: # network_type == "drive"
        speed = 25

    return (distance / speed) * 60.0 # impedance in minuti
