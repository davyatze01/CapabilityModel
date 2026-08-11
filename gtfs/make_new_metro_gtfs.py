#!/usr/bin/env python3
"""Add the (extended) Metrocagliari line on top of Cagliari's own bus feed, unmodified.

scenarios/new-metro/metro_cagliari.gpkg (supplied by the colleague who digitized it) carries
30 stop rows: the 24 existing Metrocagliari stops (M_9100.. / M_9200.., both directions,
byte-identical to gtfs_metrocagliari.zip's own stops.txt) plus 6 new stops flagged
``project == 1`` -- SAN SATURNINO, BONARIA, LUSSU, DARSENA, MUNICIPIO, STAZIONE -- in
geographic order walking away from REPUBBLICA towards the port/train station. Those 6 are
the only thing this script adds to the metro.

The output merges TWO feeds that were never meant to share a calendar:
  - --bus-input (default gtfs/GTFS.zip): Cagliari's real bus network (50 CTM routes),
    calendar.txt on Mon-Fri/Sat/Sun weekday flags valid 2025-10-15..2025-11-30 -- this is
    also what profiles.BASELINE routes against, so the "new-metro" comparison should add
    *only* the metro on top of it, not swap in a different/bigger bus network.
  - --metro-input (default gtfs/gtfs_metrocagliari.zip): MCA1/MCA2 only, whose own
    calendar.txt has all-zero weekday flags -- its FER/FEST services exist purely via
    calendar_dates.txt exceptions dated across 2026, a completely different vintage than
    the bus feed with zero overlapping active dates.
A departure date is a download-time artifact, not a property of the line itself, so rather
than switch the bus feed to a newer (much larger, ~313-route) export just to share a
calendar with the metro, this script keeps the bus feed as-is and re-dates the metro:
FER/FEST are re-declared as plain weekday-flag services (FER = Mon-Fri, FEST = Sun) spanning
the bus feed's own calendar window, and the metro's original calendar_dates exceptions are
dropped (they'd only reintroduce the 2026 dates). Same frequency the source feed publishes,
just made to run on the same day as the buses it needs to be compared against.

Only MCA1 trips that actually terminate at REPUBBLICA (M_9100 as a trip's *first* stop,
M_9210 as a trip's *last* stop -- MCA2 never touches REPUBBLICA) are extended, by prepending
(direction 1, M_9100 first) or appending (direction 0, M_9210 last) the 6 new stops. Every
extended trip keeps its original stop-to-stop *frequency* untouched: the trips and every
existing arrival/departure time are copied verbatim; only the new stops' own times are
synthesized, using the average speed of the existing MCA1 REPUBBLICA<->POLICLINICO run
(dist/time of shape M_REP_POL: ~8.0 km in 22 min) applied to the new stops' haversine
spacing. That is "assume similar frequency/speed to gtfs_metrocagliari" made concrete,
without inventing a headway that doesn't exist in the source feed. The bus feed's own
routes/trips/stops/shapes/calendar/calendar_dates pass through completely untouched.

Usage:
    python gtfs/make_new_metro_gtfs.py                                    # defaults below
    python gtfs/make_new_metro_gtfs.py --bus-input b.zip --metro-input m.zip -o out.zip
"""
import argparse
import csv
import io
import math
import sys
import zipfile
from pathlib import Path

REPUBBLICA_FIRST = "M_9100"  # MCA1 direction_id=1 trips that start the full run here
REPUBBLICA_LAST = "M_9210"   # MCA1 direction_id=0 trips that end the full run here
NEW_STOP_ID_BASE = 9300      # M_9300.. -- unused by the existing M_91xx/M_92xx numbering

# metro_cagliari.gpkg carries the 24 existing Metrocagliari stops (project = NULL/empty) and
# the 6 new ones (project = 1) side by side; this knob is what picks the new ones out. Only
# change it if the colleague's gpkg schema changes (e.g. a different flag column/value).
NEW_STOP_FILTER_COLUMN = "project"
NEW_STOP_FILTER_VALUE = 1


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


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def hms_to_sec(hms: str) -> int:
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


