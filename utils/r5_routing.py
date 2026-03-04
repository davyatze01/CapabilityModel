from __future__ import annotations

import csv
import datetime as dt
import hashlib
import math
import multiprocessing as mp
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
from tqdm import tqdm


COORD_ROUND = 6
MODE_FAST = "fast_routing"
MODE_SLOW = "slow_routing"
JAVA_SAFE_OPTS = (
    "-XX:+UnlockDiagnosticVMOptions",
    "-XX:TieredStopAtLevel=1",
    "-XX:CICompilerCount=1",
)
JAVA_SLOW_ROUTING_JIT_EXCLUDE_OPTS = (
    # Work around repeated JVM access violations observed while compiling/executing
    # these methods in DetailedItineraries (slow_routing) on Windows + JDK21.
    "-XX:CompileCommand=exclude,com/conveyal/r5/profile/McRaptorSuboptimalPathProfileRouter.doOneRound",
    "-XX:CompileCommand=exclude,com/conveyal/r5/profile/McRaptorSuboptimalPathProfileRouter.addState",
    "-XX:CompileCommand=exclude,com/conveyal/r5/profile/McRaptorSuboptimalPathProfileRouter.doTransfers",
)
R5_FAST_WAIT_ESTIMATE_ENABLED = True
R5_FAST_WAIT_DEFAULT_MIN = 5.0
R5_FAST_WAIT_WINDOW_MIN = 90
R5_FAST_WAIT_STRATEGY = "expected"  # "expected" -> headway/2, "worst" -> full headway


class JavaMajorVersionNotFoundError(RuntimeError):
    pass


@dataclass
class RoutingIndex:
    conn: sqlite3.Connection
    db_path: str

    def close(self) -> None:
        self.conn.close()


def _round_coord_pair(coord: tuple[float, float]) -> tuple[float, float]:
    return (round(float(coord[0]), COORD_ROUND), round(float(coord[1]), COORD_ROUND))


def _configure_java_home_from_path() -> None:
    java_exe = shutil.which("java")
    if java_exe is None:
        return
    java_path = Path(java_exe).resolve()
    if java_path.parent.name.lower() != "bin":
        return
    detected_java_home = java_path.parent.parent
    os.environ["JAVA_HOME"] = str(detected_java_home)


