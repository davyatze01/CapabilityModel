import r5py
import geopandas
import shapely
import matplotlib.pyplot as plt

# ---------------------------
# Percorsi ai file
# ---------------------------
osm_path = 'pbf_files/cagliari-latest.osmv2.pbf'
gtfs_path = 'gtfs/GTFS.zip'

# ---------------------------
# Crea la rete di trasporto (solo strade e trasporto pubblico)
# ---------------------------
network = r5py.TransportNetwork(osm_path, [gtfs_path])

print(type(network))

POSTO_1 = shapely.Point(9.100524, 39.232183)

origins = geopandas.GeoDataFrame(
    {
        "id": [1, 2],
        "geometry": [
            shapely.Point(9.100524, 39.232183),
            shapely.Point(9.099051, 39.236679),
        ],
    },
    crs="EPSG:4326",
)

origins["snapped_geometry"] = network.snap_to_network(origins["geometry"])

origins.plot()

plt.show()