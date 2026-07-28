"""R5 routing support for transit matrix building, wait estimation, caching, and lookups."""

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


# Shared routing constants.
COORD_ROUND = 6
MODE_FAST = "fast_routing"
MODE_SLOW = "slow_routing"

JAVA_SAFE_OPTS: tuple[str, ...] = (
    #Using these flags guarantees more JVM stability
    "-XX:+UnlockDiagnosticVMOptions",
    "-XX:TieredStopAtLevel=1",
    "-XX:CICompilerCount=1",
)

JAVA_SLOW_ROUTING_JIT_EXCLUDE_OPTS: tuple[str, ...] = (
    # Work around repeated JVM access violations observed while compiling/executing
    # these methods in DetailedItineraries (slow_routing) on Windows + JDK21.
    "-XX:CompileCommand=exclude,com/conveyal/r5/profile/McRaptorSuboptimalPathProfileRouter.doOneRound",
    "-XX:CompileCommand=exclude,com/conveyal/r5/profile/McRaptorSuboptimalPathProfileRouter.addState",
    "-XX:CompileCommand=exclude,com/conveyal/r5/profile/McRaptorSuboptimalPathProfileRouter.doTransfers",
)
R5_FAST_WAIT_ESTIMATE_ENABLED = True
R5_FAST_WAIT_DEFAULT_MIN = 5.0
R5_FAST_WAIT_WINDOW_MIN = 90

# For waiting the  
R5_FAST_WAIT_STRATEGY = "worst"
R5_WALK_SPEED_M_PER_MIN = 80.0
R5_ORIGIN_WAIT_BUCKET_DEG = 0.01
R5_ORIGIN_WAIT_NEAREST_STOPS = 3
ROUTING_SCHEMA_VERSION = "3"


class JavaMajorVersionNotFoundError(RuntimeError):
    """Raised when a Java runtime with the required major version cannot be found."""
    pass


@dataclass
class RoutingIndex:
    """Thin wrapper around the SQLite connection used by downstream lookup helpers."""
    conn: sqlite3.Connection
    db_path: str

    def close(self) -> None:
        self.conn.close()


@dataclass(frozen=True)
class StopWaitRow:
    """Typed representation of one GTFS stop-level wait estimate."""
    stop_id: str
    stop_lat: float
    stop_lon: float
    wait_time_min: float


@dataclass(frozen=True)
class RouteRow:
    """One normalized route row ready to be written into the SQLite cache."""
    run_id: int
    from_point_id: int
    to_point_id: int
    travel_time_min: float | None
    wait_time_min: float | None
    impedance_min: float | None

    def as_db_tuple(self) -> tuple[Any, ...]:
        return (
            self.run_id,
            self.from_point_id,
            self.to_point_id,
            self.travel_time_min,
            self.wait_time_min,
            self.impedance_min,
        )


@dataclass(frozen=True)
class TripPlannerUpdateRow:
    """One exact-wait update produced by the TripPlanner phase-2 pass."""
    from_point_id: int
    to_point_id: int
    travel_time_min: float
    wait_time_min: float | None

    @property
    def impedance_min(self) -> float | None:
        if self.wait_time_min is None:
            return None
        return float(self.travel_time_min) + float(self.wait_time_min)

    def as_db_tuple(self, run_id: int) -> tuple[Any, ...]:
        return (
            self.wait_time_min,
            self.impedance_min,
            int(run_id),
            self.from_point_id,
            self.to_point_id,
        )


@dataclass(frozen=True)
class WaitModelContext:
    """Precomputed configuration and state for the selected fast wait model."""
    mode: str
    fast_wait_model: str
    fallback_wait_time_min: float | None
    origin_wait_estimates: dict[tuple[float, float], float] | None
    tripplanner_workers: int
    tripplanner_timeout_s: float
    max_time_walking_min: int
    departure_window_min: int


@dataclass(frozen=True)
class RoutingRunContext:
    """Immutable state shared across chunk execution for one routing build."""
    mode: str
    departure_dt: dt.datetime
    departure_iso: str
    origins_sig: str
    destinations_sig: str
    transport_network: Any
    r5py_module: Any
    transit_kwargs: dict[str, Any]
    destinations_gdf: gpd.GeoDataFrame
    destinations_map: pd.DataFrame
    wait_model: WaitModelContext


@dataclass
class ResumeState:
    """Snapshot of what can be reused from an existing temporary routing cache."""
    has_existing_state: bool
    run_id: int | None
    processed_origin_keys: set[tuple[float, float]]
    total_rows: int
    missing_rows: int


# Java and JVM runtime setup.
def _round_coord_pair(coord: tuple[float, float]) -> tuple[float, float]:
    """Normalize coordinates to the precision used in cache keys and DB lookups."""
    return (round(float(coord[0]), COORD_ROUND), round(float(coord[1]), COORD_ROUND))


def _configure_java_home_from_path() -> None:
    """Populate JAVA_HOME from the Java executable already visible on PATH."""
    java_exe = shutil.which("java")
    if java_exe is None:
        return
    java_path = Path(java_exe).resolve()
    if java_path.parent.name.lower() != "bin":
        return
    detected_java_home = java_path.parent.parent
    os.environ["JAVA_HOME"] = str(detected_java_home)


def _java_major_from_exe(java_exe: str) -> int | None:
    """Read the major version reported by a Java executable."""
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
    """Collect distinct Java executables that may satisfy the required runtime."""
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
    """Pin the process environment to a Java runtime with the requested major version."""
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
    """Inject the r5py classpath argument when the caller did not provide one."""
    if "--r5-classpath" in sys.argv or "-r" in sys.argv:
        return
    sys.argv.extend(["--r5-classpath", jar_path])


def _ensure_r5_max_memory_arg(mode: str) -> None:
    """Set a conservative JVM heap limit for R5 when one was not provided explicitly."""
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
    """Apply JVM flags used to reduce native/JIT instability during large routing runs."""
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
    """Fail fast when routing inputs or the Java runtime are unavailable."""
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
    """Build the GeoDataFrame shape expected by r5py for origin or destination points."""
    ids = [f"{prefix}{start_idx + i}" for i in range(len(coords))]
    points = [Point(lon, lat) for lat, lon in coords]
    return gpd.GeoDataFrame({"id": ids, "geometry": points}, crs="EPSG:4326")


