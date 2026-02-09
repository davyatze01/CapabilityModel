import csv
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
import numpy as np

PATH_FILE = "./outputs/capability_to_eat.csv"

dati_capability_to_eat = []

colonne = [
    "node_id",
    "lat",
    "lon",
    "capability_to_eat"
]

# node_id,lat,lon,capability_to_eat,dining_out_service,on_the_go_service

df = pd.read_csv(PATH_FILE, usecols=colonne)
print(df.head(10))


import numpy as np
import matplotlib.pyplot as plt

# numero di celle per asse (regola la dimensione dei quadrati)
GRID_SIZE = 10  

# coordinate
x = df["lon"].values
y = df["lat"].values
values = df["capability_to_eat"].values

# somma pesata
heatmap, xedges, yedges = np.histogram2d(
    x, y,
    bins=GRID_SIZE,
    weights=values
)

# conteggio punti per cella
counts, _, _ = np.histogram2d(x, y, bins=GRID_SIZE)

# media per cella (evita divisioni per 0)
heatmap = np.divide(
    heatmap, counts,
    out=np.zeros_like(heatmap),
    where=counts != 0
)


plt.figure(figsize=(10, 8))

plt.pcolormesh(
    xedges,
    yedges,
    heatmap.T,
    cmap="viridis",
    edgecolors="black",
    linewidth=1,
    shading="flat"
)

plt.colorbar(label="Capability to eat (media)")
plt.xlabel("Longitude")
plt.ylabel("Latitude")
plt.title("Heatmap a griglia – Accessibilità POI Cagliari")

plt.show()





