from dataclasses import dataclass, field
import datetime as dt
import os
import unicodedata
import re


def derive_city_slug(city_name: str) -> str:
    """Build a stable ASCII slug from a city label like 'Name, Region, Country'."""
    head = city_name.split(",", 1)[0].strip()
    if not head:
        head = city_name.strip()

    normalized = unicodedata.normalize("NFKD", head)
    ascii_head = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_head).strip("_")
    if not slug:
        raise ValueError("city_name must contain at least one ASCII alphanumeric character.")
    return slug


@dataclass
class PipelineConfig:
    city_name: str = field(default_factory=lambda: os.getenv("CAP_CITY_NAME", "Cagliari, Sardinia, Italy"))
    city_slug: str = field(init=False)
    worker_count: int | None = None
    skip_routing: bool = False
    accessibility_chunksize: int = 100
    accessibility_deduplicate_entries: bool = True

    # Accessibility matrix cache
    accessibility_matrix_cache_enabled: bool = True
    accessibility_matrix_path: str = os.path.join("outputs", "accessibility_matrix.dat")
    accessibility_meta_path: str = os.path.join("outputs", "accessibility_meta.json")
    accessibility_node_to_row_path: str = os.path.join("outputs", "access_node_to_row.json")
    accessibility_poi_to_col_path: str = os.path.join("outputs", "access_poi_to_col.json")
    accessibility_matrix_schema_version: int = 1

    # Service matrix cache
    service_matrix_cache_enabled: bool = True
    service_matrix_path: str = os.path.join("outputs", "service_matrix.dat")
    service_meta_path: str = os.path.join("outputs", "service_meta.json")
    service_node_to_row_path: str = os.path.join("outputs", "service_node_to_row.json")
    service_to_col_path: str = os.path.join("outputs", "service_to_col.json")
    service_matrix_schema_version: int = 1

    debug_max_nodes: int | None = None
    debug_max_pois: int | None = None
    seed: int = 42
    enable_progress: bool = True

    non_bus_cache_dir: str = os.path.join("cache", "non_bus")
    non_bus_cache_schema_version: int = 5
    poi_snap_cache_dir: str = os.path.join("cache", "poi_snap_cache")

    bus_departure_dt: dt.datetime = dt.datetime(2025, 10, 15, 12, 0, 0)
    routing_data_dir: str = "gtfs"
    osm_pbf_autobuild: bool = True
    osm_autobuild_network_type: str = "all"
    osm_autobuild_simplify: bool = False
    osm_autobuild_retain_all: bool = True
    gtfs_feeds: list[str] = field(default_factory=lambda: [os.path.join("gtfs", "GTFS.zip"), os.path.join("gtfs","arst-cagliari-it.zip")])
    bus_routing_matrix_path: str = os.path.join("outputs", "r5r_expanded_travel_time_matrix.csv")
    bus_routing_cache_path: str = os.path.join("outputs", "r5r_best_routes.pkl")
    bus_routing_origins_input_path: str = os.path.join("outputs", "r5r_origins.csv")
    bus_routing_destinations_input_path: str = os.path.join("outputs", "r5r_dest.csv")
    bus_source_id_to_row_path : str = os.path.join("outputs","source_id_to_row.json")
    bus_dest_id_to_col_path : str = os.path.join("outputs","dest_id_to_col.json")
    bus_impedance_matrix_path : str = os.path.join("outputs","bus_impedance_matrix.dat")
    bus_impedance_meta_path: str = os.path.join("outputs", "bus_impedance_meta.json")
    pool_max_retries: int = 4
    pool_retry_delay_s: float = 2.0

    walkability_cache_dir : str = os.path.join("cache","walkability")

    def __post_init__(self) -> None:
        self.city_slug = derive_city_slug(self.city_name)
