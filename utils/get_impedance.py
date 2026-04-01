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

def impedance_base(distance,network_type,walk_score: float | None=None,lambda_walk: float = 0.15):
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
        speed = 30

    base_impedance = (distance / speed) * 60.0
    if network_type != "walk":
        return base_impedance

    # neutral fallback if walk score missing
    if walk_score is None:
        return base_impedance
    w = float(walk_score)
    coef = 1.0 + lambda_walk * ((5.0 - w) / 4.0)
    return coef * base_impedance



