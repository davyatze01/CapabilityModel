import ast
from pathlib import Path

import pandas as pd


config_dir = Path(__file__).resolve().parents[1] / "config"
poi_df = pd.read_csv(config_dir / "poi_types.csv")
services_df = pd.read_csv(config_dir / "services.csv")

valid_poi_types = set(poi_df["poi_type"].astype(str))
declared_pairs = set()
for _, row in poi_df.iterrows():
    poi_type = str(row["poi_type"])
    for service in ast.literal_eval(row["services"]):
        declared_pairs.add((str(service), poi_type))

configured_pairs = set()
for _, row in services_df.iterrows():
    service = str(row["service"])
    poi_types = ast.literal_eval(row["poi_types"])
    caps = ast.literal_eval(row["choquet_capacity"])
    contribs_raw = row.get("contribution_coefficient") or row.get("contribution_constant")
    contribs = ast.literal_eval(contribs_raw)
    if len(poi_types) != len(caps) or len(poi_types) != len(contribs):
        raise ValueError(f"Length mismatch in services.csv for service={service!r}")
    for poi_type in poi_types:
        poi = str(poi_type)
        if poi not in valid_poi_types:
            raise ValueError(f"Unknown poi_type in services.csv: {poi!r}")
        configured_pairs.add((service, poi))

missing_in_services = sorted(declared_pairs - configured_pairs)
extra_in_services = sorted(configured_pairs - declared_pairs)
if missing_in_services or extra_in_services:
    raise ValueError(
        "Mismatch between poi_types.csv and services.csv.\n"
        f"Missing in services.csv: {missing_in_services}\n"
        f"Extra in services.csv: {extra_in_services}"
    )

print("services.csv consistency check passed.")
