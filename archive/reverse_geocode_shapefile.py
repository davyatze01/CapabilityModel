from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
from tqdm import tqdm


DEFAULT_USER_AGENT = "CapabilityModelReverseGeocoder/1.0"
DEFAULT_SLEEP_SECONDS = 1.0
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reverse-geocode node points from a shapefile with Nominatim and "
            "write the results to a CSV."
        )
    )
    parser.add_argument(
        "input_shapefile",
        type=Path,
        help="Path to the input shapefile containing node_id and point geometry.",
    )
    parser.add_argument(
        "output_csv",
        type=Path,
        help="Path to the output CSV file.",
    )
    parser.add_argument(
        "--node-column",
        default="node_id",
        help="Name of the column containing the node identifier. Default: node_id",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=DEFAULT_SLEEP_SECONDS,
        help="Minimum delay between Nominatim requests. Default: 1.0",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help=(
            "User-Agent header sent to Nominatim. Use a descriptive value and "
            "include contact information if possible."
        ),
    )
    parser.add_argument(
        "--email",
        default="",
        help="Optional email parameter forwarded to Nominatim.",
    )
    return parser.parse_args()


def _ensure_wgs84(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.crs is None:
        raise ValueError(
            "Input shapefile has no CRS defined. Reverse geocoding requires WGS84 coordinates."
        )
    if gdf.crs.to_string().upper() in {"EPSG:4326", "WGS84"}:
        return gdf
    return gdf.to_crs(epsg=4326)


def _geometry_to_lon_lat(geometry: Any) -> tuple[float, float]:
    if geometry is None or geometry.is_empty:
        raise ValueError("Geometry is empty")

    if isinstance(geometry, Point):
        return float(geometry.x), float(geometry.y)

    point = geometry.representative_point()
    return float(point.x), float(point.y)


def _reverse_geocode(
    lon: float,
    lat: float,
    *,
    user_agent: str,
    email: str = "",
    timeout: float = 30.0,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "lat": lat,
        "lon": lon,
        "format": "jsonv2",
        "addressdetails": 1,
    }
    if email:
        params["email"] = email

    url = f"{NOMINATIM_REVERSE_URL}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})

    with urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if isinstance(data, dict):
        return data
    raise ValueError("Unexpected Nominatim response format")


def _structured_address(address: dict[str, Any] | None) -> dict[str, str]:
    address = address or {}
    keys = [
        "road",
        "house_number",
        "neighborhood",
        "suburb",
    ]
    out = {
        "road": str(address.get("road", "")),
        "house_number": str(address.get("house_number", "")),
        "neighborhood": str(
            address.get("neighborhood", address.get("neighbourhood", ""))
        ),
        "suburb": str(address.get("suburb", "")),
    }
    return {key: out[key] for key in keys}


def reverse_geocode_shapefile(
    input_shapefile: str | Path,
    output_csv: str | Path,
    *,
    node_column: str = "node_id",
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    email: str = "",
) -> Path:
    input_shapefile = Path(input_shapefile)
    output_csv = Path(output_csv)

    if not input_shapefile.exists():
        raise FileNotFoundError(f"Input shapefile not found: {input_shapefile}")
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    gdf = gpd.read_file(input_shapefile)
    if gdf.empty:
        raise ValueError(f"Input shapefile has no rows: {input_shapefile}")
    if node_column not in gdf.columns:
        raise ValueError(
            f"Column '{node_column}' not found in {input_shapefile.name}. "
            "The shapefile must contain node identifiers."
        )
    if "geometry" not in gdf.columns:
        raise ValueError("Input shapefile does not contain geometry.")

    gdf = _ensure_wgs84(gdf)
    gdf = gdf.copy()
    gdf[node_column] = gdf[node_column].astype(str)

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    unique_rows = gdf[[node_column, "geometry"]].drop_duplicates(subset=[node_column])

    total_requests = len(unique_rows)
    progress = tqdm(
        total=total_requests,
        desc="Reverse geocoding",
        unit="node",
        file=sys.stderr,
        mininterval=1.0,
        dynamic_ncols=True,
    )

    last_request_time = 0.0
    for _, record in unique_rows.iterrows():
        node_id = str(record[node_column])
        if node_id in seen:
            continue
        seen.add(node_id)

        try:
            lon, lat = _geometry_to_lon_lat(record.geometry)
        except Exception as exc:
            rows.append(
                {
                    "node_id": node_id,
                    **{key: "" for key in _structured_address({}).keys()},
                }
            )
            progress.update(1)
            continue

        elapsed = time.monotonic() - last_request_time
        if last_request_time > 0 and elapsed < sleep_seconds:
            time.sleep(sleep_seconds - elapsed)

        try:
            payload = _reverse_geocode(
                lon,
                lat,
                user_agent=user_agent,
                email=email,
            )
            last_request_time = time.monotonic()
            address = _structured_address(payload.get("address"))
            rows.append(
                {
                    "node_id": node_id,
                    **address,
                }
            )
            progress.update(1)
            progress.set_postfix_str(f"node_id={node_id}")
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            last_request_time = time.monotonic()
            rows.append(
                {
                    "node_id": node_id,
                    **{key: "" for key in _structured_address({}).keys()},
                }
            )
            progress.update(1)
            progress.set_postfix_str(f"node_id={node_id} failed")

    progress.close()
    output_frame = pd.DataFrame(
        rows,
        columns=["node_id", "road", "house_number", "neighborhood", "suburb"],
    )
    output_frame.to_csv(output_csv, index=False, quoting=csv.QUOTE_MINIMAL)
    return output_csv


if __name__ == "__main__":
    reverse_geocode_shapefile(
        r"C:\Users\mocci\Desktop\PhD\II\CapabilityModel\outputs\shapefiles\Cagliari_Shapefile\Cagliari_Shapefile.shp",
        r"outputs\Cagliari_Shapefile_addresses.csv",
    )
