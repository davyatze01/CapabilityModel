import geopandas as gpd
import matplotlib.pyplot as plt

# Carica lo shapefile
shape = gpd.read_file('./ca_shapefile/ca_shapefile.shp')

# Controlla che sia stato caricato
print(shape.head())
print(shape.crs)

# Plot
shape.plot(edgecolor="black")

plt.title("Cagliari Shapefile")
plt.xlabel("Longitude")
plt.ylabel("Latitude")
plt.show()