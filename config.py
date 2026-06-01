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


def derive_artifact_slug(city_slug: str, use_shapefile: bool, shapefile_name: str) -> str:
    """Build the artifact namespace slug for either city-wide or shapefile runs."""
    if not use_shapefile:
        return city_slug

    shapefile_base = os.path.splitext(os.path.basename(shapefile_name))[0].strip()
    if not shapefile_base:
        return city_slug
    return derive_city_slug(shapefile_base)


@dataclass
class PipelineConfig:
    city_name: str = field(default_factory=lambda: os.getenv("CAP_CITY_NAME", "Cagliari, Sardinia, Italy"))
    city_slug: str = field(init=False)
    artifact_slug: str = field(init=False)
    use_shapefile: bool = True
    name_shapefile: str = "Cagliari Shapefile.shp"
    open_qgis_after_run: bool = True
    qgis_bin_path: str = ""
    qgis_project_path: str = ""
    qgis_autostyle_project: bool = True
    qgis_autostyle_field: str = "capability_care"
    qgis_autostyle_classes: int = 5
    qgis_autostyle_ramp: str = "Viridis"
    qgis_autostyle_basemap: bool = True
    qgis_grid_enabled: bool = True
    qgis_grid_cell_size_m: float = 500.0
    qgis_grid_capability_field: str = "capability_care"
    qgis_grid_opacity: float = 0.55
    qgis_grid_max_cells: int = 500000
    poi_from_shp: bool = True
    worker_count: int | None = 12
    skip_routing: bool = True
    accessibility_chunksize: int = 100
    accessibility_deduplicate_entries: bool = True
    artifacts_root_dir: str = "artifacts"
    impedance_artifact_path: str = ""

    # Accessibility matrix cache
    accessibility_matrix_cache_enabled: bool = True
    accessibility_matrix_path: str = ""
    accessibility_meta_path: str = ""
    accessibility_node_to_row_path: str = ""
    accessibility_poi_to_col_path: str = ""
    accessibility_matrix_schema_version: int = 1

    # Service matrix cache
    service_matrix_cache_enabled: bool = True
    service_matrix_path: str = ""
    service_meta_path: str = ""
    service_node_to_row_path: str = ""
    service_to_col_path: str = ""
    service_matrix_schema_version: int = 1

    debug_max_nodes: int | None = None
    debug_max_pois: int | None = None
    seed: int = 42
    enable_progress: bool = True

    non_bus_cache_dir: str = ""
    non_bus_cache_schema_version: int = 6
    poi_snap_cache_dir: str = ""

    bus_departure_dt: dt.datetime = dt.datetime(2025, 10, 15, 12, 0, 0)
    bus_gamma: float = 1.0
    routing_data_dir: str = "gtfs"
    osm_pbf_autobuild: bool = True
    osm_autobuild_network_type: str = "all"
    osm_autobuild_simplify: bool = False
    osm_autobuild_retain_all: bool = True
    gtfs_feeds: list[str] = field(default_factory=lambda: [os.path.join("gtfs", "GTFS.zip"), os.path.join("gtfs","arst-cagliari-it.zip")])
    bus_routing_matrix_path: str = ""
    bus_routing_cache_path: str = ""
    bus_routing_origins_input_path: str = ""
    bus_routing_destinations_input_path: str = ""
    bus_source_id_to_row_path : str = ""
    bus_dest_id_to_col_path : str = ""
    bus_impedance_matrix_path : str = ""
    bus_impedance_meta_path: str = ""
    pool_max_retries: int = 4
    pool_retry_delay_s: float = 2.0
    non_bus_max_workers: int = 16

    walkability_cache_dir : str = ""

    def __post_init__(self) -> None:
        self.city_slug = derive_city_slug(self.city_name)
        self.artifact_slug = derive_artifact_slug(
            self.city_slug,
            self.use_shapefile,
            self.name_shapefile,
        )
        city_artifacts = os.path.join(self.artifacts_root_dir, self.artifact_slug)
        self.impedance_artifact_path = os.path.join(city_artifacts, "impedances.npz")
        self.poi_snap_cache_dir = os.path.join(city_artifacts, "snapping", "poi_snap_cache")
        self.non_bus_cache_dir = os.path.join(city_artifacts, "non_bus")
        self.walkability_cache_dir = os.path.join(city_artifacts, "walkability")

        self.bus_routing_matrix_path = os.path.join(city_artifacts, "bus", "r5r_expanded_travel_time_matrix.csv")
        self.bus_routing_cache_path = os.path.join(city_artifacts, "bus", "r5r_best_routes.pkl")
        self.bus_routing_origins_input_path = os.path.join(city_artifacts, "bus", "r5r_origins.csv")
        self.bus_routing_destinations_input_path = os.path.join(city_artifacts, "bus", "r5r_dest.csv")
        self.bus_source_id_to_row_path = os.path.join(city_artifacts, "bus", "source_id_to_row.json")
        self.bus_dest_id_to_col_path = os.path.join(city_artifacts, "bus", "dest_id_to_col.json")
        self.bus_impedance_matrix_path = os.path.join(city_artifacts, "bus", "bus_impedance_matrix.dat")
        self.bus_impedance_meta_path = os.path.join(city_artifacts, "bus", "bus_impedance_meta.json")

        self.accessibility_matrix_path = os.path.join(city_artifacts, "accessibility", "accessibility_matrix.dat")
        self.accessibility_meta_path = os.path.join(city_artifacts, "accessibility", "accessibility_meta.json")
        self.accessibility_node_to_row_path = os.path.join(city_artifacts, "accessibility", "access_node_to_row.json")
        self.accessibility_poi_to_col_path = os.path.join(city_artifacts, "accessibility", "access_poi_to_col.json")

        self.service_matrix_path = os.path.join(city_artifacts, "service", "service_matrix.dat")
        self.service_meta_path = os.path.join(city_artifacts, "service", "service_meta.json")
        self.service_node_to_row_path = os.path.join(city_artifacts, "service", "service_node_to_row.json")
        self.service_to_col_path = os.path.join(city_artifacts, "service", "service_to_col.json")
