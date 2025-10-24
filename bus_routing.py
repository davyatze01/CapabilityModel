import r5py
import geopandas
import shapely
import datetime
import folium
import folium.plugins
import pandas

# Recupero la path del pbf di Cagliari e del GTFS di Ctm
CA_PATH = "pbf_files/cagliari-latest.osmv2.pbf"
GTFS_PATH = "gtfs/GTFS.zip"

# Prendo due posizioni

ORIGIN = shapely.Point(9.110754, 39.235687 )

DESTINATION = shapely.Point(9.1235628871721, 39.22348159436033)

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
            ORIGIN
        ],
    },
    crs="EPSG:4326",
)

# Creo un GeoDataFrame con i punti di destinazione

destinations = geopandas.GeoDataFrame(
    {
        "id": [2],
        "geometry": [
            DESTINATION
        ],
    },
    crs="EPSG:4326",
)

# Creo la matrice di viaggio

travel_times = r5py.TravelTimeMatrix(
    ctm_network_cagliari,
    origins=origins,
    destinations=destinations,
    departure = datetime.datetime(2025, 10, 27, 10, 22),
    transport_modes = [
        r5py.TransportMode.BUS
    ],
    snap_to_network= True,
)

detailed_itineraries = r5py.DetailedItineraries(
    ctm_network_cagliari,
    origins=origins,
    destinations=destinations,
    departure= datetime.datetime(2025, 10, 27, 10, 22),
    transport_modes=[r5py.TransportMode.BUS],
    snap_to_network=True,
)

print(travel_times)

print("------------------------")

print(detailed_itineraries)

print("-----------------------------------")

detailed_itineraries["mode"] = detailed_itineraries.transport_mode.astype(str)
detailed_itineraries["travel time (min)"] = detailed_itineraries.travel_time.apply(
    lambda t: round(t.total_seconds() / 60.0, 2)
)
detailed_itineraries["trip"] = detailed_itineraries.apply(
    lambda row: f"{row.from_id} → destination",
    axis=1
)

detailed_routes_map = (
    detailed_itineraries[
        [
            "geometry",
            "distance",
            "mode",
            "travel time (min)",
            "from_id",
            "to_id",
            "trip",
            "option",
            "segment",
        ]
    ]
    .explore(
        tooltip=["trip", "option", "segment", "mode", "travel time (min)", "distance"],
        column="mode",
        tiles="CartoDB.Positron",
        style_kwds={
            "weight": 3,
            "opacity": 0.8,
        },
        highlight_kwds={
            "weight": 6,
            "opacity": 1,
        },

    )
)