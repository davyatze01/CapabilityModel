import pandas as pd
from sklearn.metrics.pairwise import haversine_distances
from math import radians

PATH_FILE = "./outputs/capability_to_eat.csv"

dati_capability_to_eat = []

colonne = [
    "lat",
    "lon",
    "capability_to_eat"
]

# node_id,lat,lon,capability_to_eat,dining_out_service,on_the_go_service

df = pd.read_csv(PATH_FILE, usecols=colonne)

"""df["latitudine"] = df["lat"].round(3)
df["longitudine"] = df["lon"].round(3)

 md = df.pivot_table(
    index="latitudine", 
    columns="longitudine", 
    values="capability_to_eat",
    aggfunc="mean"
) """

sns.kdeplot(
    data=df,
    x="lon",
    y="lat",
    weights="capability_to_eat",  # L'intensità dipende dal tuo valore
    fill=True,                    # Riempie di colore
    cmap="rocket",                # O 'viridis', 'flare', etc.
    thresh=0.00001,                  # Taglia i valori di sfondo troppo bassi
    alpha=0.8                     # Trasparenza
)

""" print(md.head(10))
sns.set_theme()

f,ax = plt.subplots(figsize=(10,8))
sns.heatmap(md) """

plt.show()





