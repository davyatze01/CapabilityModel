import math
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import main as main_module
from utils import r5_routing


class TestR5RoutingHelpers(unittest.TestCase):
    def test_configure_java_runtime_flags(self):
        prev_tool_opts = os.environ.get("JAVA_TOOL_OPTIONS")
        prev_safe_mode = os.environ.get("R5_JAVA_SAFE_MODE")
        try:
            os.environ["R5_JAVA_SAFE_MODE"] = "1"
            os.environ["JAVA_TOOL_OPTIONS"] = "-Xmx1g"
            r5_routing._configure_java_runtime_flags()
            tool_opts = os.environ["JAVA_TOOL_OPTIONS"]
            self.assertIn("-Xmx1g", tool_opts)
            self.assertIn("-XX:TieredStopAtLevel=1", tool_opts)
            self.assertIn("-XX:CICompilerCount=1", tool_opts)
            self.assertIn("-XX:+UnlockDiagnosticVMOptions", tool_opts)
        finally:
            if prev_tool_opts is None:
                os.environ.pop("JAVA_TOOL_OPTIONS", None)
            else:
                os.environ["JAVA_TOOL_OPTIONS"] = prev_tool_opts
            if prev_safe_mode is None:
                os.environ.pop("R5_JAVA_SAFE_MODE", None)
            else:
                os.environ["R5_JAVA_SAFE_MODE"] = prev_safe_mode

    def test_slow_aggregation_wait_time(self):
        detailed = pd.DataFrame(
            {
                "from_id": ["o1", "o1", "o1", "o1"],
                "to_id": ["d1", "d1", "d1", "d1"],
                "option": [0, 0, 1, 1],
                "travel_time": [
                    pd.Timedelta(minutes=10),
                    pd.Timedelta(minutes=5),
                    pd.Timedelta(minutes=20),
                    pd.Timedelta(minutes=5),
                ],
                "wait_time": [
                    pd.Timedelta(minutes=2),
                    pd.Timedelta(minutes=1),
                    pd.Timedelta(minutes=8),
                    pd.Timedelta(minutes=2),
                ],
            }
        )
        agg = r5_routing._aggregate_slow_itineraries(detailed)
        self.assertEqual(len(agg), 1)
        self.assertAlmostEqual(float(agg.iloc[0]["travel_time_min"]), 15.0)
        self.assertAlmostEqual(float(agg.iloc[0]["wait_time_min"]), 3.0)

    def test_routing_index_lookup_rounding(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "routes.sqlite"
            conn = sqlite3.connect(db_path)
            r5_routing._create_db_schema(conn)
            conn.execute(
                """
                INSERT INTO routes (
                    from_lat_r, from_lon_r, to_lat_r, to_lon_r,
                    travel_time_min, wait_time_min, impedance_min, mode, departure_iso
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (39.123457, 9.123457, 39.765432, 9.765432, 25.0, 4.0, 25.0, r5_routing.MODE_FAST, "2025-10-15T12:00:00"),
            )
            conn.commit()
            conn.close()

            idx = r5_routing.open_routing_index(str(db_path))
            try:
                val = r5_routing.lookup_impedance(
                    idx,
                    (39.1234567, 9.1234567),
                    (39.7654324, 9.7654324),
                    mode=r5_routing.MODE_FAST,
                    departure_iso="2025-10-15T12:00:00",
                )
            finally:
                idx.close()
            self.assertEqual(val, 25.0)

    def test_fast_impedance_equals_travel_time(self):
        ttm = pd.DataFrame(
            {
                "from_id": ["o1"],
                "to_id": ["d1"],
                "travel_time": [33.0],
            }
        )
        out = r5_routing._prepare_fast_chunk_df(ttm)
        self.assertEqual(float(out.iloc[0]["travel_time_min"]), 33.0)
        self.assertTrue(math.isnan(float(out.iloc[0]["wait_time_min"])))
        imp = r5_routing._compute_impedance_min(r5_routing.MODE_FAST, out)
        self.assertEqual(float(imp.iloc[0]), 33.0)

    def test_slow_impedance_includes_wait_time(self):
        chunk_df = pd.DataFrame(
            {
                "travel_time_min": [15.0, math.nan],
                "wait_time_min": [3.0, 4.0],
            }
        )
        imp = r5_routing._compute_impedance_min(r5_routing.MODE_SLOW, chunk_df)
        self.assertAlmostEqual(float(imp.iloc[0]), 18.0)
        self.assertTrue(math.isnan(float(imp.iloc[1])))

    def test_skip_routing_requires_artifacts(self):
        with self.assertRaises(RuntimeError):
            main_module._validate_routing_artifacts_for_skip(
                True,
                "outputs/missing_fast.csv",
                "outputs/missing_fast.sqlite",
            )

    def test_coords_signature_stable_on_rounded_values(self):
        a = [(39.1234567, 9.1234567), (39.2, 9.2)]
        b = [(39.123456699, 9.1234567001), (39.2, 9.2)]
        self.assertEqual(r5_routing._coords_signature(a), r5_routing._coords_signature(b))

    def test_resume_helpers_meta_and_processed_origins(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "routes.sqlite"
            conn = sqlite3.connect(db_path)
            r5_routing._create_db_schema(conn)
            r5_routing._set_run_meta(
                conn,
                {
                    "mode": r5_routing.MODE_FAST,
                    "departure_iso": "2025-10-15T12:00:00",
                    "origins_sig": "abc",
                    "destinations_sig": "def",
                },
            )
            conn.execute(
                """
                INSERT INTO routes (
                    from_lat_r, from_lon_r, to_lat_r, to_lon_r,
                    travel_time_min, wait_time_min, impedance_min, mode, departure_iso
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (39.1, 9.1, 39.8, 9.8, 10.0, None, 10.0, r5_routing.MODE_FAST, "2025-10-15T12:00:00"),
            )
            conn.commit()

            meta = r5_routing._get_run_meta(conn)
            self.assertEqual(meta.get("mode"), r5_routing.MODE_FAST)
            self.assertEqual(meta.get("origins_sig"), "abc")

            processed = r5_routing._collect_processed_origin_keys(
                conn,
                mode=r5_routing.MODE_FAST,
                departure_iso="2025-10-15T12:00:00",
            )
            self.assertEqual(processed, {(39.1, 9.1)})

            rows_count, missing_count = r5_routing._routing_counts(
                conn,
                mode=r5_routing.MODE_FAST,
                departure_iso="2025-10-15T12:00:00",
            )
            self.assertEqual(rows_count, 1)
            self.assertEqual(missing_count, 0)
            conn.close()

    def test_resilient_builder_rejects_negative_retries(self):
        with self.assertRaises(ValueError):
            r5_routing.build_routing_store_resilient(
                origins=[(39.1, 9.1)],
                destinations=[(39.2, 9.2)],
                mode=r5_routing.MODE_FAST,
                pbf_path="missing.pbf",
                gtfs_path="missing.zip",
                jar_path="missing.jar",
                departure_dt=pd.Timestamp("2025-10-15T12:00:00").to_pydatetime(),
                workers=1,
                out_csv="outputs/never.csv",
                out_db="outputs/never.sqlite",
                max_retries=-1,
            )

    def test_resilient_builder_rejects_non_positive_timeout(self):
        with self.assertRaises(ValueError):
            r5_routing.build_routing_store_resilient(
                origins=[(39.1, 9.1)],
                destinations=[(39.2, 9.2)],
                mode=r5_routing.MODE_FAST,
                pbf_path="missing.pbf",
                gtfs_path="missing.zip",
                jar_path="missing.jar",
                departure_dt=pd.Timestamp("2025-10-15T12:00:00").to_pydatetime(),
                workers=1,
                out_csv="outputs/never.csv",
                out_db="outputs/never.sqlite",
                attempt_timeout_s=0,
            )


if __name__ == "__main__":
    unittest.main()
