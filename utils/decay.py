import math
import os

# RRA redundancy-weight scheme, overridable via the RRA_LAMBDA_MODE env var so
# the sensitivity harness (sensitivity_upstream.py) can flip it for a fresh
# subprocess without touching any CSV config:
#   "rank_desc" (baseline/production): fastest mode for THIS poi gets lambda=1,
#     next 1/2, etc. -- redundancy is discounted by how much a mode is backed up.
#   "uniform": every mode gets lambda=1 -- no redundancy discount at all, so
#     RRA collapses toward "any single fast mode is as good as having several".
#   "rank_asc": the taper is reversed -- the WORST mode gets full weight and the
#     best gets the smallest -- the polar-opposite assumption, penalizing exactly
#     the redundancy the baseline rewards.
RRA_LAMBDA_MODES = ("rank_desc", "uniform", "rank_asc")


def _lambda_series(m: int, mode: str) -> list[float]:
    base = [1 / (i + 1) for i in range(m)]
    if mode == "rank_desc":
        return base
    if mode == "uniform":
        return [1.0] * m
    if mode == "rank_asc":
        return list(reversed(base))
    raise ValueError(f"unknown RRA_LAMBDA_MODE={mode!r}; expected one of {RRA_LAMBDA_MODES}")


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


def rra_breakdown(decay_walk, decay_bike, decay_drive, decay_bus, decay_subway=None, lambda_mode=None):
    """Same computation as calculate_rra, but also reports which redundancy
    weight (lambda) each mode received.

    The modes are re-ranked by decay value for every call -- under the baseline
    "rank_desc" scheme the single fastest mode for THIS POI gets lambda=1, the
    second-fastest 1/2, etc. -- so which mode carries which weight varies from
    POI to POI (e.g. walk might be rank 1 for a nearby POI and rank 3 for a
    distant one where drive/bus dominate). This makes that rank<->mode
    assignment explicit instead of discarding it once the modes are merged into
    a single RRA score.

    lambda_mode: one of RRA_LAMBDA_MODES ("rank_desc"/"uniform"/"rank_asc").
    Defaults to the RRA_LAMBDA_MODE env var (or "rank_desc" if unset), so the
    production call sites (utils/delta_g.py, debug_pipeline.py) don't need to
    change to pick up a sensitivity-harness override.

    Outputs:
    - dict keyed by mode name ("walk", "bike", "drive", "bus", + "subway" if
      provided), each mapping to {"decay", "rank" (1 = best), "lambda",
      "weighted" = decay*lambda}, plus "rra": the merged score and
      "lambda_mode": the scheme actually used.
    """
    mode_name = lambda_mode or os.environ.get("RRA_LAMBDA_MODE", "rank_desc")
    modes = [("walk", decay_walk), ("bike", decay_bike), ("drive", decay_drive), ("bus", decay_bus)]
    if decay_subway is not None:
        modes.append(("subway", decay_subway))
    ranked = sorted(modes, key=lambda kv: kv[1], reverse=True)
    m = len(ranked)
    lambdas = _lambda_series(m, mode_name)

    out: dict = {}
    weighted_decay = []
    for rank, ((mode, decay), lam) in enumerate(zip(ranked, lambdas), start=1):
        weighted = decay * lam
        weighted_decay.append(weighted)
        out[mode] = {"decay": decay, "rank": rank, "lambda": lam, "weighted": weighted}
    out["rra"] = 1 - math.prod(1 - w for w in weighted_decay)
    out["lambda_mode"] = mode_name
    return out


def calculate_rra(decay_walk, decay_bike, decay_drive, decay_bus, decay_subway=None, lambda_mode=None):
    """Combine modal decay values into a single RRA value.

    Inputs:
    - decay_walk, decay_bike, decay_drive, decay_bus: modal decay terms.
    - decay_subway: optional extra modality (subway). When None it is excluded so the mode
      count m stays 4 (identical to the original behaviour); when provided, m becomes 5.
    - lambda_mode: see rra_breakdown() -- defaults to the RRA_LAMBDA_MODE env var
      (or "rank_desc", the original behaviour) when not given explicitly.

    The redundancy weights lambda are derived from the actual number of modes m as
    1, 1/2, ..., 1/(m-1), 1/m (best mode full weight, each next mode discounted), so adding
    subway extends the series to 1/5 only for cities that have it.

    Outputs:
    - float: merged route/resource availability (RRA) score.
    """
    return rra_breakdown(decay_walk, decay_bike, decay_drive, decay_bus, decay_subway, lambda_mode)["rra"]
