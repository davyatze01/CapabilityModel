"""Individual-profile (persona) definitions for scenario evaluation.

The baseline capability model runs a single, universal traveler. This module adds named
*individual profiles* that reconfigure a run to represent a specific person — their available
travel modes, walking speed, economic affordability of opportunities, and (for public
transport) whether they are restricted to wheelchair-accessible stops.

A profile touches the pipeline in two places:

1. Before routing, via :meth:`Profile.config_overrides` — walking speed, the set of non-bus
   modes actually routed/fused, the GTFS feed (an accessible-stops-only feed when required),
   and an ``artifact_slug`` suffix so the run lands in its own namespace and never overwrites
   the baseline.
2. During the accessibility stage, via :meth:`Profile.utility_for` — a per-POI utility
   multiplier ``u(y)`` (the paper's affordability term) applied to every POI's accessibility,
   with a per-instance override for "benefit" POIs such as university canteens.

See ``profiles.SCENARIOS`` for the named comparisons a runner can play by string key, and
``scenarios.ProfileRunner`` for the orchestration.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field

# Canteen ("mensa") POI instances that carry a status-benefit utility override. For Cagliari
# these are the three university/ERSU canteens; the file lists their OSM ids (see
# outputs/pois_by_service.txt). Kept as data, not a POI type/category, per the model design.
CANTEENS_CSV = os.path.join("config", "canteens_cagliari.csv")

# Default location of the wheelchair-accessible-stops GTFS feed produced by
# gtfs/make_accessible_gtfs.py (only used by profiles with pt_accessible_stops_only).
ACCESSIBLE_GTFS_PATH = os.path.join("gtfs", "GTFS_accessible.zip")

# Default location of the Metrocagliari-plus-extension GTFS feed produced by
# gtfs/make_new_metro_gtfs.py (only used by the "new-metro" scenario's NEW_METRO profile).
NEW_METRO_GTFS_PATH = os.path.join("gtfs", "gtfs_new_metro.zip")

_ALL_NON_BUS_MODES = ("walk", "bike", "drive")


def load_canteen_ids(path: str = CANTEENS_CSV) -> frozenset[str]:
    """Read the canteen OSM ids (as strings) from the canteens CSV.

    Returns an empty set when the file is missing so a run without it degrades to "no
    canteen override" rather than crashing.
    """
    ids: set[str] = set()
    if os.path.isfile(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                value = (row.get("osmid") or "").strip()
                if value:
                    ids.add(value)
    return frozenset(ids)


def osmid_from_source_key(source_key: object) -> str | None:
    """Extract the OSM id from a POI ``source_key`` (see utils.poi_identity.build_poi_source_key).

    Source keys are JSON strings like ``{"kind":"osmid","value":12655832971,...}``. Returns the
    id as a string, or ``None`` when the key is not an osmid-based key (e.g. a fallback key).
    """
    if source_key is None:
        return None
    try:
        data = json.loads(source_key)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict) and data.get("kind") == "osmid" and data.get("value") is not None:
        return str(data["value"])
    return None


# poi_types where affordability plausibly gates access -- market/discretionary spending, not
# free public space or subsidized public healthcare/social services. Everything NOT listed
# here gets u=1.0 regardless of a profile's affordability value (see Profile.utility_for).
PAID_POI_TYPES: frozenset[str] = frozenset({
    "organised_sport_indoor",      # gym, climbing wall, ice rink, bowling
    "organized_sport_outdoor",     # private pitch, golf course, stadium, horse riding
    "informal_sport_indoor",       # fitness_centre, indoor pool, dance studio
    "passive_consumption",         # cinema, theatre, events venue
    "mediated_experience",         # museum, gallery, tourist attraction
    "on_site_dining",              # restaurant, cafe, pub, bar
    "takeaway_consumption",        # bakery, fast food, ice cream
    "therapeutic_wellness",        # spa, sauna, massage (private wellness, not medical care)
})


@dataclass(frozen=True)
class Profile:
    """One individual profile / persona.

    Attributes:
    - key: short slug used for the artifact/output namespace (e.g. "elderly").
    - enabled_modes: travel modes available to this person, among
      {"walk","bike","drive","bus"} (public-transport "bus" is always kept when present;
      the non-bus subset gates routing and RRA fusion).
    - walk_speed_kmh: walking speed for non-bus routing (written onto cfg.speed_walk_kmh).
    - affordability: general per-POI utility u in [0,1] (economic access to opportunities).
    - canteen_utility: utility u for canteen instances (status-benefit override).
    - canteen_source_keys: canteen OSM ids the override applies to.
    - pt_accessible_stops_only: when True, public transport is routed against an
      accessible-stops-only GTFS feed (accessible_gtfs_path).
    - accessible_gtfs_path: path to that feed.
    - extra_config_overrides: additional ``PipelineConfig`` kwargs merged in on top of the
      ones above (last, so it can override them) -- the escape hatch for scenarios that
      aren't persona traits but still want a full ProfileRunner-style re-route, e.g. a new
      transit line (``gtfs_feeds`` + ``enable_subway``). Empty for ordinary personas.
    """

    key: str
    enabled_modes: frozenset[str]
    walk_speed_kmh: float
    affordability: float
    canteen_utility: float
    canteen_source_keys: frozenset[str] = field(default_factory=frozenset)
    pt_accessible_stops_only: bool = False
    accessible_gtfs_path: str = ACCESSIBLE_GTFS_PATH
    extra_config_overrides: dict[str, object] = field(default_factory=dict)

    def enabled_non_bus_modes(self) -> tuple[str, ...]:
        """The routed/fused non-bus modes, in the canonical walk/bike/drive order."""
        return tuple(m for m in _ALL_NON_BUS_MODES if m in self.enabled_modes)

    def config_overrides(self) -> dict[str, object]:
        """Keyword overrides to pass to ``PipelineConfig(...)`` for this profile.

        These must be passed at construction time because every downstream path is derived
        from ``artifact_slug`` in ``PipelineConfig.__post_init__``.
        """
        overrides: dict[str, object] = {
            "speed_walk_kmh": self.walk_speed_kmh,
            "enabled_non_bus_modes": self.enabled_non_bus_modes(),
            "artifact_slug_suffix": self.key,
        }
        if self.pt_accessible_stops_only and self.accessible_gtfs_path:
            overrides["gtfs_feeds"] = [self.accessible_gtfs_path]
        overrides.update(self.extra_config_overrides)
        return overrides

    def utility_for(self, source_key: object, poi_type: str) -> float:
        """Per-POI utility multiplier u(y): canteen override when applicable, else affordability
        for market/paid poi_types (PAID_POI_TYPES), else 1.0 for free/public ones."""
        osmid = osmid_from_source_key(source_key)
        if osmid is not None and osmid in self.canteen_source_keys:
            return self.canteen_utility
        return self.affordability if poi_type in PAID_POI_TYPES else 1.0


# --- Named personas -------------------------------------------------------------------

_CANTEENS = load_canteen_ids()

# Baseline universal traveler: every mode, full walking speed, no affordability penalty, full
# GTFS. Reproduces the default main.py run exactly (utility u = 1.0 everywhere, so the
# accessibility stage's multiplier is a no-op), but in its own artifact namespace so the
# comparison never overwrites the user's main outputs.
BASELINE = Profile(
    key="baseline",
    enabled_modes=frozenset({"walk", "bike", "drive", "bus"}),
    walk_speed_kmh=5.0,
    affordability=1.0,
    canteen_utility=1.0,
    canteen_source_keys=_CANTEENS,
    pt_accessible_stops_only=False,
)

# Elderly retiree: low income (reduced affordability), no scholarship (canteen u=0), no driving
# licence (walk+bus only), physical disability (accessible stops only), difficulty walking (2 km/h).
ELDERLY = Profile(
    key="elderly",
    enabled_modes=frozenset({"walk", "bus"}),
    walk_speed_kmh=2.0,
    affordability=0.5,
    canteen_utility=0.0,
    canteen_source_keys=_CANTEENS,
    pt_accessible_stops_only=True,
)

# Young student: no income (low affordability), scholarship benefit (canteen u=1), car ownership
# (walk+bus+car), normal walking (5 km/h).
STUDENT = Profile(
    key="student",
    enabled_modes=frozenset({"walk", "drive", "bus"}),
    walk_speed_kmh=5.0,
    affordability=0.2,
    canteen_utility=1.0,
    canteen_source_keys=_CANTEENS,
    pt_accessible_stops_only=False,
)

# Universal traveler again, but routed against Cagliari's own bus feed (gtfs/GTFS.zip,
# untouched) *plus* MCA1 extended past REPUBBLICA to SAN SATURNINO/BONARIA/LUSSU/DARSENA/
# MUNICIPIO/STAZIONE, merged by gtfs/make_new_metro_gtfs.py into gtfs/gtfs_new_metro.zip,
# with subway routing turned on -- Cagliari has no metro modality at all in the ordinary
# baseline (enable_subway defaults to False there), so this is "what if the city added this
# line on top of what it already has", not a persona trait. Same enabled_modes/
# affordability/canteen as BASELINE, and the same bus network + no bus_departure_dt
# override, so the *only* difference from BASELINE is the metro line's presence.
#
# The metro source (gtfs_metrocagliari.zip) only has service via calendar_dates.txt
# exceptions dated across 2026 -- a different vintage than gtfs/GTFS.zip's Oct-Nov 2025
# calendar, with zero overlapping dates -- so make_new_metro_gtfs.py re-declares the
# metro's FER/FEST services as plain weekday-flag services spanning the *bus* feed's own
# calendar window instead of carrying over the metro's 2026 exceptions. That is what makes
# BASELINE's default bus_departure_dt (2025-10-15, a Wednesday) resolve real metro trips
# here too, without touching the bus network at all.
NEW_METRO = Profile(
    key="new_metro",
    enabled_modes=frozenset({"walk", "bike", "drive", "bus"}),
    walk_speed_kmh=5.0,
    affordability=1.0,
    canteen_utility=1.0,
    canteen_source_keys=_CANTEENS,
    pt_accessible_stops_only=False,
    extra_config_overrides={
        "gtfs_feeds": [NEW_METRO_GTFS_PATH],
        "enable_subway": True,
        # Metrocagliari (MCA1/MCA2) is published as GTFS route_type=0 (tram/light rail),
        # not route_type=1 -- despite being colloquially "the metro" -- so r5r must be asked
        # for "TRAM" here, not the "SUBWAY" default (which matches Paris' true route_type=1
        # métro). Requesting the wrong mode finds zero matching routes silently, not an
        # error: see core.config.PipelineConfig.subway_transit_mode's docstring.
        "subway_transit_mode": "TRAM",
    },
)

# Named scenario bundles, selected by string (e.g. `python scenarios.py elder-student`).
# Order matters: it sets the display order of the per-scenario capability grids and the
# pairwise-difference order (all C(n,2) pairs in listing order) — here baseline→student,
# baseline→elderly, student→elderly.
SCENARIOS: dict[str, tuple[Profile, ...]] = {
    "elder-student": (BASELINE, STUDENT, ELDERLY),
    # baseline (no metro) -> new-metro (bus + extended Metrocagliari line to STAZIONE).
    "new-metro": (BASELINE, NEW_METRO),
}

# The configured capability keys (config/capability.csv), in a stable order for
# grids/legends. Not fixed to any particular set or count of capabilities.
from utils.capabilities import CAPABILITY_SERVICES as _CAPABILITY_SERVICES

CAPABILITIES: tuple[str, ...] = tuple(_CAPABILITY_SERVICES.keys())
