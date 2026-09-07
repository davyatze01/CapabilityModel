from dataclasses import dataclass, field
import datetime as dt
import os
import unicodedata
import re
from typing import TypedDict, NotRequired

class PresetDict(TypedDict):
    city_name : str
    use_shapefile : bool
    name_shapefile : str
    poi_from_shp : bool
    poi_shapefile_paths : list[str]
    poi_label_field : str
    bus_ticket_price : float
    osm_extract_url : str
    metro_ticket_price : NotRequired[float]
    enable_subway: NotRequired[bool]
    gtfs_feeds : NotRequired[list[str]]
    bus_departure_dt : NotRequired[dt.datetime]
    subway_departure_dt : NotRequired[dt.datetime]
    subway_transit_mode : NotRequired[str]
    export_hex_radius_m : NotRequired[float]

def normalize_study_city(study_city: str) -> str:
    """Normalize a study city identifier to a stable lookup key."""
    return re.sub(r"[^a-z0-9]+", "_", study_city.strip().lower()).strip("_")


# Absolute ELECTRE TRI thresholds, in use since <this fix>. Originally derived as
# std(x) * the factors below on a representative run, then frozen as fixed values --
# every implementation (production classifier, vectorized sensitivity analysis) must
# use these same absolute numbers, not recompute std(x) per node.
ELECTRE_Q: float = 0.02   # indifference threshold
ELECTRE_P: float = 0.06   # preference threshold
# Historical factors used to originally derive ELECTRE_Q/ELECTRE_P from std(x).
# Provenance only -- not read at runtime anymore.
ELECTRE_Q_FACTOR: float = 0.2   # indifference threshold  q = std(x) * Q_FACTOR
ELECTRE_P_FACTOR: float = 0.8   # preference threshold    p = std(x) * P_FACTOR
# ELECTRE TRI's 5-class boundaries (Very Low/Low/Medium/High/Very High cut points),
# frozen from a Jenks natural-breaks calibration against a baseline run's capability
# score distribution -- see analysis/calibrate_electre_boundaries.py. Recalibrating
# means rerunning that script (set its CITY_SLUG) and pasting its output below.
# One set per calibration city; auto-follows study_city (mirrors PipelineConfig.study_city's
# own default) rather than a separate manual knob, so the boundaries always match whichever
# city CAP_STUDY_CITY/main.py's study_city selects. Falls back to "cagliari" for any city
# without its own calibrated set.
ELECTRE_BOUNDARIES_BY_PROFILE: dict[str, list[float]] = {
    "cagliari": [0.35, 0.51, 0.66, 0.80],
    "paris": [0.35, 0.51, 0.66, 0.80],
}
ELECTRE_BOUNDARIES_PROFILE: str = normalize_study_city(os.getenv("CAP_STUDY_CITY", "cagliari"))
ELECTRE_BOUNDARIES: list[float] = ELECTRE_BOUNDARIES_BY_PROFILE.get(
    ELECTRE_BOUNDARIES_PROFILE, ELECTRE_BOUNDARIES_BY_PROFILE["cagliari"]
)
# Cutting level λ: minimum outranking credibility for a node to be assigned above a
# boundary. The single source of truth — the assignment code, the debug details, and
# the sensitivity baseline all read this value. The sensitivity report flags λ as the
# model's steepest parameter (moving it flips up to ~10% of care nodes), so any change
# here must come with an explicit justification.
ELECTRE_LAMBDA_CUT: float = 0.75

