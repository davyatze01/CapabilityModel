from dataclasses import dataclass, field
import datetime as dt
import os
import unicodedata
import re


def normalize_study_city(study_city: str) -> str:
    """Normalize a study city identifier to a stable lookup key."""
    return re.sub(r"[^a-z0-9]+", "_", study_city.strip().lower()).strip("_")


# ELECTRE TRI threshold factors — multiplied by std(service scores) at runtime.
ELECTRE_Q_FACTOR: float = 0.25   # indifference threshold  q = std(x) * Q_FACTOR
ELECTRE_P_FACTOR: float = 0.75   # preference threshold    p = std(x) * P_FACTOR

CITY_PRESETS: dict[str, dict[str, object]] = {
    "cagliari": {
        "city_name": "Cagliari, Sardinia, Italy",
        "use_shapefile": False,
        "name_shapefile": "Cagliari Shapefile.shp",
        "poi_from_shp": False,
        "poi_shapefile_paths": [
            "pois_shp/poi_points.shp",
            "pois_shp/poi_lines.shp",
            "pois_shp/poi_polygons.shp",
        ],
        "bus_ticket_price": 1.3,
    },
    "paris": {
        "city_name": "Paris, France",
        "use_shapefile": True,
        "name_shapefile": "mgp_boundary.shp",
        "poi_from_shp": True,
        "poi_shapefile_paths": [
            "Paris/POI_point.shp",
            "Paris/POI_line.shp",
            "Paris/POI_polygon.shp",
        ],
        "bus_ticket_price": 2.05,
    },
}


