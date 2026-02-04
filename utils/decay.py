import math

def distance_decay(beta, imp):

    if type(imp) == list:
        impedance = sum(imp)
    else:
        impedance = imp

    return math.exp(-beta * impedance)

def calculate_rra(decay_walk,decay_bike,decay_drive,decay_bus):
    all_decay = [1 - decay for decay in [decay_walk,decay_bike,decay_drive,decay_bus]]
    rra = 1 - (math.prod(all_decay))
    return rra