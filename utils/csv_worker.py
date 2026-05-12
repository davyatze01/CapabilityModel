import pandas as pd
import ast
from collections import defaultdict

df = pd.read_csv(f"config/poi_types.csv")

df_services = pd.read_csv("service_new.csv")

target_services = df_services["service"]

service_map = defaultdict(lambda: {
    "poi_types": [],
    "choquet_capacity": [],
    "contribution_constant": []
})

for _,row in df.iterrows():
    poi_type = row["poi_type"]

    services = ast.literal_eval(row["services"])
    choquet = ast.literal_eval(row["choquet_capacity"])
    contrib = ast.literal_eval(row["contribution_constant"])

    for i,service in enumerate(services):
        service_map[service]["poi_types"].append(poi_type)
        service_map[service]["choquet_capacity"].append(choquet[i])
        service_map[service]["contribution_constant"].append(contrib[i])
    
rows = []
        
for service, data in service_map.items():

    rows.append({
        "service": service,
        "poi_types": data["poi_types"],
        "choquet_capacity": data["choquet_capacity"],
        "contribution_constant": data["contribution_constant"]
    })

result_df = pd.DataFrame(rows)

result_df.to_csv("services_new2.csv", index=False)