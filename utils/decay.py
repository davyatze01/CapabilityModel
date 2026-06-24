import math

def distance_decay(beta, imp):
    """Apply exponential decay to an impedance value.

    Inputs:
    - beta: decay coefficient.
    - imp: impedance value or list of impedance components.

    Outputs:
    - float: decay value in [0, 1] for positive impedance.
    """

    if type(imp) == list:
        impedance = sum(imp)
    else:
        impedance = imp

    return math.exp(-beta * impedance)

def threshold_radius_m(decay_coeff: float, threshold: float, max_speed_kmh: float = 60.0) -> float:
    """Max haversine radius (metres) at which decay >= threshold.

    Inputs:
    - decay_coeff: POI-type decay coefficient (minutes at which decay = 0.5).
    - threshold: minimum meaningful decay value (e.g. 0.05).
    - max_speed_kmh: reference travel speed used to convert time to distance.

    Outputs:
    - float: radius in metres.
    """
    beta = math.log(2) / decay_coeff
    max_time_min = -math.log(threshold) / beta
    return max_time_min * max_speed_kmh * 1000.0 / 60.0


def calculate_rra(decay_walk,decay_bike,decay_drive,decay_bus):
    """Combine modal decay values into a single RRA value.

    Inputs:
    - decay_walk, decay_bike, decay_drive, decay_bus: modal decay terms.

    Outputs:
    - float: merged route/resource availability (RRA) score.
    """
    all_decay = [decay_walk,decay_bike,decay_drive,decay_bus]
    all_decay.sort(reverse=True)
    lambdas = [1, 1/2, 1/3, 1/4]
    weighted_decay = []
    for i in range(len(all_decay)):
        weighted_decay.append(all_decay[i] * lambdas[i])
    rra = 1 - math.prod(1 - decay for decay in weighted_decay)
    return rra
