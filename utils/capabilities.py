from utils.services import choquet_integral, dining_out_list, on_the_go_list, cap_dining_out, cap_on_the_go
import utils.delta_g as delta_g

CAP_EAT_IDX = {"dining_out": 0, "on_the_go": 1}

def cap_eat(S):
    S = frozenset(S)  # normalize input (works for list, set, tuple)
    if not S:
        return 0.0
    if CAP_EAT_IDX["dining_out"] in S:
        if CAP_EAT_IDX["on_the_go"] in S:
            return 1.0
        else:
            return 0.6
    else:
        return 0.4
