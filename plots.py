import pandas as pd
from sklearn.metrics.pairwise import haversine_distances
from math import radians
import os
import csv
import json
from tqdm import tqdm

PATH_FILE = "./outputs/capability_to_eat.csv"
FILE_IS_NEW = True
ACCESSIBILITY_TYPE = "capability_to_eat"
k = 50 # ogni quanti metri aggiornare i punteggi
NEW_FILE_PATH = f"./plots/csv/{ACCESSIBILITY_TYPE}_{k}.csv"

colonne = [
    "node_id",
    "lat",
    "lon",
    ACCESSIBILITY_TYPE
]

# node_id,lat,lon,capability_to_eat,dining_out_service,on_the_go_service

df = pd.read_csv(PATH_FILE, usecols=colonne)
df = df.reset_index(drop=True)
nodes = df[["lat","lon",ACCESSIBILITY_TYPE]].values

file_exists = os.path.exists(NEW_FILE_PATH)

with open(NEW_FILE_PATH, "a", newline="") as f:
    writer = csv.writer(f)

    # scrivi header solo se file nuovo
    if not file_exists:
        writer.writerow(["id","node1","node2","scores"])

    for i in tqdm(range(len(nodes)), desc="Processing nodes"):

        lat1, lon1, score1 = nodes[i]
        n1_rad = [radians(lat1), radians(lon1)]

        for j in range(i+1, len(nodes)):
            lat2, lon2, score2 = nodes[j]
            n2_rad = [radians(lat2), radians(lon2)]

            L = haversine_distances([n1_rad, n2_rad])[0][1] * 6371000

            if L == 0:
                continue

            # punteggio k = pA + (pB - pA) * (distanza k da a - posizione di a)/ distanza tra b e a

            dist = 0
            scores = []
            pos1 = [lat1, lon1]
            pos2 = [lat2, lon2]

            while dist < L:
                if dist == 0:
                    scores.append(score1)
                else:
                    scoreK = score1 + (score2 - score1) * (dist/L)
                    scores.append(scoreK)

                dist += k

            scores.append(score2)
            writer.writerow([
                f"{i}_{j}",
                json.dumps(pos1),
                json.dumps(pos2),
                json.dumps(scores)
            ])







