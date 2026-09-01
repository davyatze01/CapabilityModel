import geopandas as gpd

SHP_PATH = "shapefile_base/mgp_boundary.shp"
OUT_GPKG = "shapefile_base/mgp_boundary_expanded.gpkg"
BUFFER_M = 15_000.0

def main():
    gdf = gpd.read_file(SHP_PATH)
    if gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    utm_crs = gdf.estimate_utm_crs()
    original_polygon = gdf.to_crs(utm_crs).unary_union
    buffered_polygon = original_polygon.buffer(BUFFER_M)

    original_gdf = gpd.GeoDataFrame(geometry=[original_polygon], crs=utm_crs).to_crs(epsg=4326)
    buffered_gdf = gpd.GeoDataFrame(geometry=[buffered_polygon], crs=utm_crs).to_crs(epsg=4326)

    original_gdf.to_file(OUT_GPKG, layer="case_study_area", driver="GPKG")
    buffered_gdf.to_file(OUT_GPKG, layer="expanded_buffer_15km", driver="GPKG")
    print(f"Wrote {OUT_GPKG} with layers 'case_study_area' and 'expanded_buffer_15km'")

if __name__ == "__main__":
    main()
