"""
Scenario-based analysis framework.

Allows running the capability model under different scenarios (e.g., public strike)
and comparing results. Scenarios modify routing or service availability and reuse
the same pre-computed impedance bundle to efficiently explore variations.
"""

import os
import sys
import csv
import copy
import shutil
import json
import time
import gc
import numpy as np
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from types import SimpleNamespace
import pandas as pd
import matplotlib.pyplot as plt

# Allow running directly (python analysis/scenarios.py): put the project root on sys.path and
# make it the cwd, since config paths and the outputs/scenarios/experiments dirs are relative
# to the project root. Mirrors the bootstrap in ops/*.py.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

from core.config import PipelineConfig
from core.context import build_context
from exports.artifact_bundle import load_impedance_bundle
from stages.accessibility_stage import run_accessibility_stage
from stages.service_stage import run_service_stage
from stages.capability_stage import run_capability_stage
from core.pipeline_runner import generate_spatial_outputs
from core.pipeline_runner import get_or_compute_impedances
from core.pipeline_runner import _resolve_qgis_executable, _resolve_qgis_python_launcher
from utils import graphml
from utils import poi_dedup
from utils.capabilities import CAPABILITY_COLOR_STOPS, ELECTRE_BOUNDS, ELECTRE_LABELS
from core.pipeline_types import BusRoutingStageResult, NonBusRoutingStageResult
from core.profiles import SCENARIOS, ACCESSIBLE_GTFS_PATH, NEW_METRO_GTFS_PATH


# ── Execution knobs (edit here instead of passing command-line arguments) ──────────────
# STUDY_CITY: which city these scenarios run against. Independent of main.py's study_city,
#   so main can stay on e.g. "paris" while scenarios run "cagliari". Must be a key known to
#   config.CITY_PRESETS ("cagliari", "paris", ...).
STUDY_CITY: str = "cagliari"
# SCENARIO: which comparison `python scenarios.py` runs. Every value produces the same
#   new-style export under scenarios/<SCENARIO>/ — a per-scenario ELECTRE capability hex grid
#   for each scenario, a red/yellow/green pairwise level-difference grid, a QGIS project, and a
#   per-hexagon comparison CSV for statistical testing. Allowed values:
#
#   Persona/network comparisons — each side re-routes from scratch (own modes/GTFS/walk speed);
#   keys come from profiles.SCENARIOS:
#     "elder-student"  — baseline vs. student vs. elderly
#     "new-metro"      — baseline (no metro) vs. new-metro (Cagliari bus + the Metrocagliari
#                        MCA1 line extended past REPUBBLICA to SAN SATURNINO/BONARIA/LUSSU/
#                        DARSENA/MUNICIPIO/STAZIONE, subway routing enabled). Builds
#                        gtfs/gtfs_new_metro.zip on first run (gtfs/make_new_metro_gtfs.py) from
#                        scenarios/new-metro/metro_cagliari.gpkg.
#
#   Routing-disruption comparisons — reuse the main run's impedance bundle and only perturb it,
#   so the main pipeline (python main.py) must have been run for STUDY_CITY first:
#     "public-strike"              — baseline vs. bus strike (all bus impedance → ∞, bus decay = 0.0)
#     "underservice-is-mirrionis"  — baseline vs. removing parks/supermarkets/hospitals located
#                                    in the Is Mirrionis neighbourhood (Cagliari) from the POI set
#
#   Ignored when RUN_ALL_SCENARIOS is True (below) — use ALL_SCENARIO_KEYS instead.
SCENARIO: str = "new-metro"

# RUN_ALL_SCENARIOS: when True, `python scenarios.py` runs every key in ALL_SCENARIO_KEYS in
# one process instead of just SCENARIO — no need to edit SCENARIO and rerun per comparison.
# Each entry gets the same full run_*_comparison pass SCENARIO would trigger, one after another
# (each is itself several full routing/pipeline runs — see "Fail fast": this can take a long
# time, run it yourself and watch the per-scenario progress logs rather than backgrounding it).
# A scenario whose prerequisite hasn't been met yet (e.g. a routing-disruption scenario before
# `python main.py` has been run for STUDY_CITY) fails with a clear error and the batch moves on
# to the next key instead of aborting the whole run — mirrors significance_analysis.R's
# SCENARIO_KEYS, which skips a missing comparison_per_hexagon.csv with a warning rather than a
# hard failure. A summary of which keys succeeded/failed prints at the end.
RUN_ALL_SCENARIOS: bool = True

# ALL_SCENARIO_KEYS: keys run when RUN_ALL_SCENARIOS is True. Keep in sync with
# significance_analysis.R's SCENARIO_KEYS if you want the R pass to cover the same set.
ALL_SCENARIO_KEYS: tuple[str, ...] = (
    "public-strike",
    "elder-student",
    "underservice-is-mirrionis",
    "new-metro",
)

# When the comparison QGIS project auto-opens (cfg.open_qgis_after_run), whether the
# per-scenario capability grid layers (baseline/arm, one group each) start CHECKED in the
# layer tree. They're always written into the project either way -- this only controls their
# initial visibility, so switching it off means only the Δ (difference) grids render on open,
# with the per-scenario grids one click away in the legend instead of needing to be manually
# unchecked/closed every time.
QGIS_SHOW_PER_SCENARIO_GRIDS_BY_DEFAULT: bool = False

# SKIP_QGIS_OPEN: when True, no scenario/profile comparison in this run auto-opens its QGIS
# project (overrides PipelineConfig.open_qgis_after_run, which otherwise defaults to True) --
# the .qgz project files are still written under scenarios/<key>/, just not launched. Handy with
# RUN_ALL_SCENARIOS so a multi-scenario batch doesn't pop up a QGIS window per comparison.
SKIP_QGIS_OPEN: bool = False

# CAPABILITY_SCORE_MODE: which capability-score formula this comparison run computes with
# (PipelineConfig.capability_score_mode). "discrete" (default) reproduces the exact production
# behavior -- every node's score is one of 5 ELECTRE band midpoints (0.1/0.3/0.5/0.7/0.9), so
# comparison_per_hexagon.csv's continuous columns (<cap>_<key>) carry no more information than
# the level columns. "continuous" instead writes utils.capabilities.
# electre_tri_continuous_score() -- the averaged outranking credibility, before the lambda-cut
# classification step -- giving the continuous columns real sub-band resolution to test
# significance against (see analysis/significance_analysis.R's SCORE_MODE knob). Statistics-only:
# does not change the level_<cap>_<key> columns or the per-scenario grid map's legend classes.
CAPABILITY_SCORE_MODE: str = "continuous"

# Routing-disruption scenarios (reuse the bundle) vs. persona scenarios (re-route from scratch).
# A name here dispatches to its own run_*_comparison; anything else must be a profiles.SCENARIOS key.
DISRUPTION_SCENARIOS: frozenset[str] = frozenset({"public-strike", "underservice-is-mirrionis"})


class ScenarioModifier:
    """Base class for scenario modifications."""
    
    def modify_bus_routing(
        self, 
        bus: BusRoutingStageResult,
        ctx
    ) -> BusRoutingStageResult:
        """Modify bus routing result. Override in subclasses."""
        return bus
    
    def modify_non_bus_routing(
        self,
        non_bus: NonBusRoutingStageResult,
        ctx
    ) -> NonBusRoutingStageResult:
        """Modify non-bus routing result. Override in subclasses."""
        return non_bus
    
    def poi_drop_map(self, ctx) -> Dict[str, List[str]]:
        """Extra {poi_type: [source_key, ...]} entries to merge into the POI dedup drop map.

        Accessibility treats a dropped source_key as zero-accessibility for that poi_type,
        i.e. removed — this is the hook scenarios that remove specific POIs (rather than
        perturbing routing) plug into. See IsMirrionisScenario. Override in subclasses.
        """
        return {}

    def get_name(self) -> str:
        """Return scenario name."""
        raise NotImplementedError


class BaselineScenario(ScenarioModifier):
    """Baseline scenario with no modifications."""
    
    def get_name(self) -> str:
        return "baseline"


class PublicStrikeScenario(ScenarioModifier):
    """Public strike scenario: disable all bus routing (bus decay = 0.0)."""
    
    def modify_bus_routing(
        self,
        bus: BusRoutingStageResult,
        ctx
    ) -> BusRoutingStageResult:
        """Set all bus impedances to very large values so decay = 0.0."""
        matrix_path = ctx.config.bus_impedance_matrix_path
        backup_path = matrix_path + ".backup"
        
        # Backup the original matrix if not already backed up
        if not os.path.exists(backup_path):
            shutil.copy2(matrix_path, backup_path)
            print(f"[Scenario] Backed up bus matrix to: {backup_path}", flush=True)
        else:
            # Restore from backup before modifying
            shutil.copy2(backup_path, matrix_path)
        
        # Load the bus matrix dimensions
        with open(ctx.config.bus_source_id_to_row_path, encoding="utf-8") as f:
            source_id_to_row = json.load(f)
        
        with open(ctx.config.bus_dest_id_to_col_path, encoding="utf-8") as f:
            dest_id_to_col = json.load(f)
        
        n_rows = len(source_id_to_row)
        n_cols = len(dest_id_to_col)
        
        # Set all bus impedances to a very large value (effectively infinity)
        # This makes exp(-beta * impedance) ≈ 0 for all bus routes,
        # resulting in bus decay = 0.0 (no accessibility from bus service)
        modified_matrix = np.full((n_rows, n_cols), 1e10, dtype=np.float32)
        
        # Write modified matrix
        mat = np.memmap(
            matrix_path,
            dtype=np.float32,
            mode="w+",
            shape=(n_rows, n_cols),
        )
        mat[:] = modified_matrix
        mat.flush()
        del mat  # Release the memmap
        
        print(
            "[Scenario] Modified bus matrix: all impedances set to 1e10 "
            "(bus decay = 0.0)",
            flush=True
        )
        
        return bus
    
    def get_name(self) -> str:
        return "public_strike"


