from __future__ import annotations

import copy
import concurrent.futures as cf
import csv
import datetime as dt
import hashlib
import math
import multiprocessing as mp
import os
import random
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
from urllib.parse import urlencode

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
ROUTING_SCHEMA_VERSION = "3"


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
        CREATE TABLE points (
            point_id INTEGER PRIMARY KEY,
            lat_r REAL NOT NULL,
            lon_r REAL NOT NULL,
            UNIQUE(lat_r, lon_r)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE runs (
            run_id INTEGER PRIMARY KEY,
            mode TEXT NOT NULL,
            departure_iso TEXT NOT NULL,
            wait_time_estimated_min REAL,
            origins_sig TEXT NOT NULL,
            destinations_sig TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(mode, departure_iso, origins_sig, destinations_sig)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE routes (
            run_id INTEGER NOT NULL,
            from_point_id INTEGER NOT NULL,
            to_point_id INTEGER NOT NULL,
            travel_time_min REAL,
            wait_time_min REAL,
            impedance_min REAL,
            PRIMARY KEY (run_id, from_point_id, to_point_id),
            FOREIGN KEY (run_id) REFERENCES runs(run_id),
            FOREIGN KEY (from_point_id) REFERENCES points(point_id),
            FOREIGN KEY (to_point_id) REFERENCES points(point_id)
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_points_lat_lon ON points (lat_r, lon_r)"
    )
    conn.execute(
        "CREATE INDEX idx_routes_run_from ON routes (run_id, from_point_id)"
    )
    conn.execute(
        "CREATE INDEX idx_routes_run_to ON routes (run_id, to_point_id)"
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
            run_id, from_point_id, to_point_id, travel_time_min, wait_time_min, impedance_min
        ) VALUES (?, ?, ?, ?, ?, ?)
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


def _assert_supported_schema(conn: sqlite3.Connection, db_path: str) -> None:
    meta = _get_run_meta(conn)
    version = meta.get("schema_version")
    required_tables = {"points", "runs", "routes", "run_meta"}
    existing_tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if version != ROUTING_SCHEMA_VERSION or not required_tables.issubset(existing_tables):
        raise RuntimeError(
            f"Unsupported routing DB schema in {db_path}. "
            "Delete routing cache and rebuild (hard-break schema change)."
        )


def _get_or_create_point_id(
    conn: sqlite3.Connection,
    lat_r: float,
    lon_r: float,
    cache: dict[tuple[float, float], int],
) -> int:
    key = (lat_r, lon_r)
    cached = cache.get(key)
    if cached is not None:
        return cached
    conn.execute(
        "INSERT OR IGNORE INTO points (lat_r, lon_r) VALUES (?, ?)",
        (lat_r, lon_r),
    )
    row = conn.execute(
        "SELECT point_id FROM points WHERE lat_r = ? AND lon_r = ? LIMIT 1",
        (lat_r, lon_r),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Failed resolving point_id for ({lat_r}, {lon_r})")
    point_id = int(row[0])
    cache[key] = point_id
    return point_id


def _resolve_point_id(conn: sqlite3.Connection, lat_r: float, lon_r: float) -> int | None:
    row = conn.execute(
        "SELECT point_id FROM points WHERE lat_r = ? AND lon_r = ? LIMIT 1",
        (lat_r, lon_r),
    ).fetchone()
    if row is None:
        return None
    return int(row[0])


def _get_or_create_run_id(
    conn: sqlite3.Connection,
    mode: str,
    departure_iso: str,
    origins_sig: str,
    destinations_sig: str,
    wait_time_estimated_min: float | None,
) -> int:
    created_at = dt.datetime.now(dt.timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO runs (
            mode, departure_iso, wait_time_estimated_min, origins_sig, destinations_sig, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            mode,
            departure_iso,
            None if wait_time_estimated_min is None else float(wait_time_estimated_min),
            origins_sig,
            destinations_sig,
            created_at,
        ),
    )
    row = conn.execute(
        """
        SELECT run_id
        FROM runs
        WHERE mode = ? AND departure_iso = ? AND origins_sig = ? AND destinations_sig = ?
        LIMIT 1
        """,
        (mode, departure_iso, origins_sig, destinations_sig),
    ).fetchone()
    if row is None:
        raise RuntimeError("Failed resolving run_id for routing run.")
    return int(row[0])


def _resolve_run_id(
    conn: sqlite3.Connection,
    mode: str,
    departure_iso: str,
) -> int | None:
    row = conn.execute(
        """
        SELECT run_id
        FROM runs
        WHERE mode = ? AND departure_iso = ?
        ORDER BY run_id DESC
        LIMIT 1
        """,
        (mode, departure_iso),
    ).fetchone()
    if row is None:
        return None
    return int(row[0])


def _write_sample_csv_header(csv_f) -> None:
    writer = csv.writer(csv_f)
    writer.writerow(
        [
            "from_lat",
            "from_lon",
            "to_lat",
            "to_lon",
            "google_maps_transit_url",
            "travel_time_min",
            "wait_time_min",
            "impedance_min",
            "is_missing",
        ]
    )


def _google_maps_transit_link(
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    departure_iso: str,
) -> str:
    try:
        departure_dt = dt.datetime.fromisoformat(departure_iso)
    except Exception:
        return ""
    if departure_dt.tzinfo is None:
        departure_unix = int(departure_dt.replace(tzinfo=dt.timezone.utc).timestamp())
    else:
        departure_unix = int(departure_dt.timestamp())
    params = urlencode(
        {
            "api": 1,
            "origin": f"{from_lat},{from_lon}",
            "destination": f"{to_lat},{to_lon}",
            "travelmode": "transit",
            "departure_time": departure_unix,
        }
    )
    return f"https://www.google.com/maps/dir/?{params}"


def _write_sample_csv_from_db(
    csv_path: Path,
    conn: sqlite3.Connection,
    run_id: int,
    mode: str,
    departure_iso: str,
    sample_rows: int,
    missing_share: float,
    rng_seed: int = 42,
) -> int:
    sample_rows = max(1, int(sample_rows))
    missing_share = max(0.0, min(1.0, float(missing_share)))
    missing_target = int(round(sample_rows * missing_share))
    non_missing_target = max(0, sample_rows - missing_target)

    def _fetch(limit: int, missing_only: bool) -> list[tuple[Any, ...]]:
        if limit <= 0:
            return []
        where_missing = "AND r.travel_time_min IS NULL" if missing_only else "AND r.travel_time_min IS NOT NULL"
        cur = conn.execute(
            f"""
            SELECT
                fp.lat_r, fp.lon_r, tp.lat_r, tp.lon_r,
                r.travel_time_min, r.wait_time_min, r.impedance_min
            FROM routes AS r
            JOIN points AS fp ON fp.point_id = r.from_point_id
            JOIN points AS tp ON tp.point_id = r.to_point_id
            WHERE r.run_id = ?
              {where_missing}
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (run_id, limit),
        )
        return list(cur.fetchall())

    missing_rows_data = _fetch(missing_target, missing_only=True)
    non_missing_rows_data = _fetch(non_missing_target, missing_only=False)

    current_total = len(missing_rows_data) + len(non_missing_rows_data)
    if current_total < sample_rows:
        remainder = sample_rows - current_total
        cur = conn.execute(
            """
            SELECT
                fp.lat_r, fp.lon_r, tp.lat_r, tp.lon_r,
                r.travel_time_min, r.wait_time_min, r.impedance_min
            FROM routes AS r
            JOIN points AS fp ON fp.point_id = r.from_point_id
            JOIN points AS tp ON tp.point_id = r.to_point_id
            WHERE r.run_id = ?
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (run_id, remainder),
        )
        fallback = list(cur.fetchall())
    else:
        fallback = []

    combined = missing_rows_data + non_missing_rows_data + fallback
    if len(combined) > sample_rows:
        rng = random.Random(int(rng_seed))
        combined = rng.sample(combined, sample_rows)

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as csv_f:
        _write_sample_csv_header(csv_f)
        writer = csv.writer(csv_f)
        for row in combined:
            from_lat, from_lon, to_lat, to_lon, travel, wait, impedance = row
            writer.writerow(
                [
                    from_lat,
                    from_lon,
                    to_lat,
                    to_lon,
                    _google_maps_transit_link(from_lat, from_lon, to_lat, to_lon, departure_iso),
                    travel,
                    wait,
                    impedance,
                    1 if travel is None else 0,
                ]
            )
    return len(combined)


def _collect_processed_origin_keys(conn: sqlite3.Connection, run_id: int) -> set[tuple[float, float]]:
    rows = conn.execute(
        """
        SELECT DISTINCT p.lat_r, p.lon_r
        FROM routes AS r
        JOIN points AS p ON p.point_id = r.from_point_id
        WHERE r.run_id = ?
        """,
        (run_id,),
    ).fetchall()
    return {(float(lat), float(lon)) for lat, lon in rows}


def _routing_counts(conn: sqlite3.Connection, run_id: int) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS rows_count,
               SUM(CASE WHEN travel_time_min IS NULL THEN 1 ELSE 0 END) AS missing_count
        FROM routes
        WHERE run_id = ?
        """,
        (run_id,),
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


def _prepare_fast_chunk_df(ttm_df: pd.DataFrame, wait_time_min: float | None) -> pd.DataFrame:
    if "travel_time" in ttm_df.columns:
        ttm_df["travel_time_min"] = ttm_df["travel_time"]
    elif "travel_time_p50" in ttm_df.columns:
        ttm_df["travel_time_min"] = ttm_df["travel_time_p50"]
    else:
        raise RuntimeError("Unexpected TravelTimeMatrix schema: missing travel_time column.")
    if wait_time_min is None:
        ttm_df["wait_time_min"] = float("nan")
    else:
        ttm_df["wait_time_min"] = float(wait_time_min)
    return ttm_df[["from_id", "to_id", "travel_time_min", "wait_time_min"]]


def _compute_impedance_min(mode: str, chunk_df: pd.DataFrame) -> pd.Series:
    return chunk_df["travel_time_min"] + chunk_df["wait_time_min"]


def _build_transit_routing_kwargs(
    r5py_module: Any,
    departure_dt: dt.datetime,
    r5_max_time_walking_min: int,
    r5_departure_window_min: int,
) -> dict[str, Any]:
    if int(r5_max_time_walking_min) <= 0:
        raise ValueError("r5_max_time_walking_min must be > 0")
    if int(r5_departure_window_min) <= 0:
        raise ValueError("r5_departure_window_min must be > 0")
    return {
        "departure": departure_dt,
        "transport_modes": [r5py_module.TransportMode.BUS],
        "access_modes": [r5py_module.TransportMode.WALK],
        "egress_modes": [r5py_module.TransportMode.WALK],
        "max_time_walking": dt.timedelta(minutes=int(r5_max_time_walking_min)),
        "departure_time_window": dt.timedelta(minutes=int(r5_departure_window_min)),
        "snap_to_network": True,
    }


def _normalize_fast_wait_model(value: str | None) -> str:
    token = (value or "").strip().lower()
    if token in {"tripplanner_exact", "tripplanner", "exact"}:
        return "tripplanner_exact"
    if token in {"global_estimate", "estimate", "legacy"}:
        return "global_estimate"
    raise ValueError(f"Unsupported r5_fast_wait_model: {value}")


def _estimate_wait_time_tripplanner_min(
    trip_planner_cls: Any,
    base_request: Any,
    origin: tuple[float, float],
    destination: tuple[float, float],
    timeout_s: float,
) -> float | None:
    started = time.monotonic()
    request = copy.copy(base_request)
    request._regional_task.fromLat = float(origin[0])
    request._regional_task.fromLon = float(origin[1])
    request._regional_task.toLat = float(destination[0])
    request._regional_task.toLon = float(destination[1])
    planner = trip_planner_cls(base_request.transport_network, request)
    trips = planner.trips
    if not trips:
        return None
    best_wait: float | None = None
    best_total = None
    for trip in trips:
        try:
            total = trip.travel_time + trip.wait_time
            wait = trip.wait_time.total_seconds() / 60.0
        except Exception:
            continue
        if best_total is None or total < best_total:
            best_total = total
            best_wait = float(wait)
    elapsed = time.monotonic() - started
    if timeout_s > 0 and elapsed > timeout_s:
        return None
    return best_wait


def _apply_tripplanner_exact_waits(
    chunk_df: pd.DataFrame,
    transport_network: Any,
    r5py_module: Any,
    departure_dt: dt.datetime,
    r5_max_time_walking_min: int,
    r5_departure_window_min: int,
    r5_tripplanner_timeout_s: float,
    r5_tripplanner_workers: int,
    progress_callback: Any | None = None,
) -> pd.DataFrame:
    from r5py.r5.regional_task import RegionalTask
    from r5py.r5.trip_planner import TripPlanner

    base_request = RegionalTask(
        transport_network,
        departure=departure_dt,
        transport_modes=[r5py_module.TransportMode.BUS],
        access_modes=[r5py_module.TransportMode.WALK],
        egress_modes=[r5py_module.TransportMode.WALK],
        max_time_walking=dt.timedelta(minutes=int(r5_max_time_walking_min)),
        departure_time_window=dt.timedelta(minutes=int(r5_departure_window_min)),
    )

    chunk_df = chunk_df.copy()
    chunk_df["wait_time_min"] = float("nan")
    feasible = chunk_df[chunk_df["travel_time_min"].notna()]
    jobs = [
        (
            idx,
            (float(row["from_lat"]), float(row["from_lon"])),
            (float(row["to_lat"]), float(row["to_lon"])),
        )
        for idx, row in feasible.iterrows()
    ]
    if not jobs:
        return chunk_df

    workers = max(1, int(r5_tripplanner_workers))
    timeout_s = max(1.0, float(r5_tripplanner_timeout_s))
    max_batch_wait_s = timeout_s * math.ceil(len(jobs) / workers) + 5.0

    executor = cf.ThreadPoolExecutor(max_workers=workers)
    try:
        future_to_idx = {
            executor.submit(
                _estimate_wait_time_tripplanner_min,
                trip_planner_cls=TripPlanner,
                base_request=base_request,
                origin=origin,
                destination=destination,
                timeout_s=timeout_s,
            ): idx
            for idx, origin, destination in jobs
        }
        try:
            for future in cf.as_completed(future_to_idx, timeout=max_batch_wait_s):
                idx = future_to_idx[future]
                try:
                    wait_min = future.result()
                except Exception:
                    wait_min = None
                if wait_min is not None:
                    chunk_df.at[idx, "wait_time_min"] = float(wait_min)
                if progress_callback is not None:
                    progress_callback()
        except cf.TimeoutError:
            # Avoid stalling the whole pass on a small number of hanging ODs.
            pass
        finally:
            for future in future_to_idx:
                if not future.done():
                    future.cancel()
                    if progress_callback is not None:
                        progress_callback()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return chunk_df


def _apply_tripplanner_waits_to_run(
    conn: sqlite3.Connection,
    run_id: int,
    transport_network: Any,
    r5py_module: Any,
    departure_dt: dt.datetime,
    r5_max_time_walking_min: int,
    r5_departure_window_min: int,
    r5_tripplanner_timeout_s: float,
    r5_tripplanner_workers: int,
    enable_progress: bool,
    batch_size: int = 1024,
) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM routes WHERE run_id = ? AND travel_time_min IS NOT NULL",
        (run_id,),
    ).fetchone()
    total = int(row[0] or 0) if row is not None else 0
    if total <= 0:
        return 0

    pbar = (
        tqdm(
            total=total,
            desc="r5 fast tripplanner (phase 2/2)",
            mininterval=1,
            maxinterval=1,
            miniters=1,
        )
        if enable_progress
        else None
    )
    processed = 0
    cur = conn.execute(
        """
        SELECT
            r.from_point_id,
            r.to_point_id,
            fp.lat_r,
            fp.lon_r,
            tp.lat_r,
            tp.lon_r,
            r.travel_time_min
        FROM routes AS r
        JOIN points AS fp ON fp.point_id = r.from_point_id
        JOIN points AS tp ON tp.point_id = r.to_point_id
        WHERE r.run_id = ?
          AND r.travel_time_min IS NOT NULL
        """,
        (run_id,),
    )
    try:
        while True:
            rows = cur.fetchmany(int(batch_size))
            if not rows:
                break
            batch_df = pd.DataFrame(
                rows,
                columns=[
                    "from_point_id",
                    "to_point_id",
                    "from_lat",
                    "from_lon",
                    "to_lat",
                    "to_lon",
                    "travel_time_min",
                ],
            )
            def _tick_progress() -> None:
                if pbar:
                    pbar.update(1)

            batch_df = _apply_tripplanner_exact_waits(
                batch_df,
                transport_network=transport_network,
                r5py_module=r5py_module,
                departure_dt=departure_dt,
                r5_max_time_walking_min=r5_max_time_walking_min,
                r5_departure_window_min=r5_departure_window_min,
                r5_tripplanner_timeout_s=r5_tripplanner_timeout_s,
                r5_tripplanner_workers=r5_tripplanner_workers,
                progress_callback=_tick_progress,
            )
            updates: list[tuple[Any, ...]] = []
            for item in batch_df.itertuples(index=False):
                wait = None if pd.isna(item.wait_time_min) else float(item.wait_time_min)
                imp = None if wait is None else float(item.travel_time_min) + float(wait)
                updates.append((wait, imp, int(run_id), int(item.from_point_id), int(item.to_point_id)))
            conn.executemany(
                """
                UPDATE routes
                SET wait_time_min = ?, impedance_min = ?
                WHERE run_id = ?
                  AND from_point_id = ?
                  AND to_point_id = ?
                """,
                updates,
            )
            conn.commit()
            processed += len(rows)
    finally:
        if pbar:
            pbar.close()
    return processed


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
    sample_csv_path: str | None = None,
    sample_rows: int = 10000,
    sample_missing_share: float = 0.3,
    r5_fast_wait_model: str = "tripplanner_exact",
    r5_tripplanner_workers: int | None = None,
    r5_tripplanner_timeout_s: float = 15.0,
    r5_max_time_walking_min: int = 30,
    r5_departure_window_min: int = 60,
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
    transit_kwargs = _build_transit_routing_kwargs(
        r5py_module=r5py,
        departure_dt=departure_dt,
        r5_max_time_walking_min=r5_max_time_walking_min,
        r5_departure_window_min=r5_departure_window_min,
    )
    fast_wait_model = _normalize_fast_wait_model(r5_fast_wait_model)
    tripplanner_workers = (
        max(1, min(4, workers))
        if r5_tripplanner_workers is None
        else max(1, int(r5_tripplanner_workers))
    )
    total_rows = 0
    missing_rows = 0
    fast_wait_estimate_min = (
        _estimate_fast_wait_time_min(gtfs_path, departure_dt)
        if mode == MODE_FAST and fast_wait_model == "global_estimate"
        else None
    )

    if not persist_outputs:
        progress = (
            tqdm(
                total=len(origins),
                desc=f"r5 {mode}",
                mininterval=1,
                maxinterval=1,
                miniters=1,
            )
            if enable_progress
            else None
        )
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
                        **transit_kwargs,
                    )
                    chunk_df = _prepare_fast_chunk_df(pd.DataFrame(ttm), wait_time_min=fast_wait_estimate_min)
                else:
                    detailed_ctor = cast(Any, r5py.DetailedItineraries)
                    detailed = detailed_ctor(
                        transport_network,
                        origins=origins_gdf,
                        destinations=destinations_gdf,
                        **transit_kwargs,
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
                if mode == MODE_FAST and fast_wait_model == "tripplanner_exact":
                    chunk_df = _apply_tripplanner_exact_waits(
                        chunk_df,
                        transport_network=transport_network,
                        r5py_module=r5py,
                        departure_dt=departure_dt,
                        r5_max_time_walking_min=r5_max_time_walking_min,
                        r5_departure_window_min=r5_departure_window_min,
                        r5_tripplanner_timeout_s=r5_tripplanner_timeout_s,
                        r5_tripplanner_workers=tripplanner_workers,
                    )
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

    out_csv_path = Path(sample_csv_path or out_csv)
    out_db_path = Path(out_db)
    out_csv_path.parent.mkdir(parents=True, exist_ok=True)
    out_db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_db = out_db_path.with_suffix(out_db_path.suffix + ".tmp")

    # Resume from completed cache as well: if no temp artifact exists but a finished
    # DB does, seed tmp from it so we can continue with phase 2 without recomputing
    # phase 1 matrix rows.
    if (not tmp_db.exists()) and out_db_path.exists():
        shutil.copy2(out_db_path, tmp_db)

    processed_origin_keys: set[tuple[float, float]] = set()
    has_existing_state = False
    run_id: int | None = None

    # Resume from a previous interrupted run when a temp DB artifact is available.
    if tmp_db.exists():
        clear_tmp = False
        conn_probe = sqlite3.connect(tmp_db)
        try:
            if _table_exists(conn_probe, "routes"):
                _assert_supported_schema(conn_probe, str(tmp_db))
                meta = _get_run_meta(conn_probe)
                current_meta = {
                    "schema_version": ROUTING_SCHEMA_VERSION,
                    "mode": mode,
                    "departure_iso": departure_iso,
                    "origins_sig": origins_sig,
                    "destinations_sig": destinations_sig,
                }
                can_resume = all(meta.get(k) == v for k, v in current_meta.items())
                if can_resume:
                    maybe_run_id = _get_or_create_run_id(
                        conn_probe,
                        mode=mode,
                        departure_iso=departure_iso,
                        origins_sig=origins_sig,
                        destinations_sig=destinations_sig,
                        wait_time_estimated_min=fast_wait_estimate_min,
                    )
                    run_id = maybe_run_id
                    processed_origin_keys = _collect_processed_origin_keys(conn_probe, run_id)
                    total_rows, missing_rows = _routing_counts(conn_probe, run_id)
                    has_existing_state = True
                else:
                    clear_tmp = True
            else:
                clear_tmp = True
        finally:
            conn_probe.close()
        if clear_tmp:
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
    matrix_desc = (
        "r5 fast matrix (phase 1/2)"
        if mode == MODE_FAST and fast_wait_model == "tripplanner_exact"
        else f"r5 {mode}"
    )
    total_matrix_chunks = max(1, math.ceil(len(origins) / max(1, int(chunk_size))))
    completed_origins = len(origins) - len(origins_todo)
    completed_matrix_chunks = min(
        total_matrix_chunks,
        math.ceil(max(0, completed_origins) / max(1, int(chunk_size))),
    )
    progress = (
        tqdm(
            total=total_matrix_chunks,
            desc=matrix_desc,
            mininterval=1,
            maxinterval=1,
            miniters=1,
            unit="chunk",
        )
        if enable_progress
        else None
    )

    conn = sqlite3.connect(tmp_db)
    try:
        if not has_existing_state:
            _create_db_schema(conn)
            _set_run_meta(
                conn,
                {
                    "schema_version": ROUTING_SCHEMA_VERSION,
                    "mode": mode,
                    "departure_iso": departure_iso,
                    "origins_sig": origins_sig,
                    "destinations_sig": destinations_sig,
                },
            )
            conn.commit()
        else:
            _assert_supported_schema(conn, str(tmp_db))

        if run_id is None:
            run_id = _get_or_create_run_id(
                conn,
                mode=mode,
                departure_iso=departure_iso,
                origins_sig=origins_sig,
                destinations_sig=destinations_sig,
                wait_time_estimated_min=fast_wait_estimate_min,
            )

        sample_written_early = False

        def _maybe_write_early_sample() -> None:
            nonlocal sample_written_early
            if sample_written_early:
                return
            if int(sample_rows) <= 0:
                return
            if total_rows < int(sample_rows):
                return
            _write_sample_csv_from_db(
                out_csv_path,
                conn,
                run_id=int(run_id),
                mode=mode,
                departure_iso=departure_iso,
                sample_rows=sample_rows,
                missing_share=sample_missing_share,
            )
            sample_written_early = True

        if progress and completed_matrix_chunks:
            progress.update(completed_matrix_chunks)

        point_id_cache: dict[tuple[float, float], int] = {}
        if origins_todo:
            for lat, lon in origins_todo:
                lat_r, lon_r = _round_coord_pair((lat, lon))
                _get_or_create_point_id(conn, lat_r, lon_r, point_id_cache)
            for lat, lon in destinations:
                lat_r, lon_r = _round_coord_pair((lat, lon))
                _get_or_create_point_id(conn, lat_r, lon_r, point_id_cache)
            conn.commit()

        # If resumed rows already cover the requested sample size, emit the sample immediately.
        _maybe_write_early_sample()

        for chunk_ix, (start_idx, origins_chunk) in enumerate(_chunked(origins_todo, chunk_size)):
            if progress:
                progress.set_postfix_str(f"chunk {completed_matrix_chunks + chunk_ix + 1}/{total_matrix_chunks}")
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
                    **transit_kwargs,
                )
                chunk_df = _prepare_fast_chunk_df(pd.DataFrame(ttm), wait_time_min=fast_wait_estimate_min)
            else:
                detailed_ctor = cast(Any, r5py.DetailedItineraries)
                detailed = detailed_ctor(
                    transport_network,
                    origins=origins_gdf,
                    destinations=destinations_gdf,
                    **transit_kwargs,
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
            chunk_df["impedance_min"] = _compute_impedance_min(mode, chunk_df)

            total_rows += len(chunk_df)
            missing_rows += int(chunk_df["travel_time_min"].isna().sum())

            db_rows: list[tuple[Any, ...]] = []
            for row in chunk_df.itertuples(index=False):
                from_lat_r = round(float(row.from_lat), COORD_ROUND)
                from_lon_r = round(float(row.from_lon), COORD_ROUND)
                to_lat_r = round(float(row.to_lat), COORD_ROUND)
                to_lon_r = round(float(row.to_lon), COORD_ROUND)
                from_point_id = _get_or_create_point_id(conn, from_lat_r, from_lon_r, point_id_cache)
                to_point_id = _get_or_create_point_id(conn, to_lat_r, to_lon_r, point_id_cache)
                travel = None if pd.isna(row.travel_time_min) else float(row.travel_time_min)
                wait = None if pd.isna(row.wait_time_min) else float(row.wait_time_min)
                impedance = None if pd.isna(row.impedance_min) else float(row.impedance_min)
                db_rows.append((int(run_id), from_point_id, to_point_id, travel, wait, impedance))
            _insert_rows(conn, db_rows)
            conn.commit()
            _maybe_write_early_sample()

            if progress:
                progress.update(1)

        if mode == MODE_FAST and fast_wait_model == "tripplanner_exact" and run_id is not None:
            _apply_tripplanner_waits_to_run(
                conn=conn,
                run_id=int(run_id),
                transport_network=transport_network,
                r5py_module=r5py,
                departure_dt=departure_dt,
                r5_max_time_walking_min=r5_max_time_walking_min,
                r5_departure_window_min=r5_departure_window_min,
                r5_tripplanner_timeout_s=r5_tripplanner_timeout_s,
                r5_tripplanner_workers=tripplanner_workers,
                enable_progress=enable_progress,
            )
    finally:
        if progress:
            progress.close()
        conn.close()

    os.replace(tmp_db, out_db_path)

    # Build a compact sampled CSV for manual inspection.
    sample_rows_written = 0
    conn_sample = sqlite3.connect(out_db_path)
    try:
        _assert_supported_schema(conn_sample, str(out_db_path))
        maybe_run_id = _resolve_run_id(conn_sample, mode=mode, departure_iso=departure_iso)
        if maybe_run_id is not None:
            sample_rows_written = _write_sample_csv_from_db(
                out_csv_path,
                conn_sample,
                run_id=maybe_run_id,
                mode=mode,
                departure_iso=departure_iso,
                sample_rows=sample_rows,
                missing_share=sample_missing_share,
            )
    finally:
        conn_sample.close()

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
        "sample_rows_written": sample_rows_written,
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
    sample_csv_path: str | None = None,
    sample_rows: int = 10000,
    sample_missing_share: float = 0.3,
    r5_fast_wait_model: str = "tripplanner_exact",
    r5_tripplanner_workers: int | None = None,
    r5_tripplanner_timeout_s: float = 15.0,
    max_retries: int = 3,
    min_workers: int = 1,
    min_chunk_size: int = 16,
    retry_delay_s: float = 2.0,
    attempt_timeout_s: float | None = None,
    r5_max_time_walking_min: int = 30,
    r5_departure_window_min: int = 60,
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
            "sample_csv_path": sample_csv_path,
            "sample_rows": int(sample_rows),
            "sample_missing_share": float(sample_missing_share),
            "r5_fast_wait_model": str(r5_fast_wait_model),
            "r5_tripplanner_workers": None if r5_tripplanner_workers is None else int(r5_tripplanner_workers),
            "r5_tripplanner_timeout_s": float(r5_tripplanner_timeout_s),
            "r5_max_time_walking_min": int(r5_max_time_walking_min),
            "r5_departure_window_min": int(r5_departure_window_min),
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
    _assert_supported_schema(conn, db_path)
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
    run_id = _resolve_run_id(index.conn, mode=mode, departure_iso=departure_iso)
    if run_id is None:
        return None
    from_point_id = _resolve_point_id(index.conn, from_lat_r, from_lon_r)
    to_point_id = _resolve_point_id(index.conn, to_lat_r, to_lon_r)
    if from_point_id is None or to_point_id is None:
        return None
    cur = index.conn.execute(
        """
        SELECT impedance_min
        FROM routes
        WHERE run_id = ?
          AND from_point_id = ?
          AND to_point_id = ?
        LIMIT 1
        """,
        (run_id, from_point_id, to_point_id),
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
    run_id = _resolve_run_id(index.conn, mode=mode, departure_iso=departure_iso)
    if run_id is None:
        return {}
    from_point_id = _resolve_point_id(index.conn, from_lat_r, from_lon_r)
    if from_point_id is None:
        return {}
    cur = index.conn.execute(
        """
        SELECT p.lat_r, p.lon_r, r.impedance_min
        FROM routes AS r
        JOIN points AS p ON p.point_id = r.to_point_id
        WHERE r.run_id = ?
          AND r.from_point_id = ?
          AND r.impedance_min IS NOT NULL
        """,
        (run_id, from_point_id),
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
    run_id = _resolve_run_id(index.conn, mode=mode, departure_iso=departure_iso)
    if run_id is None:
        return {}
    from_point_id = _resolve_point_id(index.conn, from_lat_r, from_lon_r)
    if from_point_id is None:
        return {}

    destination_point_ids: list[int] = []
    for to_lat_r, to_lon_r in rounded_destinations:
        point_id = _resolve_point_id(index.conn, to_lat_r, to_lon_r)
        if point_id is not None:
            destination_point_ids.append(point_id)
    if not destination_point_ids:
        return {}

    out: dict[tuple[float, float], float] = {}
    for i in range(0, len(destination_point_ids), chunk_size):
        chunk = destination_point_ids[i : i + chunk_size]
        if not chunk:
            continue
        placeholders = ",".join(["?"] * len(chunk))
        sql = f"""
        SELECT p.lat_r, p.lon_r, r.impedance_min
        FROM routes AS r
        JOIN points AS p ON p.point_id = r.to_point_id
        WHERE r.run_id = ?
          AND r.from_point_id = ?
          AND r.impedance_min IS NOT NULL
          AND r.to_point_id IN ({placeholders})
        """
        params: list[float | str | int] = [run_id, from_point_id]
        params.extend(chunk)
        cur = index.conn.execute(sql, tuple(params))
        for to_lat_r, to_lon_r, imp in cur.fetchall():
            out[(float(to_lat_r), float(to_lon_r))] = float(imp)
    return out
