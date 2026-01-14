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

def test():
    dining_out_accessibility = []
    on_the_go_accessibility = []
    services = []
    origin = (39.22231439353061, 9.113848879825527)

    for type in dining_out_list:
        dining_out_accessibility.append(delta_g.accessibility(type, origin))
    print("dining_out_accessibility:", dining_out_accessibility)
    services.append(choquet_integral(dining_out_accessibility, cap_dining_out))
    print("Dining out service:", services[-1])

    for type in on_the_go_list:
        on_the_go_accessibility.append(delta_g.accessibility(type, origin))
    print("on_the_go_accessibility:", on_the_go_accessibility)
    services.append(choquet_integral(on_the_go_accessibility, cap_on_the_go))
    print("On the go service:", services[-1])

    capability_to_eat = choquet_integral(services, cap_eat)
    print("Capability to eat:", capability_to_eat)