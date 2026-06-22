def impedance_base(
    distance,
    network_type,
    walk_score: float | None = None,
    lambda_walk: float = 0.15,
    vot: float = 0.0,
    cost_per_liter: float = 0.0,
    distance_for_liter: float = 1.0,
    speed_walk_kmh: float = 5.0,
    speed_bike_kmh: float = 15.0,
    speed_drive_kmh: float = 30.0,
    drive_access_time_min: float = 10.0,
):
    """Convert distance to travel-time impedance for a given transport mode.

    Inputs:
    - distance: route distance in kilometers.
    - network_type: one of walk, bike, or drive.
    - speed_walk_kmh, speed_bike_kmh, speed_drive_kmh: mode speeds in km/h.
    - drive_access_time_min: fixed overhead added to every drive trip (minutes).

    Outputs:
    - float: impedance in minutes.
    """
    if network_type == "walk":
        speed = speed_walk_kmh
    elif network_type == "bike":
        speed = speed_bike_kmh
    else:
        speed = speed_drive_kmh

    if network_type == "drive":
        monetary_cost = cost_per_liter / distance_for_liter
        base_impedance = drive_access_time_min + (distance / speed) * 60.0 + vot * monetary_cost
    else:
        base_impedance = (distance / speed) * 60.0

    if network_type != "walk":
        return base_impedance

    # neutral fallback if walk score missing
    if walk_score is None:
        return base_impedance
    w = float(walk_score)
    coef = 1.0 + lambda_walk * ((5.0 - w) / 4.0)
    return coef * base_impedance



