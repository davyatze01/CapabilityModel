from dataclasses import dataclass, field
from typing import Any
from core.config import PipelineConfig
from core.profiles import Profile

@dataclass
class PipelineContext:
    config: PipelineConfig
    graph: Any
    nodes_with_coords: list[tuple[Any, dict]]
    workers: int
    output_paths: dict[str, str]
    # Ordered services per capability, as configured in config/capability.csv (see
    # utils.capabilities.CAPABILITY_SERVICES) -- not fixed to any particular set of
    # capability names.
    capability_services: dict[str, list[str]]
    # Individual-profile override (see core.profiles.Profile) -- set by main.py/scenarios.py
    # after construction, never by build_context itself. None reproduces the baseline
    # universal traveler exactly (see accessibility_stage's getattr(ctx, "profile", None) reads).
    profile: Profile | None = None


@dataclass
class SnappingStageResult:
    query_by_key: dict[Any, Any]
    poi_bus_snap_info_by_type: dict[Any, dict[Any, list[tuple[tuple[float, float], float]]]]
    poi_mode_snap_info_by_type: dict[Any, dict[str, dict[Any, list[tuple[tuple[float, float], float]]]]]
    shared_mode_graphs: dict[str, Any]


@dataclass
class BusRoutingStageResult:
    routing_csv: str
    routing_pkl: str
    routing_departure_iso: str
    origins_sig: str
    destinations_sig: str


@dataclass
class NonBusRoutingStageResult:
    cache_paths: dict[Any, str]
    cached_nodes: int
    computed_nodes: int
    # Origin-invariant POI identity, one entry per poi_type: {"src_keys": bytes array,
    # "source_coords": float64 (N, 2)}. Written once into the artifact bundle; each
    # node's per-POI "kept_idx" indexes into this instead of duplicating keys/coords.
    poi_catalog: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class AccessibilityNodeResult:
    node_id: Any
    lat: float
    lon: float
    # Per-POI-type aggregated accessibility (used by service/capability stages).
    accessibility_by_service: dict[str, list[dict[str, Any]]]
    # Per-individual-POI accessibility keyed by source_key (used by poi_exports).
    # Maps source_key → accessibility_value.
    accessibility_by_poi: dict[str, float] = field(default_factory=dict)


@dataclass
class AccessibilityStageResult:
    node_results: list[AccessibilityNodeResult] = field(default_factory=list)


@dataclass
class ServiceNodeResult:
    node_id: Any
    lat: float
    lon: float
    service_scores: dict[str, float]


@dataclass
class ServiceStageResult:
    node_results: list[ServiceNodeResult] = field(default_factory=list)


@dataclass
class CapabilityStageResult:
    output_paths: dict[str, str]
    rows_written: int
