from dataclasses import dataclass, field
from typing import Any
from config import PipelineConfig

@dataclass
class PipelineContext:
    config: PipelineConfig
    graph: Any
    nodes_with_coords: list[tuple[Any, dict]]
    workers: int
    output_paths: dict[str, str]
    rest_services: list[str]
    nut_services: list[str]
    care_services: list[str]


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


@dataclass
class AccessibilityNodeResult:
    node_id: Any
    lat: float
    lon: float
    accessibility_by_service: dict[str, list[dict[str, Any]]]
    missing_bus_ods: int


@dataclass
class AccessibilityStageResult:
    node_results: list[AccessibilityNodeResult] = field(default_factory=list)
    missing_bus_ods_total: int = 0


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
