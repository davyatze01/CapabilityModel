from dataclasses import dataclass, field
from typing import Any
import os
import datetime as dt
import multiprocessing as mp
from utils import graphml, capabilities as cap


@dataclass
class PipelineConfig:
    cap_workers: int | None = None      # Limit workers to a particular number
    skip_routing: bool = True           # Skip routing phase if results are already cached
    max_nodes: int | None = None        # For test purposes, one can limit the number of source nodes to run the pipeline
    max_pois: int | None = None         # For test purposes, one can limit the number of pois to consider for computing the accessibilities
    seed: int = 42                      # Random seed set to a fixed value for reproducibility
    enable_progress: bool = True        # True to show progress bars, false to hide them

    non_bus_cache_dir: str = os.path.join("cache", "non_bus")           # This is where car, bike and walking routings are cached. If present, the routing step is skipped
    non_bus_cache_schema_version: int = 3                               # The cache version, when a new version is updated, the old files are invalidated
    poi_snap_cache_dir: str = os.path.join("cache", "poi_snap_cache")   # Each poi is snapped to one or more network nodes to compute routings. They are cached so that geometries are not re-downloaded when re-executing the step on the sme points

    r5_fixed_departure: dt.datetime = dt.datetime(2025, 10, 15, 12, 0, 0)       # The date in which we are querying the system. It is fixed: 15-10-2025 midday
    r5_pbf_path: str = os.path.join("gtfs-pbf", "cagliari-latest.osmv2.pbf")    # Where to find the pbf file used for graph extraction
    r5_gtfs_path: str = os.path.join("gtfs-pbf", "GTFS.zip")                    # Where to find the GTFS transport data
    r5_jar_path: str = "r5-v7.5-r5py-all.jar"                                   # Where to find r5py's jar, necessary for installation
    r5_fast_csv: str = os.path.join("outputs", "r5_fast_routes.csv")            # Where to find the csv containing cached bus routes
    r5_fast_db: str = os.path.join("outputs", "r5_fast_routes.sqlite")          # Where to find the sqlite table for faster accessing of the cached bus routes
    r5_max_retries: int = 20                                                    # When r5 accidentally crashes, it retries from the last crash for a maximum of 20 times                                                   
    r5_retry_delay_s: float = 3.0                                               # Wait three seconds before retrying
    r5_attempt_timeout_s: float = 1800.0                                        # Consider the attempt failed if nothing progresses after 30 minutes
    r5_fast_chunk_size: int = 128                                               # Divide the data into chunks between workers. By default a chunk contains 128 rows of the table
    r5_fast_workers: int | None = None                                          # The numbers of workers that will split the table. Ideally, if a table is N rows and there are K workers, each worker will work with a portion of size N/K of the table

    pool_max_retries: int = 4                                                   # Retries during multiprocess stage when a worker pool crashes, to avoid crashing completely because of a single worker crash
    pool_retry_delay_s: float = 2.0                                             # Wait some seconds before trying again


@dataclass
class PipelineContext:
    config: PipelineConfig                      # The configuration parameter whose structure is defined above
    graph: Any                                  # The graph to use for snapping
    nodes_with_coords: list[tuple[Any, dict]]   # The list of nodes sources to use for the routing phases
    workers: int                                # The worker count for multiprocessing
    output_paths: dict[str, str]                # The dicts that contain for each capability the path of the output csv where to save data
    rest_services: list[str]                    # The services that contribute to the capability "restorativeness"
    nut_services: list[str]                     # The services that contribute to the capability "nutrition"
    care_services: list[str]                    # The services that contribute to the capability "care"


