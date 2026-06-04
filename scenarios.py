"""
Scenario-based analysis framework.

Allows running the capability model under different scenarios (e.g., public strike)
and comparing results. Scenarios modify routing or service availability and reuse
the same pre-computed impedance bundle to efficiently explore variations.
"""

import os
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

from config import PipelineConfig
from context import build_context
from artifact_bundle import load_impedance_bundle
from accessibility_stage import run_accessibility_stage
from service_stage import run_service_stage
from capability_stage import run_capability_stage
from pipeline_runner import generate_spatial_outputs
from pipeline_runner import _resolve_qgis_executable, _resolve_qgis_python_launcher
from utils import graphml
from pipeline_types import BusRoutingStageResult, NonBusRoutingStageResult
from main import study_city


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
        field_literal = repr(str(self.cfg.qgis_autostyle_field))
        ramp_literal = repr(str(self.cfg.qgis_autostyle_ramp))
        classes_count = int(self.cfg.qgis_autostyle_classes)
        basemap_flag = "True" if self.cfg.qgis_autostyle_basemap else "False"

        script = f"""
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsGradientColorRamp,
    QgsGraduatedSymbolRenderer,
    QgsProject,
    QgsRasterLayer,
    QgsStyle,
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
    renderer = QgsGraduatedSymbolRenderer()
    renderer.setClassAttribute(field_name)
    renderer.setMode(QgsGraduatedSymbolRenderer.EqualInterval)
    renderer.updateClasses(layer, int({classes_count}))
    ramp = QgsStyle.defaultStyle().colorRamp({ramp_literal})
    if ramp is None:
        ramp = QgsGradientColorRamp(QColor("#440154"), QColor("#FDE725"))
    renderer.updateColorRamp(ramp)
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


def run_all_scenarios():
    """Run baseline and all scenarios, then compare."""
    os.environ["CAP_STUDY_CITY"] = study_city
    cfg = PipelineConfig(study_city=study_city)
    
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
                        subprocess.Popen([str(qgis_exe), str(comparison_project)])
    
    print("\n[Done] Scenario analysis complete", flush=True)


if __name__ == "__main__":
    run_all_scenarios()
