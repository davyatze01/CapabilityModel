from pyrosm import OSM
import geopandas
import shapely
import matplotlib.pyplot as plt

pbf_path = "pbf_files/cagliari-latest.osmv2.pbf"   # your .osm.pbf file
osm = OSM(pbf_path)
 
# Examples of common layers
roads = osm.get_network(network_type="all")
ax = roads.plot(figsize=(9,9), linewidth=0.6, color="black")
ax.set_axis_off()



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

origins.plot( ax = ax, marker="*" , color="crimson", markersize=70,zorder=6)
plt.show()
