#!/usr/bin/env python3
"""Build a GTFS feed keeping only wheelchair-accessible stops.

Models "public transport is restricted to accessible stops" (a physically-disabled traveler):
keep only ``stops.txt`` rows with ``wheelchair_boarding == 1``, then cascade the filter so the
result is a self-consistent GTFS — stop_times are dropped at inaccessible stops, trips left with
fewer than two boardable stops are removed, and routes/services/shapes/transfers are pruned to
what survives. A vehicle still runs between accessible stops; the traveler simply cannot board or
alight at the inaccessible ones (their stop_times rows are gone).

Per GTFS: wheelchair_boarding 1 = accessible, 2 = not accessible, 0/empty = no info (treated as
not accessible by default; pass --keep-unknown to also keep 0/empty, e.g. if the elderly run
loses almost all bus reachability).

Usage:
    python gtfs/make_accessible_gtfs.py                       # gtfs/GTFS.zip -> gtfs/GTFS_accessible.zip
    python gtfs/make_accessible_gtfs.py -i in.zip -o out.zip
    python gtfs/make_accessible_gtfs.py --keep-unknown        # also keep wheelchair_boarding 0/empty
"""
import argparse
import csv
import io
import sys
import zipfile
from pathlib import Path

csv.field_size_limit(sys.maxsize)


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


def _is_accessible(stop: dict, keep_unknown: bool) -> bool:
    value = (stop.get("wheelchair_boarding") or "").strip()
    if value == "1":
        return True
    if keep_unknown and value in ("", "0"):
        return True
    return False


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("-i", "--input", type=Path, default=here / "GTFS.zip")
    ap.add_argument("-o", "--output", type=Path, default=here / "GTFS_accessible.zip")
    ap.add_argument(
        "--keep-unknown",
        action="store_true",
        help="also keep stops with wheelchair_boarding 0/empty (unknown), not just 1",
    )
    args = ap.parse_args()

    if not args.input.exists():
        print(f"ERROR: input feed not found: {args.input}", file=sys.stderr)
        return 1

    print(f"Filtering {args.input.name} -> {args.output.name}")

    with zipfile.ZipFile(args.input) as zf:
        agency_fn, agency = read_table(zf, "agency.txt")
        routes_fn, routes = read_table(zf, "routes.txt")
        trips_fn, trips = read_table(zf, "trips.txt")
        st_fn, stop_times = read_table(zf, "stop_times.txt")
        stops_fn, stops = read_table(zf, "stops.txt")
        shapes_fn, shapes = read_table(zf, "shapes.txt")
        cal_fn, calendar = read_table(zf, "calendar.txt")
        caldates_fn, calendar_dates = read_table(zf, "calendar_dates.txt")
        transfers_fn, transfers = read_table(zf, "transfers.txt")
        feedinfo_fn, feed_info = read_table(zf, "feed_info.txt")

    if stops_fn is None or "wheelchair_boarding" not in (stops_fn or []):
        print(
            "ERROR: stops.txt is missing or has no wheelchair_boarding column; "
            "cannot build an accessible-stops feed.",
            file=sys.stderr,
        )
        return 1

    stop_by_id = {s["stop_id"]: s for s in stops}

    # 1. Accessible boardable stops, plus the parent stations of any kept stop (structural).
    keep_stops = {s["stop_id"] for s in stops if _is_accessible(s, args.keep_unknown)}
    n_accessible = len(keep_stops)
    for sid in list(keep_stops):
        parent = stop_by_id.get(sid, {}).get("parent_station")
        if parent:
            keep_stops.add(parent)

    # 2. Drop stop_times at non-kept stops.
    stop_times = [s for s in stop_times if s["stop_id"] in keep_stops]

    # 3. Keep trips that still have >= 2 boardable stops (a usable trip).
    stops_per_trip: dict[str, int] = {}
    for s in stop_times:
        stops_per_trip[s["trip_id"]] = stops_per_trip.get(s["trip_id"], 0) + 1
    keep_trips = {tid for tid, n in stops_per_trip.items() if n >= 2}

    stop_times = [s for s in stop_times if s["trip_id"] in keep_trips]
    trips = [t for t in trips if t["trip_id"] in keep_trips]

    # 4. Cascade to routes / services / shapes / agencies.
    keep_routes = {t["route_id"] for t in trips}
    keep_services = {t["service_id"] for t in trips}
    keep_shapes = {t.get("shape_id") for t in trips if t.get("shape_id")}

    routes = [r for r in routes if r["route_id"] in keep_routes]
    keep_agencies = {r.get("agency_id") for r in routes if r.get("agency_id")}

    calendar = [c for c in calendar if c["service_id"] in keep_services]
    calendar_dates = [c for c in calendar_dates if c["service_id"] in keep_services]
    shapes = [s for s in shapes if s.get("shape_id") in keep_shapes]
    if keep_agencies:
        agency = [a for a in agency if a.get("agency_id") in keep_agencies]

    # 5. Stops actually referenced by surviving stop_times (plus their parents).
    referenced = {s["stop_id"] for s in stop_times}
    for sid in list(referenced):
        parent = stop_by_id.get(sid, {}).get("parent_station")
        if parent:
            referenced.add(parent)
    stops = [s for s in stops if s["stop_id"] in referenced]

    # 6. Transfers only between surviving stops.
    if transfers_fn:
        transfers = [
            t
            for t in transfers
            if t.get("from_stop_id") in referenced and t.get("to_stop_id") in referenced
        ]

    print(
        f"\nAccessible stops kept: {n_accessible} "
        f"(referenced after cascade: {len(stops)}); trips kept: {len(trips)}"
    )
    if not trips:
        print(
            "ERROR: no trips survived the accessible-stops filter. Try --keep-unknown.",
            file=sys.stderr,
        )
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as zf:
        print("Wrote:")
        if agency_fn:
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
        if transfers_fn:
            write_table(zf, "transfers.txt", transfers_fn, transfers)
        if feedinfo_fn:
            write_table(zf, "feed_info.txt", feedinfo_fn, feed_info)

    print(f"\nDone -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
