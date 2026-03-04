#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode
import csv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compute a tiny r5py travel-time matrix and save it to CSV "
            "using a local R5 jar."
        )
    )
    parser.add_argument(
        "--pbf",
        default="gtfs-pbf/cagliari-latest.osmv2.pbf",
        help="Path to OSM PBF file.",
    )
    parser.add_argument(
        "--gtfs",
        default="gtfs-pbf/GTFS.zip",
        help="Path to GTFS zip file.",
    )
    parser.add_argument(
        "--r5-classpath",
        default="r5-v7.5-r5py-all.jar",
        help="Path to r5-all jar file.",
    )
    parser.add_argument(
        "--departure",
        default="2026-02-18T08:30:00",
        help="Departure datetime in ISO format, e.g. 2026-02-18T08:30:00.",
    )
    parser.add_argument(
        "--out-csv",
        default="outputs/r5py_matrix_test.csv",
        help="Output CSV path.",
    )
    parser.add_argument(
        "--num-sources",
        type=int,
        default=2,
        help="Number of origin/source points to generate.",
    )
    parser.add_argument(
        "--num-pois",
        type=int,
        default=3,
        help="Number of destination/POI points to generate.",
    )
    parser.add_argument(
        "--with-wait-time",
        action="store_true",
        help=(
            "Also run DetailedItineraries and append OD-level wait_time "
            "(minutes) based on the fastest itinerary option."
        ),
    )
    return parser.parse_args()


