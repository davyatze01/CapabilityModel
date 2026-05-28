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
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import pandas as pd
import matplotlib.pyplot as plt

from config import PipelineConfig
from context import build_context
from artifact_bundle import load_impedance_bundle
from accessibility_stage import run_accessibility_stage
from service_stage import run_service_stage
from capability_stage import run_capability_stage
from pipeline_runner import generate_spatial_outputs
from pipeline_types import BusRoutingStageResult, NonBusRoutingStageResult


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


def run_all_scenarios():
    """Run baseline and all scenarios, then compare."""
    cfg = PipelineConfig()
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
            pattern = f"capability_{cap_type}.csv"
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
    
    print("\n[Done] Scenario analysis complete", flush=True)


if __name__ == "__main__":
    run_all_scenarios()