def apply_study_city(cfg: "PipelineConfig", study_city: str) -> None:
    """Apply city-specific defaults to a PipelineConfig instance."""
    preset = CITY_PRESETS.get(normalize_study_city(study_city))
    if preset is None:
        return

    cfg.city_name = str(preset["city_name"])
    cfg.use_shapefile = bool(preset["use_shapefile"])
    cfg.name_shapefile = str(preset["name_shapefile"])
    cfg.poi_from_shp = bool(preset["poi_from_shp"])
    cfg.poi_shapefile_paths = list(preset["poi_shapefile_paths"])
    if "bus_ticket_price" in preset:
        cfg.bus_ticket_price = float(preset["bus_ticket_price"])  # type: ignore[arg-type]


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
    study_city: str = field(default_factory=lambda: os.getenv("CAP_STUDY_CITY", "cagliari"))
    city_name: str = field(default_factory=lambda: os.getenv("CAP_CITY_NAME", "Paris, France"))
    city_slug: str = field(init=False)
    artifact_slug: str = field(init=False)
    use_shapefile: bool = True
    name_shapefile: str = "mgp_boundary.shp"
    open_qgis_after_run: bool = True
    qgis_bin_path: str = ""
    qgis_project_path: str = ""
    qgis_autostyle_project: bool = True
    qgis_autostyle_field: str = "capability_care"
    qgis_autostyle_classes: int = 5
    qgis_autostyle_ramp: str = "Viridis"
    qgis_autostyle_basemap: bool = True
    qgis_grid_enabled: bool = True
    qgis_grid_cell_size_m: float = 100.0
    qgis_grid_capability_field: str = "capability_care"
    qgis_grid_opacity: float = 0.55
    qgis_grid_max_cells: int = 500000
    poi_from_shp: bool = True
    poi_shapefile_paths: list[str] = field(
        default_factory=lambda: [
            "Paris/POI_point.shp",
            "Paris/POI_line.shp",
            "Paris/POI_polygon.shp",
        ]
    )
    worker_count: int | None = None
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
    accessibility_poi_by_node_path: str = ""
    accessibility_matrix_schema_version: int = 1

    # Service matrix cache
    service_matrix_cache_enabled: bool = True
    service_matrix_path: str = ""
    service_meta_path: str = ""
    service_node_to_row_path: str = ""
    service_to_col_path: str = ""
    service_matrix_schema_version: int = 1

    # POI exports
    poi_export_dir: str = ""
    poi_export_shapefile_path: str = ""
    poi_export_geopackage_path: str = ""
    hexagon_service_pois_path: str = ""

    debug_max_nodes: int | None = None
    debug_max_pois: int | None = None
    seed: int = 42
    enable_progress: bool = True

    non_bus_cache_dir: str = ""
    non_bus_cache_schema_version: int = 8
    poi_snap_cache_dir: str = ""

    # POI radius filtering — applies to bus and non-bus routing
    poi_radius_enabled: bool = True
    poi_radius_decay_threshold: float = 0.5    # used when poi_radius_m is None
    poi_radius_max_speed_kmh: float = 20.0     # used when poi_radius_m is None
    poi_radius_m: float | None = None          # if set, use this fixed radius and skip the decay computation

    # Network-distance cutoff for non-bus Dijkstra = poi_radius * factor. A factor > typical urban
    # street-network detour ratio guarantees no in-radius POI is dropped (bit-exact vs full Dijkstra)
    # while pruning exploration far beyond the radius.
    non_bus_dijkstra_detour_factor: float = 1.6

    origin_hex_enabled: bool = True

    bus_departure_dt: dt.datetime = dt.datetime(2025, 10, 15, 12, 0, 0)
    time_indifference_bus: float = 60.0
    routing_data_dir: str = "gtfs"
    osm_pbf_autobuild: bool = True
    osm_autobuild_network_type: str = "all"
    osm_autobuild_simplify: bool = False
    osm_autobuild_retain_all: bool = True
    gtfs_feeds: list[str] = field(default_factory=lambda: [
        os.path.join("gtfs", "GTFS.zip")
    ])
    # gtfs_feeds: list[str] = field(default_factory=lambda: [os.path.join("gtfs", "GTFS.zip"), os.path.join("gtfs", "arst-cagliari-it.zip")])
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
    non_bus_max_workers: int = 24
    # Route non-bus Dijkstra on a topology-simplified copy of each mode graph.
    # Origins/POIs are still enumerated and snapped on the full graph; only the
    # shortest-path graph is simplified, which preserves network distances while
    # cutting node count (and per-worker RAM) ~5x. Disable to route on full graphs.
    route_on_simplified_graph: bool = True
    # Estimated resident RAM per worker (mostly the mode graphs it loads). Used to
    # derive a memory-safe worker count in context.build_context. Tune per dataset.
    # Simplified routing graphs are far smaller, so this can be low when the flag
    # above is on; raise it if you route on full graphs (Cagliari full ~= 3 GB).
    mem_per_worker_gb: float = 1.2
    worker_mem_reserve_gb: float = 10.0
    bus_ticket_price : float = 1.3
    metro_ticket_price : float = 2.55
    train_ticket_price : float = 2.55
    vot : float = 0.2
    cost_per_liter : float = 1.8
    distance_for_liter : float = 15.0

    speed_walk_kmh: float = 5.0
    speed_bike_kmh: float = 15.0
    speed_drive_kmh: float = 30.0
    drive_access_time_min: float = 10.0

    walkability_cache_dir : str = ""

    def __post_init__(self) -> None:
        apply_study_city(self, self.study_city)
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
        self.accessibility_poi_by_node_path = os.path.join(city_artifacts, "accessibility", "access_poi_by_node.json")

        self.service_matrix_path = os.path.join(city_artifacts, "service", "service_matrix.dat")
        self.service_meta_path = os.path.join(city_artifacts, "service", "service_meta.json")
        self.service_node_to_row_path = os.path.join(city_artifacts, "service", "service_node_to_row.json")
        self.service_to_col_path = os.path.join(city_artifacts, "service", "service_to_col.json")

        self.poi_export_dir = os.path.join("outputs", "poi_exports", self.artifact_slug)
        self.poi_export_geopackage_path = os.path.join(self.poi_export_dir, "pois_used.gpkg")
        self.poi_export_shapefile_path = self.poi_export_geopackage_path
        self.hexagon_service_pois_path = os.path.join(self.poi_export_dir, "hexagon_service_pois.json")