# Cached Is Mirrionis boundary polygon (EPSG:4326) — geocoded once per process, since it never
# changes within a run and OSM/Nominatim lookups shouldn't be repeated per scenario pass.
_IS_MIRRIONIS_BOUNDARY_CACHE = None


def _is_mirrionis_boundary():
    """Geocode (and cache) the Is Mirrionis neighbourhood boundary polygon."""
    global _IS_MIRRIONIS_BOUNDARY_CACHE
    if _IS_MIRRIONIS_BOUNDARY_CACHE is None:
        import osmnx as ox

        gdf = ox.geocode_to_gdf("Is Mirrionis, Cagliari, Italy")
        if gdf.empty:
            raise RuntimeError("geocode_to_gdf returned no geometry for 'Is Mirrionis, Cagliari, Italy'")
        _IS_MIRRIONIS_BOUNDARY_CACHE = gdf.geometry.iloc[0]
    return _IS_MIRRIONIS_BOUNDARY_CACHE


# Whether the Is Mirrionis underservice scenario removes only the three literal OSM tags
# (leisure=park, shop=supermarket, amenity=hospital -> narrow) or every poi_type across every
# service that feeds the same capability (-> expanded; see IsMirrionisScenario's docstring for
# why the narrow version barely moves any capability's ELECTRE-TRI class). Toggle to compare
# both against the same baseline.
IS_MIRRIONIS_EXPAND_TO_CAPABILITY: bool = True

# Narrow mode: OSM tag -> poi_types it identifies (see config/poi_types.csv). Every POI of
# each identified poi_type is dropped in full — not just the subset carrying this exact tag —
# since e.g. managed_aesthetic also matches amenity=library/leisure=playground/
# leisure=recreation_ground, and those are still managed_aesthetic accessibility in Is
# Mirrionis even though they aren't literally a park.
_IS_MIRRIONIS_NARROW_TAG_TARGETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "leisure": ("park", ("managed_aesthetic", "relative_quietness", "accessible_nature")),
    "shop": ("supermarket", ("general_food_retail",)),
    "amenity": ("hospital", ("residential_healthcare", "hospital_emergency_care")),
}


def _is_mirrionis_poi_universe(cfg):
    """Load (and cache) the city-wide POI universe used by both narrow and expanded modes."""
    from utils import services as serv

    poi_cache_slug = cfg.artifact_slug if cfg.use_shapefile else cfg.city_slug
    city_poi_dir = graphml._city_poi_cache_dir(poi_cache_slug)
    buffer_m = serv.get_global_radius_m(cfg) or 0.0
    return graphml._get_city_poi_universe(cfg.city_name, poi_cache_slug, city_poi_dir, buffer_m=buffer_m)


def _is_mirrionis_representative_point(row):
    """Representative (lon, lat) Shapely Point for one POI universe row.

    `universe` is a plain DataFrame from the on-disk GeoJSON cache (deliberately not a
    GeoDataFrame — graphml._read_geojson_without_gdal avoids constructing full Shapely
    geometries for the whole city universe, since that has previously segfaulted on large
    workloads). Each row instead carries pre-extracted `(lat, lon)` vertices; average them for a
    representative point (a way's `__snap_coord` is only its first vertex, which can sit right
    at a park's/hospital's corner and miss the neighbourhood boundary). Only matched rows ever
    get a real Shapely Point built here, never the full universe.
    """
    from shapely.geometry import Point

    vertices = row.get("__snap_vertices") or (
        [row["__snap_coord"]] if row.get("__snap_coord") is not None else []
    )
    if not vertices:
        return None
    lat = sum(v[0] for v in vertices) / len(vertices)
    lon = sum(v[1] for v in vertices) / len(vertices)
    return Point(lon, lat)


def _is_mirrionis_narrow_poi_types() -> tuple[str, ...]:
    """The distinct poi_types identified by the narrow tag targets (dedup, stable order)."""
    seen: list[str] = []
    for _, poi_types in _IS_MIRRIONIS_NARROW_TAG_TARGETS.values():
        for pt in poi_types:
            if pt not in seen:
                seen.append(pt)
    return tuple(seen)


def _build_is_mirrionis_drop_map_narrow(cfg) -> Dict[str, List[str]]:
    """POI source_keys to drop: parks, supermarkets, hospitals inside Is Mirrionis (Cagliari).

    A leisure=park/shop=supermarket/amenity=hospital tag only *identifies* the poi_type
    (managed_aesthetic/relative_quietness/accessible_nature, general_food_retail,
    residential_healthcare/hospital_emergency_care — see config/poi_types.csv); every POI of
    that poi_type inside the neighbourhood is then dropped in full, via that poi_type's own
    complete tag-clause list (so a library or playground counted under managed_aesthetic is
    removed too, not just the literal parks), not just the subset that happens to carry the
    identifying tag.
    """
    from utils import services as serv
    from utils.poi_identity import build_poi_source_key

    universe = _is_mirrionis_poi_universe(cfg)
    if universe is None or universe.empty:
        print("[Scenario] Is Mirrionis: POI universe is empty, nothing to drop.", flush=True)
        return {}

    boundary = _is_mirrionis_boundary()

    # poi_type -> its own PoiQuery(s) (a poi_type can appear in more than one service).
    queries_by_poi_type: Dict[str, list] = {}
    for q in serv.all_queries():
        queries_by_poi_type.setdefault(q.poi_type, []).append(q)

    drop: Dict[str, set] = {}
    for poi_type in _is_mirrionis_narrow_poi_types():
        n_in_area = 0
        for q in queries_by_poi_type.get(poi_type, []):
            if not q.tags:
                continue
            matched = graphml._filter_by_tags(universe, q.tags)
            if matched.empty:
                continue
            for _, row in matched.iterrows():
                point = _is_mirrionis_representative_point(row)
                if point is None or not boundary.intersects(point):
                    continue
                n_in_area += 1
                source_key = build_poi_source_key(row, None)
                drop.setdefault(poi_type, set()).add(source_key)
        print(
            f"[Scenario] Is Mirrionis: removing {n_in_area} {poi_type} POIs (full poi_type, "
            "not just the identifying tag).",
            flush=True,
        )

    return {pt: sorted(keys) for pt, keys in drop.items()}


def _build_is_mirrionis_drop_map_expanded(cfg) -> Dict[str, List[str]]:
    """POI source_keys to drop: every POI feeding restorativeness/nutrition/care inside
    Is Mirrionis (Cagliari) — a full "underservice" removal per capability, not just one OSM
    tag each, so the effect isn't diluted by untouched sibling services in the same capability
    (see IsMirrionisScenario's docstring for why the narrow park/supermarket/hospital-only mode
    barely moved any capability score).

    Only capabilities.CAPABILITY_SERVICES's services are targeted (the ones
    capability_stage.py's electre_tri_integration actually sums into a capability score) —
    e.g. "impatient_and_rehabilitation" (residential_healthcare, outpatient_therapeutic_care,
    therapeutic_wellness) is configured in config/services.csv but is NOT one of the four
    services CAPABILITY_SERVICES["care"] sums into "care", so removing those POIs would have zero effect on
    the care score and only look like a bug.
    """
    from utils import capabilities as cap_mod
    from utils import services as serv
    from utils.poi_identity import build_poi_source_key

    universe = _is_mirrionis_poi_universe(cfg)
    if universe is None or universe.empty:
        print("[Scenario] Is Mirrionis: POI universe is empty, nothing to drop.", flush=True)
        return {}

    boundary = _is_mirrionis_boundary()

    drop: Dict[str, set] = {}
    for capability in IsMirrionisScenario.CAPABILITIES:
        n_capability = 0
        matched_poi_types: set[str] = set()
        for service in cap_mod.CAPABILITY_SERVICES[capability]:
            for q in serv.get_service_queries(service):
                if not q.tags:
                    continue
                matched = graphml._filter_by_tags(universe, q.tags)
                if matched.empty:
                    continue
                for _, row in matched.iterrows():
                    point = _is_mirrionis_representative_point(row)
                    if point is None or not boundary.intersects(point):
                        continue
                    n_capability += 1
                    matched_poi_types.add(q.poi_type)
                    source_key = build_poi_source_key(row, None)
                    drop.setdefault(q.poi_type, set()).add(source_key)
        print(
            f"[Scenario] Is Mirrionis: removing {n_capability} {capability}-feeding POI "
            f"instances (poi_types {sorted(matched_poi_types)}).",
            flush=True,
        )

    return {pt: sorted(keys) for pt, keys in drop.items()}


def _build_is_mirrionis_drop_map(cfg) -> Dict[str, List[str]]:
    """Dispatch to the narrow or expanded builder per IS_MIRRIONIS_EXPAND_TO_CAPABILITY."""
    if IS_MIRRIONIS_EXPAND_TO_CAPABILITY:
        return _build_is_mirrionis_drop_map_expanded(cfg)
    return _build_is_mirrionis_drop_map_narrow(cfg)


class IsMirrionisScenario(ScenarioModifier):
    """Underservice scenario: remove parks, supermarkets, and hospitals (or, in expanded mode,
    every POI feeding restorativeness/nutrition/care) located inside the Is Mirrionis
    neighbourhood (Cagliari) from the POI set feeding accessibility.

    Unlike PublicStrikeScenario (which perturbs routing/impedance), this scenario doesn't touch
    routing at all — it drops specific POIs post-routing via the same {poi_type: [source_key]}
    "drop map" mechanism the POI-service dedup uses (see utils/poi_dedup.py), so a dropped POI's
    per-POI accessibility is zeroed exactly as if it didn't exist.

    Narrow mode (IS_MIRRIONIS_EXPAND_TO_CAPABILITY = False) identifies its 6 target poi_types
    via one OSM tag each (leisure=park, shop=supermarket, amenity=hospital), but then drops every
    POI of those poi_types in full — via each poi_type's own complete tag-clause list, not just
    the subset carrying the identifying tag (e.g. a library or playground counted under
    managed_aesthetic is removed too, not just literal parks). It still tends to barely move
    restorativeness/care's ELECTRE-TRI class, because each of the three modeled capabilities
    blends 2-5 sibling *services* (see config/services.csv / utils.capabilities.
    CAPABILITY_SERVICES) and narrow mode only ever touches one service per capability — the
    untouched sibling services keep propping the aggregate up past the classification threshold.
    Expanded mode instead removes every poi_type across every service that actually feeds a given
    capability (utils.capabilities.CAPABILITY_SERVICES[capability]), so the whole
    capability is starved locally instead of one service's worth of it.
    """

    CAPABILITIES = ("restorativeness", "nutrition", "care")

    def poi_drop_map(self, ctx) -> Dict[str, List[str]]:
        return _build_is_mirrionis_drop_map(ctx.config)

    def get_name(self) -> str:
        return "underservice_is_mirrionis"


