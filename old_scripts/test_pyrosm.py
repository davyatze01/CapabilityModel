from pyrosm import OSM
import geopandas as gpd
import matplotlib.pyplot as plt

# Percorso al file PBF di Cagliari
pbf_file = "cagliari.pbf"

# Carica il file PBF con pyrosm
osm = OSM(pbf_file)

# Estrai i confini (relazioni di tipo boundary)
boundaries = osm.get_boundaries()

# boundaries è un GeoDataFrame, puoi filtrarne uno specifico
print(boundaries.head())

# Plotta i confini
boundaries.plot(edgecolor='red', facecolor='none')
plt.title("Confini amministrativi di Cagliari")
plt.show()