def resolve_path(value: str, repo_root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (repo_root / path)


def preflight(args: argparse.Namespace, repo_root: Path) -> tuple[int, dict[str, Path], dt.datetime | None]:
    pbf_path = resolve_path(args.pbf, repo_root)
    gtfs_path = resolve_path(args.gtfs, repo_root)
    jar_path = resolve_path(args.r5_classpath, repo_root)
    out_csv_path = resolve_path(args.out_csv, repo_root)

    errors: list[str] = []

    if not pbf_path.is_file():
        errors.append(f"Missing PBF file: {pbf_path}")
    if not gtfs_path.is_file():
        errors.append(f"Missing GTFS file: {gtfs_path}")
    if not jar_path.is_file():
        errors.append(
            f"Missing R5 jar file: {jar_path}. "
            "Provide --r5-classpath with a valid local jar path."
        )

    departure_dt: dt.datetime | None = None
    try:
        departure_dt = dt.datetime.fromisoformat(args.departure)
    except ValueError:
        errors.append(
            f"Invalid --departure value '{args.departure}'. "
            "Use ISO format, e.g. 2026-02-18T08:30:00."
        )

    try:
        subprocess.run(
            ["java", "-version"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        errors.append("Java not found in PATH. Install Java and retry.")
    except subprocess.CalledProcessError:
        errors.append("Java command failed. Verify your Java installation.")

    if args.num_sources <= 0:
        errors.append("--num-sources must be a positive integer.")
    if args.num_pois <= 0:
        errors.append("--num-pois must be a positive integer.")

    if errors:
        print("Preflight checks failed:", file=sys.stderr)
        for err in errors:
            print(f"- {err}", file=sys.stderr)
        return 2, {}, None

    paths = {
        "pbf": pbf_path,
        "gtfs": gtfs_path,
        "jar": jar_path,
        "out_csv": out_csv_path,
    }
    return 0, paths, departure_dt


def ensure_r5_classpath_arg(jar_path: Path) -> None:
    if "--r5-classpath" in sys.argv or "-r" in sys.argv:
        return
    sys.argv.extend(["--r5-classpath", str(jar_path)])


def configure_java_home_from_path() -> None:
    java_exe = shutil.which("java")
    if java_exe is None:
        return

    java_path = Path(java_exe).resolve()
    if java_path.parent.name.lower() != "bin":
        return

    detected_java_home = java_path.parent.parent
    current_java_home = os.environ.get("JAVA_HOME")

    if current_java_home:
        try:
            if Path(current_java_home).resolve() == detected_java_home:
                return
        except OSError:
            pass

    os.environ["JAVA_HOME"] = str(detected_java_home)
    print(f"Using JAVA_HOME={detected_java_home}")


def build_points(num_sources: int, num_pois: int):
    import geopandas as gpd
    from shapely.geometry import Point

    def generate_points(prefix: str, count: int, base_lon: float, base_lat: float):
        points = []
        ids = []
        for idx in range(count):
            row = idx // 10
            col = idx % 10
            lon = base_lon + (col * 0.0012)
            lat = base_lat + (row * 0.0010)
            ids.append(f"{prefix}{idx + 1}")
            points.append(Point(lon, lat))
        return ids, points

    source_ids, source_points = generate_points("o", num_sources, 9.0950, 39.2280)
    poi_ids, poi_points = generate_points("d", num_pois, 9.1100, 39.2200)

    origins = gpd.GeoDataFrame(
        {
            "id": source_ids,
            "geometry": source_points,
        },
        crs="EPSG:4326",
    )

    destinations = gpd.GeoDataFrame(
        {
            "id": poi_ids,
            "geometry": poi_points,
        },
        crs="EPSG:4326",
    )
    return origins, destinations


def matrix_with_coordinates(matrix, origins, destinations, departure_dt: dt.datetime):
    origins_map = origins.copy()
    origins_map["from_id"] = origins_map["id"]
    origins_map["from_lon"] = origins_map.geometry.x
    origins_map["from_lat"] = origins_map.geometry.y
    origins_map = origins_map[["from_id", "from_lon", "from_lat"]]

    destinations_map = destinations.copy()
    destinations_map["to_id"] = destinations_map["id"]
    destinations_map["to_lon"] = destinations_map.geometry.x
    destinations_map["to_lat"] = destinations_map.geometry.y
    destinations_map = destinations_map[["to_id", "to_lon", "to_lat"]]

    matrix_out = matrix.merge(origins_map, on="from_id", how="left")
    matrix_out = matrix_out.merge(destinations_map, on="to_id", how="left")
    matrix_out["google_maps_transit_url"] = matrix_out.apply(
        lambda row: google_maps_transit_link(
            row["from_lat"],
            row["from_lon"],
            row["to_lat"],
            row["to_lon"],
            departure_dt,
        ),
        axis=1,
    )

    ordered = [
        "from_lon",
        "from_lat",
        "to_lon",
        "to_lat",
        "google_maps_transit_url",
    ]
    other_cols = [col for col in matrix_out.columns if col not in {*ordered}]
    return matrix_out[ordered + other_cols]


def google_maps_transit_link(
    from_lat, from_lon, to_lat, to_lon, departure_dt: dt.datetime
) -> str:
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


def write_csv_quote_only_url(matrix_out, out_csv: Path) -> None:
    columns = list(matrix_out.columns)
    url_idx = columns.index("google_maps_transit_url")

    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(
            fh,
            delimiter=",",
            quoting=csv.QUOTE_NONE,
            quotechar="'",
            escapechar="\\",
        )
        writer.writerow(columns)
        for row in matrix_out.itertuples(index=False, name=None):
            row_values = ["" if value is None else str(value) for value in row]
            row_values[url_idx] = f"\"{row_values[url_idx]}\""
            writer.writerow(row_values)


def compute_wait_time_by_od(
    r5py_module,
    network,
    origins,
    destinations,
    departure_dt: dt.datetime,
):
    detailed = r5py_module.DetailedItineraries(
        network,
        origins=origins,
        destinations=destinations,
        departure=departure_dt,
        transport_modes=[r5py_module.TransportMode.BUS],
        snap_to_network=True,
        force_all_to_all=True,
    )

    if detailed.empty:
        import pandas as pd

        return pd.DataFrame(columns=["from_id", "to_id", "wait_time"])

    per_option = (
        detailed.groupby(["from_id", "to_id", "option"], as_index=False)[["travel_time", "wait_time"]]
        .sum()
    )
    fastest = per_option.sort_values(
        by=["from_id", "to_id", "travel_time", "wait_time", "option"]
    ).drop_duplicates(subset=["from_id", "to_id"], keep="first")

    fastest["wait_time"] = fastest["wait_time"].dt.total_seconds() / 60.0
    return fastest[["from_id", "to_id", "wait_time"]]


def run() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]

    status, paths, departure_dt = preflight(args, repo_root)
    if status != 0:
        return status
    if departure_dt is None:
        print("Internal error: departure datetime is missing after preflight.", file=sys.stderr)
        return 2

    ensure_r5_classpath_arg(paths["jar"])
    configure_java_home_from_path()

    try:
        import r5py
    except Exception as exc:
        print(f"Failed to import r5py: {exc}", file=sys.stderr)
        return 1

    try:
        origins, destinations = build_points(args.num_sources, args.num_pois)

        network = r5py.TransportNetwork(str(paths["pbf"]), [str(paths["gtfs"])])
        # r5py's class derives from pandas.DataFrame; static analyzers may pick
        # DataFrame __new__ overloads instead of r5py's runtime constructor.
        travel_time_matrix_ctor = cast(Any, r5py.TravelTimeMatrix)
        matrix = travel_time_matrix_ctor(
            network,
            origins=origins,
            destinations=destinations,
            departure=departure_dt,
            transport_modes=[r5py.TransportMode.BUS],
            snap_to_network=True,
        )
        matrix_out = matrix_with_coordinates(matrix, origins, destinations, departure_dt)

        if args.with_wait_time:
            wait_time_by_od = compute_wait_time_by_od(
                r5py, network, origins, destinations, departure_dt
            )
            matrix_out = matrix_out.merge(
                wait_time_by_od, on=["from_id", "to_id"], how="left"
            )

        matrix_out = matrix_out.drop(columns=["from_id", "to_id"], errors="ignore")

        out_csv = paths["out_csv"]
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        write_csv_quote_only_url(matrix_out, out_csv)

        print(f"Matrix saved to: {out_csv}")
        print(f"Rows: {len(matrix_out)}")
        preview_cols = [
            col
            for col in [
                "from_lon",
                "from_lat",
                "to_lon",
                "to_lat",
                "travel_time",
                "travel_time_p50",
                "wait_time",
            ]
            if col in matrix_out.columns
        ]
        if preview_cols:
            print("Preview:")
            print(matrix_out[preview_cols].head(5).to_string(index=False))
        else:
            print("Preview:")
            print(matrix_out.head(5).to_string(index=False))
        return 0
    except Exception as exc:
        print(f"r5py matrix computation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
