
from utils import delta_g

# dining_out -> restaurant e pub 
# on_the_go -> cafe, bar, ice_cream, fast_food
dining_out_list = [ 
    "restaurant",
    "pub"
]

on_the_go_list = [
    "cafe",
    "bar",
    "ice_cream",
    "fast_food"
]

DINING_OUT_IDX = {"restaurant": 0, "pub": 1}
ON_THE_GO_IDX = {"cafe": 0, "bar": 1, "ice_cream": 2, "fast_food": 3}



def cap_dining_out(S):
    S = frozenset(S)  # normalize input (works for list, set, tuple)
    if not S:
        return 0.0
    if DINING_OUT_IDX["restaurant"] in S:
        return 1.0
    return 0.5

def cap_on_the_go(S):
    S = frozenset(S)  # normalize input (works for list, set, tuple)
    if not S:
        return 0.0
    elif ON_THE_GO_IDX["fast_food"] in S:
        return 1.0
    elif ON_THE_GO_IDX["cafe"] in S:
        return 0.75
    elif ON_THE_GO_IDX["bar"] in S:
        return 0.5
    else:
        return 0.25
    

def choquet_integral(x, cap):
    n = len(x)
    order = sorted(range(n), key=lambda i: x[i])
    x_sorted = [x[i] for i in order]

    total = 0.0
    prev = 0.0
    for j in range(n):
        tail = order[j:]
        total += (x_sorted[j] - prev) * cap(tail)
        prev = x_sorted[j]
    return total