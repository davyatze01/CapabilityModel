import os
from collections import Counter, OrderedDict
import geopandas as gpd
import pandas as pd
import osmnx as ox

CAP_EAT_IDX = {"dining_out": 0, "on_the_go": 1}

def cap_eat(S):
    S = frozenset(S)  # normalize input (works for list, set, tuple)
    if not S:
        return 0.0
    if CAP_EAT_IDX["dining_out"] in S:
        if CAP_EAT_IDX["on_the_go"] in S:
            return 1.0
        else:
            return 0.6
    else:
        return 0.4


POI_TAG_KEYS = {
    "amenity",
    "shop",
    "tourism",
    "leisure",
    "craft",
    "office",
    "healthcare",
    "historic",
    "sport",
    "education",
}


def poi_tag_value_recap(
    poi_dir="poi",
    exclude_cols=None,
    poi_keys=None,
):
    if exclude_cols is None:
        exclude_cols = {
            "geometry",
            "name",
            "id",
            "osmid",
            "osm_id",
            "element_type",
            "timestamp",
            "version",
            "changeset",
            "uid",
            "user",
        }

    if not os.path.isdir(poi_dir):
        return OrderedDict()

    frames = []
    for name in os.listdir(poi_dir):
        if name.lower().endswith(".geojson"):
            try:
                frames.append(gpd.read_file(os.path.join(poi_dir, name)))
            except Exception:
                continue

    if not frames:
        return OrderedDict()

    gdf = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]

    if poi_keys is None:
        poi_keys = POI_TAG_KEYS

    tag_keys = [
        col for col in gdf.columns
        if col in poi_keys and gdf[col].notna().any()
    ]

    recap = OrderedDict()
    for key in sorted(tag_keys):
        observed = gdf[key].dropna().astype(str)
        counts = Counter(observed)

        recap[key] = OrderedDict()
        for val, count in counts.most_common():
            recap[key][val] = count

    return recap


def poi_tag_value_recap_from_osm(
    place_name="Cagliari, Sardinia, Italy",
    poi_keys=None,
    use_cache=False,
    output_path="outputs/poi_recap.txt",
):
    if poi_keys is None:
        poi_keys = POI_TAG_KEYS

    prev_cache = ox.settings.use_cache
    ox.settings.use_cache = use_cache
    try:
        tags = {key: True for key in poi_keys}
        gdf = ox.features_from_place(place_name, tags)
    finally:
        ox.settings.use_cache = prev_cache

    recap = OrderedDict()
    for key in sorted(poi_keys):
        if key not in gdf.columns:
            continue
        observed = gdf[key].dropna().astype(str)
        counts = Counter(observed)
        recap[key] = OrderedDict()
        for val, count in counts.most_common():
            recap[key][val] = count

    if output_path:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(format_poi_tag_value_recap(recap))

    return recap


def format_poi_tag_value_recap(recap):
    lines = []
    for key, values in recap.items():
        lines.append(key.capitalize())
        for val, count in values.items():
            lines.append(f"{val} {count}")
        lines.append("----")
    return "\n".join(lines).rstrip()




if __name__ == "__main__":
    recap = poi_tag_value_recap_from_osm(poi_keys={"leisure", "sport", "landuse", "natural","water","waterway","man_made","amenity","tourism","historic","healthcare","emergency","social_facility","shop","office","craft"})
    print(format_poi_tag_value_recap(recap))
