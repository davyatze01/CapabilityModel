"""Offline basemap layers for the capability GeoPackage.

The QGIS project's only background is an online XYZ layer, which stays blank wherever
tile servers can't be reached. These layers come from data the pipeline already has --
the drive graph's main roads, the water mask, the hex grid's footprint -- and are stored
in the GeoPackage itself, clipped to the study area.
"""
import os
import xml.etree.ElementTree as ET

import geopandas as gpd
import shapely
from tqdm import tqdm

from utils.graphml import _xml_local_name

# OSM highway classes drawn (with their *_link ramps), in the two widths the style uses.
MAJOR_ROADS = {"motorway", "trunk", "primary"}
MINOR_ROADS = {"secondary", "tertiary"}


def _road_class(highway: str | None) -> str | None:
    """'major' / 'minor' for a graphml highway value (possibly a list repr), else None."""
    if not highway:
        return None
    tags = {t.strip().removesuffix("_link") for t in highway.strip("[]").replace("'", "").split(",")}
    if tags & MAJOR_ROADS:
        return "major"
    if tags & MINOR_ROADS:
        return "minor"
    return None


def _read_main_roads(graphml_path: str) -> gpd.GeoDataFrame:
    """Stream the drive graph and keep only main-road edges, as straight node-to-node lines.

    No NetworkX graph is built: the graphs are unsimplified, so every edge is a straight
    segment between two nodes, and node coordinates are all that is needed.
    """
    key_to_attr: dict[str, str] = {}
    coords: dict[str, tuple[float, float]] = {}
    seen: set[frozenset] = set()
    classes, lines = [], []
    total = os.path.getsize(graphml_path)
    with open(graphml_path, "rb") as f, \
            tqdm(total=total, unit="B", unit_scale=True, desc="[Basemap] drive graph") as bar:
        graph_elem = None
        for count, (event, elem) in enumerate(ET.iterparse(f, events=("start", "end"))):
            name = _xml_local_name(elem.tag)
            if event == "start":
                if name == "graph":
                    graph_elem = elem
                continue
            if name == "key":
                key_to_attr[elem.attrib.get("id")] = elem.attrib.get("attr.name")
            elif name in ("node", "edge"):
                data = {key_to_attr.get(c.attrib.get("key")): c.text for c in elem}
                if name == "node":
                    try:
                        coords[elem.attrib["id"]] = (float(data["x"]), float(data["y"]))
                    except (KeyError, TypeError, ValueError):
                        pass
                else:
                    u, v = elem.attrib.get("source"), elem.attrib.get("target")
                    road_class = _road_class(data.get("highway"))
                    pair = frozenset((u, v))
                    if road_class and pair not in seen and u in coords and v in coords:
                        seen.add(pair)
                        classes.append(road_class)
                        lines.append(shapely.LineString([coords[u], coords[v]]))
                # Drop processed elements so the tree doesn't grow with the file.
                elem.clear()
                if graph_elem is not None:
                    graph_elem.remove(elem)
            if count % 200_000 == 0:
                bar.update(f.tell() - bar.n)
        bar.update(total - bar.n)
    return gpd.GeoDataFrame({"road_class": classes}, geometry=lines, crs="EPSG:4326")


def write_offline_basemap(gpkg_path, drive_graphml_path: str, water_path: str) -> None:
    """Write basemap_area / basemap_water / basemap_roads into gpkg_path, clipped to the
    hex grid's footprint (the area the results cover)."""
    grid = gpd.read_file(gpkg_path, layer="zz_capability_grid")
    print(f"[Basemap] Study area from {len(grid)} hexagons", flush=True)
    area = gpd.GeoDataFrame(geometry=[shapely.union_all(grid.geometry.values)], crs=grid.crs)

    roads = gpd.clip(_read_main_roads(drive_graphml_path).to_crs(grid.crs), area)
    # One multi-line per class instead of ~160k two-point segments: far fewer rows to draw.
    roads = roads.dissolve(by="road_class", as_index=False)
    roads["geometry"] = shapely.line_merge(roads.geometry.values)
    layers = [("basemap_area", area), ("basemap_roads", roads)]

    if os.path.exists(water_path):
        water = gpd.clip(gpd.read_file(water_path).to_crs(grid.crs), area)
        layers.append(("basemap_water", water))
    else:
        print(f"[Basemap] No water mask at {water_path}; skipping basemap_water", flush=True)

    for name, gdf in layers:
        gdf.to_file(gpkg_path, layer=name, driver="GPKG")
        print(f"[Basemap] Wrote {name} ({len(gdf)} features)", flush=True)
