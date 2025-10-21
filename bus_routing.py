import r5py
import geopandas
import geopandas
import shapely
import datetime

# Recupero la path del pbf di Cagliari e del GTFS di Ctm
CA_PATH = "pbf_files/cagliari-latest.osmv2.pbf"
GTFS_PATH = "gtfs/GTFS.zip"

# Prendo due posizioni

# Origine A = 9.110754, 39.235687 

# Destinazione B = 9.1235628871721, 39.22348159436033

# Creo la transport network dei bus di Cagliari
# utilizzando il gtfs fornito da CTM Spa

ctm_network_cagliari = r5py.TransportNetwork(
    CA_PATH,
    [
        GTFS_PATH
    ]
)

# print(type(ctm_network_cagliari))


# Creo un GeoDataFrame con i punti di partenza

origins = geopandas.GeoDataFrame(
    {
        "id": [1],
        "geometry": [
            shapely.Point(9.110754, 39.235687)
        ],
    },
    crs="EPSG:4326",
)

# Creo un GeoDataFrame con i punti di destinazione

destinations = geopandas.GeoDataFrame(
    {
        "id": [1],
        "geometry": [
            shapely.Point(9.123562, 39.223481)
        ],
    },
    crs="EPSG:4326",
)

# Creo la matrice di viaggio

travel_times = r5py.TravelTimeMatrix(
    ctm_network_cagliari,
    origins=origins,
    destinations=destinations,
    departure = datetime.datetime(2025, 10, 22, 10, 22),
    transport_modes = [
        r5py.TransportMode.BUS,
        r5py.TransportMode.WALK,
    ],
    snap_to_network= True,
)

print(travel_times)