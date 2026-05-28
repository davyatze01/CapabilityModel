import os
import osmnx as ox

# Tags POI
tags = {
    "amenity": True,
    "shop": True,
    "tourism": True
}

# Download POI
pois = ox.features_from_place(
    "Cagliari, Sardinia, Italy",
    tags
)

# Creazione campo poiType
def extract_poi_type(row):
    for col in ["amenity", "shop", "tourism", "building"]:
        value = row.get(col)

        if value is not None:
            return str(value)

    return "unknown"

pois["poi_type"] = pois.apply(extract_poi_type, axis=1)

# Separazione geometrie
points = pois[pois.geometry.geom_type == "Point"].copy()

lines = pois[
    pois.geometry.geom_type.isin(
        ["LineString", "MultiLineString"]
    )
].copy()

polygons = pois[
    pois.geometry.geom_type.isin(
        ["Polygon", "MultiPolygon"]
    )
].copy()

# Creazione cartella output
os.makedirs("pois_shp", exist_ok=True)

# Export shapefile
points.to_file("pois_shp/poi_points.shp")

lines.to_file("pois_shp/poi_lines.shp")

polygons.to_file("pois_shp/poi_polygons.shp")

print("[POI] Shapefile export completed")
