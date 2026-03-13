import csv
import datetime as dt
import os
import shutil
import sqlite3
import types
import unittest
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pandas as pd

from bus_routing_stage import _resolve_routing_sample_csv_path
from helpers import PipelineConfig
from utils import r5_routing


class TestR5RoutingStorageRefactor(unittest.TestCase):
    @contextmanager
    def _temp_dir(self):
        root = Path("cache") / "test_tmp" / str(uuid.uuid4())
        root.mkdir(parents=True, exist_ok=True)
        try:
            yield str(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def _create_fresh_db(self, db_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        r5_routing._create_db_schema(conn)
        r5_routing._set_run_meta(conn, {"schema_version": r5_routing.ROUTING_SCHEMA_VERSION})
        conn.commit()
        return conn

    def _write_test_gtfs_zip(self, zip_path: Path) -> None:
        files = {
            "calendar.txt": "\n".join(
                [
                    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date",
                    "wk,1,1,1,1,1,1,1,20250101,20251231",
                ]
            ),
            "trips.txt": "\n".join(
                [
                    "route_id,service_id,trip_id",
                    "r1,wk,t1",
                    "r1,wk,t2",
                    "r1,wk,t3",
                    "r1,wk,t4",
                    "r2,wk,t5",
                    "r2,wk,t6",
                    "r2,wk,t7",
                ]
            ),
            "stops.txt": "\n".join(
                [
                    "stop_id,stop_name,stop_lat,stop_lon",
                    "A,Stop A,39.2001,9.1001",
                    "B,Stop B,39.2101,9.1101",
                ]
            ),
            "stop_times.txt": "\n".join(
                [
                    "trip_id,arrival_time,departure_time,stop_id,stop_sequence",
                    "t1,12:00:00,12:00:00,A,1",
                    "t2,12:10:00,12:10:00,A,1",
                    "t3,12:20:00,12:20:00,A,1",
                    "t4,12:30:00,12:30:00,A,1",
                    "t5,12:00:00,12:00:00,B,1",
                    "t6,12:30:00,12:30:00,B,1",
                    "t7,13:00:00,13:00:00,B,1",
                ]
            ),
        }
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)

    def test_pipeline_config_defaults_for_sample(self):
        cfg = PipelineConfig()
        self.assertEqual(cfg.r5_sample_rows, 10000)
        self.assertAlmostEqual(cfg.r5_sample_missing_share, 0.3)
        self.assertEqual(cfg.r5_sample_csv_path, os.path.join("outputs", "r5_routes_sample.csv"))
        self.assertEqual(cfg.r5_fast_wait_model, "origin_estimate")

    def test_sample_csv_path_includes_fast_wait_model(self):
        path = _resolve_routing_sample_csv_path(
            os.path.join("outputs", "r5_routes_sample.csv"),
            routing_mode=r5_routing.MODE_FAST,
            wait_model="origin_estimate",
        )
        self.assertEqual(
            path,
            os.path.join("outputs", "r5_routes_sample_fast_routing_origin_estimate.csv"),
        )

    def test_create_db_schema_normalized(self):
        with self._temp_dir() as tmp:
            db_path = Path(tmp) / "routes.sqlite"
            conn = self._create_fresh_db(db_path)
            try:
                tables = {
                    row[0]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                self.assertTrue({"points", "runs", "routes", "run_meta"}.issubset(tables))
                indexes = {
                    row[0]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
                }
                self.assertIn("idx_points_lat_lon", indexes)
                self.assertIn("idx_routes_run_from", indexes)
                self.assertIn("idx_routes_run_to", indexes)
            finally:
                conn.close()

    def test_lookup_impedance_uses_point_ids_and_rounding(self):
        with self._temp_dir() as tmp:
            db_path = Path(tmp) / "routes.sqlite"
            conn = self._create_fresh_db(db_path)
            try:
                run_id = r5_routing._get_or_create_run_id(
                    conn,
                    mode=r5_routing.MODE_FAST,
                    departure_iso="2025-10-15T12:00:00",
                    origins_sig="orig",
                    destinations_sig="dest",
                    wait_time_estimated_min=5.0,
                )
                cache: dict[tuple[float, float], int] = {}
                from_id = r5_routing._get_or_create_point_id(conn, 39.123457, 9.123457, cache)
                to_id = r5_routing._get_or_create_point_id(conn, 39.765432, 9.765432, cache)
                r5_routing._insert_rows(conn, [(run_id, from_id, to_id, 20.0, 4.0, 25.0)])
                conn.commit()
            finally:
                conn.close()

            idx = r5_routing.open_routing_index(str(db_path))
            try:
                value = r5_routing.lookup_impedance(
                    idx,
                    origin=(39.1234567, 9.1234567),
                    destination=(39.7654324, 9.7654324),
                    mode=r5_routing.MODE_FAST,
                    departure_iso="2025-10-15T12:00:00",
                )
            finally:
                idx.close()
            self.assertEqual(value, 25.0)

    def test_subset_fetch_parity(self):
        with self._temp_dir() as tmp:
            db_path = Path(tmp) / "routes.sqlite"
            conn = self._create_fresh_db(db_path)
            try:
                run_id = r5_routing._get_or_create_run_id(
                    conn,
                    mode=r5_routing.MODE_FAST,
                    departure_iso="2025-10-15T12:00:00",
                    origins_sig="o",
                    destinations_sig="d",
                    wait_time_estimated_min=5.0,
                )
                cache: dict[tuple[float, float], int] = {}
                from_id = r5_routing._get_or_create_point_id(conn, 39.1, 9.1, cache)
                to1 = r5_routing._get_or_create_point_id(conn, 39.2, 9.2, cache)
                to2 = r5_routing._get_or_create_point_id(conn, 39.3, 9.3, cache)
                r5_routing._insert_rows(
                    conn,
                    [
                        (run_id, from_id, to1, 10.0, 3.0, 13.0),
                        (run_id, from_id, to2, 20.0, 5.0, 25.0),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            idx = r5_routing.open_routing_index(str(db_path))
            try:
                out = r5_routing.fetch_origin_impedance_subset_map(
                    idx,
                    origin=(39.1, 9.1),
                    destinations={(39.2, 9.2), (39.3, 9.3), (40.0, 10.0)},
                    mode=r5_routing.MODE_FAST,
                    departure_iso="2025-10-15T12:00:00",
                )
            finally:
                idx.close()
            self.assertEqual(out[(39.2, 9.2)], 13.0)
            self.assertEqual(out[(39.3, 9.3)], 25.0)
            self.assertNotIn((40.0, 10.0), out)

    def test_hard_break_rejects_old_schema(self):
        with self._temp_dir() as tmp:
            db_path = Path(tmp) / "old.sqlite"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE routes (
                        from_lat_r REAL, from_lon_r REAL, to_lat_r REAL, to_lon_r REAL,
                        impedance_min REAL, mode TEXT, departure_iso TEXT
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with self.assertRaises(RuntimeError):
                r5_routing.open_routing_index(str(db_path))

    def test_sample_csv_generation_contains_expected_columns(self):
        with self._temp_dir() as tmp:
            db_path = Path(tmp) / "routes.sqlite"
            sample_path = Path(tmp) / "sample.csv"
            conn = self._create_fresh_db(db_path)
            try:
                run_id = r5_routing._get_or_create_run_id(
                    conn,
                    mode=r5_routing.MODE_FAST,
                    departure_iso="2025-10-15T12:00:00",
                    origins_sig="o",
                    destinations_sig="d",
                    wait_time_estimated_min=5.0,
                )
                cache: dict[tuple[float, float], int] = {}
                rows: list[tuple[int, int, int, float | None, float | None, float | None]] = []
                for i in range(40):
                    from_lat = round(39.1 + i * 0.0001, 6)
                    from_lon = round(9.1 + i * 0.0001, 6)
                    to_lat = round(39.2 + i * 0.0001, 6)
                    to_lon = round(9.2 + i * 0.0001, 6)
                    from_id = r5_routing._get_or_create_point_id(conn, from_lat, from_lon, cache)
                    to_id = r5_routing._get_or_create_point_id(conn, to_lat, to_lon, cache)
                    if i % 2 == 0:
                        rows.append((run_id, from_id, to_id, None, None, None))
                    else:
                        rows.append((run_id, from_id, to_id, 12.0 + i, 3.0, 15.0 + i))
                r5_routing._insert_rows(conn, rows)
                conn.commit()

                written = r5_routing._write_sample_csv_from_db(
                    sample_path,
                    conn,
                    run_id=run_id,
                    mode=r5_routing.MODE_FAST,
                    departure_iso="2025-10-15T12:00:00",
                    sample_rows=20,
                    missing_share=0.5,
                )
            finally:
                conn.close()

            self.assertLessEqual(written, 20)
            with sample_path.open("r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertTrue(rows)
            self.assertIn("google_maps_transit_url", rows[0])
            self.assertTrue(rows[0]["google_maps_transit_url"].startswith("https://www.google.com/maps/dir/?"))
            self.assertIn("wait_time", rows[0])
            self.assertIn("impedance", rows[0])
            self.assertIn("is_missing", rows[0])
            self.assertNotIn("wait_time_estimated_min", rows[0])
            self.assertGreater(sum(int(r["is_missing"]) for r in rows), 0)

    def test_build_routing_store_smoke_with_mocked_r5(self):
        captured: list[dict] = []

        class _FakeTTM(list):
            def __init__(self, *args, **kwargs):
                captured.append(kwargs)
                super().__init__(
                    [
                        {"from_id": "o0", "to_id": "d0", "travel_time": 12.0},
                        {"from_id": "o0", "to_id": "d1", "travel_time": float("nan")},
                    ]
                )

        fake_r5py = types.ModuleType("r5py")
        fake_r5py.TransportMode = types.SimpleNamespace(BUS="BUS", WALK="WALK")
        fake_r5py.TransportNetwork = lambda *args, **kwargs: object()
        fake_r5py.TravelTimeMatrix = _FakeTTM
        fake_r5py.DetailedItineraries = object

        fake_base = types.ModuleType("r5py.r5.base_travel_time_matrix")
        fake_base.BaseTravelTimeMatrix = type("BaseTravelTimeMatrix", (), {"NUM_THREADS": 1})
        fake_regional_task_mod = types.ModuleType("r5py.r5.regional_task")
        fake_trip_planner_mod = types.ModuleType("r5py.r5.trip_planner")

        class _FakeRegionalTask:
            def __init__(self, transport_network, **kwargs):
                self.transport_network = transport_network
                self.kwargs = kwargs
                self._regional_task = types.SimpleNamespace(
                    fromLat=None,
                    fromLon=None,
                    toLat=None,
                    toLon=None,
                )

            def __copy__(self):
                new = type(self)(self.transport_network, **self.kwargs)
                new._regional_task.fromLat = self._regional_task.fromLat
                new._regional_task.fromLon = self._regional_task.fromLon
                new._regional_task.toLat = self._regional_task.toLat
                new._regional_task.toLon = self._regional_task.toLon
                return new

        class _FakeTripPlanner:
            def __init__(self, transport_network, request):
                _ = transport_network
                od_key = (request._regional_task.fromLat, request._regional_task.toLat)
                # One OD feasible with explicit wait, the other no itinerary.
                if od_key == (39.2, 39.3):
                    self.trips = [
                        types.SimpleNamespace(
                            travel_time=dt.timedelta(minutes=12),
                            wait_time=dt.timedelta(minutes=4),
                        )
                    ]
                else:
                    self.trips = []

        fake_regional_task_mod.RegionalTask = _FakeRegionalTask
        fake_trip_planner_mod.TripPlanner = _FakeTripPlanner

        with self._temp_dir() as tmp:
            out_db = str(Path(tmp) / "routes.sqlite")
            out_csv = str(Path(tmp) / "sample.csv")
            with mock.patch.object(r5_routing, "_force_java_major", return_value=None), \
                 mock.patch.object(r5_routing, "_preflight", return_value=None), \
                 mock.patch.object(r5_routing, "_configure_java_home_from_path", return_value=None), \
                 mock.patch.object(r5_routing, "_configure_java_runtime_flags", return_value=None), \
                 mock.patch.object(r5_routing, "_ensure_r5_classpath_arg", return_value=None), \
                 mock.patch.object(r5_routing, "_ensure_r5_max_memory_arg", return_value=None), \
                 mock.patch.object(r5_routing, "_estimate_fast_wait_time_min", return_value=5.0), \
                 mock.patch.dict(
                     "sys.modules",
                     {
                         "r5py": fake_r5py,
                         "r5py.r5": types.ModuleType("r5py.r5"),
                         "r5py.r5.base_travel_time_matrix": fake_base,
                         "r5py.r5.regional_task": fake_regional_task_mod,
                         "r5py.r5.trip_planner": fake_trip_planner_mod,
                     },
                     clear=False,
                 ):
                summary = r5_routing.build_routing_store(
                    origins=[(39.2, 9.1)],
                    destinations=[(39.3, 9.2), (39.31, 9.21)],
                    mode=r5_routing.MODE_FAST,
                    pbf_path="dummy.pbf",
                    gtfs_path="dummy.zip",
                    jar_path="dummy.jar",
                    departure_dt=dt.datetime(2025, 10, 15, 12, 0, 0),
                    workers=1,
                    out_csv=out_csv,
                    out_db=out_db,
                    chunk_size=1,
                    enable_progress=False,
                    sample_rows=10,
                    sample_missing_share=0.5,
                    r5_tripplanner_workers=2,
                    persist_outputs=True,
                )

            self.assertTrue(captured)
            self.assertTrue(Path(out_db).is_file())
            self.assertTrue(Path(out_csv).is_file())
            self.assertEqual(summary["rows"], 2)
            self.assertEqual(summary["missing_rows"], 1)
            self.assertLessEqual(int(summary["sample_rows_written"]), 10)

            conn = sqlite3.connect(out_db)
            try:
                run_id = r5_routing._resolve_run_id(conn, r5_routing.MODE_FAST, "2025-10-15T12:00:00")
                self.assertIsNotNone(run_id)
                rows_count, missing_count = r5_routing._routing_counts(conn, int(run_id))
                self.assertEqual(rows_count, 2)
                self.assertEqual(missing_count, 1)
                rows = conn.execute(
                    "SELECT travel_time_min, wait_time_min, impedance_min FROM routes WHERE run_id = ? ORDER BY to_point_id",
                    (int(run_id),),
                ).fetchall()
                self.assertEqual(len(rows), 2)
                # Feasible OD has exact wait from TripPlanner and impedance = travel + wait.
                self.assertEqual(rows[0][0], 12.0)
                self.assertEqual(rows[0][1], 4.0)
                self.assertEqual(rows[0][2], 16.0)
                # Infeasible OD remains missing across all fields.
                self.assertIsNone(rows[1][0])
                self.assertIsNone(rows[1][1])
                self.assertIsNone(rows[1][2])
            finally:
                conn.close()

    def test_build_routing_store_origin_estimate_assigns_wait_by_origin(self):
        captured: list[dict] = []

        class _FakeTTM(list):
            def __init__(self, *args, **kwargs):
                captured.append(kwargs)
                super().__init__(
                    [
                        {"from_id": "o0", "to_id": "d0", "travel_time": 12.0},
                        {"from_id": "o1", "to_id": "d0", "travel_time": 18.0},
                    ]
                )

        fake_r5py = types.ModuleType("r5py")
        fake_r5py.TransportMode = types.SimpleNamespace(BUS="BUS", WALK="WALK")
        fake_r5py.TransportNetwork = lambda *args, **kwargs: object()
        fake_r5py.TravelTimeMatrix = _FakeTTM
        fake_r5py.DetailedItineraries = object

        fake_base = types.ModuleType("r5py.r5.base_travel_time_matrix")
        fake_base.BaseTravelTimeMatrix = type("BaseTravelTimeMatrix", (), {"NUM_THREADS": 1})

        with self._temp_dir() as tmp:
            gtfs_path = Path(tmp) / "mini_gtfs.zip"
            self._write_test_gtfs_zip(gtfs_path)
            out_db = str(Path(tmp) / "routes.sqlite")
            out_csv = str(Path(tmp) / "sample.csv")
            with mock.patch.object(r5_routing, "_force_java_major", return_value=None), \
                 mock.patch.object(r5_routing, "_preflight", return_value=None), \
                 mock.patch.object(r5_routing, "_configure_java_home_from_path", return_value=None), \
                 mock.patch.object(r5_routing, "_configure_java_runtime_flags", return_value=None), \
                 mock.patch.object(r5_routing, "_ensure_r5_classpath_arg", return_value=None), \
                 mock.patch.object(r5_routing, "_ensure_r5_max_memory_arg", return_value=None), \
                 mock.patch.dict(
                     "sys.modules",
                     {
                         "r5py": fake_r5py,
                         "r5py.r5": types.ModuleType("r5py.r5"),
                         "r5py.r5.base_travel_time_matrix": fake_base,
                     },
                     clear=False,
                 ):
                summary = r5_routing.build_routing_store(
                    origins=[(39.2, 9.1), (39.21, 9.11)],
                    destinations=[(39.3, 9.2)],
                    mode=r5_routing.MODE_FAST,
                    pbf_path="dummy.pbf",
                    gtfs_path=str(gtfs_path),
                    jar_path="dummy.jar",
                    departure_dt=dt.datetime(2025, 10, 15, 12, 0, 0),
                    workers=1,
                    out_csv=out_csv,
                    out_db=out_db,
                    chunk_size=2,
                    enable_progress=False,
                    sample_rows=10,
                    sample_missing_share=0.5,
                    r5_fast_wait_model="origin_estimate",
                    r5_max_time_walking_min=2,
                    persist_outputs=True,
                )

            self.assertTrue(captured)
            self.assertEqual(summary["rows"], 2)

            conn = sqlite3.connect(out_db)
            try:
                run_id = r5_routing._resolve_run_id(conn, r5_routing.MODE_FAST, "2025-10-15T12:00:00")
                self.assertIsNotNone(run_id)
                rows = conn.execute(
                    """
                    SELECT fp.lat_r, fp.lon_r, r.travel_time_min, r.wait_time_min, r.impedance_min
                    FROM routes AS r
                    JOIN points AS fp ON fp.point_id = r.from_point_id
                    WHERE r.run_id = ?
                    ORDER BY fp.lat_r, fp.lon_r
                    """,
                    (int(run_id),),
                ).fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0][2], 12.0)
                self.assertAlmostEqual(rows[0][3], 10.0, places=3)
                self.assertAlmostEqual(rows[0][4], 22.0, places=3)
                self.assertEqual(rows[1][2], 18.0)
                self.assertAlmostEqual(rows[1][3], 30.0, places=3)
                self.assertAlmostEqual(rows[1][4], 48.0, places=3)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
