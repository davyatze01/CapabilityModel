"""Join every Cagliari routing origin to its OMI zone and price.

Spatial-joins the 1563 origin points in Cagliari_origins.gpkg against the 22 OMI
zone polygons in Cagliari_OMI.gpkg (point-in-polygon on CODZONA), and writes one
row per origin with its coordinates, zone code/label, its buy/rent price -- both
normalized to €/sqm/month so they're on the same time basis: buy uses
omi_sale_monthly (the sale price amortized into a mortgage installment -- see
fetch_omi_prices.py), rent uses omi_rent_final (already monthly) -- and the
housing_capability ELECTRE TRI category (Q1..Q5) that origin's own buy/rent prices
classify into. Origins that fall in the gaps between zone polygons (not inside
any zone) are assigned to whichever zone's centroid is nearest, rather than being
dropped. Only origins inside a zone with no price data at all (e.g. E5) are
dropped, since there's no other zone's price that would legitimately apply to them.

Usage: python housing/generate_cagliari_nodes_omi.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running directly -- see housing_affordability.py for why this is needed.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import geopandas as gpd
import pandas as pd

from housing.housing_capability import classify_housing_opportunities

HOUSING_DIR = Path(__file__).resolve().parent
ORIGINS_PATH = HOUSING_DIR / "Cagliari_origins.gpkg"
OMI_PATH = HOUSING_DIR / "Cagliari_OMI.gpkg"
OUT_PATH = HOUSING_DIR / "cagliari_nodes_omi.csv"


def main() -> None:
    origins = gpd.read_file(ORIGINS_PATH)
    print(f"[load] {ORIGINS_PATH} ({len(origins)} origins)", flush=True)

    omi = gpd.read_file(OMI_PATH)
    print(f"[load] {OMI_PATH} ({len(omi)} zones)", flush=True)

    omi_wgs84 = omi.to_crs(origins.crs)[["CODZONA", "Name", "omi_sale_monthly", "omi_rent_final", "geometry"]]
    joined = gpd.sjoin(origins, omi_wgs84, how="left", predicate="within")

    zone_centroids = omi.set_index("CODZONA").geometry.centroid  # metric CRS, not lat/lon
    origins_proj = origins.to_crs(omi.crs).geometry

    # A handful of origins land in two zones at once -- the OMI polygon mirror has
    # tiny boundary-slice overlaps between adjacent zones (imprecise digitization,
    # not a real double-assignment). Break ties by keeping whichever zone's
    # centroid the origin is actually closer to.
    dupe_mask = joined["node_id"].duplicated(keep=False) & joined["CODZONA"].notna()
    if dupe_mask.any():
        n_dupe_nodes = joined.loc[dupe_mask, "node_id"].nunique()
        print(f"[warn] {n_dupe_nodes} origins matched multiple overlapping zones; keeping the nearer centroid", flush=True)
        joined["_centroid_dist"] = [
            origins_proj.loc[idx].distance(zone_centroids[zone]) if pd.notna(zone) else float("inf")
            for idx, zone in zip(joined.index, joined["CODZONA"])
        ]
        joined = joined.sort_values("_centroid_dist").drop_duplicates(subset="node_id", keep="first").sort_index()
        joined = joined.drop(columns="_centroid_dist")

    # Origins in the gaps between zone polygons (not inside any zone) get assigned
    # to whichever zone's centroid is nearest, instead of being dropped -- same
    # "closest zone wins" rule as the overlap tie-break above, just for zero
    # matches instead of multiple.
    unmatched_mask = joined["CODZONA"].isna()
    if unmatched_mask.any():
        n_unmatched = int(unmatched_mask.sum())
        print(f"[warn] {n_unmatched} origins fall outside every OMI zone; assigning nearest zone by centroid", flush=True)
        zone_attrs = omi_wgs84.set_index("CODZONA")[["Name", "omi_sale_monthly", "omi_rent_final"]]
        for idx in joined[unmatched_mask].index:
            pt = origins_proj.loc[idx]
            nearest_zone = zone_centroids.apply(pt.distance).idxmin()
            joined.loc[idx, ["CODZONA", "Name", "omi_sale_monthly", "omi_rent_final"]] = (
                nearest_zone,
                zone_attrs.loc[nearest_zone, "Name"],
                zone_attrs.loc[nearest_zone, "omi_sale_monthly"],
                zone_attrs.loc[nearest_zone, "omi_rent_final"],
            )

    no_price = int(joined["omi_sale_monthly"].isna().sum())
    if no_price:
        print(f"[warn] dropping {no_price}/{len(joined)} origins in a zone with no price data", flush=True)
    joined = joined[joined["omi_sale_monthly"].notna()]

    out = joined[["node_id", "lat", "lon", "CODZONA", "Name", "omi_sale_monthly", "omi_rent_final"]].rename(
        columns={
            "CODZONA": "omi_zone",
            "Name": "omi_zone_label",
            "omi_sale_monthly": "buy_price_eur_sqm_month",
            "omi_rent_final": "rent_price_eur_sqm_month",
        }
    )
    out["housing_category"] = [
        classify_housing_opportunities({"buying": buy, "renting": rent})[1]
        for buy, rent in zip(out["buy_price_eur_sqm_month"], out["rent_price_eur_sqm_month"])
    ]
    out.to_csv(OUT_PATH, index=False)
    print(f"[done] wrote {len(out)} rows to {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
