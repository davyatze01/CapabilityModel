"""Export the routing origin points (walk-graph nodes used to start routing) for a
study city to a standalone GeoPackage. Reads from the cached walk CSR bundle via
build_context(), so no routing/recomputation happens -- just a fast reconstruction
of the same node list every pipeline run uses as origins.

Run with: python tools/export_origins_gpkg.py
"""

import os

# Which study city's origins to export -- matches core.config.PipelineConfig.study_city.
STUDY_CITY = "cagliari"

# Where to write the output GeoPackage.
OUTPUT_PATH = os.path.join("outputs", "gpkg", "Cagliari", "Cagliari_origins.gpkg")


def main() -> None:
    import geopandas as gpd
    from shapely.geometry import Point

    from core.config import PipelineConfig
    from core.context import build_context

    cfg = PipelineConfig(study_city=STUDY_CITY)
    print(f"[Origins Export] study_city={cfg.study_city} city_name={cfg.city_name}", flush=True)

    ctx = build_context(cfg)
    nodes_with_coords = ctx.nodes_with_coords
    print(f"[Origins Export] {len(nodes_with_coords)} origin nodes", flush=True)

    rows = []
    for node_id, data in nodes_with_coords:
        rows.append(
            {
                "node_id": node_id,
                "hex_id": data.get("hex_id"),
                "lon": data["x"],
                "lat": data["y"],
                "geometry": Point(data["x"], data["y"]),
            }
        )

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    gdf.to_file(OUTPUT_PATH, driver="GPKG", layer="origins")
    print(f"[Origins Export] Wrote {len(gdf)} origin points -> {OUTPUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