def _build_point_lookup_df(
    points_gdf: gpd.GeoDataFrame,
    id_col: str,
    lat_col: str,
    lon_col: str,
) -> pd.DataFrame:
    """Expose point ids and coordinates in a merge-friendly tabular form."""
    lookup_df = points_gdf.copy()
    lookup_df[id_col] = lookup_df["id"]
    lookup_df[lat_col] = lookup_df.geometry.y
    lookup_df[lon_col] = lookup_df.geometry.x
    return lookup_df[[id_col, lat_col, lon_col]]


def _build_destination_lookup_df(destinations: list[tuple[float, float]]) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Prepare both the r5py destination input and the coordinate lookup table."""
    destinations_gdf = _build_points_gdf("d", destinations, start_idx=0)
    destinations_map = _build_point_lookup_df(destinations_gdf, "to_id", "to_lat", "to_lon")
    return destinations_gdf, destinations_map


def _build_origin_lookup_df(
    origin_coords_chunk: list[tuple[float, float]],
    start_idx: int,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Prepare one origin chunk for r5py and for coordinate joins back onto results."""
    origins_gdf = _build_points_gdf("o", origin_coords_chunk, start_idx=start_idx)
    origins_map = _build_point_lookup_df(origins_gdf, "from_id", "from_lat", "from_lon")
    return origins_gdf, origins_map


def _build_full_pairs_df(origins_gdf: gpd.GeoDataFrame, destinations_gdf: gpd.GeoDataFrame) -> pd.DataFrame:
    """Enumerate all OD pairs so slow routing can join itineraries onto the full matrix."""
    return pd.MultiIndex.from_product(
        [origins_gdf["id"].tolist(), destinations_gdf["id"].tolist()],
        names=["from_id", "to_id"],
    ).to_frame(index=False)


def _merge_chunk_coordinates(
    matrix_chunk_df: pd.DataFrame,
    origins_map: pd.DataFrame,
    destinations_map: pd.DataFrame,
) -> pd.DataFrame:
    """Attach origin and destination coordinates to a matrix chunk for persistence and inspection."""
    matrix_chunk_df = matrix_chunk_df.merge(origins_map, on="from_id", how="left")
    matrix_chunk_df = matrix_chunk_df.merge(destinations_map, on="to_id", how="left")
    return matrix_chunk_df


def _aggregate_slow_itineraries(detailed: pd.DataFrame) -> pd.DataFrame:
    """Collapse slow detailed itineraries to one best travel/wait pair per OD."""
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


