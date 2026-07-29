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
    """

    key: str
    enabled_modes: frozenset[str]
    walk_speed_kmh: float
    affordability: float
    canteen_utility: float
    canteen_source_keys: frozenset[str] = field(default_factory=frozenset)
    pt_accessible_stops_only: bool = False
    accessible_gtfs_path: str = ACCESSIBLE_GTFS_PATH

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
        return overrides

    def utility_for(self, source_key: object) -> float:
        """Per-POI utility multiplier u(y): canteen override when applicable, else affordability."""
        osmid = osmid_from_source_key(source_key)
        if osmid is not None and osmid in self.canteen_source_keys:
            return self.canteen_utility
        return self.affordability


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

# Named scenario bundles, selected by string (e.g. `python scenarios.py elder-student`).
# Order matters: it sets the display order of the per-scenario capability grids and the
# pairwise-difference order (all C(n,2) pairs in listing order) — here baseline→student,
# baseline→elderly, student→elderly.
SCENARIOS: dict[str, tuple[Profile, ...]] = {
    "elder-student": (BASELINE, STUDENT, ELDERLY),
}

# The three capability keys, in a stable order for grids/legends.
CAPABILITIES: tuple[str, ...] = ("restorativeness", "nutrition", "care")