def _java_major_from_exe(java_exe: str) -> int | None:
    try:
        proc = subprocess.run(
            [java_exe, "-version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception:
        return None

    combined = f"{proc.stdout}\n{proc.stderr}"
    m = re.search(r'version\s+"([^"]+)"', combined)
    if not m:
        return None
    version = m.group(1).strip()
    if version.startswith("1."):
        parts = version.split(".")
        if len(parts) > 1 and parts[1].isdigit():
            return int(parts[1])
        return None
    head = version.split(".", 1)[0]
    return int(head) if head.isdigit() else None


def _collect_java_candidates() -> list[str]:
    candidates: list[str] = []
    on_path = shutil.which("java")
    if on_path:
        candidates.append(on_path)
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["where", "java"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            for line in proc.stdout.splitlines():
                path = line.strip()
                if path:
                    candidates.append(path)
        except Exception:
            pass
    deduped: list[str] = []
    seen = set()
    for c in candidates:
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    return deduped


def _force_java_major(required_major: int) -> None:
    for java_exe in _collect_java_candidates():
        major = _java_major_from_exe(java_exe)
        if major != required_major:
            continue
        java_path = Path(java_exe).resolve()
        if java_path.parent.name.lower() != "bin":
            continue
        java_home = str(java_path.parent.parent)
        os.environ["JAVA_HOME"] = java_home
        java_bin = str(java_path.parent)
        current_path = os.environ.get("PATH", "")
        parts = [p for p in current_path.split(os.pathsep) if p]
        parts = [p for p in parts if os.path.normcase(p) != os.path.normcase(java_bin)]
        os.environ["PATH"] = os.pathsep.join([java_bin] + parts)
        return
    raise JavaMajorVersionNotFoundError(
        f"Java {required_major} runtime not found on PATH/where output. "
        "Install JDK 21 and ensure its bin directory is available."
    )


def _ensure_r5_classpath_arg(jar_path: str) -> None:
    if "--r5-classpath" in sys.argv or "-r" in sys.argv:
        return
    sys.argv.extend(["--r5-classpath", jar_path])


def _ensure_r5_max_memory_arg(mode: str) -> None:
    if "--max-memory" in sys.argv or "-m" in sys.argv:
        return
    env_key = "R5_MAX_MEMORY_SLOW" if mode == MODE_SLOW else "R5_MAX_MEMORY_FAST"
    max_memory = os.getenv(env_key, "").strip() or os.getenv("R5_MAX_MEMORY", "").strip()
    if not max_memory:
        # Keep a safer default than r5py's 80% RAM to reduce JVM instability under
        # heavy all-to-all computations on large hosts.
        max_memory = "12G" if mode == MODE_SLOW else "16G"
    sys.argv.extend(["--max-memory", max_memory])


def _configure_java_runtime_flags(mode: str) -> None:
    # Work around intermittent JVM crashes in C2 compiler thread on some Windows hosts.
    safe_mode = os.getenv("R5_JAVA_SAFE_MODE", "1").strip().lower()
    if safe_mode not in {"1", "true", "yes", "on"}:
        return
    current = os.environ.get("JAVA_TOOL_OPTIONS", "").strip()
    required = list(JAVA_SAFE_OPTS)
    slow_exclude = os.getenv("R5_JAVA_SLOW_JIT_EXCLUDE", "1").strip().lower()
    if mode == MODE_SLOW and slow_exclude in {"1", "true", "yes", "on"}:
        required.extend(JAVA_SLOW_ROUTING_JIT_EXCLUDE_OPTS)
    missing = [opt for opt in required if opt not in current]
    if not missing:
        return
    os.environ["JAVA_TOOL_OPTIONS"] = (current + " " + " ".join(missing)).strip()


def _preflight(pbf_path: str, gtfs_path: str, jar_path: str) -> None:
    missing = [path for path in (pbf_path, gtfs_path, jar_path) if not os.path.isfile(path)]
    if missing:
        raise FileNotFoundError(f"Missing routing input files: {missing}")
    try:
        subprocess.run(
            ["java", "-version"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        raise RuntimeError("Java runtime unavailable.") from exc


def _build_points_gdf(prefix: str, coords: list[tuple[float, float]], start_idx: int = 0) -> gpd.GeoDataFrame:
    ids = [f"{prefix}{start_idx + i}" for i in range(len(coords))]
    points = [Point(lon, lat) for lat, lon in coords]
    return gpd.GeoDataFrame({"id": ids, "geometry": points}, crs="EPSG:4326")


def _aggregate_slow_itineraries(detailed: pd.DataFrame) -> pd.DataFrame:
    if detailed.empty:
        return pd.DataFrame(columns=["from_id", "to_id", "travel_time_min", "wait_time_min"])
    per_option = (
        detailed.groupby(["from_id", "to_id", "option"], as_index=False)[["travel_time", "wait_time"]]
        .sum()
    )
    best = per_option.sort_values(
        by=["from_id", "to_id", "travel_time", "wait_time", "option"]
    ).drop_duplicates(subset=["from_id", "to_id"], keep="first")
    best["travel_time_min"] = best["travel_time"].dt.total_seconds() / 60.0
    best["wait_time_min"] = best["wait_time"].dt.total_seconds() / 60.0
    return best[["from_id", "to_id", "travel_time_min", "wait_time_min"]]


def _create_db_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE routes (
            from_lat_r REAL NOT NULL,
            from_lon_r REAL NOT NULL,
            to_lat_r REAL NOT NULL,
            to_lon_r REAL NOT NULL,
            travel_time_min REAL,
            wait_time_min REAL,
            impedance_min REAL,
            mode TEXT NOT NULL,
            departure_iso TEXT NOT NULL,
            PRIMARY KEY (from_lat_r, from_lon_r, to_lat_r, to_lon_r, mode, departure_iso)
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_routes_from ON routes (from_lat_r, from_lon_r, mode, departure_iso)"
    )
    conn.execute(
        "CREATE INDEX idx_routes_to ON routes (to_lat_r, to_lon_r, mode, departure_iso)"
    )
    conn.execute(
        """
        CREATE TABLE run_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def _insert_rows(conn: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> None:
    conn.executemany(
        """
        INSERT OR REPLACE INTO routes (
            from_lat_r, from_lon_r, to_lat_r, to_lon_r,
            travel_time_min, wait_time_min, impedance_min, mode, departure_iso
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _coords_signature(coords: list[tuple[float, float]]) -> str:
    h = hashlib.sha1()
    for lat, lon in coords:
        h.update(f"{round(float(lat), COORD_ROUND)},{round(float(lon), COORD_ROUND)};".encode("ascii"))
    return h.hexdigest()


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _set_run_meta(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO run_meta (key, value) VALUES (?, ?)",
        list(values.items()),
    )


def _get_run_meta(conn: sqlite3.Connection) -> dict[str, str]:
    if not _table_exists(conn, "run_meta"):
        return {}
    rows = conn.execute("SELECT key, value FROM run_meta").fetchall()
    return {str(k): str(v) for k, v in rows}


def _write_csv_header(csv_f) -> None:
    writer = csv.writer(csv_f)
    writer.writerow(
        [
            "from_lat",
            "from_lon",
            "to_lat",
            "to_lon",
            "travel_time_min",
            "wait_time_min",
            "wait_time_estimated_min",
            "mode",
            "departure_iso",
        ]
    )


def _rewrite_tmp_csv_from_db(tmp_csv: Path, conn: sqlite3.Connection, mode: str, departure_iso: str) -> None:
    with tmp_csv.open("w", newline="", encoding="utf-8") as csv_f:
        _write_csv_header(csv_f)
        writer = csv.writer(csv_f)
        cur = conn.execute(
            """
            SELECT
                from_lat_r, from_lon_r, to_lat_r, to_lon_r,
                travel_time_min, wait_time_min, mode, departure_iso
            FROM routes
            WHERE mode = ? AND departure_iso = ?
            """,
            (mode, departure_iso),
        )
        for row in cur:
            row_list = list(row)
            wait_val = row_list[5]
            writer.writerow(
                [
                    row_list[0],
                    row_list[1],
                    row_list[2],
                    row_list[3],
                    row_list[4],
                    wait_val,
                    wait_val,
                    row_list[6],
                    row_list[7],
                ]
            )


def _collect_processed_origin_keys(conn: sqlite3.Connection, mode: str, departure_iso: str) -> set[tuple[float, float]]:
    rows = conn.execute(
        """
        SELECT DISTINCT from_lat_r, from_lon_r
        FROM routes
        WHERE mode = ? AND departure_iso = ?
        """,
        (mode, departure_iso),
    ).fetchall()
    return {(float(lat), float(lon)) for lat, lon in rows}


def _routing_counts(conn: sqlite3.Connection, mode: str, departure_iso: str) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS rows_count,
               SUM(CASE WHEN travel_time_min IS NULL THEN 1 ELSE 0 END) AS missing_count
        FROM routes
        WHERE mode = ? AND departure_iso = ?
        """,
        (mode, departure_iso),
    ).fetchone()
    if row is None:
        return (0, 0)
    rows_count = int(row[0] or 0)
    missing_count = int(row[1] or 0)
    return (rows_count, missing_count)


def _chunked(seq: list[Any], size: int):
    for i in range(0, len(seq), size):
        yield i, seq[i : i + size]


def _parse_gtfs_hms_to_seconds(value: Any) -> int | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    token = str(value).strip()
    parts = token.split(":")
    if len(parts) != 3:
        return None
    try:
        hh = int(parts[0])
        mm = int(parts[1])
        ss = int(parts[2])
    except Exception:
        return None
    if mm < 0 or mm > 59 or ss < 0 or ss > 59 or hh < 0:
        return None
    return hh * 3600 + mm * 60 + ss


def _load_gtfs_active_service_ids(gtfs_zip: zipfile.ZipFile, service_date: dt.date) -> set[str] | None:
    active: set[str] = set()
    have_calendar_source = False
    date_key = service_date.strftime("%Y%m%d")
    weekday_names = (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    )
    weekday_col = weekday_names[service_date.weekday()]

    if "calendar.txt" in gtfs_zip.namelist():
        have_calendar_source = True
        with gtfs_zip.open("calendar.txt") as f:
            calendar_df = pd.read_csv(
                f,
                usecols=["service_id", "start_date", "end_date", weekday_col],
                dtype=str,
            )
        mask = (
            (calendar_df["start_date"] <= date_key)
            & (calendar_df["end_date"] >= date_key)
            & (calendar_df[weekday_col] == "1")
        )
        active.update(calendar_df.loc[mask, "service_id"].astype(str).tolist())

    if "calendar_dates.txt" in gtfs_zip.namelist():
        have_calendar_source = True
        with gtfs_zip.open("calendar_dates.txt") as f:
            cal_dates_df = pd.read_csv(
                f,
                usecols=["service_id", "date", "exception_type"],
                dtype=str,
            )
        day_events = cal_dates_df[cal_dates_df["date"] == date_key]
        add_ids = day_events.loc[day_events["exception_type"] == "1", "service_id"].astype(str).tolist()
        remove_ids = day_events.loc[day_events["exception_type"] == "2", "service_id"].astype(str).tolist()
        active.update(add_ids)
        for sid in remove_ids:
            active.discard(sid)

    if not have_calendar_source:
        return None
    return active


def _estimate_fast_wait_time_min(gtfs_path: str, departure_dt: dt.datetime) -> float:
    if not R5_FAST_WAIT_ESTIMATE_ENABLED:
        return 0.0
    default_wait_min = float(R5_FAST_WAIT_DEFAULT_MIN)
    window_min = max(15, int(R5_FAST_WAIT_WINDOW_MIN))
    strategy = str(R5_FAST_WAIT_STRATEGY).strip().lower()

    try:
        with zipfile.ZipFile(gtfs_path) as gtfs_zip:
            if "stop_times.txt" not in gtfs_zip.namelist():
                return default_wait_min

            with gtfs_zip.open("stop_times.txt") as f:
                stop_times_df = pd.read_csv(
                    f,
                    usecols=["trip_id", "stop_id", "departure_time"],
                    dtype=str,
                )

            active_services = _load_gtfs_active_service_ids(gtfs_zip, departure_dt.date())
            if active_services is not None and "trips.txt" in gtfs_zip.namelist():
                with gtfs_zip.open("trips.txt") as f:
                    trips_df = pd.read_csv(
                        f,
                        usecols=["trip_id", "service_id"],
                        dtype=str,
                    )
                active_trip_ids = set(
                    trips_df.loc[trips_df["service_id"].isin(active_services), "trip_id"].astype(str).tolist()
                )
                if active_trip_ids:
                    stop_times_df = stop_times_df[stop_times_df["trip_id"].isin(active_trip_ids)]

            if stop_times_df.empty:
                return default_wait_min

            stop_times_df["dep_sec"] = stop_times_df["departure_time"].map(_parse_gtfs_hms_to_seconds)
            stop_times_df = stop_times_df.dropna(subset=["dep_sec"]).copy()
            if stop_times_df.empty:
                return default_wait_min
            stop_times_df["dep_sec"] = stop_times_df["dep_sec"].astype(int)

            dep_sec = departure_dt.hour * 3600 + departure_dt.minute * 60 + departure_dt.second
            upper_sec = dep_sec + window_min * 60
            window_df = stop_times_df[(stop_times_df["dep_sec"] >= dep_sec) & (stop_times_df["dep_sec"] <= upper_sec)]
            if window_df.empty:
                return default_wait_min

            window_df = window_df.sort_values(["stop_id", "dep_sec"])
            window_df["headway_min"] = window_df.groupby("stop_id")["dep_sec"].diff() / 60.0
            valid = window_df[(window_df["headway_min"] > 0.0) & (window_df["headway_min"] <= 180.0)]
            if valid.empty:
                return default_wait_min

            per_stop = valid.groupby("stop_id")["headway_min"].median()
            if per_stop.empty:
                return default_wait_min
            headway_med = float(per_stop.median())
            if not math.isfinite(headway_med) or headway_med <= 0.0:
                return default_wait_min
            if strategy in {"worst", "max", "full_headway", "full-headway"}:
                return headway_med
            return headway_med / 2.0
    except Exception:
        return default_wait_min


def _prepare_fast_chunk_df(ttm_df: pd.DataFrame, wait_time_min: float) -> pd.DataFrame:
    if "travel_time" in ttm_df.columns:
        ttm_df["travel_time_min"] = ttm_df["travel_time"]
    elif "travel_time_p50" in ttm_df.columns:
        ttm_df["travel_time_min"] = ttm_df["travel_time_p50"]
    else:
        raise RuntimeError("Unexpected TravelTimeMatrix schema: missing travel_time column.")
    ttm_df["wait_time_min"] = float(wait_time_min)
    return ttm_df[["from_id", "to_id", "travel_time_min", "wait_time_min"]]


def _compute_impedance_min(mode: str, chunk_df: pd.DataFrame) -> pd.Series:
    wait = chunk_df["wait_time_min"].fillna(0.0)
    return chunk_df["travel_time_min"] + wait


def build_routing_store(
    origins: list[tuple[float, float]],
    destinations: list[tuple[float, float]],
    mode: str,
    pbf_path: str,
    gtfs_path: str,
    jar_path: str,
    departure_dt: dt.datetime,
    workers: int,
    out_csv: str,
    out_db: str,
    chunk_size: int = 128,
    enable_progress: bool = True,
    persist_outputs: bool = True,
) -> dict[str, Any]:
    if mode not in {MODE_FAST, MODE_SLOW}:
        raise ValueError(f"Unsupported routing mode: {mode}")
    if not origins or not destinations:
        raise ValueError("Origins and destinations must be non-empty.")

    _force_java_major(21)
    _preflight(pbf_path, gtfs_path, jar_path)
    _configure_java_home_from_path()
    _configure_java_runtime_flags(mode)
    _ensure_r5_classpath_arg(jar_path)
    _ensure_r5_max_memory_arg(mode)

    import r5py
    from r5py.r5.base_travel_time_matrix import BaseTravelTimeMatrix

    workers = max(1, int(workers))
    if mode == MODE_SLOW:
        workers = 1
    BaseTravelTimeMatrix.NUM_THREADS = workers

    transport_network = r5py.TransportNetwork(pbf_path, [gtfs_path])
    destinations_gdf = _build_points_gdf("d", destinations, start_idx=0)
    destinations_map = destinations_gdf.copy()
    destinations_map["to_id"] = destinations_map["id"]
    destinations_map["to_lat"] = destinations_map.geometry.y
    destinations_map["to_lon"] = destinations_map.geometry.x
    destinations_map = destinations_map[["to_id", "to_lat", "to_lon"]]

    departure_iso = departure_dt.isoformat()
    origins_sig = _coords_signature(origins)
    destinations_sig = _coords_signature(destinations)
    total_rows = 0
    missing_rows = 0
    fast_wait_estimate_min = _estimate_fast_wait_time_min(gtfs_path, departure_dt) if mode == MODE_FAST else None

    if not persist_outputs:
        progress = tqdm(total=len(origins), desc=f"r5 {mode}", mininterval=1) if enable_progress else None
        try:
            for start_idx, origins_chunk in _chunked(origins, chunk_size):
                origins_gdf = _build_points_gdf("o", origins_chunk, start_idx=start_idx)
                origins_map = origins_gdf.copy()
                origins_map["from_id"] = origins_map["id"]
                origins_map["from_lat"] = origins_map.geometry.y
                origins_map["from_lon"] = origins_map.geometry.x
                origins_map = origins_map[["from_id", "from_lat", "from_lon"]]

                if mode == MODE_FAST:
                    ttm_ctor = cast(Any, r5py.TravelTimeMatrix)
                    ttm = ttm_ctor(
                        transport_network,
                        origins=origins_gdf,
                        destinations=destinations_gdf,
                        departure=departure_dt,
                        transport_modes=[r5py.TransportMode.BUS],
                        snap_to_network=True,
                    )
                    chunk_df = _prepare_fast_chunk_df(pd.DataFrame(ttm), wait_time_min=float(fast_wait_estimate_min or 0.0))
                else:
                    detailed_ctor = cast(Any, r5py.DetailedItineraries)
                    detailed = detailed_ctor(
                        transport_network,
                        origins=origins_gdf,
                        destinations=destinations_gdf,
                        departure=departure_dt,
                        transport_modes=[r5py.TransportMode.BUS],
                        snap_to_network=True,
                        force_all_to_all=True,
                    )
                    slow_agg = _aggregate_slow_itineraries(pd.DataFrame(detailed))
                    full_pairs = pd.MultiIndex.from_product(
                        [origins_gdf["id"].tolist(), destinations_gdf["id"].tolist()],
                        names=["from_id", "to_id"],
                    ).to_frame(index=False)
                    chunk_df = full_pairs.merge(slow_agg, on=["from_id", "to_id"], how="left")

                chunk_df = chunk_df.merge(origins_map, on="from_id", how="left")
                chunk_df = chunk_df.merge(destinations_map, on="to_id", how="left")
                chunk_df["mode"] = mode
                chunk_df["departure_iso"] = departure_iso
                chunk_df["impedance_min"] = _compute_impedance_min(mode, chunk_df)

                total_rows += len(chunk_df)
                missing_rows += int(chunk_df["travel_time_min"].isna().sum())
                if progress:
                    progress.update(len(origins_chunk))
        finally:
            if progress:
                progress.close()

        return {
            "mode": mode,
            "departure_iso": departure_iso,
            "rows": total_rows,
            "missing_rows": missing_rows,
            "resumed": False,
            "processed_origins": 0,
            "out_csv": None,
            "out_db": None,
            "persist_outputs": False,
            "fast_wait_estimate_min": fast_wait_estimate_min,
        }

    out_csv_path = Path(out_csv)
    out_db_path = Path(out_db)
    out_csv_path.parent.mkdir(parents=True, exist_ok=True)
    out_db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_csv = out_csv_path.with_suffix(out_csv_path.suffix + ".tmp")
    tmp_db = out_db_path.with_suffix(out_db_path.suffix + ".tmp")

    processed_origin_keys: set[tuple[float, float]] = set()
    has_existing_state = False

    # Resume from a previous interrupted run when both temp artifacts are available.
    if tmp_csv.exists() and tmp_db.exists():
        clear_tmp = False
        conn_probe = sqlite3.connect(tmp_db)
        try:
            if _table_exists(conn_probe, "routes"):
                meta = _get_run_meta(conn_probe)
                current_meta = {
                    "mode": mode,
                    "departure_iso": departure_iso,
                    "origins_sig": origins_sig,
                    "destinations_sig": destinations_sig,
                }
                can_resume = False
                if meta:
                    can_resume = all(meta.get(k) == v for k, v in current_meta.items())
                else:
                    can_resume = True
                if can_resume:
                    processed_origin_keys = _collect_processed_origin_keys(conn_probe, mode, departure_iso)
                    total_rows, missing_rows = _routing_counts(conn_probe, mode, departure_iso)
                    _rewrite_tmp_csv_from_db(tmp_csv, conn_probe, mode, departure_iso)
                    has_existing_state = True
                else:
                    clear_tmp = True
            else:
                clear_tmp = True
        finally:
            conn_probe.close()
        if clear_tmp:
            tmp_csv.unlink(missing_ok=True)
            tmp_db.unlink(missing_ok=True)
    elif tmp_csv.exists() or tmp_db.exists():
        # One temp artifact without the other is considered inconsistent; restart cleanly.
        tmp_csv.unlink(missing_ok=True)
        tmp_db.unlink(missing_ok=True)

    if not has_existing_state:
        processed_origin_keys = set()
        total_rows = 0
        missing_rows = 0

    origins_todo = [
        coord
        for coord in origins
        if (round(float(coord[0]), COORD_ROUND), round(float(coord[1]), COORD_ROUND))
        not in processed_origin_keys
    ]
    progress = tqdm(total=len(origins), desc=f"r5 {mode}", mininterval=1) if enable_progress else None

    conn = sqlite3.connect(tmp_db)
    try:
        if not has_existing_state:
            _create_db_schema(conn)
            _set_run_meta(
                conn,
                {
                    "mode": mode,
                    "departure_iso": departure_iso,
                    "origins_sig": origins_sig,
                    "destinations_sig": destinations_sig,
                },
            )
            conn.commit()

        if progress and processed_origin_keys:
            progress.update(len(processed_origin_keys))

        csv_mode = "a" if has_existing_state else "w"
        with tmp_csv.open(csv_mode, newline="", encoding="utf-8") as csv_f:
            writer = csv.writer(csv_f)
            if csv_mode == "w":
                _write_csv_header(csv_f)

            for start_idx, origins_chunk in _chunked(origins_todo, chunk_size):
                origins_gdf = _build_points_gdf("o", origins_chunk, start_idx=start_idx)
                origins_map = origins_gdf.copy()
                origins_map["from_id"] = origins_map["id"]
                origins_map["from_lat"] = origins_map.geometry.y
                origins_map["from_lon"] = origins_map.geometry.x
                origins_map = origins_map[["from_id", "from_lat", "from_lon"]]

                if mode == MODE_FAST:
                    ttm_ctor = cast(Any, r5py.TravelTimeMatrix)
                    ttm = ttm_ctor(
                        transport_network,
                        origins=origins_gdf,
                        destinations=destinations_gdf,
                        departure=departure_dt,
                        transport_modes=[r5py.TransportMode.BUS],
                        snap_to_network=True,
                    )
                    chunk_df = _prepare_fast_chunk_df(pd.DataFrame(ttm), wait_time_min=float(fast_wait_estimate_min or 0.0))
                else:
                    detailed_ctor = cast(Any, r5py.DetailedItineraries)
                    detailed = detailed_ctor(
                        transport_network,
                        origins=origins_gdf,
                        destinations=destinations_gdf,
                        departure=departure_dt,
                        transport_modes=[r5py.TransportMode.BUS],
                        snap_to_network=True,
                        force_all_to_all=True,
                    )
                    slow_agg = _aggregate_slow_itineraries(pd.DataFrame(detailed))
                    full_pairs = pd.MultiIndex.from_product(
                        [origins_gdf["id"].tolist(), destinations_gdf["id"].tolist()],
                        names=["from_id", "to_id"],
                    ).to_frame(index=False)
                    chunk_df = full_pairs.merge(slow_agg, on=["from_id", "to_id"], how="left")

                chunk_df = chunk_df.merge(origins_map, on="from_id", how="left")
                chunk_df = chunk_df.merge(destinations_map, on="to_id", how="left")
                chunk_df["mode"] = mode
                chunk_df["departure_iso"] = departure_iso
                chunk_df["impedance_min"] = _compute_impedance_min(mode, chunk_df)

                total_rows += len(chunk_df)
                missing_rows += int(chunk_df["travel_time_min"].isna().sum())

                for row in chunk_df.itertuples(index=False):
                    writer.writerow(
                        [
                            row.from_lat,
                            row.from_lon,
                            row.to_lat,
                            row.to_lon,
                            row.travel_time_min,
                            row.wait_time_min,
                            row.wait_time_min,
                            row.mode,
                            row.departure_iso,
                        ]
                    )

                db_rows: list[tuple[Any, ...]] = []
                for row in chunk_df.itertuples(index=False):
                    from_lat_r = round(float(row.from_lat), COORD_ROUND)
                    from_lon_r = round(float(row.from_lon), COORD_ROUND)
                    to_lat_r = round(float(row.to_lat), COORD_ROUND)
                    to_lon_r = round(float(row.to_lon), COORD_ROUND)
                    travel = None if pd.isna(row.travel_time_min) else float(row.travel_time_min)
                    wait = None if pd.isna(row.wait_time_min) else float(row.wait_time_min)
                    impedance = None if pd.isna(row.impedance_min) else float(row.impedance_min)
                    db_rows.append(
                        (
                            from_lat_r,
                            from_lon_r,
                            to_lat_r,
                            to_lon_r,
                            travel,
                            wait,
                            impedance,
                            mode,
                            departure_iso,
                        )
                    )
                _insert_rows(conn, db_rows)
                conn.commit()

                if progress:
                    progress.update(len(origins_chunk))
    finally:
        if progress:
            progress.close()
        conn.close()

    os.replace(tmp_csv, out_csv_path)
    os.replace(tmp_db, out_db_path)
    return {
        "mode": mode,
        "departure_iso": departure_iso,
        "rows": total_rows,
        "missing_rows": missing_rows,
        "resumed": has_existing_state,
        "processed_origins": len(processed_origin_keys),
        "out_csv": str(out_csv_path),
        "out_db": str(out_db_path),
        "persist_outputs": True,
        "fast_wait_estimate_min": fast_wait_estimate_min,
    }


def _build_routing_store_child(run_args: dict[str, Any], result_queue: Any) -> None:
    try:
        summary = build_routing_store(**run_args)
        result_queue.put({"ok": True, "summary": summary})
    except Exception as exc:
        result_queue.put(
            {
                "ok": False,
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            }
        )


def build_routing_store_resilient(
    origins: list[tuple[float, float]],
    destinations: list[tuple[float, float]],
    mode: str,
    pbf_path: str,
    gtfs_path: str,
    jar_path: str,
    departure_dt: dt.datetime,
    workers: int,
    out_csv: str,
    out_db: str,
    chunk_size: int = 128,
    enable_progress: bool = True,
    max_retries: int = 3,
    min_workers: int = 1,
    min_chunk_size: int = 16,
    retry_delay_s: float = 2.0,
    attempt_timeout_s: float | None = None,
    persist_outputs: bool = True,
) -> dict[str, Any]:
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")
    if attempt_timeout_s is not None and attempt_timeout_s <= 0:
        raise ValueError("attempt_timeout_s must be > 0 when provided")

    attempts_total = max_retries + 1
    last_error: str | None = None
    last_traceback: str | None = None
    last_exitcode: int | None = None

    for attempt in range(attempts_total):
        factor = 2**attempt
        attempt_workers = max(int(min_workers), int(max(1, workers)) // factor)
        attempt_chunk = max(int(min_chunk_size), int(max(1, chunk_size)) // factor)
        run_args = {
            "origins": origins,
            "destinations": destinations,
            "mode": mode,
            "pbf_path": pbf_path,
            "gtfs_path": gtfs_path,
            "jar_path": jar_path,
            "departure_dt": departure_dt,
            "workers": attempt_workers,
            "out_csv": out_csv,
            "out_db": out_db,
            "chunk_size": attempt_chunk,
            "enable_progress": enable_progress,
            "persist_outputs": persist_outputs,
        }
        result_queue: Any = mp.Queue()
        proc = mp.Process(target=_build_routing_store_child, args=(run_args, result_queue), daemon=False)
        proc.start()
        proc.join(timeout=attempt_timeout_s)
        timed_out = proc.is_alive()
        if timed_out:
            proc.terminate()
            proc.join(timeout=5)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=5)
        last_exitcode = proc.exitcode

        child_result = None
        try:
            if not result_queue.empty():
                child_result = result_queue.get_nowait()
        except Exception:
            child_result = None
        finally:
            result_queue.close()
            result_queue.join_thread()

        if last_exitcode == 0 and isinstance(child_result, dict) and child_result.get("ok") is True:
            summary = dict(child_result["summary"])
            summary["supervisor_attempt"] = attempt + 1
            summary["supervisor_attempts_total"] = attempts_total
            summary["supervisor_workers_used"] = attempt_workers
            summary["supervisor_chunk_size_used"] = attempt_chunk
            return summary

        if isinstance(child_result, dict) and child_result.get("ok") is False:
            last_error = str(child_result.get("error"))
            last_traceback = str(child_result.get("traceback", ""))
        elif timed_out:
            timeout_label = f"{attempt_timeout_s:.0f}s" if attempt_timeout_s is not None else "configured timeout"
            last_error = (
                f"routing subprocess timed out after {timeout_label} "
                "(possible deadlock / excessive workload for current chunk)."
            )
            last_traceback = None
        else:
            last_error = (
                f"routing subprocess exited unexpectedly with exit code {last_exitcode} "
                "(possible native crash / OOM in dependency code)."
            )
            last_traceback = None

        if attempt + 1 < attempts_total and retry_delay_s > 0:
            time.sleep(retry_delay_s)

    detail = f"Routing failed after {attempts_total} attempts. Last error: {last_error}"
    if last_traceback:
        detail += f"\nChild traceback:\n{last_traceback}"
    else:
        detail += f"\nLast subprocess exit code: {last_exitcode}"
    raise RuntimeError(detail)


def open_routing_index(db_path: str) -> RoutingIndex:
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"Routing index DB not found: {db_path}")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    return RoutingIndex(conn=conn, db_path=db_path)


def lookup_impedance(
    index: RoutingIndex,
    origin: tuple[float, float],
    destination: tuple[float, float],
    mode: str,
    departure_iso: str,
) -> float | None:
    from_lat_r, from_lon_r = _round_coord_pair(origin)
    to_lat_r, to_lon_r = _round_coord_pair(destination)
    cur = index.conn.execute(
        """
        SELECT impedance_min
        FROM routes
        WHERE from_lat_r = ?
          AND from_lon_r = ?
          AND to_lat_r = ?
          AND to_lon_r = ?
          AND mode = ?
          AND departure_iso = ?
        LIMIT 1
        """,
        (from_lat_r, from_lon_r, to_lat_r, to_lon_r, mode, departure_iso),
    )
    row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def fetch_origin_impedance_map(
    index: RoutingIndex,
    origin: tuple[float, float],
    mode: str,
    departure_iso: str,
) -> dict[tuple[float, float], float]:
    from_lat_r, from_lon_r = _round_coord_pair(origin)
    cur = index.conn.execute(
        """
        SELECT to_lat_r, to_lon_r, impedance_min
        FROM routes
        WHERE from_lat_r = ?
          AND from_lon_r = ?
          AND mode = ?
          AND departure_iso = ?
          AND impedance_min IS NOT NULL
        """,
        (from_lat_r, from_lon_r, mode, departure_iso),
    )
    out: dict[tuple[float, float], float] = {}
    for to_lat_r, to_lon_r, imp in cur.fetchall():
        out[(float(to_lat_r), float(to_lon_r))] = float(imp)
    return out


def fetch_origin_impedance_subset_map(
    index: RoutingIndex,
    origin: tuple[float, float],
    destinations: list[tuple[float, float]] | set[tuple[float, float]],
    mode: str,
    departure_iso: str,
    chunk_size: int = 400,
) -> dict[tuple[float, float], float]:
    from_lat_r, from_lon_r = _round_coord_pair(origin)
    rounded_destinations = sorted({_round_coord_pair(d) for d in destinations})
    if not rounded_destinations:
        return {}

    out: dict[tuple[float, float], float] = {}
    for i in range(0, len(rounded_destinations), chunk_size):
        chunk = rounded_destinations[i : i + chunk_size]
        if not chunk:
            continue
        pair_filters = " OR ".join(["(to_lat_r = ? AND to_lon_r = ?)"] * len(chunk))
        sql = f"""
        SELECT r.to_lat_r, r.to_lon_r, r.impedance_min
        FROM routes AS r
        WHERE r.from_lat_r = ?
          AND r.from_lon_r = ?
          AND r.mode = ?
          AND r.departure_iso = ?
          AND r.impedance_min IS NOT NULL
          AND ({pair_filters})
        """
        params: list[float | str] = [from_lat_r, from_lon_r, mode, departure_iso]
        for to_lat_r, to_lon_r in chunk:
            params.extend([to_lat_r, to_lon_r])
        cur = index.conn.execute(sql, tuple(params))
        for to_lat_r, to_lon_r, imp in cur.fetchall():
            out[(float(to_lat_r), float(to_lon_r))] = float(imp)
    return out
