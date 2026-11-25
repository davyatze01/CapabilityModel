"""Regression checks: ELECTRE capabilities and independent service-average evaluation.

Run: python -m tools.test_scenario_service_means
All generated artifacts remain in temporary directories.
"""
import csv
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from analysis import scenarios
from core.config import PipelineConfig
from core.profiles import CAPABILITIES
from core.pipeline_types import PipelineContext, ServiceNodeResult, ServiceStageResult
from exports.generate_experiment_shapefiles import (
    GRID_TABLE_NAME, _build_capability_grid, _prepare_combined_geodataframe,
)
from stages.capability_stage import run_capability_stage
from utils import capabilities as cap


class ServiceMeanTests(unittest.TestCase):
    def test_stage_and_grid_keep_both_measures_with_unequal_weights(self):
        scores = {s: 0.2 for services in cap.CAPABILITY_SERVICES.values() for s in services}
        scores.update(eating_out=0.2, food_access=0.8)
        weights = dict(cap.CAP_ELECTRE_W)
        weights['nutrition'] = {'eating_out': 0.9, 'food_access': 0.1}
        cfg = PipelineConfig(enable_progress=False)
        self.assertEqual(cfg.capability_score_mode, 'discrete')
        self.assertEqual(scenarios.CAPABILITY_SCORE_MODE, 'discrete')
        with tempfile.TemporaryDirectory() as tmp, patch.object(cap, 'CAP_ELECTRE_W', weights):
            previous = os.getcwd()
            try:
                os.chdir(tmp)
                ctx = PipelineContext(cfg, None, [], 1, {'capabilities': str(Path(tmp) / 'scores.csv')}, cap.CAPABILITY_SERVICES)
                svc = ServiceStageResult([
                    ServiceNodeResult(1, 39.2, 9.1, scores),
                    ServiceNodeResult(2, 39.202, 9.102, scores),
                ])
                result = run_capability_stage(ctx, svc)
                path = Path(result.output_paths['capabilities'])
                with path.open() as f:
                    row = next(csv.DictReader(f))
                self.assertAlmostEqual(float(row['capability_nutrition']), cap.electre_tri_integration([0.2, 0.8], 'nutrition'))
                self.assertAlmostEqual(float(row['service_mean_nutrition']), 0.5)
                self.assertNotAlmostEqual(float(row['service_mean_nutrition']), cap.weighted_sum_integration([0.2, 0.8], 'nutrition'))
                self.assertAlmostEqual(cap.service_mean_integration([0, 0.8], 'nutrition'), 0.4)
                points = _prepare_combined_geodataframe([path], None)
                grid = _build_capability_grid(points, 'capability_nutrition', 100, 500,
                    fill_hull=False, exclude_water=False, grid_params_path=Path(tmp) / 'grid_params.json')
                measured = grid.loc[grid.has_data.astype(bool)]
                self.assertGreater(len(measured), 0)
                np.testing.assert_allclose(measured.grid_mean_service_mean_nutrition, 0.5)
                np.testing.assert_allclose(measured.grid_mean_nutrition, float(row['capability_nutrition']))
            finally:
                os.chdir(previous)

    def test_comparisons_use_means_and_align_by_hex_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = []
            for key, means in [('baseline', [0.21, 0.42, 0.99]), ('arm_with_underscores', [0.36, 0.40, 0.99])]:
                fields = {'hex_id': ['h1', 'h2', 'nodata'], 'has_data': [1, 1, 0]}
                for capability in CAPABILITIES:
                    fields[f'grid_mean_{capability}'] = [0.3, 0.3, 0.3]
                    fields[f'grid_mean_service_mean_{capability}'] = means
                grid = gpd.GeoDataFrame(fields, geometry=[Point(9.1, 39.2), Point(9.11, 39.21), Point(9.12, 39.22)], crs='EPSG:4326').to_crs('EPSG:3857')
                if key != 'baseline':
                    grid = grid.iloc[::-1]
                path = Path(tmp) / f'{key}.gpkg'
                grid.to_file(path, layer=GRID_TABLE_NAME, driver='GPKG')
                results.append({'profile': SimpleNamespace(key=key), 'gpkg_path': str(path)})
            out = Path(tmp) / 'comparison.csv'
            scenarios.export_comparison_csv(results, str(out))
            frame = pd.read_csv(out).set_index('hex_id')
            self.assertAlmostEqual(frame.loc['h1', 'dmean_nutrition_baseline_to_arm_with_underscores'], 0.15)
            self.assertAlmostEqual(frame.loc['h1', 'dcont_nutrition_baseline_to_arm_with_underscores'], 0)
            self.assertTrue(pd.isna(frame.loc['nodata', 'service_mean_nutrition_baseline']))
            self.assertEqual(frame.loc['h1', 'evaluation_method'], 'service_mean')
            diff_path = Path(tmp) / 'differences.gpkg'
            layers = scenarios.build_profile_difference_gpkg(results, str(diff_path))
            diff = gpd.read_file(diff_path, layer=layers[('baseline', 'arm_with_underscores')]['nutrition']).set_index('hex_id')
            self.assertEqual(diff.loc['h1', 'd_nutrition'], 1)
            self.assertEqual(diff.loc['h2', 'd_nutrition'], 0)
            self.assertTrue(pd.isna(diff.loc['nodata', 'd_nutrition']))

    def test_node_comparisons_align_service_means_by_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = []
            for key, ids, means in [('baseline', [1, 2], [0.2, 0.4]),
                                    ('arm_with_underscores', [2, 1], [0.3, 0.5])]:
                frame = pd.DataFrame({'node_id': ids, 'lat': [39.2, 39.21], 'lon': [9.1, 9.11],
                                      'capability_nutrition': [0.3, 0.3],
                                      'service_mean_nutrition': means})
                path = Path(tmp) / f'{key}.csv'
                frame.to_csv(path, index=False)
                results.append({'name': key, 'output_paths': {'capabilities': str(path)}})
            runner = scenarios.ScenarioRunner.__new__(scenarios.ScenarioRunner)
            comparison = runner.compare_scenarios(results).set_index('node_id')
            self.assertAlmostEqual(comparison.loc[1, 'diff_nutrition_arm_with_underscores'], 0.3)
            self.assertAlmostEqual(comparison.loc[2, 'diff_nutrition_arm_with_underscores'], -0.1)

    def test_stale_grids_are_rejected_and_scales_are_separate(self):
        with self.assertRaisesRegex(ValueError, 'regenerate'):
            scenarios._service_mean_scores(pd.DataFrame({'grid_mean_care': [0.3]}), 'care')
        values = pd.Series([0, 0.1, 0.9, 1, np.nan])
        np.testing.assert_allclose(scenarios._service_mean_level(values), [1, 2, 10, 10, np.nan], equal_nan=True)
        self.assertLessEqual(scenarios._capability_level(pd.Series([1])).iloc[0], 5)


if __name__ == '__main__':
    unittest.main()
