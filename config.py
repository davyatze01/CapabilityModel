from dataclasses import dataclass
import datetime as dt
import os


@dataclass
class PipelineConfig:
    worker_count: int | None = None
    skip_routing: bool = True
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
    non_bus_cache_schema_version: int = 4
    poi_snap_cache_dir: str = os.path.join("cache", "poi_snap_cache")

    bus_departure_dt: dt.datetime = dt.datetime(2025, 10, 15, 12, 0, 0)
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

    skip_r5r_if_csv_exists: bool = True