CITY_PRESETS: dict[str, PresetDict] = {
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
        # Cagliari's own shapefiles (produced by utils/pois_to_shp.py) carry the
        # classification attribute under this column name.
        "poi_label_field": "poiType",
        "bus_ticket_price": 1.3,
        "metro_ticket_price": 1.3,
        "osm_extract_url": "https://download.geofabrik.de/europe/italy/isole-latest.osm.pbf",
        "enable_subway" : True,
        "gtfs_feeds" : [os.path.join("gtfs", "GTFS.zip"), os.path.join("gtfs","gtfs_metrocagliari.zip")],
        "subway_transit_mode" : "TRAM"
    },
    "paris": {
        "city_name": "Paris, France",
        "use_shapefile": True,
        "name_shapefile": "mgp_boundary.shp",
        "poi_from_shp": True,
        "poi_shapefile_paths": [
            "Paris/POI_point2.shp",
            "Paris/POI_line2.shp",
            "Paris/POI_polygon2.shp",
        ],
        # Paris' MGP layers carry the classification attribute under this column name.
        "poi_label_field": "TYPEQU",
        "bus_ticket_price": 2.05,
        # The IDFM feed carries both bus (route_type 3) and subway (route_type 1); route them
        # as two separate modalities.
        "enable_subway": True,
        "gtfs_feeds": [os.path.join("gtfs", "IDFM-gtfs.zip")],
        # The IDFM GTFS calendar covers 2025-12-12..2026-01-13, so the global default
        # departure (2025-10-15) falls outside it and R5 reports "no transit services on
        # the selected date". Pin a normal weekday well inside the feed's calendar (a
        # Wednesday, before the holiday tail) for Paris only.
        "bus_departure_dt": dt.datetime(2025, 12, 17, 12, 0, 0),
        "subway_departure_dt": dt.datetime(2025, 12, 17, 12, 0, 0),
        "osm_extract_url": "https://download.geofabrik.de/europe/france/ile-de-france-latest.osm.pbf",
        # Paris' dense POI universe at the full poi_radius_m (~15km) makes the hex-POI
        # provenance export (exports/poi_exports.py, analysis/score_report.py) hit the
        # process memory cap. Real accessibility/capability computation is unaffected --
        # see export_hex_radius_m's definition in PipelineConfig.
        "export_hex_radius_m": 5000.0,
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
    if "poi_label_field" in preset:
        cfg.poi_label_field = str(preset["poi_label_field"])
    if "bus_ticket_price" in preset:
        cfg.bus_ticket_price = float(preset["bus_ticket_price"])  # type: ignore[arg-type]
    if "metro_ticket_price" in preset:
        cfg.metro_ticket_price = float(preset["metro_ticket_price"])
    if "enable_subway" in preset:
        cfg.enable_subway = bool(preset["enable_subway"])
    if "gtfs_feeds" in preset:
        cfg.gtfs_feeds = list(preset["gtfs_feeds"])  # type: ignore[arg-type]
    if "bus_departure_dt" in preset:
        cfg.bus_departure_dt = preset["bus_departure_dt"]  # type: ignore[assignment]
    if "osm_extract_url" in preset:
        cfg.osm_extract_url = str(preset["osm_extract_url"])
    if "subway_transit_mode" in preset:
        cfg.subway_transit_mode = str(preset["subway_transit_mode"])
    if "subway_departure_dt" in preset:
        cfg.subway_departure_dt = preset["subway_departure_dt"]
    if "export_hex_radius_m" in preset:
        cfg.export_hex_radius_m = float(preset["export_hex_radius_m"])



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
    default_capability: str = "capability_care"
    # Which capability-score formula capability_stage.py writes into capability_<name>:
    # "discrete" (default) = the ELECTRE TRI band midpoint (0.1/0.3/0.5/0.7/0.9, current
    # production behavior, unchanged). "continuous" = utils.capabilities.
    # electre_tri_continuous_score(), the raw outranking credibility averaged across the 4
    # boundaries -- a statistics-only knob for testing whether scenario comparisons show more
    # significant effects with the resolution the band midpoint collapses away (see
    # analysis/scenarios.py's CAPABILITY_SCORE_MODE knob). Leave "discrete" for any run whose
    # output should match the existing maps/legends.
    capability_score_mode: str = "discrete"
    qgis_autostyle_ramp: str = "Viridis"
    show_basemap: bool = True
    qgis_grid_enabled: bool = True
    hexagon_radius: float = 100.0
    # Signature colour for each capability, in the order capabilities are defined
    # (see utils.capabilities.CAPABILITY_SERVICES): restorativeness, nutrition, care.
    # One capability, one colour; add an entry here for every capability the model
    # is configured to compute.
    capability_colors: list[str] = field(
        default_factory=lambda: ["#006BFF", "#FFA200", "#EB4CCC"]
    )
    qgis_grid_capability_field: str = "capability_care"
    qgis_grid_opacity: float = 1.0
    # Per-service heatmap grids and the per-capability colored grids are both drawn
    # as flat color fills, so they share the same opacity treatment.
    qgis_service_grid_opacity: float = 0.9
    qgis_grid_max_cells: int = 500000
    # Stroke width (mm, QGIS symbol units) of the hexagon grid outline layer. Kept
    # comfortably above the ~1px screen-space threshold: at 0.1mm the exported
    # OpenLayers stroke rendered sub-pixel-wide, which made the hexagon borders
    # blink in and out during zoom (canvas hairline flicker) in the qgis2web export.
    qgis_grid_outline_width: float = 0.35
    # Stroke color of the hexagon grid outline layer, as "R,G,B,A" (0-255 each).
    qgis_grid_outline_color: str = "90,90,90,170"
    # Fill the convex hull of the sampled nodes with hexagons (vs. only cells next to a node),
    # so the study area's shape/perimeter is recognizable and interior holes are filled.
    qgis_grid_fill_hull: bool = True
    # Optional margin (metres) added around the hull before filling.
    qgis_grid_hull_buffer_m: float = 0.0
    # Boundary shape: >=1.0 = convex hull; ~0.3 = concave hull following a real concave outline
    # (coastlines/bays); lower = tighter/jaggier. Falls back to convex if concave_hull fails.
    qgis_grid_hull_ratio: float = 0.3
    # Drop hull-fill cells (no real sampled node, has_data=0) that lie entirely within OSM
    # water polygons. Cells with a real node are never dropped, even if partly over water.
    qgis_grid_exclude_water: bool = True
    # Overlay a point layer of named places (comuni in normal case, quartieri in
    # UPPERCASE) fetched from OSM, the way Google Maps labels an area.
    qgis_place_labels_enabled: bool = True
    poi_from_shp: bool = True
    poi_shapefile_paths: list[str] = field(
        default_factory=lambda: [
            "Paris/POI_point.shp",
            "Paris/POI_line.shp",
            "Paris/POI_polygon.shp",
        ]
    )
    # Name of the attribute in the source POI shapefiles whose values classify each
    # record into a configured POI type (see utils/load_shapefile.poi_from_shp).
    poi_label_field: str = "poi_type"
    worker_count: int | None = None
    # Opt-in gentle execution. Set by CAP_SAFE_MODE in __post_init__ (kept consistent with
    # runtime_setup, which also caps native math threads before numpy import). When on: the
    # worker count is capped to ~physical_cores//2 and worker processes run below-normal
    # priority. Pools already recycle workers (maxtasksperchild). Reduces sustained CPU/power
    # load without changing results.
    safe_mode: bool = field(init=False)
    # Kept small deliberately: a worker computes its whole chunk (holding every node's
    # full result, including accessibility_by_poi, in memory) before returning any of
    # it over IPC -- the per-node sparse .npz write only runs in the main process after
    # the chunk comes back. On large cities (Paris: ~16MB avg non-bus cache/node, with
    # much bigger outliers downtown) a chunksize of 100 let each of ~24 workers buffer
    # up to 100 large results at once, which OOM-killed the whole run under the 45GB
    # cgroup cap. A small chunksize bounds that per-worker buffer at the cost of more
    # frequent (cheap) IPC round-trips.
    accessibility_chunksize: int = 4
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
    accessibility_poi_by_node_dir: str = ""
    accessibility_matrix_schema_version: int = 1

    # Per-service POI de-duplication (OSM-download mode only): when a physical POI is
    # matched by several poi_types of one service, keep it only under the owning type.
    poi_service_dedup_enabled: bool = True
    poi_ownership_drop_path: str = ""

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
    hex_pois_dir: str = ""
    hex_pois_zip_path: str = ""
    # Per-hexagon, per-poi_type aggregate accessibility (before it's folded into
    # service/capability power) -- see poi_exports._build_poi_type_accessibility.
    # Small (hexagons x poi_types, tens of thousands of values), so a single
    # JSON/JS pair is enough; unlike hex_pois it doesn't need shard/compress.
    poi_type_accessibility_path: str = ""

    debug_max_nodes: int | None = None
    debug_max_pois: int | None = None
    seed: int = 42
    enable_progress: bool = True

    non_bus_cache_dir: str = ""
    non_bus_cache_schema_version: int = 11
    # Shared per-poi_type {src_keys, source_coords} catalog -- written once by the live
    # pipeline and restored to this same path when an impedance bundle is loaded, so
    # both paths give the accessibility stage one place to resolve a node's kept_idx.
    non_bus_poi_catalog_path: str = ""
    poi_snap_cache_dir: str = ""

    # POI radius filtering — applies to bus and non-bus routing
    poi_radius_enabled: bool = True
    poi_radius_decay_threshold: float = 0.5    # used when poi_radius_m is None
    poi_radius_max_speed_kmh: float = 20.0     # used when poi_radius_m is None
    # if set, use this fixed radius and skip the decay computation. CAP_POI_RADIUS_M lets a
    # fresh PipelineConfig() built deep in the call graph (e.g. utils.graphml.get_poi(), which
    # constructs its own cfg rather than taking one) pick up the same override as the top-level
    # run -- without it, such call sites silently re-derive the radius from the live
    # poi_types.csv instead of honoring a frozen/overridden radius.
    poi_radius_m: float | None = field(
        default_factory=lambda: float(os.environ["CAP_POI_RADIUS_M"]) if os.environ.get("CAP_POI_RADIUS_M") else None
    )

    # Post-hoc distance cutoff applied ONLY when building the hex-POI provenance/interface
    # exports (exports/poi_exports.py, analysis/score_report.py) -- trims the membership
    # list and score computation to POIs within this radius of each origin. Independent of
    # poi_radius_m: the real accessibility/capability computation still uses the full
    # poi_radius_m-derived cache. None = no extra filtering (export radius == poi_radius_m).
    export_hex_radius_m: float | None = None

    # Network-distance cutoff for non-bus Dijkstra = poi_radius * factor. A factor > typical urban
    # street-network detour ratio guarantees no in-radius POI is dropped (bit-exact vs full Dijkstra)
    # while pruning exploration far beyond the radius.
    non_bus_dijkstra_detour_factor: float = 1.6

    origin_hex_enabled: bool = True

    bus_departure_dt: dt.datetime = dt.datetime(2025, 10, 15, 12, 0, 0)
    subway_departure_dt: dt.datetime = dt.datetime(2026, 1, 7, 12, 0, 0)
    time_indifference_bus: float = 60.0
    routing_data_dir: str = "gtfs"
    osm_pbf_autobuild: bool = True
    # Regional Geofabrik PBF the city extract is clipped from (osmium extract).
    osm_extract_url: str = field(default_factory=lambda: os.getenv("CAP_OSM_EXTRACT_URL", ""))
    # Margin added around the study-area bounds when clipping, so routing near the
    # boundary still sees the surrounding network.
    osm_extract_buffer_m: float = 2000.0
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
    # Subway is an optional second public-transport modality (enabled per city, e.g. France).
    # Its routing/impedance artifacts mirror the bus ones but live under a separate subfolder
    # so the two modes don't overwrite each other. Populated in __post_init__.
    enable_subway: bool = False
    # The r5r transit mode string used to isolate this second modality's routes from the rest
    # of the combined GTFS feed (see routing.public_transport_routing_stage._resolve_transit_
    # mode). r5r's mode filter is keyed on GTFS route_type, not on what a city colloquially
    # calls the line: Paris' IDFM métro is route_type=1 ("SUBWAY"), but Cagliari's
    # Metrocagliari is published as route_type=0 ("TRAM", i.e. light rail) despite being
    # called "metro" -- requesting the wrong mode string silently finds zero matching routes
    # (every routing call falls back to a walk-only path, n_rides=0) rather than erroring.
    # Check the actual route_type in the feed before changing this for a new city.
    subway_transit_mode: str = "SUBWAY"
    subway_routing_matrix_path: str = ""
    subway_routing_cache_path: str = ""
    subway_routing_origins_input_path: str = ""
    subway_routing_destinations_input_path: str = ""
    subway_source_id_to_row_path: str = ""
    subway_dest_id_to_col_path: str = ""
    subway_impedance_matrix_path: str = ""
    subway_impedance_meta_path: str = ""
    pool_max_retries: int = 4
    pool_retry_delay_s: float = 2.0
    # Kept low deliberately: some origins' 18km-radius queries pull in "hundreds of
    # thousands of in-radius POIs" (see _get_mode_lengths_and_paths's docstring in
    # utils/delta_g.py), and each such origin materializes gigabytes of live Python
    # objects (per-POI dicts/lists across 36 queries) WHILE it's being processed --
    # that's live working-set memory, not something GC/malloc_trim can reclaim. At
    # workers=24, several concurrent dense origins stacked their peaks and OOM-killed
    # a 45G-capped run in as little as ~2 minutes (2026-07-16, profiled: ~1.8GB live
    # per dense origin x 24 concurrent workers ~= 43GB). Fewer concurrent workers is
    # the direct, deterministic bound on how many of these expensive peaks can stack
    # at once -- raise this only alongside either a higher MemoryMax or a rewrite of
    # utils.delta_g.accessibility_non_bus_from_snap_map to process POIs as numpy
    # arrays instead of per-item Python objects (would cut this ~15-20x).
    # Was 16 (2026-07-16); a run OOM-killed at the 45G cap after ~13.5min even with
    # that setting plus the malloc_trim/maxtasksperchild mitigations below, confirming
    # the ~29GB worst-case estimate for workers=16 was too optimistic (trimming only
    # every _TRIM_EVERY_N_ORIGINS origins lets each worker's transient peak run higher
    # than the flat ~1.8GB/origin estimate assumed, and dense origins can still land on
    # many workers at once). Dropped back to 8 per this file's own fallback plan
    # (~14GB worst case, more headroom under the 45G cap). Only raise this again
    # alongside either a higher MemoryMax or the numpy-array rewrite of
    # utils.delta_g.accessibility_non_bus_from_snap_map noted above.
    non_bus_max_workers: int = 8
    # score_report.py's node-scoring pool (added 2026-08-04) pickles the precomputed
    # per-POI weight dict to every worker via Pool initargs. Each worker's copy gets
    # touched (refcounted) on nearly every dict access, which -- per the fork COW note
    # in accessibility_stage.py's graph-loading comment -- tends to actually materialize
    # as N private copies rather than one shared one, not just N x the dict's flat size.
    # Capped independently of ctx.workers (which can be 20+ on a big machine) so this
    # new, less-battle-tested pool can't reproduce the non_bus_max_workers-style OOM
    # incident above until it's been observed running cleanly at full worker count.
    score_report_max_workers: int = 8
    # Estimated resident RAM per worker. With CSR-based routing this is mostly the
    # compressed adjacency arrays and related caches rather than full mode graphs.
    # Does NOT include the per-origin transient working-set spike described above --
    # that's bounded separately via non_bus_max_workers, not this budget.
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

    # Non-bus travel modes actually routed and fused into accessibility. The default is the
    # full universal-traveler set; individual-profile runs (see profiles.py) narrow it, e.g.
    # ("walk",) for someone with no car/bike. Both the routing stages and the RRA fusion in
    # accessibility_stage read this so the mode count m (which sets the redundancy taper)
    # stays consistent. Bus/subway are public-transport modes, gated separately.
    enabled_non_bus_modes: tuple[str, ...] = ("walk", "bike", "drive")

    # Optional suffix appended to artifact_slug in __post_init__, so an individual-profile
    # run gets its own artifacts/<slug>_<suffix>/ and outputs/.../<slug>_<suffix> namespace
    # and never overwrites the baseline. Empty = baseline (no suffix), unchanged behavior.
    artifact_slug_suffix: str = ""

    walkability_cache_dir : str = ""

    def __post_init__(self) -> None:
        self.safe_mode = str(os.environ.get("CAP_SAFE_MODE", "")).strip().lower() in ("1", "true", "yes", "on")
        self.study_city = normalize_study_city(self.study_city)
        apply_study_city(self, self.study_city)
        self.city_slug = derive_city_slug(self.city_name)
        self.artifact_slug = derive_artifact_slug(
            self.city_slug,
            self.use_shapefile,
            self.name_shapefile,
        )
        # Individual-profile runs namespace every downstream artifact/output path by
        # appending a suffix here, before any *_path is derived from artifact_slug below.
        if self.artifact_slug_suffix:
            self.artifact_slug = f"{self.artifact_slug}_{self.artifact_slug_suffix}"
        city_artifacts = os.path.join(self.artifacts_root_dir, self.artifact_slug)
        self.poi_snap_cache_dir = os.path.join(city_artifacts, "snapping", "poi_snap_cache")
        # Whole-stage snapping checkpoint (post dedup/radius filter). Lets a restart after a
        # later-stage crash (e.g. bus routing) skip re-running run_snapping_stage from scratch.
        self.snap_checkpoint_path = os.path.join(city_artifacts, "snapping", "snap_stage_checkpoint.pkl")
        # Radius-INDEPENDENT selected destination nodes, keyed by an origins+snap signature
        # (no radius). The expensive O(origins × POIs) per-origin candidate selection does not
        # depend on the radius -- radius enters only as a cheap post-filter -- so caching it
        # here lets a radius change (e.g. a sensitivity sweep over poi_radius_m) reuse the
        # selection instead of recomputing it. Shared across bus/subway.
        self.pt_selection_cache_path = os.path.join(city_artifacts, "snapping", "pt_selected_nodes.pkl")
        # Final radius-FILTERED routing destinations, keyed by an origins+snap+radius
        # signature. Cheap to rebuild from the selection cache above (one BallTree pass), but
        # kept so a restart skips even that, and so _load_pt_destination_cache_raw can resolve
        # an old run's "d{idx}" labels; shared across bus/subway.
        self.pt_destination_cache_path = os.path.join(city_artifacts, "snapping", "pt_selected_destinations.pkl")
        # Unlike bus (filtered post-hoc, see pt_destination_cache_path above), non-bus
        # (walk/bike/drive) impedance is aggregated AT ROUTING TIME from only the POIs
        # inside poi_radius_m (utils.delta_g.accessibility_non_bus_from_snap_map) -- the
        # per-node cache below stores that aggregate, not raw per-POI distances, so it
        # cannot be reused across a radius change. Its on-disk validity check also does
        # not look at radius. Bucketing by the fixed radius (when set) keeps a same-radius
        # rerun cache-hit while making a different radius a clean cache MISS instead of a
        # silent (wrong) cache HIT on another radius's aggregated values. The decay-based
        # default (poi_radius_m is None) keeps the unsuffixed path so ordinary runs are
        # unaffected.
        radius_bucket = f"_r{int(self.poi_radius_m)}" if self.poi_radius_m is not None else ""
        self.impedance_artifact_path = os.path.join(city_artifacts, f"impedances{radius_bucket}.npz")
        self.non_bus_cache_dir = os.path.join(city_artifacts, f"non_bus{radius_bucket}")
        self.non_bus_poi_catalog_path = os.path.join(city_artifacts, f"non_bus_poi_catalog{radius_bucket}.pkl")
        self.walkability_cache_dir = os.path.join(city_artifacts, "walkability")

        self.bus_routing_matrix_path = os.path.join(city_artifacts, "bus", "r5r_expanded_travel_time_matrix.csv")
        self.bus_routing_cache_path = os.path.join(city_artifacts, "bus", "r5r_best_routes.pkl")
        self.bus_routing_origins_input_path = os.path.join(city_artifacts, "bus", "r5r_origins.csv")
        self.bus_routing_destinations_input_path = os.path.join(city_artifacts, "bus", "r5r_dest.csv")
        self.bus_source_id_to_row_path = os.path.join(city_artifacts, "bus", "source_id_to_row.json")
        self.bus_dest_id_to_col_path = os.path.join(city_artifacts, "bus", "dest_id_to_col.json")
        self.bus_impedance_matrix_path = os.path.join(city_artifacts, "bus", "bus_impedance_matrix.dat")
        self.bus_impedance_meta_path = os.path.join(city_artifacts, "bus", "bus_impedance_meta.json")

        self.subway_routing_matrix_path = os.path.join(city_artifacts, "subway", "r5r_expanded_travel_time_matrix.csv")
        self.subway_routing_cache_path = os.path.join(city_artifacts, "subway", "r5r_best_routes.pkl")
        self.subway_routing_origins_input_path = os.path.join(city_artifacts, "subway", "r5r_origins.csv")
        self.subway_routing_destinations_input_path = os.path.join(city_artifacts, "subway", "r5r_dest.csv")
        self.subway_source_id_to_row_path = os.path.join(city_artifacts, "subway", "source_id_to_row.json")
        self.subway_dest_id_to_col_path = os.path.join(city_artifacts, "subway", "dest_id_to_col.json")
        self.subway_impedance_matrix_path = os.path.join(city_artifacts, "subway", "subway_impedance_matrix.dat")
        self.subway_impedance_meta_path = os.path.join(city_artifacts, "subway", "subway_impedance_meta.json")

        self.accessibility_matrix_path = os.path.join(city_artifacts, "accessibility", "accessibility_matrix.dat")
        self.accessibility_meta_path = os.path.join(city_artifacts, "accessibility", "accessibility_meta.json")
        self.accessibility_node_to_row_path = os.path.join(city_artifacts, "accessibility", "access_node_to_row.json")
        self.accessibility_poi_to_col_path = os.path.join(city_artifacts, "accessibility", "access_poi_to_col.json")
        self.accessibility_poi_by_node_path = os.path.join(city_artifacts, "accessibility", "access_poi_by_node.json")
        self.accessibility_poi_by_node_dir = os.path.join(city_artifacts, "accessibility", "poi_by_node")
        self.poi_ownership_drop_path = os.path.join(city_artifacts, "accessibility", "poi_ownership_drop.json")

        self.service_matrix_path = os.path.join(city_artifacts, "service", "service_matrix.dat")
        self.service_meta_path = os.path.join(city_artifacts, "service", "service_meta.json")
        self.service_node_to_row_path = os.path.join(city_artifacts, "service", "service_node_to_row.json")
        self.service_to_col_path = os.path.join(city_artifacts, "service", "service_to_col.json")

        self.poi_export_dir = os.path.join("outputs", "poi_exports", self.artifact_slug)
        self.poi_export_geopackage_path = os.path.join(self.poi_export_dir, "pois_used.gpkg")
        self.poi_export_shapefile_path = self.poi_export_geopackage_path
        self.hexagon_service_pois_path = os.path.join(self.poi_export_dir, "hexagon_service_pois.json")
        self.hex_pois_dir = os.path.join(self.poi_export_dir, "hex_pois")
        self.hex_pois_zip_path = os.path.join(self.poi_export_dir, "hex_pois.zip")
        self.poi_type_accessibility_path = os.path.join(self.poi_export_dir, "poi_type_accessibility.json")

    def public_transport_paths(self, transport_type: str) -> dict[str, str]:
        """Return the artifact path set for a public-transport modality.

        Bus uses the legacy ``bus/`` paths; subway/metro use the parallel ``subway/``
        paths. Keys mirror the names used by the routing stage / accessibility worker.
        """
        if transport_type in ("subway", "metro"):
            return {
                "routing_matrix": self.subway_routing_matrix_path,
                "routing_cache": self.subway_routing_cache_path,
                "routing_origins_input": self.subway_routing_origins_input_path,
                "routing_destinations_input": self.subway_routing_destinations_input_path,
                "source_id_to_row": self.subway_source_id_to_row_path,
                "dest_id_to_col": self.subway_dest_id_to_col_path,
                "impedance_matrix": self.subway_impedance_matrix_path,
            }
        return {
            "routing_matrix": self.bus_routing_matrix_path,
            "routing_cache": self.bus_routing_cache_path,
            "routing_origins_input": self.bus_routing_origins_input_path,
            "routing_destinations_input": self.bus_routing_destinations_input_path,
            "source_id_to_row": self.bus_source_id_to_row_path,
            "dest_id_to_col": self.bus_dest_id_to_col_path,
            "impedance_matrix": self.bus_impedance_matrix_path,
        }