class ScenarioRunner:
    """Run and compare scenarios."""
    
    def __init__(self, cfg: PipelineConfig):
        self.cfg = cfg
        self.ctx = build_context(cfg)
        self.scenarios: Dict[str, Dict] = {}
        self.scenario_results_map: Dict[str, Dict[str, str]] = {}
    
    def run_scenario(
        self,
        modifier: ScenarioModifier,
        output_suffix: str
    ) -> Dict:
        """Run a scenario and return results."""
        print(f"\n[Scenario] Running: {modifier.get_name()}", flush=True)
        
        # Backup bus matrix before modifications
        matrix_path = self.ctx.config.bus_impedance_matrix_path
        backup_path = matrix_path + ".scenario_backup"
        if os.path.exists(matrix_path):
            shutil.copy2(matrix_path, backup_path)
        
        try:
            # Load the impedance bundle
            loaded = load_impedance_bundle(self.ctx)
            if loaded is None:
                print(
                    "[Scenario] ERROR: No impedance bundle found. "
                    "Please run the baseline pipeline first.",
                    flush=True,
                )
                return None
            
            bus, non_bus = loaded
            
            # Apply scenario modifications
            bus = modifier.modify_bus_routing(bus, self.ctx)
            non_bus = modifier.modify_non_bus_routing(non_bus, self.ctx)
            
            # Run accessibility stage
            print("[Stage] Accessibility (Scenario)", flush=True)
            acc = run_accessibility_stage(self.ctx, non_bus, bus)
            
            # Run service stage
            print("[Stage] Service Aggregation (Scenario)", flush=True)
            svc = run_service_stage(self.ctx, acc)
            
            # Temporarily modify artifact_slug to get scenario-specific output paths
            original_artifact_slug = self.ctx.config.artifact_slug
            self.ctx.config.artifact_slug = f"{original_artifact_slug}_{output_suffix}"
            
            # Temporarily modify output paths
            original_output_paths = self.ctx.output_paths.copy()
            os.makedirs("outputs", exist_ok=True)
            self.ctx.output_paths = {
                key: os.path.join("outputs", f"capability_{key}_{output_suffix}.csv")
                for key in original_output_paths.keys()
            }
            
            # Run capability stage
            print("[Stage] Capability Aggregation (Scenario)", flush=True)
            cap = run_capability_stage(self.ctx, svc)

            # Generate scenario spatial outputs (shapefile + gpkg).
            spatial_outputs = generate_spatial_outputs(self.ctx.config, cap)
            
            # Restore original settings
            self.ctx.config.artifact_slug = original_artifact_slug
            self.ctx.output_paths = original_output_paths
            
            print(
                f"[Output] Scenario results: "
                + ", ".join(str(path) for path in cap.output_paths.values()),
                flush=True,
            )
            
            # Store scenario results for later comparison
            self.scenario_results_map[modifier.get_name()] = cap.output_paths
            
            return {
                "name": modifier.get_name(),
                "output_suffix": output_suffix,
                "output_paths": cap.output_paths,
                "accessibility": acc,
                "service": svc,
                "capability": cap,
                "spatial_outputs": spatial_outputs,
            }
        
        finally:
            # Restore original bus matrix after scenario completes
            if os.path.exists(backup_path):
                shutil.copy2(backup_path, matrix_path)
                os.remove(backup_path)
                print(f"[Scenario] Restored original bus matrix", flush=True)
    
    def load_capability_results(self, capability_paths: Dict[str, str]) -> pd.DataFrame:
        """Load capability results from CSV files."""
        dfs = {}
        for cap_type, path in capability_paths.items():
            if os.path.exists(path):
                df = pd.read_csv(path)
                dfs[cap_type] = df
        
        if not dfs:
            return None
        
        # Merge all capability CSVs on node_id
        merged = dfs[list(dfs.keys())[0]][["node_id", "lat", "lon"]].copy()
        for cap_type, df in dfs.items():
            merged[f"capability_{cap_type}"] = df["capability_" + cap_type]
        
        return merged
    
    def compare_scenarios(
        self,
        scenario_results: List[Dict]
    ) -> pd.DataFrame:
        """Compare results across scenarios."""
        scenario_dfs = {}
        for result in scenario_results:
            df = self.load_capability_results(result["output_paths"])
            if df is not None:
                scenario_dfs[result["name"]] = df
        
        if not scenario_dfs:
            print("[Comparison] No results to compare", flush=True)
            return None
        
        # Merge all scenarios
        comparison = scenario_dfs[list(scenario_dfs.keys())[0]].copy()
        comparison = comparison.rename(
            columns={
                col: f"{col}_{list(scenario_dfs.keys())[0]}"
                for col in comparison.columns
                if col.startswith("capability_")
            }
        )
        
        for scenario_name in list(scenario_dfs.keys())[1:]:
            df = scenario_dfs[scenario_name]
            for col in df.columns:
                if col.startswith("capability_"):
                    comparison[f"{col}_{scenario_name}"] = df[col]
        
        # Compute differences from baseline
        baseline_name = list(scenario_dfs.keys())[0]
        scenario_names_for_diff = list(scenario_dfs.keys())[1:]
        
        for scenario_name in scenario_names_for_diff:
            for cap_type in ["restorativeness", "nutrition", "care"]:
                baseline_col = f"capability_{cap_type}_{baseline_name}"
                scenario_col = f"capability_{cap_type}_{scenario_name}"
                diff_col = f"diff_{cap_type}_{scenario_name}"
                
                if baseline_col in comparison.columns and scenario_col in comparison.columns:
                    comparison[diff_col] = comparison[scenario_col] - comparison[baseline_col]
        
        return comparison
    
    def visualize_comparison(
        self,
        comparison_df: pd.DataFrame,
        scenario_name: str,
        output_dir: str = None
    ) -> None:
        """Create visualizations comparing scenarios."""
        if output_dir is None:
            output_dir = os.path.join("scenarios", scenario_name)
        
        os.makedirs(output_dir, exist_ok=True)
        
        # Get all capability columns
        capability_cols = [col for col in comparison_df.columns if col.startswith("capability_")]
        scenario_names = list(set([col.rsplit("_", 1)[-1] for col in capability_cols]))
        capability_types = ["restorativeness", "nutrition", "care"]
        
        for cap_type in capability_types:
            fig, axes = plt.subplots(1, 2, figsize=(14, 5))
            
            # Plot 1: Distribution comparison
            ax = axes[0]
            for scen_name in scenario_names:
                col = f"capability_{cap_type}_{scen_name}"
                if col in comparison_df.columns:
                    ax.hist(
                        comparison_df[col].dropna(),
                        alpha=0.6,
                        label=scen_name,
                        bins=30
                    )
            ax.set_xlabel("Capability Score")
            ax.set_ylabel("Frequency")
            ax.set_title(f"Distribution: Capability {cap_type}")
            ax.legend()
            ax.grid(True, alpha=0.3)
            
            # Plot 2: Differences from baseline
            if len(scenario_names) > 1:
                ax = axes[1]
                for scen_name in scenario_names[1:]:
                    diff_col = f"diff_{cap_type}_{scen_name}"
                    if diff_col in comparison_df.columns:
                        ax.hist(
                            comparison_df[diff_col].dropna(),
                            alpha=0.6,
                            label=f"vs {scen_name}",
                            bins=30
                        )
                ax.set_xlabel("Capability Difference")
                ax.set_ylabel("Frequency")
                ax.set_title(f"Impact on Capability {cap_type}")
                ax.legend()
                ax.grid(True, alpha=0.3)
                ax.axvline(x=0, color="black", linestyle="--", alpha=0.5)
            
            plt.tight_layout()
            output_path = os.path.join(output_dir, f"comparison_{cap_type}.png")
            plt.savefig(output_path, dpi=150, bbox_inches="tight")
            print(f"[Output] Saved plot: {output_path}", flush=True)
            plt.close()
        
        # Summary statistics
        summary_path = os.path.join(output_dir, "scenario_summary.csv")
        with open(summary_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Scenario", "Capability Type", "Mean", "Median", "Std Dev", "Min", "Max"])
            
            for scen_name in scenario_names:
                for cap_type in capability_types:
                    col = f"capability_{cap_type}_{scen_name}"
                    if col in comparison_df.columns:
                        data = comparison_df[col].dropna()
                        writer.writerow([
                            scen_name,
                            cap_type,
                            f"{data.mean():.4f}",
                            f"{data.median():.4f}",
                            f"{data.std():.4f}",
                            f"{data.min():.4f}",
                            f"{data.max():.4f}",
                        ])
        
        print(f"[Output] Saved summary: {summary_path}", flush=True)

    def rebuild_baseline_gpkg_from_csv(self, baseline_gpkg: Path, experiments_dir: str = "experiments") -> bool:
        """Rebuild baseline geopackage from CSV data in experiments directory.
        
        Returns True if rebuilt successfully, False otherwise.
        """
        import geopandas as gpd
        import pandas as pd
        
        print(f"[Baseline] Attempting to rebuild from CSV files in {experiments_dir}", flush=True)
        
        # Load baseline CSVs
        baseline_csvs = {}
        if os.path.isdir(experiments_dir):
            for cap_type in ["restorativeness", "nutrition", "care"]:
                pattern = f"capability_{cap_type}.csv"
                for filename in sorted(os.listdir(experiments_dir), reverse=True):
                    if pattern in filename:
                        path = os.path.join(experiments_dir, filename)
                        if os.path.isfile(path):
                            baseline_csvs[cap_type] = path
                            break
        
        if not baseline_csvs or len(baseline_csvs) < 3:
            print("[Baseline] Could not find all required CSV files", flush=True)
            return False
        
        # Load and merge CSVs
        try:
            baseline_df = pd.read_csv(baseline_csvs["restorativeness"])
            baseline_df["capability_nutrition"] = pd.read_csv(baseline_csvs["nutrition"])["capability_nutrition"]
            baseline_df["capability_care"] = pd.read_csv(baseline_csvs["care"])["capability_care"]
            
            # Convert to GeoDataFrame
            from shapely.geometry import Point
            geometry = [Point(xy) for xy in zip(baseline_df.lon, baseline_df.lat)]
            baseline_gdf = gpd.GeoDataFrame(
                baseline_df[["node_id", "lat", "lon", "capability_restorativeness", "capability_nutrition", "capability_care"]],
                geometry=geometry,
                crs="EPSG:4326"
            )
            
            # Ensure output directory exists
            baseline_gpkg.parent.mkdir(parents=True, exist_ok=True)
            
            # Write to geopackage
            baseline_gdf.to_file(baseline_gpkg, layer="capability_points", driver="GPKG")
            print(f"[Baseline] Successfully rebuilt from CSV: {baseline_gpkg}", flush=True)
            return True
            
        except Exception as e:
            print(f"[Baseline] Failed to rebuild geopackage: {e}", flush=True)
            return False

    def create_scenario_comparison_project(
        self,
        baseline_gpkg: str | Path,
        scenario_gpkg: str | Path,
        scenario_name: str,
    ) -> Path | None:
        """Create a QGIS project with baseline and scenario layers side-by-side."""
        qgis_exe = _resolve_qgis_executable(self.cfg)
        if not qgis_exe:
            print("[QGIS] Comparison project skipped: QGIS executable not found.", flush=True)
            return None

        python_launcher = _resolve_qgis_python_launcher(qgis_exe)
        if python_launcher is None:
            print("[QGIS] Comparison project skipped: python-qgis launcher not found.", flush=True)
            return None

        baseline_gpkg = Path(baseline_gpkg)
        scenario_gpkg = Path(scenario_gpkg)
        if not baseline_gpkg.exists() or not scenario_gpkg.exists():
            print(
                f"[QGIS] Comparison project skipped: missing inputs baseline={baseline_gpkg.exists()} scenario={scenario_gpkg.exists()}",
                flush=True,
            )
            return None

        output_dir = Path("scenarios") / scenario_name
        output_dir.mkdir(parents=True, exist_ok=True)
        output_project = output_dir / "scenario_comparison.qgz"

        baseline_literal = repr(str(baseline_gpkg))
        scenario_literal = repr(str(scenario_gpkg))
        output_literal = repr(str(output_project))
        field_literal = repr(str(self.cfg.default_capability))
        ramp_literal = repr(str(self.cfg.qgis_autostyle_ramp))
        basemap_flag = "True" if self.cfg.show_basemap else "False"

        script = f"""
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsGradientColorRamp,
    QgsGraduatedSymbolRenderer,
    QgsProject,
    QgsRasterLayer,
    QgsRendererRange,
    QgsStyle,
    QgsSymbol,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor

app = QgsApplication([], False)
app.initQgis()
project = QgsProject.instance()
project.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))

if {basemap_flag}:
    osm_uri = "type=xyz&url=https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png&zmin=0&zmax=19&crs=EPSG:3857"
    osm_layer = QgsRasterLayer(osm_uri, "OSM", "wms")
    if osm_layer.isValid():
        project.addMapLayer(osm_layer)

def load_layer(gpkg_path, layer_name):
    layer = QgsVectorLayer(gpkg_path + "|layername=capability_points", layer_name, "ogr")
    if not layer.isValid():
        layer = QgsVectorLayer(gpkg_path, layer_name, "ogr")
    return layer

def style_layer(layer):
    if not layer.isValid():
        return
    available_fields = [field.name() for field in layer.fields()]
    preferred_fields = [{field_literal}, "capability_care", "capability_restorativeness", "capability_nutrition", "value"]
    field_name = next((name for name in preferred_fields if name in available_fields), None)
    if not field_name:
        return
    electre_bounds = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    electre_labels = ["Very Low (0.0–0.2)", "Low (0.2–0.4)", "Medium (0.4–0.6)", "High (0.6–0.8)", "Very High (0.8–1.0)"]
    n_cls = len(electre_labels)
    ramp = QgsStyle.defaultStyle().colorRamp({ramp_literal})
    if ramp is None:
        ramp = QgsGradientColorRamp(QColor("#440154"), QColor("#FDE725"))
    ranges = []
    for i, lbl in enumerate(electre_labels):
        sym = QgsSymbol.defaultSymbol(layer.geometryType())
        if sym is None:
            continue
        sym.setColor(ramp.color(float(i) / max(1, n_cls - 1)))
        ranges.append(QgsRendererRange(electre_bounds[i], electre_bounds[i + 1], sym, lbl))
    renderer = QgsGraduatedSymbolRenderer(field_name, ranges) if ranges else QgsGraduatedSymbolRenderer()
    layer.setRenderer(renderer)

baseline_layer = load_layer({baseline_literal}, "Scenario 1 - Baseline")
scenario_layer = load_layer({scenario_literal}, "Scenario 2 - {scenario_name}")

if not baseline_layer.isValid() and not scenario_layer.isValid():
    raise SystemExit("Could not load either scenario layer.")

if baseline_layer.isValid():
    style_layer(baseline_layer)
    project.addMapLayer(baseline_layer)
if scenario_layer.isValid():
    style_layer(scenario_layer)
    project.addMapLayer(scenario_layer)
    scenario_layer.setOpacity(0.65)

project.write({output_literal})

# Zoom to layers
if baseline_layer.isValid() and scenario_layer.isValid():
    extent = baseline_layer.extent()
    extent.combineExtentWith(scenario_layer.extent())
elif baseline_layer.isValid():
    extent = baseline_layer.extent()
else:
    extent = scenario_layer.extent()

canvas = project.layerTreeRoot()
if canvas:
    canvas.findLayer(baseline_layer.id()).setExpanded(True) if baseline_layer.isValid() else None
    canvas.findLayer(scenario_layer.id()).setExpanded(True) if scenario_layer.isValid() else None

app.exitQgis()
"""

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
            handle.write(script)
            script_path = handle.name

        try:
            subprocess.run([python_launcher, script_path], check=True)
        except subprocess.CalledProcessError as exc:
            print(f"[QGIS] Failed to create scenario comparison project: {exc}", flush=True)
            return None

        print(f"[QGIS] Scenario comparison project: {output_project}", flush=True)
        return output_project


class ProfileRunner:
    """Run individual-profile (persona) pipelines from scratch and compare them.

    Unlike ScenarioRunner (which reuses one impedance bundle and only perturbs the routing
    matrices), each persona here gets a *full* pipeline pass — snapping + routing at the
    persona's walk speed / mode set / GTFS feed — into its own artifact namespace, then the
    per-POI affordability utility u(y) is applied in the accessibility stage via ctx.profile.
    """

    def __init__(self, study_city_name: str):
        self.study_city = study_city_name

    def _ensure_accessible_gtfs(self) -> None:
        out = Path(ACCESSIBLE_GTFS_PATH)
        if out.exists():
            print(f"[Profile] Accessible-stops GTFS already present: {out}", flush=True)
            return
        print("[Profile] Building accessible-stops GTFS feed...", flush=True)
        subprocess.run(
            [sys.executable, os.path.join("gtfs", "make_accessible_gtfs.py")],
            check=True,
        )

    def _ensure_new_metro_gtfs(self) -> None:
        out = Path(NEW_METRO_GTFS_PATH)
        if out.exists():
            print(f"[Profile] New-metro GTFS already present: {out}", flush=True)
            return
        print("[Profile] Building new-metro (REPUBBLICA-STAZIONE extension) GTFS feed...", flush=True)
        subprocess.run(
            [sys.executable, os.path.join("gtfs", "make_new_metro_gtfs.py")],
            check=True,
        )

    def run_persona(self, profile) -> Dict:
        """Run the full capability pipeline for one persona; return its cap + gpkg path."""
        if profile.pt_accessible_stops_only:
            self._ensure_accessible_gtfs()
        if NEW_METRO_GTFS_PATH in profile.extra_config_overrides.get("gtfs_feeds", ()):
            self._ensure_new_metro_gtfs()

        os.environ["CAP_STUDY_CITY"] = self.study_city
        cfg = PipelineConfig(study_city=self.study_city, **profile.config_overrides())
        cfg.capability_score_mode = CAPABILITY_SCORE_MODE
        if SKIP_QGIS_OPEN:
            cfg.open_qgis_after_run = False
        print(
            f"\n[Profile] === {profile.key} ===  slug={cfg.artifact_slug}  "
            f"non_bus_modes={cfg.enabled_non_bus_modes}  walk={cfg.speed_walk_kmh}km/h  "
            f"gtfs={cfg.gtfs_feeds}",
            flush=True,
        )

        # New config (different GTFS/modes) -> drop cached graphs so this persona's feed and
        # mode set take effect instead of a previous run's.
        graphml._GRAPH_CACHE = None
        graphml._MODE_GRAPH_CACHE = {}
        graphml._CITY_POI_UNIVERSE_CACHE = {}
        graphml._POI_DOWNLOAD_LOGGED = set()

        ctx = build_context(cfg)
        # Read by the accessibility stage for the per-POI utility multiplier u(y).
        ctx.profile = profile

        bus, non_bus = get_or_compute_impedances(ctx, cfg)

        # Light post-routing sequence (no hex/interface exports): only the capability CSVs
        # and the spatial gpkg are needed for the comparison.
        print("[Stage] Accessibility (profile)", flush=True)
        acc = run_accessibility_stage(ctx, non_bus, bus)
        print("[Stage] Service Aggregation (profile)", flush=True)
        svc = run_service_stage(ctx, acc)
        print("[Stage] Capability Aggregation (profile)", flush=True)
        cap = run_capability_stage(ctx, svc)

        spatial = generate_spatial_outputs(cfg, cap)
        return {
            "profile": profile,
            "cfg": cfg,
            "cap": cap,
            "gpkg_path": spatial.get("gpkg_path"),
        }


# ELECTRE class bands: a capability score in [0,1] maps to an integer level 1..5
# (Very Low .. Very High). Sourced from utils.capabilities so this never drifts from the
# bounds/colors main.py's own QGIS export uses for the same capability grids.
_ELECTRE_UPPER_BOUNDS = tuple(ELECTRE_BOUNDS[1:-1])       # internal band edges: (0.2,0.4,0.6,0.8)

# Red–Yellow–Green diverging palette for level differences −4..+4 (ColorBrewer RdYlGn-9).
# Index 0 == −4 (deepest red, second scenario 4 levels lower), 4 == 0 (yellow, same),
# 8 == +4 (deepest green, second scenario 4 levels higher).
_DIFF_LEVELS = (-4, -3, -2, -1, 0, 1, 2, 3, 4)
_DIFF_COLORS = (
    "#a50026", "#d73027", "#f46d43", "#fdae61", "#ffffbf",
    "#a6d96a", "#66bd63", "#1a9850", "#006837",
)


def _capability_level(series):
    """Map continuous capability scores in [0,1] to integer ELECTRE levels 1..5.

    NaN scores stay NaN (so no-data cells are skipped downstream).
    """
    import numpy as np
    import pandas as pd

    values = pd.to_numeric(series, errors="coerce")
    levels = np.digitize(values.to_numpy(dtype=float), _ELECTRE_UPPER_BOUNDS) + 1
    out = pd.Series(levels, index=series.index, dtype="float64")
    out[values.isna()] = np.nan
    return out


def _load_grid_layer(gpkg_path):
    """Read the hexagon capability-grid layer (hex_id, geometry, grid_mean_*, has_data)."""
    import geopandas as gpd
    from exports.generate_experiment_shapefiles import GRID_TABLE_NAME

    return gpd.read_file(gpkg_path, layer=GRID_TABLE_NAME)


def build_profile_difference_gpkg(results: List[Dict], out_gpkg: str) -> Dict[tuple, Dict[str, str]]:
    """Build a gpkg of pairwise per-capability level-difference grids.

    For every scenario pair (A, B) in listing order and every capability, each hexagon gets
    ``d_<capability> = level_B − level_A`` (negative = B lower → red, positive = B higher →
    green, 0 = same → yellow). Cells lacking data in either scenario are left NULL.

    Each (pair, capability) gets its own layer/table (``diff_<A>_to_<B>__<capability>``), one
    ``d`` field each — mirroring the per-capability grid *views* ``pipeline_runner.py`` uses for
    the main capability grids. That's what lets a default style be embedded per capability via
    ``saveStyleToDatabase`` (a GeoPackage table has only one default style, so packing every
    capability into one shared table would leave two of the three unstyled on a plain open).

    Returns a mapping ``(key_A, key_B) -> {capability: layer_name}``.
    """
    import itertools

    from core.profiles import CAPABILITIES

    grids: Dict[str, object] = {}
    for r in results:
        gpkg = r.get("gpkg_path")
        if not gpkg or not Path(gpkg).exists():
            raise FileNotFoundError(f"missing grid gpkg for {r['profile'].key}: {gpkg}")
        grids[r["profile"].key] = _load_grid_layer(gpkg)

    keys = [r["profile"].key for r in results]

    # Per-scenario per-capability levels, indexed by hex_id.
    levels_by_key: Dict[str, object] = {}
    geom_by_hex = None
    for k in keys:
        g = grids[k].copy()
        has_data = g["has_data"].astype(bool) if "has_data" in g.columns else True
        frame = g[["hex_id"]].copy()
        for cap in CAPABILITIES:
            col = f"grid_mean_{cap}"
            lvl = _capability_level(g[col]) if col in g.columns else None
            if lvl is not None:
                # blank out no-data cells so they don't read as level 1
                lvl = lvl.where(has_data)
            frame[cap] = lvl
        levels_by_key[k] = frame.set_index("hex_id")
        if geom_by_hex is None:
            geom_by_hex = grids[k][["hex_id", "geometry"]].set_index("hex_id")

    out_path = Path(out_gpkg)
    if out_path.exists():
        out_path.unlink()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    import geopandas as gpd

    pair_layers: Dict[tuple, Dict[str, str]] = {}
    for i, j in itertools.combinations(range(len(keys)), 2):
        a, b = keys[i], keys[j]
        la, lb = levels_by_key[a], levels_by_key[b]
        common = la.index.intersection(lb.index)
        cap_layers: Dict[str, str] = {}
        for cap in CAPABILITIES:
            diff = gpd.GeoDataFrame(
                {
                    "hex_id": common,
                    f"d_{cap}": (lb.loc[common, cap] - la.loc[common, cap]).values,
                },
                geometry=geom_by_hex.loc[common, "geometry"].values,
                crs=grids[a].crs,
            )
            layer = f"diff_{a}_to_{b}__{cap}"
            diff.to_file(out_path, layer=layer, driver="GPKG")
            cap_layers[cap] = layer
        pair_layers[(a, b)] = cap_layers
        print(f"[Diff] {a} → {b}: {len(common)} cells  (layers {list(cap_layers.values())})", flush=True)

    return pair_layers


def export_comparison_csv(results: List[Dict], out_csv: str) -> "str | None":
    """Write one wide CSV — one row per hexagon — for downstream statistical testing.

    Columns: ``hex_id``, ``centroid_lon``/``centroid_lat``, then for every scenario the
    continuous capability score (``<capability>_<key>``) and its ELECTRE level
    (``level_<capability>_<key>``), and for every scenario pair the continuous and level
    differences (``dcont_<capability>_<a>_to_<b>`` = B − A, ``dlevel_<capability>_<a>_to_<b>``).
    Hexagons are aligned by ``hex_id`` (the grid geometry is shared across scenarios), so each
    row pairs the same location across scenarios — ready for a paired test (Wilcoxon/paired-t).
    No-data cells are left blank.
    """
    import itertools
    import numpy as np
    import pandas as pd

    from core.profiles import CAPABILITIES

    grids: Dict[str, object] = {}
    for r in results:
        gpkg = r.get("gpkg_path")
        if not gpkg or not Path(gpkg).exists():
            raise FileNotFoundError(f"missing grid gpkg for {r['profile'].key}: {gpkg}")
        grids[r["profile"].key] = _load_grid_layer(gpkg)

    keys = [r["profile"].key for r in results]

    # Continuous scores + ELECTRE levels per scenario, indexed by hex_id.
    cont_by_key: Dict[str, object] = {}
    level_by_key: Dict[str, object] = {}
    base_frame = None
    for k in keys:
        g = grids[k].copy()
        has_data = g["has_data"].astype(bool) if "has_data" in g.columns else True
        frame = g[["hex_id"]].copy()
        for cap in CAPABILITIES:
            col = f"grid_mean_{cap}"
            vals = pd.to_numeric(g[col], errors="coerce") if col in g.columns else np.nan
            if col in g.columns:
                vals = vals.where(has_data)
            frame[cap] = vals
        cont_by_key[k] = frame.set_index("hex_id")
        lvl_frame = g[["hex_id"]].copy()
        for cap in CAPABILITIES:
            col = f"grid_mean_{cap}"
            lvl = _capability_level(g[col]) if col in g.columns else None
            if lvl is not None:
                lvl = lvl.where(has_data)
            lvl_frame[cap] = lvl
        level_by_key[k] = lvl_frame.set_index("hex_id")
        if base_frame is None:
            # Centroid in the grid's own (projected) CRS, then reprojected to lon/lat — accurate
            # and warning-free vs. taking a centroid directly on geographic coordinates.
            cent = g.geometry.centroid
            if g.crs is not None:
                import geopandas as gpd
                cent = gpd.GeoSeries(cent, crs=g.crs).to_crs("EPSG:4326")
            centroids = cent
            base_frame = pd.DataFrame(
                {
                    "hex_id": g["hex_id"].values,
                    "centroid_lon": centroids.x.values,
                    "centroid_lat": centroids.y.values,
                }
            ).set_index("hex_id")

    out = base_frame.copy()
    for k in keys:
        for cap in CAPABILITIES:
            out[f"{cap}_{k}"] = cont_by_key[k][cap]
            out[f"level_{cap}_{k}"] = level_by_key[k][cap]

    for i, j in itertools.combinations(range(len(keys)), 2):
        a, b = keys[i], keys[j]
        for cap in CAPABILITIES:
            out[f"dcont_{cap}_{a}_to_{b}"] = cont_by_key[b][cap] - cont_by_key[a][cap]
            out[f"dlevel_{cap}_{a}_to_{b}"] = level_by_key[b][cap] - level_by_key[a][cap]

    out_path = Path(out_csv)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.reset_index().to_csv(out_path, index=False)
    print(f"[Output] Per-hexagon comparison CSV: {out_path}  ({len(out)} rows)", flush=True)
    return str(out_path)


def create_profile_comparison_project(
    results: List[Dict],
    diff_gpkg: str,
    pair_layers: Dict[tuple, Dict[str, str]],
    scenario_key: str,
    cfg,
) -> "Path | None":
    """Build a QGIS project with per-scenario capability grids and pairwise-difference grids."""
    qgis_exe = _resolve_qgis_executable(cfg)
    if not qgis_exe:
        print("[QGIS] Comparison project skipped: QGIS executable not found.", flush=True)
        return None
    python_launcher = _resolve_qgis_python_launcher(qgis_exe)
    if python_launcher is None:
        print("[QGIS] Comparison project skipped: python-qgis launcher not found.", flush=True)
        return None

    from exports.generate_experiment_shapefiles import GRID_TABLE_NAME
    from core.profiles import CAPABILITIES

    out_dir = Path("scenarios") / scenario_key
    out_dir.mkdir(parents=True, exist_ok=True)
    output_project = out_dir / "profile_comparison.qgz"

    scenario_gpkgs = {r["profile"].key: str(r["gpkg_path"]) for r in results}
    # pair_layers keys are tuples, values are {capability: layer_name} -> flatten for JSON.
    pair_layers_json = [
        [a, b, cap, layer]
        for (a, b), cap_layers in pair_layers.items()
        for cap, layer in cap_layers.items()
    ]

    # Per-capability 5-shade color maps, identical to main.py's own capability-grid styling:
    # the shared CAPABILITY_COLOR_STOPS scale, same 5 colors for every capability.
    capability_shades = {cap: CAPABILITY_COLOR_STOPS for cap in CAPABILITIES}

    payload = {
        "scenario_gpkgs": scenario_gpkgs,
        "scenario_order": [r["profile"].key for r in results],
        "grid_table": GRID_TABLE_NAME,
        "capabilities": list(CAPABILITIES),
        "cap_labels": list(ELECTRE_LABELS),
        "cap_bounds": list(ELECTRE_BOUNDS),
        "capability_shades": capability_shades,
        "diff_gpkg": str(diff_gpkg),
        "pair_layers": pair_layers_json,
        "diff_levels": list(_DIFF_LEVELS),
        "diff_colors": list(_DIFF_COLORS),
        "output_project": str(output_project),
        "basemap": bool(cfg.show_basemap),
        "show_per_scenario_grids_by_default": QGIS_SHOW_PER_SCENARIO_GRIDS_BY_DEFAULT,
        "default_capability": str(cfg.default_capability).replace("capability_", ""),
    }

    script = f"""
import json
from qgis.core import (
    QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsFillSymbol, QgsGraduatedSymbolRenderer, QgsProject, QgsRasterLayer,
    QgsReferencedRectangle, QgsRectangle, QgsRendererRange, QgsSymbol,
    QgsVectorLayer, QgsLayerTreeGroup,
)
from qgis.PyQt.QtGui import QColor

P = json.loads({json.dumps(json.dumps(payload))})

app = QgsApplication([], False)
app.initQgis()
project = QgsProject.instance()
project_crs = QgsCoordinateReferenceSystem("EPSG:3857")
project.setCrs(project_crs)
root = project.layerTreeRoot()

ELECTRE_BOUNDS = P["cap_bounds"]


def style_electre(layer, field, shades, labels):
    # Same shared CAPABILITY_COLOR_STOPS 5-shade scheme main.py's own capability grids
    # use, instead of a generic named color ramp.
    ranges = []
    for i, lbl in enumerate(labels):
        sym = QgsFillSymbol.createSimple({{"style": "solid", "color": shades[i]}})
        ranges.append(QgsRendererRange(ELECTRE_BOUNDS[i], ELECTRE_BOUNDS[i + 1], sym, lbl))
    layer.setRenderer(QgsGraduatedSymbolRenderer(field, ranges))


def style_diff(layer, field, levels, colors):
    ranges = []
    for lvl, col in zip(levels, colors):
        sym = QgsSymbol.defaultSymbol(layer.geometryType())
        if sym is None:
            continue
        sym.setColor(QColor(col))
        if lvl == 0:
            lbl = "0 (same level)"
        elif lvl < 0:
            lbl = "%d (second %d lower)" % (lvl, -lvl)
        else:
            lbl = "+%d (second %d higher)" % (lvl, lvl)
        ranges.append(QgsRendererRange(lvl - 0.5, lvl + 0.5, sym, lbl))
    layer.setRenderer(QgsGraduatedSymbolRenderer(field, ranges))


def embed_style(layer, name, is_default=True):
    # Persist the renderer into the GeoPackage's layer_styles table (same mechanism
    # pipeline_runner.py uses for the main capability grids) so opening the .gpkg file
    # directly -- outside this .qgz project -- still shows the intended colors, not
    # QGIS's default symbology.
    layer.saveStyleToDatabase(name, "", is_default, "")


extent = None


def _grow_extent(layer):
    global extent
    if layer is None or not layer.isValid():
        return
    layer_extent = layer.extent()
    if layer.crs() != project_crs:
        try:
            xform = QgsCoordinateTransform(layer.crs(), project_crs, project)
            layer_extent = xform.transformBoundingBox(layer_extent)
        except Exception:
            return
    if extent is None:
        extent = QgsRectangle(layer_extent)
    else:
        extent.combineExtentWith(layer_extent)


def add_grid(gpkg, table, name, group):
    layer = QgsVectorLayer(gpkg + "|layername=" + table, name, "ogr")
    if not layer.isValid():
        print("  [skip] invalid layer: " + name)
        return None
    project.addMapLayer(layer, False)
    group.addLayer(layer)
    _grow_extent(layer)
    return layer


# Build order controls stacking order: groups added earlier end up higher in the layer tree
# (rendered on top). Diff grids first (so they draw on top of the scenario grids beneath
# them), then per-scenario capability grids, then the OSM basemap forced to the very bottom.

# --- Pairwise difference grids (red/yellow/green by level difference), on top -----------
diff_groups = {{}}
for a, b, cap, layer_name in P["pair_layers"]:
    group = diff_groups.get((a, b))
    if group is None:
        group = root.addGroup("Δ " + a + " → " + b)
        group.setExpanded(False)
        diff_groups[(a, b)] = group
    layer = add_grid(P["diff_gpkg"], layer_name, a + "→" + b + " — " + cap, group)
    if layer is not None:
        style_diff(layer, "d_" + cap, P["diff_levels"], P["diff_colors"])
        embed_style(layer, layer_name)

# --- Per-scenario capability grids, underneath the diff grids ---------------------------
for key in P["scenario_order"]:
    gpkg = P["scenario_gpkgs"][key]
    group = root.addGroup(key)
    group.setExpanded(False)
    group.setItemVisibilityChecked(bool(P["show_per_scenario_grids_by_default"]))
    for cap in P["capabilities"]:
        layer = add_grid(gpkg, P["grid_table"], key + " — " + cap, group)
        if layer is not None:
            shades = P["capability_shades"].get(cap)
            if shades:
                style_electre(layer, "grid_mean_" + cap, shades, P["cap_labels"])
                # Scenario runs set open_qgis_after_run=False (SKIP_QGIS_OPEN), which skips
                # pipeline_runner.py's own style-embedding pass -- so these per-scenario grid
                # gpkgs would otherwise carry no default style at all when opened standalone.
                # This table is shared across all 3 capabilities (no per-capability sidecar
                # views here, unlike pipeline_runner's own main output), so a GeoPackage can
                # only mark one style default per table: give that slot to the scenario's
                # configured default capability; the others are saved as named, non-default
                # styles reachable via QGIS's "Load Style from Database".
                embed_style(layer, cap, is_default=(cap == P["default_capability"]))

# --- OSM basemap last, forced to the bottom of the layer tree (rendered first/behind) ----
if P["basemap"]:
    osm = QgsRasterLayer(
        "type=xyz&url=https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png&zmin=0&zmax=19&crs=EPSG:3857",
        "OSM", "wms",
    )
    if osm.isValid():
        project.addMapLayer(osm, False)
        root.insertLayer(-1, osm)

# Zoom to the area of interest: the combined extent of every grid layer added above (the OSM
# basemap is excluded from _grow_extent since it's added separately, but it's a global tile
# layer anyway so it never usefully constrains the extent).
if extent is not None and not extent.isEmpty():
    extent.grow(extent.width() * 0.05 if extent.width() > 0 else 100)
    project.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(extent, project_crs))

project.write(P["output_project"])
app.exitQgis()
print("WROTE " + P["output_project"])
"""

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        script_path = handle.name
    try:
        subprocess.run([python_launcher, script_path], check=True)
    except subprocess.CalledProcessError as exc:
        print(f"[QGIS] Failed to create profile comparison project: {exc}", flush=True)
        return None
    print(f"[QGIS] Profile comparison project: {output_project}", flush=True)
    return output_project


def compare_profiles(results: List[Dict], scenario_key: str) -> None:
    """Build the per-scenario capability grids + pairwise difference grids + QGIS project."""
    out_dir = os.path.join("scenarios", scenario_key)
    os.makedirs(out_dir, exist_ok=True)

    for r in results:
        gpkg = r.get("gpkg_path")
        if not gpkg or not Path(gpkg).exists():
            print(f"[Compare] Missing gpkg for {r['profile'].key}: {gpkg}", flush=True)
            return

    diff_gpkg = os.path.join(out_dir, "differences.gpkg")
    try:
        pair_layers = build_profile_difference_gpkg(results, diff_gpkg)
    except Exception as exc:
        print(f"[Compare] Failed to build difference grids: {exc}", flush=True)
        return
    print(f"[Output] Difference grids: {diff_gpkg}", flush=True)

    try:
        export_comparison_csv(results, os.path.join(out_dir, "comparison_per_hexagon.csv"))
    except Exception as exc:
        print(f"[Compare] Failed to write per-hexagon CSV: {exc}", flush=True)

    try:
        project = create_profile_comparison_project(
            results, diff_gpkg, pair_layers, scenario_key, results[0]["cfg"]
        )
    except Exception as exc:
        print(f"[QGIS] Profile comparison project skipped: {exc}", flush=True)
        project = None

    if project and results[0]["cfg"].open_qgis_after_run:
        qgis_exe = _resolve_qgis_executable(results[0]["cfg"])
        if qgis_exe:
            gc.collect()
            time.sleep(2)
            print(f"[QGIS] Opening profile comparison project: {project}", flush=True)
            # Detach so QGIS keeps running independently once this script exits, and don't let
            # it inherit our stdio / process group -- otherwise a foreground shell (or a tool
            # wrapper reporting command success/failure) waits for QGIS to be closed before it
            # regains control, since an undetached child stays in the same process group.
            _popen_kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if os.name != "nt":
                _popen_kwargs["start_new_session"] = True
            subprocess.Popen([str(qgis_exe), str(project)], **_popen_kwargs)


def run_profile_comparison(scenario_key: str = "elder-student") -> None:
    """Run every persona of a named scenario from scratch and compare them."""
    if scenario_key not in SCENARIOS:
        raise SystemExit(
            f"Unknown scenario '{scenario_key}'. Known: {sorted(SCENARIOS)}"
        )
    personas = SCENARIOS[scenario_key]
    print("\n" + "=" * 60)
    print(f"INDIVIDUAL-PROFILE COMPARISON: {scenario_key}")
    print(f"Scenarios: {', '.join(p.key for p in personas)}")
    print("=" * 60)

    runner = ProfileRunner(STUDY_CITY)
    results = [runner.run_persona(profile) for profile in personas]

    print("\n" + "=" * 60)
    print("COMPARING PROFILES")
    print("=" * 60)
    compare_profiles(results, scenario_key)
    print("\n[Done] Profile comparison complete", flush=True)


def _run_reused_bundle_pass(ctx, key: str, modifier: ScenarioModifier) -> Dict:
    """Run one scenario off the *existing* main-run impedance bundle and emit its grid gpkg.

    Unlike a persona pass (which re-routes from scratch), this reuses the impedance bundle
    the main pipeline already wrote and only perturbs it in place (e.g. the strike zeroes the
    bus matrix). The bundle + bus-matrix paths are frozen to the main namespace, so we load and
    run accessibility there, then redirect only the *outputs* (capability CSV + grid gpkg) into
    a ``<slug>_<key>`` namespace so baseline and scenario land in separate files.

    Returns a result dict shaped for :func:`compare_profiles`:
    ``{"profile": <obj with .key>, "cfg": cfg, "cap": cap, "gpkg_path": <grid gpkg>}``.
    """
    cfg = ctx.config
    matrix_path = cfg.bus_impedance_matrix_path
    strike_backup = matrix_path + ".strike_backup"
    orig_slug = cfg.artifact_slug
    orig_open = cfg.open_qgis_after_run
    orig_acc_cache = cfg.accessibility_matrix_cache_enabled
    orig_svc_cache = cfg.service_matrix_cache_enabled
    made_backup = False

    # BOTH the accessibility-matrix cache AND the service-matrix cache are keyed on run
    # signatures that ignore the bus impedance *values* (accessibility_stage
    # ._accessibility_run_signature uses the bus origins/destinations/departure sigs;
    # service_stage._service_run_signature just chains off the accessibility meta signature).
    # A strike zeroes the bus values but changes none of those signatures, so with either cache
    # enabled the strike pass silently reuses the baseline's cached fusion/service scores and
    # comes out identical. Both passes also share the main namespace's caches (paths are frozen
    # before the slug switch). Disable both so each pass recomputes from the current on-disk bus
    # matrix. (No cache files are read or written while disabled, so the main namespace's caches
    # are left intact for the next `python main.py` run.)
    cfg.accessibility_matrix_cache_enabled = False
    cfg.service_matrix_cache_enabled = False

    print(f"\n[Scenario] === {key} ===", flush=True)
    try:
        # Bundle + bus-matrix paths come from the main namespace, so load before any slug change.
        loaded = load_impedance_bundle(ctx)
        if loaded is None:
            raise SystemExit(
                "[Scenario] No impedance bundle found for "
                f"{cfg.artifact_slug}. Run `python main.py` for this city first."
            )
        bus, non_bus = loaded

        # Preserve the real bus matrix so a bus-zeroing modifier can't leave the main
        # namespace corrupted for future runs.
        if os.path.exists(matrix_path):
            shutil.copy2(matrix_path, strike_backup)
            made_backup = True

        bus = modifier.modify_bus_routing(bus, ctx)
        non_bus = modifier.modify_non_bus_routing(non_bus, ctx)

        # Diagnostic: prove what the accessibility workers will actually read for bus. If the
        # strike genuinely zeroed the matrix, min/mean here jump to ~1e10 and finite%→0.
        try:
            _n_rows = len(json.load(open(cfg.bus_source_id_to_row_path, encoding="utf-8")))
            _n_cols = len(json.load(open(cfg.bus_dest_id_to_col_path, encoding="utf-8")))
            _bm = np.memmap(matrix_path, dtype=np.float32, mode="r", shape=(_n_rows, _n_cols))
            _s = np.asarray(_bm[: min(_n_rows, 1000)])
            _finite = _s[(_s > 0) & (_s < 1e9)]
            print(
                f"[Scenario:{key}] bus matrix @accessibility: "
                f"finite_pos%={100 * _finite.size / _s.size:.1f}  "
                f"huge(>=1e9)%={100 * float((_s >= 1e9).mean()):.1f}  "
                f"mean_finite={(float(_finite.mean()) if _finite.size else float('nan')):.3f}",
                flush=True,
            )
            del _bm
        except Exception as _exc:
            print(f"[Scenario:{key}] bus matrix probe skipped: {_exc}", flush=True)

        # Extra POI exclusions (e.g. IsMirrionisScenario) are merged into the dedup drop map
        # run_accessibility_stage writes internally, by wrapping poi_dedup.write_drop_map for
        # the duration of this pass only — see ScenarioModifier.poi_drop_map.
        extra_drop_map = modifier.poi_drop_map(ctx)
        orig_write_drop_map = poi_dedup.write_drop_map
        if extra_drop_map:
            def _patched_write_drop_map(cfg_, drop_map=None, _orig=orig_write_drop_map, _extra=extra_drop_map):
                if drop_map is None:
                    drop_map = poi_dedup.build_drop_map(cfg_)
                merged: Dict[str, set] = {pt: set(keys) for pt, keys in drop_map.items()}
                for pt, keys in _extra.items():
                    merged.setdefault(pt, set()).update(keys)
                return _orig(cfg_, drop_map={pt: sorted(keys) for pt, keys in merged.items()})
            poi_dedup.write_drop_map = _patched_write_drop_map

        try:
            print("[Stage] Accessibility (scenario)", flush=True)
            acc = run_accessibility_stage(ctx, non_bus, bus)
        finally:
            poi_dedup.write_drop_map = orig_write_drop_map

        print("[Stage] Service Aggregation (scenario)", flush=True)
        svc = run_service_stage(ctx, acc)

        # Redirect outputs into this scenario's own namespace; suppress the per-pass QGIS open
        # (compare_profiles opens the single comparison project at the end).
        cfg.artifact_slug = f"{orig_slug}_{key}"
        cfg.open_qgis_after_run = False

        print("[Stage] Capability Aggregation (scenario)", flush=True)
        cap = run_capability_stage(ctx, svc)
        spatial = generate_spatial_outputs(cfg, cap)
    finally:
        cfg.artifact_slug = orig_slug
        cfg.open_qgis_after_run = orig_open
        cfg.accessibility_matrix_cache_enabled = orig_acc_cache
        cfg.service_matrix_cache_enabled = orig_svc_cache
        # Restore the untouched bus matrix and drop both temp backups.
        if made_backup:
            shutil.copy2(strike_backup, matrix_path)
            os.remove(strike_backup)
        stray_backup = matrix_path + ".backup"
        if os.path.exists(stray_backup):
            os.remove(stray_backup)

    return {
        "profile": SimpleNamespace(key=key),
        "cfg": cfg,
        "cap": cap,
        "gpkg_path": spatial.get("gpkg_path"),
    }


def run_strike_comparison() -> None:
    """Run baseline + public-strike off the existing bundle and emit the new-style comparison.

    Produces, under ``scenarios/public-strike/``, the same artefacts the persona comparison
    does: a per-scenario ELECTRE capability hex grid for baseline and for the strike, a
    ``differences.gpkg`` with one ``diff_baseline_to_public_strike__<capability>`` layer per
    capability, each carrying a single ``d_<capability> = level(strike) − level(baseline)``
    field on a red/yellow/green ramp (red where the strike lowers the capability level, green
    where it raises it, yellow = unchanged), and a ``profile_comparison.qgz`` QGIS project
    wiring all of it together.
    """
    scenario_key = "public-strike"
    os.environ["CAP_STUDY_CITY"] = STUDY_CITY
    cfg = PipelineConfig(study_city=STUDY_CITY)
    cfg.capability_score_mode = CAPABILITY_SCORE_MODE
    if SKIP_QGIS_OPEN:
        cfg.open_qgis_after_run = False

    # Clear cached graphs from previous runs so the current config is used.
    graphml._GRAPH_CACHE = None
    graphml._MODE_GRAPH_CACHE = {}
    graphml._CITY_POI_UNIVERSE_CACHE = {}
    graphml._POI_DOWNLOAD_LOGGED = set()

    ctx = build_context(cfg)

    print("\n" + "=" * 60)
    print(f"ROUTING-DISRUPTION COMPARISON: {scenario_key}")
    print("Scenarios: baseline, public_strike")
    print("=" * 60)

    # Baseline first (no bus modification), then the strike (bus decay = 0.0).
    results = [
        _run_reused_bundle_pass(ctx, "baseline", BaselineScenario()),
        _run_reused_bundle_pass(ctx, "public_strike", PublicStrikeScenario()),
    ]

    print("\n" + "=" * 60)
    print("COMPARING SCENARIOS")
    print("=" * 60)
    compare_profiles(results, scenario_key)
    print("\n[Done] Strike comparison complete", flush=True)


def run_is_mirrionis_comparison() -> None:
    """Run baseline + underservice-is-mirrionis off the existing bundle and compare.

    Produces, under ``scenarios/underservice-is-mirrionis/``, the same artefacts the strike
    comparison does: a per-scenario ELECTRE capability hex grid for baseline and for the
    underservice scenario, a ``differences.gpkg`` with one
    ``diff_baseline_to_underservice_is_mirrionis__<capability>`` layer per capability, each
    carrying a single ``d_<capability> = level(underservice) − level(baseline)`` field on a
    red/yellow/green ramp, and a ``profile_comparison.qgz`` QGIS project wiring all of it
    together.
    """
    scenario_key = "underservice-is-mirrionis"
    os.environ["CAP_STUDY_CITY"] = STUDY_CITY
    cfg = PipelineConfig(study_city=STUDY_CITY)
    cfg.capability_score_mode = CAPABILITY_SCORE_MODE
    if SKIP_QGIS_OPEN:
        cfg.open_qgis_after_run = False

    # Clear cached graphs from previous runs so the current config is used.
    graphml._GRAPH_CACHE = None
    graphml._MODE_GRAPH_CACHE = {}
    graphml._CITY_POI_UNIVERSE_CACHE = {}
    graphml._POI_DOWNLOAD_LOGGED = set()

    ctx = build_context(cfg)

    print("\n" + "=" * 60)
    print(f"ROUTING-DISRUPTION COMPARISON: {scenario_key}")
    print("Scenarios: baseline, underservice_is_mirrionis")
    print("=" * 60)

    # Baseline first (no POI removal), then the underservice scenario (parks/supermarkets/
    # hospitals in Is Mirrionis dropped).
    results = [
        _run_reused_bundle_pass(ctx, "baseline", BaselineScenario()),
        _run_reused_bundle_pass(ctx, "underservice_is_mirrionis", IsMirrionisScenario()),
    ]

    print("\n" + "=" * 60)
    print("COMPARING SCENARIOS")
    print("=" * 60)
    compare_profiles(results, scenario_key)
    print("\n[Done] Is Mirrionis underservice comparison complete", flush=True)


def run_all_scenarios():
    """Run baseline and all scenarios, then compare.

    Legacy comparison (histograms + side-by-side QGIS project). Superseded by
    :func:`run_strike_comparison`, which emits the new-style ELECTRE grids + difference grid;
    kept for reference.
    """
    os.environ["CAP_STUDY_CITY"] = STUDY_CITY
    cfg = PipelineConfig(study_city=STUDY_CITY)
    
    # Clear cached graphs from previous runs so new config is used
    graphml._GRAPH_CACHE = None
    graphml._MODE_GRAPH_CACHE = {}
    graphml._CITY_POI_UNIVERSE_CACHE = {}
    graphml._POI_DOWNLOAD_LOGGED = set()
    
    runner = ScenarioRunner(cfg)
    
    # Run public strike scenario
    print("\n" + "="*60)
    print("RUNNING SCENARIOS")
    print("="*60)
    
    scenarios = [
        PublicStrikeScenario(),
    ]
    
    # We start with an empty results list since we'll use the experiments directory output
    # The baseline results should already exist from the main pipeline run
    results = []
    
    for scenario in scenarios:
        result = runner.run_scenario(scenario, scenario.get_name())
        if result is not None:
            results.append(result)
    
    # Compare scenarios (need to include baseline)
    # The baseline results are in experiments/ directory from the main pipeline
    print("\n" + "="*60)
    print("COMPARING SCENARIOS")
    print("="*60)
    
    # Load baseline results from experiments directory
    experiments_dir = "experiments"
    baseline_results = {}
    
    if os.path.isdir(experiments_dir):
        for cap_type in ["restorativeness", "nutrition", "care"]:
            # Look for the most recent file matching this capability type
            pattern = f"{cfg.artifact_slug}_capability_{cap_type}.csv"
            for filename in sorted(os.listdir(experiments_dir), reverse=True):
                if pattern in filename:
                    path = os.path.join(experiments_dir, filename)
                    if os.path.isfile(path):
                        baseline_results[cap_type] = path
                        break
    
    if not baseline_results:
        print(
            "[Comparison] No baseline results found in experiments/. "
            "Please run the main pipeline first.",
            flush=True
        )
        return
    
    # Add baseline to results for comparison
    baseline_result = {
        "name": "baseline",
        "output_suffix": "baseline",
        "output_paths": baseline_results,
    }
    all_results = [baseline_result] + results
    
    comparison_df = runner.compare_scenarios(all_results)
    if comparison_df is not None:
        # Determine scenario name for output folder
        scenario_name = results[0]["name"] if results else "unknown"
        
        # Print statistics
        print("\n[Comparison] Summary Statistics:")
        for col in comparison_df.columns:
            if col.startswith("diff_"):
                data = comparison_df[col].dropna()
                if len(data) > 0:
                    print(
                        f"  {col}: mean={data.mean():.4f}, "
                        f"median={data.median():.4f}, "
                        f"std={data.std():.4f}",
                        flush=True
                    )
        
        # Visualize in scenario-specific folder
        runner.visualize_comparison(comparison_df, scenario_name)
        
        # Save full comparison in scenario folder
        scenarios_dir = os.path.join("scenarios", scenario_name)
        os.makedirs(scenarios_dir, exist_ok=True)
        comparison_path = os.path.join(scenarios_dir, "comparison_results.csv")
        comparison_df.to_csv(comparison_path, index=False)
        print(f"\n[Output] Saved comparison: {comparison_path}", flush=True)

        # Build one QGIS project with baseline + scenario spatial layers.
        baseline_gpkg = Path("outputs") / "gpkg" / cfg.artifact_slug / f"{cfg.artifact_slug}.gpkg"
        
        print(f"\n[QGIS] Looking for baseline geopackage: {baseline_gpkg}", flush=True)
        print(f"[QGIS] Baseline exists: {baseline_gpkg.exists()}", flush=True)
        
        # Check if baseline geopackage has correct data (not scenario data)
        if baseline_gpkg.exists():
            try:
                import geopandas as gpd
                baseline_test_df = gpd.read_file(baseline_gpkg, layer="capability_points")
                baseline_care_mean = baseline_test_df['capability_care'].mean()
                print(f"[QGIS] Baseline geopackage care mean: {baseline_care_mean:.4f}", flush=True)
                
                # If baseline has scenario values (close to 0.8332), rebuild from CSV
                if baseline_care_mean < 0.85:  # Scenario mean is ~0.8332, baseline should be ~0.9541
                    print("[QGIS] Baseline geopackage contains scenario data, rebuilding from CSV...", flush=True)
                    if runner.rebuild_baseline_gpkg_from_csv(baseline_gpkg):
                        print("[QGIS] Baseline geopackage rebuilt successfully", flush=True)
                    else:
                        print("[QGIS] Failed to rebuild baseline geopackage", flush=True)
            except Exception as e:
                print(f"[QGIS] Error checking baseline geopackage: {e}", flush=True)
        
        if results:
            scenario_gpkg = results[0].get("spatial_outputs", {}).get("gpkg_path")
            if scenario_gpkg:
                if baseline_gpkg.exists():
                    comparison_project = runner.create_scenario_comparison_project(
                        baseline_gpkg=baseline_gpkg,
                        scenario_gpkg=scenario_gpkg,
                        scenario_name=results[0]["name"],
                    )
                else:
                    print(
                        "[QGIS] Baseline geopackage not found. Make sure the main pipeline was run first.",
                        flush=True
                    )
                    print(
                        f"[QGIS] Expected baseline at: {baseline_gpkg}",
                        flush=True
                    )
                    comparison_project = None
                
                # Open the comparison project in QGIS with a delay to ensure all file handles are released
                if comparison_project and cfg.open_qgis_after_run:
                    print("[QGIS] Waiting for file handles to release...", flush=True)
                    gc.collect()  # Force garbage collection to release file handles
                    time.sleep(2)  # Give OS time to release file locks
                    qgis_exe = _resolve_qgis_executable(cfg)
                    if qgis_exe:
                        print(f"[QGIS] Opening scenario comparison project: {comparison_project}", flush=True)
                        # See the detach comment in compare_profiles: without this, a foreground
                        # shell/tool waits for QGIS to close before it regains control.
                        _popen_kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
                        if os.name != "nt":
                            _popen_kwargs["start_new_session"] = True
                        subprocess.Popen([str(qgis_exe), str(comparison_project)], **_popen_kwargs)
    
    print("\n[Done] Scenario analysis complete", flush=True)


def _dispatch_scenario(key: str) -> None:
    """Run the single named comparison — the same lookup SCENARIO and each
    RUN_ALL_SCENARIOS entry go through."""
    if key == "public-strike":
        run_strike_comparison()
    elif key == "underservice-is-mirrionis":
        run_is_mirrionis_comparison()
    elif key in SCENARIOS:
        run_profile_comparison(key)
    else:
        raise SystemExit(
            f"Unknown scenario key '{key}'. Allowed: "
            f"{sorted(DISRUPTION_SCENARIOS)} (routing-disruption) or "
            f"{sorted(SCENARIOS)} (persona comparisons)."
        )


if __name__ == "__main__":
    if RUN_ALL_SCENARIOS:
        print(f"\n[Batch] Running {len(ALL_SCENARIO_KEYS)} scenarios: {list(ALL_SCENARIO_KEYS)}", flush=True)
        succeeded: list[str] = []
        failed: list[tuple[str, str]] = []
        for key in ALL_SCENARIO_KEYS:
            print(f"\n{'#' * 60}\n[Batch] Starting: {key}\n{'#' * 60}", flush=True)
            try:
                _dispatch_scenario(key)
                succeeded.append(key)
            except Exception as exc:
                print(f"[Batch] {key} FAILED: {exc}", flush=True)
                failed.append((key, str(exc)))

        print(f"\n{'=' * 60}\n[Batch] Done: {len(succeeded)}/{len(ALL_SCENARIO_KEYS)} succeeded\n{'=' * 60}", flush=True)
        for key in succeeded:
            print(f"  [ok]     {key}", flush=True)
        for key, err in failed:
            print(f"  [FAILED] {key}: {err}", flush=True)
        if failed:
            raise SystemExit(f"{len(failed)} scenario(s) failed: {[k for k, _ in failed]}")
    else:
        # The SCENARIO knob at the top of this file selects the comparison to run.
        _dispatch_scenario(SCENARIO)