def sec_to_hms(sec: int) -> str:
    sec = max(0, sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def load_new_stops(gpkg_path: Path) -> list[dict]:
    """Read the new-stop rows (``NEW_STOP_FILTER_COLUMN == NEW_STOP_FILTER_VALUE``) from the
    colleague's gpkg, in their given (geographic, REPUBBLICA-outward) order, and assign them
    fresh M_93xx stop_ids. The existing 24 Metrocagliari stops carry
    ``NEW_STOP_FILTER_COLUMN`` = NULL/empty and are excluded by this filter."""
    import geopandas as gpd

    gdf = gpd.read_file(gpkg_path)
    new_rows = gdf[gdf[NEW_STOP_FILTER_COLUMN] == NEW_STOP_FILTER_VALUE]
    if new_rows.empty:
        raise RuntimeError(
            f"no rows with {NEW_STOP_FILTER_COLUMN}=={NEW_STOP_FILTER_VALUE} found in {gpkg_path}"
        )

    out = []
    for i, (_, row) in enumerate(new_rows.iterrows()):
        out.append(
            {
                "stop_id": f"M_{NEW_STOP_ID_BASE + i}",
                "stop_code": str(NEW_STOP_ID_BASE + i),
                "stop_name": str(row["stop_name"]),
                "stop_desc": "",
                "stop_lat": f"{row.geometry.y:.6f}",
                "stop_lon": f"{row.geometry.x:.6f}",
                "zone_id": "901",  # inherit REPUBBLICA's zone (city-centre extension)
                "wheelchair_boarding": "1" if str(row.get("wheelchair", "1")) == "1" else "0",
                "stop_url": "",
            }
        )
    return out


def compute_hop_minutes(new_stops: list[dict], stop_by_id: dict, avg_speed_kmh: float) -> list[int]:
    """Minutes for each hop REPUBBLICA -> new_stops[0] -> new_stops[1] -> ..., from haversine
    distance at avg_speed_kmh, rounded to the nearest whole minute (min 1)."""
    repubblica = stop_by_id[REPUBBLICA_FIRST]
    prev_lat, prev_lon = float(repubblica["stop_lat"]), float(repubblica["stop_lon"])
    minutes = []
    for s in new_stops:
        lat, lon = float(s["stop_lat"]), float(s["stop_lon"])
        dist_km = haversine_km(prev_lat, prev_lon, lat, lon)
        minutes.append(max(1, round(dist_km / avg_speed_kmh * 60)))
        prev_lat, prev_lon = lat, lon
    return minutes


def average_speed_kmh(shapes: list[dict], stop_times: list[dict], trips: list[dict]) -> float:
    """Average speed (km/h) of the full REPUBBLICA<->POLICLINICO run, from shape M_REP_POL's
    total length and one full-length trip's total travel time."""
    shape_pts = sorted(
        (s for s in shapes if s["shape_id"] == "M_REP_POL"),
        key=lambda r: int(r["shape_pt_sequence"]),
    )
    total_km = float(shape_pts[-1]["shape_dist_traveled"])

    full_trip_id = next(
        t["trip_id"] for t in trips
        if t["route_id"] == "MCA1" and t.get("shape_id") == "M_REP_POL"
    )
    seq = sorted(
        (s for s in stop_times if s["trip_id"] == full_trip_id),
        key=lambda r: int(r["stop_sequence"]),
    )
    total_sec = hms_to_sec(seq[-1]["arrival_time"]) - hms_to_sec(seq[0]["departure_time"])
    return total_km / (total_sec / 3600.0)


def extend_trip(
    trip_stop_times: list[dict],
    new_stops: list[dict],
    hop_minutes: list[int],
    mode: str,
) -> list[dict]:
    """Return trip_stop_times with the 6 new stops prepended (mode="prepend", direction_id=1,
    first stop REPUBBLICA) or appended (mode="append", direction_id=0, last stop REPUBBLICA),
    stop_sequence renumbered from 1, and the terminus pickup/drop_off flags moved to the new
    physical terminus (STAZIONE)."""
    trip_stop_times = sorted(trip_stop_times, key=lambda r: int(r["stop_sequence"]))
    cum_minutes = []
    total = 0
    for m in hop_minutes:
        total += m
        cum_minutes.append(total)

    if mode == "prepend":
        origin = trip_stop_times[0]
        t0 = hms_to_sec(origin["departure_time"])
        new_rows = []
        # Farthest (STAZIONE) first -> nearest (SAN SATURNINO) last, i.e. reverse of
        # new_stops' REPUBBLICA-outward order, each timed backwards from REPUBBLICA.
        for stop, cum in reversed(list(zip(new_stops, cum_minutes))):
            t = t0 - cum * 60
            new_rows.append(
                {
                    "trip_id": origin["trip_id"],
                    "arrival_time": sec_to_hms(t),
                    "departure_time": sec_to_hms(t),
                    "stop_id": stop["stop_id"],
                    "pickup_type": "0",
                    "drop_off_type": "0",
                }
            )
        new_rows[0]["pickup_type"], new_rows[0]["drop_off_type"] = "0", "1"  # new terminus
        origin["pickup_type"], origin["drop_off_type"] = "0", "0"  # REPUBBLICA now mid-route
        merged = new_rows + trip_stop_times
    else:
        terminus = trip_stop_times[-1]
        t0 = hms_to_sec(terminus["arrival_time"])
        new_rows = []
        for stop, cum in zip(new_stops, cum_minutes):
            t = t0 + cum * 60
            new_rows.append(
                {
                    "trip_id": terminus["trip_id"],
                    "arrival_time": sec_to_hms(t),
                    "departure_time": sec_to_hms(t),
                    "stop_id": stop["stop_id"],
                    "pickup_type": "0",
                    "drop_off_type": "0",
                }
            )
        new_rows[-1]["pickup_type"], new_rows[-1]["drop_off_type"] = "1", "0"  # new terminus
        terminus["pickup_type"], terminus["drop_off_type"] = "0", "0"  # REPUBBLICA now mid-route
        merged = trip_stop_times + new_rows

    for i, row in enumerate(merged, start=1):
        row["stop_sequence"] = str(i)
    return merged


def synthesize_metro_calendar(bus_calendar: list[dict]) -> list[dict]:
    """Re-declare FER/FEST (the metro's two service_ids) as plain weekday-flag services
    spanning the bus feed's own calendar window, instead of the metro source's own
    calendar_dates.txt exceptions (a different vintage/calendar year than the bus feed --
    see the module docstring). FER = Mon-Fri (feriale), FEST = Sun (festivo); the exact
    Saturday convention doesn't matter here since only the departure date used for routing
    (a weekday) needs to resolve correctly.
    """
    start_date = min(r["start_date"] for r in bus_calendar)
    end_date = max(r["end_date"] for r in bus_calendar)
    base = {"start_date": start_date, "end_date": end_date, "service_description": ""}
    return [
        {**base, "service_id": "FER", "monday": "1", "tuesday": "1", "wednesday": "1",
         "thursday": "1", "friday": "1", "saturday": "0", "sunday": "0"},
        {**base, "service_id": "FEST", "monday": "0", "tuesday": "0", "wednesday": "0",
         "thursday": "0", "friday": "0", "saturday": "0", "sunday": "1"},
    ]


def extend_shape(shape_pts: list[dict], new_stops: list[dict], mode: str) -> list[dict]:
    """Prepend/append the new stops as extra shape points (straight hops -- there is no
    surveyed polyline for the new segment, only stop locations)."""
    shape_pts = sorted(shape_pts, key=lambda r: int(r["shape_pt_sequence"]))
    shape_id = shape_pts[0]["shape_id"]
    ordered = list(reversed(new_stops)) if mode == "prepend" else new_stops

    if mode == "prepend":
        base_dist = 0.0
        prev_lat, prev_lon = float(ordered[0]["stop_lat"]), float(ordered[0]["stop_lon"])
        new_pts = []
        cum = 0.0
        for s in ordered:
            lat, lon = float(s["stop_lat"]), float(s["stop_lon"])
            if new_pts:
                cum += haversine_km(prev_lat, prev_lon, lat, lon)
            new_pts.append({"shape_id": shape_id, "shape_pt_lat": s["stop_lat"],
                             "shape_pt_lon": s["stop_lon"], "shape_dist_traveled": f"{cum:.3f}"})
            prev_lat, prev_lon = lat, lon
        # distance from the last new point to the original first shape point, then shift the
        # whole original polyline's cumulative distance forward by that much.
        tail_dist = haversine_km(prev_lat, prev_lon, float(shape_pts[0]["shape_pt_lat"]), float(shape_pts[0]["shape_pt_lon"]))
        offset = cum + tail_dist
        for p in shape_pts:
            p["shape_dist_traveled"] = f"{float(p['shape_dist_traveled']) + offset:.3f}"
        merged = new_pts + shape_pts
    else:
        last = shape_pts[-1]
        base = float(last["shape_dist_traveled"])
        prev_lat, prev_lon = float(last["shape_pt_lat"]), float(last["shape_pt_lon"])
        new_pts = []
        cum = base
        for s in ordered:
            lat, lon = float(s["stop_lat"]), float(s["stop_lon"])
            cum += haversine_km(prev_lat, prev_lon, lat, lon)
            new_pts.append({"shape_id": shape_id, "shape_pt_lat": s["stop_lat"],
                             "shape_pt_lon": s["stop_lon"], "shape_dist_traveled": f"{cum:.3f}"})
            prev_lat, prev_lon = lat, lon
        merged = shape_pts + new_pts

    for i, p in enumerate(merged, start=1):
        p["shape_pt_sequence"] = str(i)
    return merged


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bus-input", type=Path, default=here / "GTFS.zip")
    ap.add_argument("--metro-input", type=Path, default=here / "gtfs_metrocagliari.zip")
    ap.add_argument("-g", "--gpkg", type=Path,
                     default=Path("scenarios") / "new-metro" / "metro_cagliari.gpkg")
    ap.add_argument("-o", "--output", type=Path, default=here / "gtfs_new_metro.zip")
    args = ap.parse_args()

    print(f"Merging bus={args.bus_input.name} + metro={args.metro_input.name} "
          f"(+ new stops from {args.gpkg}) -> {args.output.name}\n")

    with zipfile.ZipFile(args.bus_input) as zf:
        bus_agency_fn, bus_agency = read_table(zf, "agency.txt")
        bus_routes_fn, bus_routes = read_table(zf, "routes.txt")
        bus_trips_fn, bus_trips = read_table(zf, "trips.txt")
        bus_st_fn, bus_stop_times = read_table(zf, "stop_times.txt")
        bus_stops_fn, bus_stops = read_table(zf, "stops.txt")
        bus_shapes_fn, bus_shapes = read_table(zf, "shapes.txt")
        bus_cal_fn, bus_calendar = read_table(zf, "calendar.txt")
        bus_caldates_fn, bus_calendar_dates = read_table(zf, "calendar_dates.txt")
        bus_feedinfo_fn, bus_feed_info = read_table(zf, "feed_info.txt")

    with zipfile.ZipFile(args.metro_input) as zf:
        metro_agency_fn, metro_agency = read_table(zf, "agency.txt")
        metro_routes_fn, metro_routes = read_table(zf, "routes.txt")
        metro_trips_fn, metro_trips = read_table(zf, "trips.txt")
        metro_st_fn, metro_stop_times = read_table(zf, "stop_times.txt")
        metro_stops_fn, metro_stops = read_table(zf, "stops.txt")
        metro_shapes_fn, metro_shapes = read_table(zf, "shapes.txt")

    stop_by_id = {s["stop_id"]: s for s in metro_stops}
    new_stops = load_new_stops(args.gpkg)
    print("New stops (REPUBBLICA outward):")
    for s in new_stops:
        print(f"  {s['stop_id']:10s} {s['stop_name']}")

    speed = average_speed_kmh(metro_shapes, metro_stop_times, metro_trips)
    hop_minutes = compute_hop_minutes(new_stops, stop_by_id, speed)
    print(f"\nAverage MCA1 speed (M_REP_POL): {speed:.1f} km/h")
    print(f"Hop minutes REPUBBLICA -> {' -> '.join(s['stop_name'] for s in new_stops)}: {hop_minutes}\n")

    by_trip: dict[str, list[dict]] = {}
    for s in metro_stop_times:
        by_trip.setdefault(s["trip_id"], []).append(s)

    trip_by_id = {t["trip_id"]: t for t in metro_trips}
    extended_prepend = extended_append = 0
    new_stop_times: list[dict] = []
    touched_shapes: dict[str, str] = {}  # shape_id -> "prepend"/"append"

    for trip_id, rows in by_trip.items():
        rows_sorted = sorted(rows, key=lambda r: int(r["stop_sequence"]))
        first_stop, last_stop = rows_sorted[0]["stop_id"], rows_sorted[-1]["stop_id"]
        trip = trip_by_id[trip_id]
        if trip["route_id"] == "MCA1" and first_stop == REPUBBLICA_FIRST:
            rows_sorted = extend_trip(rows_sorted, new_stops, hop_minutes, "prepend")
            extended_prepend += 1
            if trip.get("shape_id"):
                touched_shapes[trip["shape_id"]] = "prepend"
        elif trip["route_id"] == "MCA1" and last_stop == REPUBBLICA_LAST:
            rows_sorted = extend_trip(rows_sorted, new_stops, hop_minutes, "append")
            extended_append += 1
            if trip.get("shape_id"):
                touched_shapes[trip["shape_id"]] = "append"
        new_stop_times.extend(rows_sorted)

    print(f"Extended {extended_prepend} direction-1 trips (prepended) and "
          f"{extended_append} direction-0 trips (appended).\n")

    # Direction-0 trips ending in REPUBBLICA are now heading to STAZIONE.
    for t in metro_trips:
        if t["route_id"] == "MCA1" and touched_shapes.get(t.get("shape_id")) == "append":
            t["trip_headsign"] = "STAZIONE"

    for r in metro_routes:
        if r["route_id"] == "MCA1":
            r["route_long_name"] = "METROCAGLIARI STAZIONE - SAN GOTTARDO - POLICLINICO"

    shapes_by_id: dict[str, list[dict]] = {}
    for s in metro_shapes:
        shapes_by_id.setdefault(s["shape_id"], []).append(s)
    new_shapes: list[dict] = []
    for shape_id, pts in shapes_by_id.items():
        mode = touched_shapes.get(shape_id)
        new_shapes.extend(extend_shape(pts, new_stops, mode) if mode else pts)

    metro_calendar = synthesize_metro_calendar(bus_calendar)
    print(
        f"Metro FER/FEST re-dated to the bus feed's own calendar window: "
        f"{metro_calendar[0]['start_date']}..{metro_calendar[0]['end_date']}\n"
    )

    bus_agency_ids = {a.get("agency_id") for a in bus_agency}
    merged_agency = bus_agency + [a for a in metro_agency if a.get("agency_id") not in bus_agency_ids]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as zf:
        print("Wrote:")
        write_table(zf, "agency.txt", bus_agency_fn or metro_agency_fn, merged_agency)
        write_table(zf, "routes.txt", bus_routes_fn, bus_routes + metro_routes)
        write_table(zf, "trips.txt", bus_trips_fn, bus_trips + metro_trips)
        write_table(zf, "stop_times.txt", bus_st_fn, bus_stop_times + new_stop_times)
        write_table(zf, "stops.txt", bus_stops_fn, bus_stops + metro_stops + new_stops)
        if bus_shapes_fn or metro_shapes_fn:
            write_table(zf, "shapes.txt", bus_shapes_fn or metro_shapes_fn, bus_shapes + new_shapes)
        write_table(zf, "calendar.txt", bus_cal_fn, bus_calendar + metro_calendar)
        if bus_caldates_fn:
            write_table(zf, "calendar_dates.txt", bus_caldates_fn, bus_calendar_dates)
        if bus_feedinfo_fn:
            write_table(zf, "feed_info.txt", bus_feedinfo_fn, bus_feed_info)

    print(f"\nDone -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
