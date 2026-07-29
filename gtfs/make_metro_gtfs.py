#!/usr/bin/env python3
"""Build a GTFS feed containing only the Metrocagliari (metro) lines.

Reads the full ARST Cagliari feed and writes a new zip that keeps only the
selected routes, cascading the filter through trips -> stop_times -> stops and
shapes -> calendar so the result is a self-consistent GTFS.

Usage:
    python make_metro_gtfs.py                       # defaults below
    python make_metro_gtfs.py --routes MCA1 MCA2    # override selection
    python make_metro_gtfs.py -i in.zip -o out.zip
"""
import argparse
import csv
import io
import sys
import zipfile
from pathlib import Path

# Metrocagliari light-rail lines (route_type=0). MSS is Metrosassari (Sassari).
DEFAULT_ROUTES = ["MCA1", "MCA2"]


def read_table(zf: zipfile.ZipFile, name: str):
    """Return (fieldnames, list-of-rows) for a GTFS file, or (None, []) if absent."""
    if name not in zf.namelist():
        return None, []
    with zf.open(name) as fh:
        reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))
        return reader.fieldnames, list(reader)


def write_table(zf: zipfile.ZipFile, name: str, fieldnames, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    zf.writestr(name, buf.getvalue())
    print(f"  {name:20s} {len(rows):>8d} rows")


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--input", type=Path,
                    default=here / "arst-cagliari-it.zip")
    ap.add_argument("-o", "--output", type=Path,
                    default=here / "gtfs_metrocagliari.zip")
    ap.add_argument("--routes", nargs="+", default=DEFAULT_ROUTES,
                    help=f"route_id values to keep (default: {DEFAULT_ROUTES})")
    args = ap.parse_args()

    keep_routes = set(args.routes)
    print(f"Filtering {args.input.name} -> {args.output.name}")
    print(f"Keeping routes: {sorted(keep_routes)}\n")

    with zipfile.ZipFile(args.input) as zf:
        routes_fn, routes = read_table(zf, "routes.txt")
        trips_fn, trips = read_table(zf, "trips.txt")
        st_fn, stop_times = read_table(zf, "stop_times.txt")
        stops_fn, stops = read_table(zf, "stops.txt")
        shapes_fn, shapes = read_table(zf, "shapes.txt")
        cal_fn, calendar = read_table(zf, "calendar.txt")
        caldates_fn, calendar_dates = read_table(zf, "calendar_dates.txt")
        agency_fn, agency = read_table(zf, "agency.txt")
        feedinfo_fn, feed_info = read_table(zf, "feed_info.txt")

    # routes -> trips
    routes = [r for r in routes if r["route_id"] in keep_routes]
    if not routes:
        print("ERROR: none of the requested routes were found.", file=sys.stderr)
        return 1
    keep_agencies = {r.get("agency_id") for r in routes if r.get("agency_id")}

    trips = [t for t in trips if t["route_id"] in keep_routes]
    keep_trips = {t["trip_id"] for t in trips}
    keep_services = {t["service_id"] for t in trips}
    keep_shapes = {t.get("shape_id") for t in trips if t.get("shape_id")}

    # trips -> stop_times -> stops
    stop_times = [s for s in stop_times if s["trip_id"] in keep_trips]
    keep_stops = {s["stop_id"] for s in stop_times}

    # include parent stations of any referenced stop
    stop_by_id = {s["stop_id"]: s for s in stops}
    for sid in list(keep_stops):
        parent = stop_by_id.get(sid, {}).get("parent_station")
        if parent:
            keep_stops.add(parent)
    stops = [s for s in stops if s["stop_id"] in keep_stops]

    shapes = [s for s in shapes if s.get("shape_id") in keep_shapes]
    calendar = [c for c in calendar if c["service_id"] in keep_services]
    calendar_dates = [c for c in calendar_dates if c["service_id"] in keep_services]
    if keep_agencies:
        agency = [a for a in agency if a.get("agency_id") in keep_agencies]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as zf:
        print("Wrote:")
        write_table(zf, "agency.txt", agency_fn, agency)
        write_table(zf, "routes.txt", routes_fn, routes)
        write_table(zf, "trips.txt", trips_fn, trips)
        write_table(zf, "stop_times.txt", st_fn, stop_times)
        write_table(zf, "stops.txt", stops_fn, stops)
        if shapes_fn:
            write_table(zf, "shapes.txt", shapes_fn, shapes)
        if cal_fn:
            write_table(zf, "calendar.txt", cal_fn, calendar)
        if caldates_fn:
            write_table(zf, "calendar_dates.txt", caldates_fn, calendar_dates)
        if feedinfo_fn:
            write_table(zf, "feed_info.txt", feedinfo_fn, feed_info)

    print(f"\nDone -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
