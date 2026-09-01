import csv
import geopandas as gpd
from shapely.geometry import Point
from shapely.prepared import prep
from tqdm import tqdm
import os

BPE_CSV = "Paris/BPE25.csv"
BOUNDARY_GPKG = "shapefile_base/mgp_boundary_expanded.gpkg"
BUFFER_LAYER = "expanded_buffer_15km"
OUT_SHP = "Paris/POI_point2.shp"

KEEP_COLUMNS = ["NOMRS", "TYPEQU", "DOM", "SDOM", "LIBCOM", "SIRET"]


class _ByteTrackingTextFile:
    def __init__(self, fileobj):
        self._f = fileobj
        self.bytes_read = 0

    def __iter__(self):
        return self

    def __next__(self):
        line = next(self._f)
        self.bytes_read += len(line.encode("utf-8"))
        return line


def main():
    buffer_gdf = gpd.read_file(BOUNDARY_GPKG, layer=BUFFER_LAYER)
    buffer_polygon = prep(buffer_gdf.geometry.iloc[0])

    total_bytes = os.path.getsize(BPE_CSV)
    rows = []

    with open(BPE_CSV, encoding="utf-8") as f:
        tracked = _ByteTrackingTextFile(f)
        reader = csv.DictReader(tracked, delimiter=";", quotechar='"')
        pbar = tqdm(total=total_bytes, unit="B", unit_scale=True, desc="Scanning BPE25.csv")
        last_reported = 0
        for i, row in enumerate(reader):
            try:
                lon = float(row["LONGITUDE"])
                lat = float(row["LATITUDE"])
            except (TypeError, ValueError):
                continue
            point = Point(lon, lat)
            if buffer_polygon.contains(point):
                record = {col: row.get(col) for col in KEEP_COLUMNS}
                record["geometry"] = point
                rows.append(record)
            if i % 100_000 == 0:
                pbar.update(tracked.bytes_read - last_reported)
                last_reported = tracked.bytes_read
        pbar.update(tracked.bytes_read - last_reported)
        pbar.close()

    out_gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    out_gdf.to_file(OUT_SHP, driver="ESRI Shapefile")
    print(f"Wrote {len(out_gdf)} points inside the buffer to {OUT_SHP}")


if __name__ == "__main__":
    main()