# SQLite cache primitives and sampled CSV export.
def _create_db_schema(conn: sqlite3.Connection) -> None:
    """Create the normalized routing cache schema used by all later lookups."""
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
    """Insert or replace a batch of normalized route rows."""
    conn.executemany(
        """
        INSERT OR REPLACE INTO routes (
            run_id, from_point_id, to_point_id, travel_time_min, wait_time_min, impedance_min
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _coords_signature(coords: list[tuple[float, float]]) -> str:
    """Hash an ordered coordinate list so runs can be matched to exact routing inputs."""
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
    """Hard-break on legacy routing caches so stale schemas are never reused silently."""
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
    """Resolve the logical routing run row, creating it if needed."""
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
    """Write the inspection-only CSV header used for sampled route exports."""
    writer = csv.writer(csv_f)
    writer.writerow(
        [
            "from_lat",
            "from_lon",
            "to_lat",
            "to_lon",
            "google_maps_transit_url",
            "travel_time",
            "wait_time",
            "impedance",
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
    """Generate a Google Maps transit link for manual spot-checking of sampled rows."""
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
    """Export an inspection CSV from the persisted cache.

    The pipeline never reads this file back. It exists so a human can open a
    manageable sample, inspect successful and missing routes side by side, and
    compare them against external tools such as Google Maps.
    """
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


# GTFS-derived wait estimation helpers.
def _iter_stop_wait_rows(stops_df: pd.DataFrame) -> list[StopWaitRow]:
    """Convert the GTFS stop-wait DataFrame into typed rows with explicit field names."""
    stop_wait_rows: list[StopWaitRow] = []
    for stop_id, stop_lat, stop_lon, stop_wait in stops_df.itertuples(index=False, name=None):
        stop_wait_rows.append(
            StopWaitRow(
                stop_id=str(cast(Any, stop_id)),
                stop_lat=float(cast(Any, stop_lat)),
                stop_lon=float(cast(Any, stop_lon)),
                wait_time_min=float(cast(Any, stop_wait)),
            )
        )
    return stop_wait_rows


def _collect_processed_origin_keys(conn: sqlite3.Connection, run_id: int) -> set[tuple[float, float]]:
    """Return origins already persisted for a partially completed routing run."""
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
    """Count total and missing rows for one logical routing run."""
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
    """Parse GTFS HH:MM:SS strings, including hours beyond 24, into seconds."""
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
    """Resolve which GTFS service_ids are active on the requested calendar date."""
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


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters used for light-weight stop proximity checks."""
    radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * radius_m * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _load_gtfs_stop_waits_df(gtfs_path: str, departure_dt: dt.datetime) -> pd.DataFrame:
    """Build the stop-level input used by the fast wait estimators.

    This function reads GTFS, keeps only services active on the requested day,
    looks at departures inside the configured time window, and turns observed
    headways into one wait estimate per stop. Later stages either collapse this
    to a single network-wide fallback or map nearby stop waits back to origins.
    """
    default_wait_min = float(R5_FAST_WAIT_DEFAULT_MIN)
    window_min = max(15, int(R5_FAST_WAIT_WINDOW_MIN))
    strategy = str(R5_FAST_WAIT_STRATEGY).strip().lower()

    try:
        with zipfile.ZipFile(gtfs_path) as gtfs_zip:
            required = {"stop_times.txt", "stops.txt"}
            if not required.issubset(set(gtfs_zip.namelist())):
                return pd.DataFrame(columns=["stop_id", "stop_lat", "stop_lon", "wait_time_min"])

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
                return pd.DataFrame(columns=["stop_id", "stop_lat", "stop_lon", "wait_time_min"])

            stop_times_df["dep_sec"] = stop_times_df["departure_time"].map(_parse_gtfs_hms_to_seconds)
            stop_times_df = stop_times_df.dropna(subset=["dep_sec"]).copy()
            if stop_times_df.empty:
                return pd.DataFrame(columns=["stop_id", "stop_lat", "stop_lon", "wait_time_min"])
            stop_times_df["dep_sec"] = stop_times_df["dep_sec"].astype(int)

            dep_sec = departure_dt.hour * 3600 + departure_dt.minute * 60 + departure_dt.second
            upper_sec = dep_sec + window_min * 60
            window_df = stop_times_df[(stop_times_df["dep_sec"] >= dep_sec) & (stop_times_df["dep_sec"] <= upper_sec)]
            if window_df.empty:
                return pd.DataFrame(columns=["stop_id", "stop_lat", "stop_lon", "wait_time_min"])

            window_df = window_df.sort_values(["stop_id", "dep_sec"])
            window_df["headway_min"] = window_df.groupby("stop_id")["dep_sec"].diff() / 60.0
            valid = window_df[(window_df["headway_min"] > 0.0) & (window_df["headway_min"] <= 180.0)]
            if valid.empty:
                return pd.DataFrame(columns=["stop_id", "stop_lat", "stop_lon", "wait_time_min"])

            per_stop = valid.groupby("stop_id", as_index=False)["headway_min"].median()
            if per_stop.empty:
                return pd.DataFrame(columns=["stop_id", "stop_lat", "stop_lon", "wait_time_min"])

            if strategy in {"worst", "max", "full_headway", "full-headway"}:
                per_stop["wait_time_min"] = per_stop["headway_min"].astype(float)
            else:
                per_stop["wait_time_min"] = per_stop["headway_min"].astype(float) / 2.0

            with gtfs_zip.open("stops.txt") as f:
                stops_df = pd.read_csv(
                    f,
                    usecols=["stop_id", "stop_lat", "stop_lon"],
                    dtype={"stop_id": str, "stop_lat": float, "stop_lon": float},
                )
    except Exception:
        return pd.DataFrame(columns=["stop_id", "stop_lat", "stop_lon", "wait_time_min"])

    merged = per_stop.merge(stops_df, on="stop_id", how="inner")
    if merged.empty:
        return pd.DataFrame(columns=["stop_id", "stop_lat", "stop_lon", "wait_time_min"])
    merged = merged.dropna(subset=["stop_lat", "stop_lon", "wait_time_min"]).copy()
    merged["wait_time_min"] = merged["wait_time_min"].astype(float).clip(lower=0.0)
    if merged.empty:
        return pd.DataFrame(columns=["stop_id", "stop_lat", "stop_lon", "wait_time_min"])
    merged["fallback_wait_min"] = default_wait_min
    return merged[["stop_id", "stop_lat", "stop_lon", "wait_time_min"]]


def _estimate_fast_wait_time_min(gtfs_path: str, departure_dt: dt.datetime) -> float:
    """Collapse stop-level GTFS waits into one network-wide fallback value.

    This is the cheapest approximation in the pipeline. It ignores where the
    trip starts and provides a single default wait that can be reused when the
    caller chooses `global_estimate` or when `origin_estimate` cannot find any
    reachable stops for a specific origin.
    """
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


def _build_origin_wait_estimates(
    origins: list[tuple[float, float]],
    gtfs_path: str,
    departure_dt: dt.datetime,
    r5_max_time_walking_min: int,
    fallback_wait_min: float,
) -> dict[tuple[float, float], float]:
    """Map each origin to a boarding wait before any destination routing is considered.

    In `origin_estimate` mode we assume the first wait depends mainly on where
    the traveler starts. This function therefore looks for stops reachable from
    each origin within the walking budget, takes the nearest useful stops, and
    stores one reusable wait per origin for the later chunk-processing stage.
    """
    origin_keys = [_round_coord_pair(origin) for origin in origins]
    if not origin_keys:
        return {}

    stops_df = _load_gtfs_stop_waits_df(gtfs_path, departure_dt)
    if stops_df.empty:
        return {key: float(fallback_wait_min) for key in origin_keys}

    walk_radius_m = max(1.0, float(r5_max_time_walking_min) * R5_WALK_SPEED_M_PER_MIN)
    bucket_deg = float(R5_ORIGIN_WAIT_BUCKET_DEG)
    stops_by_bucket: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    for stop_wait_row in _iter_stop_wait_rows(stops_df):
        bucket = (
            math.floor(stop_wait_row.stop_lat / bucket_deg),
            math.floor(stop_wait_row.stop_lon / bucket_deg),
        )
        stops_by_bucket.setdefault(bucket, []).append(
            (stop_wait_row.stop_lat, stop_wait_row.stop_lon, stop_wait_row.wait_time_min)
        )

    estimates: dict[tuple[float, float], float] = {}
    lat_deg_radius = walk_radius_m / 111320.0
    for origin in origin_keys:
        lat, lon = origin
        lon_scale = max(0.1, math.cos(math.radians(lat)))
        lon_deg_radius = walk_radius_m / (111320.0 * lon_scale)
        lat_min_bucket = math.floor((lat - lat_deg_radius) / bucket_deg)
        lat_max_bucket = math.floor((lat + lat_deg_radius) / bucket_deg)
        lon_min_bucket = math.floor((lon - lon_deg_radius) / bucket_deg)
        lon_max_bucket = math.floor((lon + lon_deg_radius) / bucket_deg)

        nearby: list[tuple[float, float]] = []
        for lat_bucket in range(lat_min_bucket, lat_max_bucket + 1):
            for lon_bucket in range(lon_min_bucket, lon_max_bucket + 1):
                for stop_lat, stop_lon, wait_min in stops_by_bucket.get((lat_bucket, lon_bucket), []):
                    dist_m = _haversine_m(lat, lon, stop_lat, stop_lon)
                    if dist_m <= walk_radius_m:
                        nearby.append((dist_m, wait_min))

        if not nearby:
            estimates[origin] = float(fallback_wait_min)
            continue

        nearby.sort(key=lambda item: item[0])
        selected = nearby[: max(1, int(R5_ORIGIN_WAIT_NEAREST_STOPS))]
        weighted_sum = 0.0
        total_weight = 0.0
        for dist_m, wait_min in selected:
            weight = 1.0 / max(25.0, float(dist_m))
            weighted_sum += weight * float(wait_min)
            total_weight += weight
        estimates[origin] = (
            float(weighted_sum / total_weight) if total_weight > 0.0 else float(fallback_wait_min)
        )
    return estimates


def _apply_origin_estimate_waits(
    chunk_df: pd.DataFrame,
    origin_wait_estimates: dict[tuple[float, float], float],
    fallback_wait_min: float,
) -> pd.DataFrame:
    """Project precomputed origin waits onto the feasible rows of one fast-routing chunk.

    At this point `travel_time_min` is already known from the fast matrix. The
    only missing piece is the boarding wait, which is reused for every
    destination that shares the same origin.
    """
    chunk_df = chunk_df.copy()
    chunk_df["wait_time_min"] = float("nan")
    feasible_mask = chunk_df["travel_time_min"].notna()
    if not feasible_mask.any():
        return chunk_df

    feasible = chunk_df.loc[feasible_mask, ["from_lat", "from_lon"]].copy()
    feasible_keys = [
        _round_coord_pair((float(lat), float(lon)))
        for lat, lon in feasible.itertuples(index=False, name=None)
    ]
    wait_values = [
        float(origin_wait_estimates.get(key, fallback_wait_min))
        for key in feasible_keys
    ]
    chunk_df.loc[feasible_mask, "wait_time_min"] = wait_values
    return chunk_df


def _prepare_fast_chunk_df(ttm_df: pd.DataFrame, wait_time_min: float | None) -> pd.DataFrame:
    """Normalize r5py fast-matrix output into the shared chunk schema."""
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
    """Compute the generalized transit cost stored by downstream accessibility stages."""
    return chunk_df["travel_time_min"] + chunk_df["wait_time_min"]


def _build_transit_routing_kwargs(
    r5py_module: Any,
    departure_dt: dt.datetime,
    r5_max_time_walking_min: int,
    r5_departure_window_min: int,
) -> dict[str, Any]:
    """Build the transit-routing argument set shared by fast and slow r5py calls."""
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
    """Normalize user-facing aliases for the configured fast wait model."""
    token = (value or "").strip().lower()
    if token in {"tripplanner_exact", "tripplanner", "exact"}:
        return "tripplanner_exact"
    if token in {"origin_estimate", "origin", "per_origin"}:
        return "origin_estimate"
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
    """Run one exact TripPlanner query and keep the wait from the best itinerary."""
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


# Exact TripPlanner wait enrichment is kept as a separate pass because the fast
# matrix can enumerate feasible ODs cheaply, while per-OD itinerary expansion is expensive.
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
    """Populate exact waits for one matrix chunk by querying TripPlanner for each feasible OD."""
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
    """Apply the expensive exact wait phase directly against persisted rows in batches."""
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
            updates = [
                update_row.as_db_tuple(run_id)
                for update_row in _iter_tripplanner_update_rows(batch_df)
            ]
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


# Routing execution helpers.
def _prepare_r5_runtime(mode: str, pbf_path: str, gtfs_path: str, jar_path: str, workers: int) -> tuple[Any, Any, int]:
    """Prepare JVM state, import r5py, and build the transport network for this run."""
    _force_java_major(21)
    _preflight(pbf_path, gtfs_path, jar_path)
    _configure_java_home_from_path()
    _configure_java_runtime_flags(mode)
    _ensure_r5_classpath_arg(jar_path)
    _ensure_r5_max_memory_arg(mode)

    import r5py
    from r5py.r5.base_travel_time_matrix import BaseTravelTimeMatrix

    effective_workers = max(1, int(workers))
    if mode == MODE_SLOW:
        effective_workers = 1
    BaseTravelTimeMatrix.NUM_THREADS = effective_workers
    transport_network = r5py.TransportNetwork(pbf_path, [gtfs_path])
    return r5py, transport_network, effective_workers


def _default_wait_time_min(fast_wait_estimate_min: float | None) -> float:
    """Resolve the fallback wait used when a more specific estimate is unavailable."""
    if fast_wait_estimate_min is not None:
        return float(fast_wait_estimate_min)
    return float(R5_FAST_WAIT_DEFAULT_MIN)


def _resolve_tripplanner_workers(workers: int, configured_workers: int | None) -> int:
    if configured_workers is None:
        return max(1, min(4, workers))
    return max(1, int(configured_workers))


def _prepare_wait_model_context(
    mode: str,
    origins: list[tuple[float, float]],
    gtfs_path: str,
    departure_dt: dt.datetime,
    workers: int,
    r5_fast_wait_model: str,
    r5_tripplanner_workers: int | None,
    r5_tripplanner_timeout_s: float,
    r5_max_time_walking_min: int,
    r5_departure_window_min: int,
) -> WaitModelContext:
    """Prepare the wait-model policy before any OD chunks are executed.

    The routing stage should not decide wait behavior repeatedly inside the main
    chunk loop. This helper computes that policy once: exact TripPlanner waits,
    one network-wide fallback wait, or one wait estimate per origin.
    """
    fast_wait_model = _normalize_fast_wait_model(r5_fast_wait_model)
    fast_wait_estimate_min = (
        _estimate_fast_wait_time_min(gtfs_path, departure_dt)
        if mode == MODE_FAST and fast_wait_model in {"global_estimate", "origin_estimate"}
        else None
    )
    fallback_wait_time_min = _default_wait_time_min(fast_wait_estimate_min)
    origin_wait_estimates = (
        _build_origin_wait_estimates(
            origins=origins,
            gtfs_path=gtfs_path,
            departure_dt=departure_dt,
            r5_max_time_walking_min=r5_max_time_walking_min,
            fallback_wait_min=fallback_wait_time_min,
        )
        if mode == MODE_FAST and fast_wait_model == "origin_estimate"
        else None
    )
    return WaitModelContext(
        mode=mode,
        fast_wait_model=fast_wait_model,
        fallback_wait_time_min=fast_wait_estimate_min,
        origin_wait_estimates=origin_wait_estimates,
        tripplanner_workers=_resolve_tripplanner_workers(workers, r5_tripplanner_workers),
        tripplanner_timeout_s=float(r5_tripplanner_timeout_s),
        max_time_walking_min=int(r5_max_time_walking_min),
        departure_window_min=int(r5_departure_window_min),
    )


def _build_routing_run_context(
    origins: list[tuple[float, float]],
    destinations: list[tuple[float, float]],
    mode: str,
    gtfs_path: str,
    departure_dt: dt.datetime,
    workers: int,
    r5_fast_wait_model: str,
    r5_tripplanner_workers: int | None,
    r5_tripplanner_timeout_s: float,
    r5_max_time_walking_min: int,
    r5_departure_window_min: int,
    r5py_module: Any,
    transport_network: Any,
) -> RoutingRunContext:
    """Assemble the immutable context that every chunk in this routing build shares.

    A chunk only varies by its origin slice. Everything else is fixed for the
    whole run: departure time, destination set, wait-model policy, r5py
    network, and the signatures used to identify or resume the cached build.
    """
    departure_iso = departure_dt.isoformat()
    wait_model = _prepare_wait_model_context(
        mode=mode,
        origins=origins,
        gtfs_path=gtfs_path,
        departure_dt=departure_dt,
        workers=workers,
        r5_fast_wait_model=r5_fast_wait_model,
        r5_tripplanner_workers=r5_tripplanner_workers,
        r5_tripplanner_timeout_s=r5_tripplanner_timeout_s,
        r5_max_time_walking_min=r5_max_time_walking_min,
        r5_departure_window_min=r5_departure_window_min,
    )
    destinations_gdf, destinations_map = _build_destination_lookup_df(destinations)
    return RoutingRunContext(
        mode=mode,
        departure_dt=departure_dt,
        departure_iso=departure_iso,
        origins_sig=_coords_signature(origins),
        destinations_sig=_coords_signature(destinations),
        transport_network=transport_network,
        r5py_module=r5py_module,
        transit_kwargs=_build_transit_routing_kwargs(
            r5py_module=r5py_module,
            departure_dt=departure_dt,
            r5_max_time_walking_min=r5_max_time_walking_min,
            r5_departure_window_min=r5_departure_window_min,
        ),
        destinations_gdf=destinations_gdf,
        destinations_map=destinations_map,
        wait_model=wait_model,
    )


def _build_matrix_chunk_df(
    run_context: RoutingRunContext,
    origins_gdf: gpd.GeoDataFrame,
) -> pd.DataFrame:
    """Compute raw routing results for one chunk before waits and persistence.

    In fast mode this calls `TravelTimeMatrix`, which gives one travel-time
    matrix quickly but does not expand exact itineraries for every OD. In slow
    mode it calls `DetailedItineraries`, aggregates the alternatives, and then
    joins them back to the full OD grid so missing OD pairs stay explicit.
    """
    if run_context.mode == MODE_FAST:
        travel_time_matrix_ctor = cast(Any, run_context.r5py_module.TravelTimeMatrix)
        travel_time_matrix = travel_time_matrix_ctor(
            run_context.transport_network,
            origins=origins_gdf,
            destinations=run_context.destinations_gdf,
            **run_context.transit_kwargs,
        )
        global_wait = (
            run_context.wait_model.fallback_wait_time_min
            if run_context.wait_model.fast_wait_model == "global_estimate"
            else None
        )
        return _prepare_fast_chunk_df(pd.DataFrame(travel_time_matrix), wait_time_min=global_wait)

    detailed_itineraries_ctor = cast(Any, run_context.r5py_module.DetailedItineraries)
    detailed_itineraries = detailed_itineraries_ctor(
        run_context.transport_network,
        origins=origins_gdf,
        destinations=run_context.destinations_gdf,
        **run_context.transit_kwargs,
        force_all_to_all=True,
    )
    slow_itineraries = _aggregate_slow_itineraries(pd.DataFrame(detailed_itineraries))
    full_pairs_df = _build_full_pairs_df(origins_gdf, run_context.destinations_gdf)
    return full_pairs_df.merge(slow_itineraries, on=["from_id", "to_id"], how="left")


def _apply_wait_model(
    matrix_chunk_df: pd.DataFrame,
    run_context: RoutingRunContext,
) -> pd.DataFrame:
    """Fill the wait column according to the wait policy chosen for this run.

    By the time this function runs, the chunk already has travel times. The
    only open question is how to obtain waiting time: exact per-OD expansion,
    one per-origin estimate, or a single network-wide fallback.
    """
    wait_model = run_context.wait_model
    if run_context.mode != MODE_FAST:
        return matrix_chunk_df
    if wait_model.fast_wait_model == "tripplanner_exact":
        return _apply_tripplanner_exact_waits(
            matrix_chunk_df,
            transport_network=run_context.transport_network,
            r5py_module=run_context.r5py_module,
            departure_dt=run_context.departure_dt,
            r5_max_time_walking_min=wait_model.max_time_walking_min,
            r5_departure_window_min=wait_model.departure_window_min,
            r5_tripplanner_timeout_s=wait_model.tripplanner_timeout_s,
            r5_tripplanner_workers=wait_model.tripplanner_workers,
        )
    if wait_model.fast_wait_model == "origin_estimate":
        return _apply_origin_estimate_waits(
            matrix_chunk_df,
            origin_wait_estimates=wait_model.origin_wait_estimates or {},
            fallback_wait_min=_default_wait_time_min(wait_model.fallback_wait_time_min),
        )
    return matrix_chunk_df


def _finalize_chunk_df(
    matrix_chunk_df: pd.DataFrame,
    origins_map: pd.DataFrame,
    run_context: RoutingRunContext,
    include_labels: bool,
) -> pd.DataFrame:
    """Turn a raw chunk into the persisted/output-ready chunk shape.

    This is the point where the routing results become self-contained: origin
    and destination coordinates are restored, waits are filled, and the final
    impedance used by downstream accessibility stages is computed.
    """
    matrix_chunk_df = _merge_chunk_coordinates(matrix_chunk_df, origins_map, run_context.destinations_map)
    if include_labels:
        matrix_chunk_df["mode"] = run_context.mode
        matrix_chunk_df["departure_iso"] = run_context.departure_iso
    matrix_chunk_df = _apply_wait_model(matrix_chunk_df, run_context)
    matrix_chunk_df["impedance_min"] = _compute_impedance_min(run_context.mode, matrix_chunk_df)
    return matrix_chunk_df


def _summarize_chunk_counts(matrix_chunk_df: pd.DataFrame) -> tuple[int, int]:
    return len(matrix_chunk_df), int(matrix_chunk_df["travel_time_min"].isna().sum())


def _iter_route_rows(
    matrix_chunk_df: pd.DataFrame,
    run_id: int,
    conn: sqlite3.Connection,
    point_id_cache: dict[tuple[float, float], int],
) -> list[RouteRow]:
    """Translate one finalized chunk into normalized cache rows.

    The SQLite schema stores rounded point ids rather than repeated coordinates,
    so this conversion is the bridge between the DataFrame world used during
    chunk processing and the normalized persistence model used for later lookups.
    """
    route_rows: list[RouteRow] = []
    for row in matrix_chunk_df.itertuples(index=False, name=None):
        from_lat = cast(Any, row[4])
        from_lon = cast(Any, row[5])
        to_lat = cast(Any, row[6])
        to_lon = cast(Any, row[7])
        travel_value = cast(Any, row[2])
        wait_value = cast(Any, row[3])
        impedance_value = cast(Any, row[8])
        from_lat_r = round(float(from_lat), COORD_ROUND)
        from_lon_r = round(float(from_lon), COORD_ROUND)
        to_lat_r = round(float(to_lat), COORD_ROUND)
        to_lon_r = round(float(to_lon), COORD_ROUND)
        from_point_id = _get_or_create_point_id(conn, from_lat_r, from_lon_r, point_id_cache)
        to_point_id = _get_or_create_point_id(conn, to_lat_r, to_lon_r, point_id_cache)
        route_rows.append(
            RouteRow(
                run_id=int(run_id),
                from_point_id=from_point_id,
                to_point_id=to_point_id,
                travel_time_min=None if pd.isna(travel_value) else float(travel_value),
                wait_time_min=None if pd.isna(wait_value) else float(wait_value),
                impedance_min=None if pd.isna(impedance_value) else float(impedance_value),
            )
        )
    return route_rows


def _iter_tripplanner_update_rows(batch_df: pd.DataFrame) -> list[TripPlannerUpdateRow]:
    """Convert one exact-wait batch into typed update payloads for SQLite."""
    update_rows: list[TripPlannerUpdateRow] = []
    for from_point_id, to_point_id, _, _, _, _, travel_time_min, wait_time_min in batch_df.itertuples(
        index=False, name=None
    ):
        wait_value = cast(Any, wait_time_min)
        update_rows.append(
            TripPlannerUpdateRow(
                from_point_id=int(from_point_id),
                to_point_id=int(to_point_id),
                travel_time_min=float(cast(Any, travel_time_min)),
                wait_time_min=None if pd.isna(wait_value) else float(wait_value),
            )
        )
    return update_rows


def _build_summary(
    run_context: RoutingRunContext,
    rows: int,
    missing_rows: int,
    resumed: bool,
    processed_origins: int,
    out_csv: str | None,
    out_db: str | None,
    persist_outputs: bool,
    sample_rows_written: int = 0,
) -> dict[str, Any]:
    """Build the stable summary payload returned to callers after routing completes."""
    return {
        "mode": run_context.mode,
        "departure_iso": run_context.departure_iso,
        "rows": rows,
        "missing_rows": missing_rows,
        "resumed": resumed,
        "processed_origins": processed_origins,
        "out_csv": out_csv,
        "out_db": out_db,
        "persist_outputs": persist_outputs,
        "fast_wait_estimate_min": run_context.wait_model.fallback_wait_time_min,
        "sample_rows_written": sample_rows_written,
    }


def _seed_tmp_db_from_completed_output(tmp_db_path: Path, out_db_path: Path) -> None:
    """Clone the finished DB into the temp path so exact-wait phase-2 can resume safely."""
    if (not tmp_db_path.exists()) and out_db_path.exists():
        shutil.copy2(out_db_path, tmp_db_path)


def _load_resume_state(
    tmp_db_path: Path,
    run_context: RoutingRunContext,
) -> ResumeState:
    """Decide whether the temporary cache belongs to the current routing run.

    Resume is only safe when the schema and the run identity match exactly. If
    any of those inputs differ, the temp DB is discarded so the build restarts
    from a clean state instead of mixing incompatible routing results.
    """
    if not tmp_db_path.exists():
        return ResumeState(False, None, set(), 0, 0)

    clear_tmp = False
    run_id: int | None = None
    processed_origin_keys: set[tuple[float, float]] = set()
    total_rows = 0
    missing_rows = 0

    conn_probe = sqlite3.connect(tmp_db_path)
    try:
        if _table_exists(conn_probe, "routes"):
            _assert_supported_schema(conn_probe, str(tmp_db_path))
            existing_meta = _get_run_meta(conn_probe)
            # Resume only when the temp cache matches the current routing inputs exactly.
            expected_meta = {
                "schema_version": ROUTING_SCHEMA_VERSION,
                "mode": run_context.mode,
                "departure_iso": run_context.departure_iso,
                "origins_sig": run_context.origins_sig,
                "destinations_sig": run_context.destinations_sig,
            }
            can_resume = all(existing_meta.get(key) == value for key, value in expected_meta.items())
            if can_resume:
                run_id = _get_or_create_run_id(
                    conn_probe,
                    mode=run_context.mode,
                    departure_iso=run_context.departure_iso,
                    origins_sig=run_context.origins_sig,
                    destinations_sig=run_context.destinations_sig,
                    wait_time_estimated_min=run_context.wait_model.fallback_wait_time_min,
                )
                processed_origin_keys = _collect_processed_origin_keys(conn_probe, run_id)
                total_rows, missing_rows = _routing_counts(conn_probe, run_id)
            else:
                clear_tmp = True
        else:
            clear_tmp = True
    finally:
        conn_probe.close()

    if clear_tmp:
        tmp_db_path.unlink(missing_ok=True)
        return ResumeState(False, None, set(), 0, 0)

    return ResumeState(True, run_id, processed_origin_keys, total_rows, missing_rows)


def _init_or_validate_routing_db(
    conn: sqlite3.Connection,
    db_path: Path,
    run_context: RoutingRunContext,
    resume_state: ResumeState,
) -> int:
    """Ensure the temp DB is ready and return the logical run_id for this build."""
    if not resume_state.has_existing_state:
        _create_db_schema(conn)
        _set_run_meta(
            conn,
            {
                "schema_version": ROUTING_SCHEMA_VERSION,
                "mode": run_context.mode,
                "departure_iso": run_context.departure_iso,
                "origins_sig": run_context.origins_sig,
                "destinations_sig": run_context.destinations_sig,
            },
        )
        conn.commit()
    else:
        _assert_supported_schema(conn, str(db_path))

    if resume_state.run_id is not None:
        return int(resume_state.run_id)
    return _get_or_create_run_id(
        conn,
        mode=run_context.mode,
        departure_iso=run_context.departure_iso,
        origins_sig=run_context.origins_sig,
        destinations_sig=run_context.destinations_sig,
        wait_time_estimated_min=run_context.wait_model.fallback_wait_time_min,
    )


def _compute_origins_todo(
    origins: list[tuple[float, float]],
    processed_origin_keys: set[tuple[float, float]],
) -> list[tuple[float, float]]:
    return [
        origin_coord
        for origin_coord in origins
        if _round_coord_pair(origin_coord) not in processed_origin_keys
    ]


def _register_points_for_run(
    conn: sqlite3.Connection,
    origin_coords: list[tuple[float, float]],
    destination_coords: list[tuple[float, float]],
    point_id_cache: dict[tuple[float, float], int],
) -> None:
    """Pre-register rounded origin and destination points to avoid repeated point lookups."""
    for lat, lon in origin_coords:
        lat_r, lon_r = _round_coord_pair((lat, lon))
        _get_or_create_point_id(conn, lat_r, lon_r, point_id_cache)
    for lat, lon in destination_coords:
        lat_r, lon_r = _round_coord_pair((lat, lon))
        _get_or_create_point_id(conn, lat_r, lon_r, point_id_cache)
    conn.commit()


def _write_sample_csv_if_ready(
    out_csv_path: Path,
    conn: sqlite3.Connection,
    run_id: int,
    run_context: RoutingRunContext,
    sample_rows: int,
    sample_missing_share: float,
    total_rows: int,
    already_written: bool,
) -> bool:
    if already_written or int(sample_rows) <= 0 or total_rows < int(sample_rows):
        return already_written
    # The sample CSV is for manual inspection only; the SQLite DB remains the source of truth.
    _write_sample_csv_from_db(
        out_csv_path,
        conn,
        run_id=run_id,
        mode=run_context.mode,
        departure_iso=run_context.departure_iso,
        sample_rows=sample_rows,
        missing_share=sample_missing_share,
    )
    return True


def _write_final_sample_csv(
    out_csv_path: Path,
    out_db_path: Path,
    run_context: RoutingRunContext,
    sample_rows: int,
    sample_missing_share: float,
) -> int:
    """Regenerate the inspection sample from the finished output DB."""
    sample_rows_written = 0
    conn_sample = sqlite3.connect(out_db_path)
    try:
        _assert_supported_schema(conn_sample, str(out_db_path))
        routing_run_id = _resolve_run_id(conn_sample, mode=run_context.mode, departure_iso=run_context.departure_iso)
        if routing_run_id is not None:
            sample_rows_written = _write_sample_csv_from_db(
                out_csv_path,
                conn_sample,
                run_id=routing_run_id,
                mode=run_context.mode,
                departure_iso=run_context.departure_iso,
                sample_rows=sample_rows,
                missing_share=sample_missing_share,
            )
    finally:
        conn_sample.close()
    return sample_rows_written


def _run_nonpersistent_routing(
    origins: list[tuple[float, float]],
    chunk_size: int,
    enable_progress: bool,
    run_context: RoutingRunContext,
) -> dict[str, Any]:
    """Run the same routing pipeline without creating cache artifacts.

    This path is useful for smoke tests and dry-run style execution. It still
    builds chunks exactly like the persistent path, but stops after computing
    counts instead of writing normalized rows into SQLite.
    """
    total_rows = 0
    missing_rows = 0
    progress = (
        tqdm(
            total=len(origins),
            desc=f"r5 {run_context.mode}",
            mininterval=1,
            maxinterval=1,
            miniters=1,
        )
        if enable_progress
        else None
    )
    try:
        for start_idx, origin_coords_chunk in _chunked(origins, chunk_size):
            origins_gdf, origins_map = _build_origin_lookup_df(origin_coords_chunk, start_idx=start_idx)
            matrix_chunk_df = _build_matrix_chunk_df(run_context, origins_gdf)
            matrix_chunk_df = _finalize_chunk_df(
                matrix_chunk_df,
                origins_map=origins_map,
                run_context=run_context,
                include_labels=True,
            )
            chunk_rows, chunk_missing_rows = _summarize_chunk_counts(matrix_chunk_df)
            total_rows += chunk_rows
            missing_rows += chunk_missing_rows
            if progress:
                progress.update(len(origin_coords_chunk))
    finally:
        if progress:
            progress.close()

    return _build_summary(
        run_context=run_context,
        rows=total_rows,
        missing_rows=missing_rows,
        resumed=False,
        processed_origins=0,
        out_csv=None,
        out_db=None,
        persist_outputs=False,
    )


def _matrix_progress_label(run_context: RoutingRunContext) -> str:
    if run_context.mode == MODE_FAST and run_context.wait_model.fast_wait_model == "tripplanner_exact":
        return "r5 fast matrix (phase 1/2)"
    return f"r5 {run_context.mode}"


def _build_matrix_progress(
    total_origins: int,
    pending_origins: int,
    chunk_size: int,
    enable_progress: bool,
    run_context: RoutingRunContext,
) -> tuple[Any | None, int]:
    """Create the progress bar and compute how much progress can be resumed immediately."""
    total_matrix_chunks = max(1, math.ceil(total_origins / max(1, int(chunk_size))))
    completed_origins = total_origins - pending_origins
    completed_chunks = min(
        total_matrix_chunks,
        math.ceil(max(0, completed_origins) / max(1, int(chunk_size))),
    )
    progress = (
        tqdm(
            total=total_matrix_chunks,
            desc=_matrix_progress_label(run_context),
            mininterval=1,
            maxinterval=1,
            miniters=1,
            unit="chunk",
        )
        if enable_progress
        else None
    )
    return progress, completed_chunks


def _run_persistent_routing(
    origins: list[tuple[float, float]],
    destinations: list[tuple[float, float]],
    out_csv_path: Path,
    out_db_path: Path,
    chunk_size: int,
    enable_progress: bool,
    sample_rows: int,
    sample_missing_share: float,
    run_context: RoutingRunContext,
) -> dict[str, Any]:
    """Execute the full resumable routing build backed by SQLite.

    This is the production path used by the pipeline: seed or resume the temp
    cache, run chunked matrix routing, persist normalized rows, optionally run
    the exact wait phase-2 pass, then export a small inspection sample from the
    finished DB.
    """
    out_csv_path.parent.mkdir(parents=True, exist_ok=True)
    out_db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_db_path = out_db_path.with_suffix(out_db_path.suffix + ".tmp")

    # Resume only when the temporary DB matches the current run identity.
    _seed_tmp_db_from_completed_output(tmp_db_path, out_db_path)
    resume_state = _load_resume_state(tmp_db_path, run_context)
    origins_todo = _compute_origins_todo(origins, resume_state.processed_origin_keys)
    progress, completed_matrix_chunks = _build_matrix_progress(
        total_origins=len(origins),
        pending_origins=len(origins_todo),
        chunk_size=chunk_size,
        enable_progress=enable_progress,
        run_context=run_context,
    )

    conn = sqlite3.connect(tmp_db_path)
    try:
        routing_run_id = _init_or_validate_routing_db(conn, tmp_db_path, run_context, resume_state)
        sample_written_early = False

        if progress and completed_matrix_chunks:
            progress.update(completed_matrix_chunks)

        point_id_cache: dict[tuple[float, float], int] = {}
        if origins_todo:
            _register_points_for_run(conn, origins_todo, destinations, point_id_cache)

        sample_written_early = _write_sample_csv_if_ready(
            out_csv_path=out_csv_path,
            conn=conn,
            run_id=routing_run_id,
            run_context=run_context,
            sample_rows=sample_rows,
            sample_missing_share=sample_missing_share,
            total_rows=resume_state.total_rows,
            already_written=sample_written_early,
        )

        total_rows = resume_state.total_rows
        missing_rows = resume_state.missing_rows
        for chunk_index, (start_idx, origin_coords_chunk) in enumerate(_chunked(origins_todo, chunk_size)):
            if progress:
                progress.set_postfix_str(
                    f"chunk {completed_matrix_chunks + chunk_index + 1}/{progress.total}"
                )
            origins_gdf, origins_map = _build_origin_lookup_df(origin_coords_chunk, start_idx=start_idx)
            matrix_chunk_df = _build_matrix_chunk_df(run_context, origins_gdf)
            matrix_chunk_df = _finalize_chunk_df(
                matrix_chunk_df,
                origins_map=origins_map,
                run_context=run_context,
                include_labels=False,
            )
            chunk_rows, chunk_missing_rows = _summarize_chunk_counts(matrix_chunk_df)
            total_rows += chunk_rows
            missing_rows += chunk_missing_rows

            route_rows = _iter_route_rows(
                matrix_chunk_df,
                run_id=routing_run_id,
                conn=conn,
                point_id_cache=point_id_cache,
            )
            _insert_rows(conn, [route_row.as_db_tuple() for route_row in route_rows])
            conn.commit()
            sample_written_early = _write_sample_csv_if_ready(
                out_csv_path=out_csv_path,
                conn=conn,
                run_id=routing_run_id,
                run_context=run_context,
                sample_rows=sample_rows,
                sample_missing_share=sample_missing_share,
                total_rows=total_rows,
                already_written=sample_written_early,
            )
            if progress:
                progress.update(1)

        if run_context.mode == MODE_FAST and run_context.wait_model.fast_wait_model == "tripplanner_exact":
            _apply_tripplanner_waits_to_run(
                conn=conn,
                run_id=routing_run_id,
                transport_network=run_context.transport_network,
                r5py_module=run_context.r5py_module,
                departure_dt=run_context.departure_dt,
                r5_max_time_walking_min=run_context.wait_model.max_time_walking_min,
                r5_departure_window_min=run_context.wait_model.departure_window_min,
                r5_tripplanner_timeout_s=run_context.wait_model.tripplanner_timeout_s,
                r5_tripplanner_workers=run_context.wait_model.tripplanner_workers,
                enable_progress=enable_progress,
            )
    finally:
        if progress:
            progress.close()
        conn.close()

    os.replace(tmp_db_path, out_db_path)
    sample_rows_written = _write_final_sample_csv(
        out_csv_path=out_csv_path,
        out_db_path=out_db_path,
        run_context=run_context,
        sample_rows=sample_rows,
        sample_missing_share=sample_missing_share,
    )
    return _build_summary(
        run_context=run_context,
        rows=total_rows,
        missing_rows=missing_rows,
        resumed=resume_state.has_existing_state,
        processed_origins=len(resume_state.processed_origin_keys),
        out_csv=str(out_csv_path),
        out_db=str(out_db_path),
        persist_outputs=True,
        sample_rows_written=sample_rows_written,
    )


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
    """Entry point used by the bus-routing stage to build the routing store.

    The top-level narrative is:
    1. prepare Java and r5py,
    2. freeze all run-wide state into `RoutingRunContext`,
    3. execute either the non-persistent or persistent chunk pipeline,
    4. return a summary that downstream stages and logs can consume.
    """
    if mode not in {MODE_FAST, MODE_SLOW}:
        raise ValueError(f"Unsupported routing mode: {mode}")
    if not origins or not destinations:
        raise ValueError("Origins and destinations must be non-empty.")

    # The top-level flow is: prepare runtime, build immutable run context, then
    # execute either an in-memory run or a resumable persisted run.
    r5py_module, transport_network, effective_workers = _prepare_r5_runtime(
        mode=mode,
        pbf_path=pbf_path,
        gtfs_path=gtfs_path,
        jar_path=jar_path,
        workers=workers,
    )
    run_context = _build_routing_run_context(
        origins=origins,
        destinations=destinations,
        mode=mode,
        gtfs_path=gtfs_path,
        departure_dt=departure_dt,
        workers=effective_workers,
        r5_fast_wait_model=r5_fast_wait_model,
        r5_tripplanner_workers=r5_tripplanner_workers,
        r5_tripplanner_timeout_s=r5_tripplanner_timeout_s,
        r5_max_time_walking_min=r5_max_time_walking_min,
        r5_departure_window_min=r5_departure_window_min,
        r5py_module=r5py_module,
        transport_network=transport_network,
    )

    if not persist_outputs:
        return _run_nonpersistent_routing(
            origins=origins,
            chunk_size=chunk_size,
            enable_progress=enable_progress,
            run_context=run_context,
        )

    sample_output_path = Path(sample_csv_path or out_csv)
    routing_db_path = Path(out_db)
    return _run_persistent_routing(
        origins=origins,
        destinations=destinations,
        out_csv_path=sample_output_path,
        out_db_path=routing_db_path,
        chunk_size=chunk_size,
        enable_progress=enable_progress,
        sample_rows=sample_rows,
        sample_missing_share=sample_missing_share,
        run_context=run_context,
    )


def _build_routing_store_child(run_args: dict[str, Any], result_queue: Any) -> None:
    """Subprocess entrypoint used by the resilient supervisor wrapper."""
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


# Resilient subprocess supervisor.
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
    """Retry routing in a subprocess, shrinking workers and chunk size after failures or stalls."""
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


# Public lookup helpers used by downstream accessibility stages.
def open_routing_index(db_path: str) -> RoutingIndex:
    """Open the routing cache and validate that it uses the current schema."""
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
    """Look up one origin-destination impedance value from the persisted routing cache."""
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
    """Fetch all available destination impedances for a single origin."""
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
    """Fetch impedances for only a selected destination subset for one origin."""
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