@dataclass
class SnappingStageResult:
    query_by_key: dict[Any, Any]                                                                            # A dictionary that contains for each poi type and tuple of tags, the corresponding PoiQuery structure
    poi_bus_snap_info_by_type: dict[Any, dict[Any, list[tuple[tuple[float, float], float]]]]                # For each POI, a list of tuple is associated, the coordinates of the candidate snap points for that POI and the relative distance
    poi_mode_snap_info_by_type: dict[Any, dict[str, dict[Any, list[tuple[tuple[float, float], float]]]]]    # Works as bus_snap but there are three distinct dictionary for "walk", "drive" and "bike"
    shared_mode_graphs: dict[str, Any]                                                                      # Pre-loaded graphs for walk, drive and bike for Dijkstra calculation


@dataclass
class BusRoutingStageResult:
    routing_csv: str            # Where the r5 routing results are saved (csv file)
    routing_db: str             # Where the sqlite database with the routing results is saved
    routing_departure_iso: str  # The date and time of departure, in ISO format


@dataclass
class NonBusRoutingStageResult:
    cache_paths: dict[Any, str] # For each origin node it maps where the corresponding cache file is. There is one for each source
    cached_nodes: int           # Number of nodes already in cache before starting computing
    computed_nodes: int         # Number of nodes that were computed during this run


@dataclass
class AccessibilityNodeResult:
    node_id: Any                                                # Id of the source node from the base graph
    lat: float                                                  # Latitude of the source node
    lon: float                                                  # Longitude of the source node
    accessibility_by_service: dict[str, list[dict[str, Any]]]   # A dict that says for each poi_type, what is the accessibility score for that type starting from the related source node
    missing_bus_ods: int                                        # How many origin destination lookups in bus routing are missing for this origin


@dataclass
class AccessibilityStageResult:
    node_results: list[AccessibilityNodeResult] = field(default_factory=list)   # Contains the result of accessibility scores for all types for all source nodes
    missing_bus_ods_total: int = 0


@dataclass
# Contains information for each source node and then a list of services. For each services, the score of opportunity of service for that node
class ServiceNodeResult:
    node_id: Any
    lat: float
    lon: float
    service_scores: dict[str, float]


@dataclass
# Stores all information of service scores of nodes into a list
class ServiceStageResult:
    node_results: list[ServiceNodeResult] = field(default_factory=list)


@dataclass
class CapabilityStageResult:
    output_paths: dict[str, str]    # The output paths of the capability csv outputs
    rows_written: int               # The number of rows for each csv file


'''
Given a set of parameters specified in PipelineConfig, the corresponding context is built
'''
def build_context(config: PipelineConfig) -> PipelineContext:
    graph = graphml.get_mode_graph("walk")                                              # Build the default walk graph used for bus snapping
    nodes = list(graph.nodes(data=True))                                                
    nodes_with_coords = [item for item in nodes if "y" in item[1] and "x" in item[1]]   # Creates a list of coordinate tuples representing nodes of the graph

    # Sample n random nodes of the graph if the debug parameter is set
    if config.max_nodes is not None:
        import random
        rng = random.Random(config.seed)
        nodes_with_coords = rng.sample(nodes_with_coords, min(config.max_nodes, len(nodes_with_coords)))

    os.makedirs("outputs", exist_ok=True)
    os.makedirs(config.non_bus_cache_dir, exist_ok=True)

    output_paths = {
        "restorativeness": os.path.join("outputs", "capability_restorativeness.csv"),
        "nutrition": os.path.join("outputs", "capability_nutrition.csv"),
        "care": os.path.join("outputs", "capability_care.csv"),
    }

    workers = max(1, mp.cpu_count()) if config.cap_workers is None else max(1, int(config.cap_workers))
    # If a cap on number of workers is set, then that number is taken as number of workers, otherwise it is going to be the cpu_count of the device. In all other cases, only 1 worker is selected

    return PipelineContext(
        config=config,
        graph=graph,
        nodes_with_coords=nodes_with_coords,
        workers=workers,
        output_paths=output_paths,
        rest_services=cap.CAPABILITY_SERVICES["restorativeness"],
        nut_services=cap.CAPABILITY_SERVICES["nutrition"],
        care_services=cap.CAPABILITY_SERVICES["care"],
    )
