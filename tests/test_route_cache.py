import tempfile
import unittest
import sys
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from old_scripts import route
import main as main_module


class TestBusCacheExists(unittest.TestCase):
    def test_bus_cache_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            route._ROUTE_CACHE_FOLDER = tmp
            route._ROUTE_CACHE.clear()
            route._ROUTE_GEOM_CACHE.clear()

            origin = (1.0, 2.0)
            dest = (3.0, 4.0)
            key = route._route_cache_key(
                "bus",
                origin,
                dest,
                route_date=route.ROUTE_DATE,
                route_time=route.ROUTE_TIME,
            )

            route._route_cache_set(key, {"result_value": 123})
            route._route_geom_cache_set(key, [[(1.0, 2.0), (3.0, 4.0)]])

            self.assertTrue(route.bus_cache_exists(origin, dest))


class TestMainPipelineSandbox(unittest.TestCase):
    def test_main_pipeline_small(self):
        try:
            main_module.empty_cache()
            main_module.run_pipeline(max_nodes=100, max_pois=10, seed=42, enable_progress=True)
        except Exception as exc:
            self.fail(f"run_pipeline failed: {exc}")


if __name__ == "__main__":
    unittest.main()
