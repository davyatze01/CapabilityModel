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

def calculate_rra(decay_walk,decay_bike,decay_drive,decay_bus):
    """Combine modal decay values into a single RRA value.

    Inputs:
    - decay_walk, decay_bike, decay_drive, decay_bus: modal decay terms.

    Outputs:
    - float: merged route/resource availability (RRA) score.
    """
    all_decay = [decay_walk,decay_bike,decay_drive,decay_bus]
    all_decay.sort(reverse=True)
    lambdas = [1,1,1,1]
    weighted_decay = []
    for i in range(len(all_decay)):
        weighted_decay.append(all_decay[i] * lambdas[i])
    rra = 1 - math.prod(1 - decay for decay in weighted_decay)
    return rra
