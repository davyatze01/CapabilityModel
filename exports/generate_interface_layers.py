"""Regenerate interfaccia's static hex-grid map layers straight from the pipeline's
GeoPackage, replacing the manual QGIS Desktop -> qgis2web export step for these 19
layers.

Writes both "layers/<name>.js" (var json_<name> = {geojson};) and the matching
"styles/<name>_style.js". The style classing (breakpoints + colors) is NOT a fixed
constant -- capability breakpoints are calibrated per profile (core.config.
ELECTRE_BOUNDARIES) and the color scale is core.config/utils.capabilities'
CAPABILITY_COLOR_STOPS/SERVICE_COLOR_STOPS, the same ones the pipeline bakes into
the gpkg's own embedded QML style -- so styles are regenerated from those constants
every run to stay in sync. No other interfaccia file needs to change: new_layers.js
already builds each ol.layer.Vector generically from json_<name> + style_<name> by
name.

Also writes resources/capability.csv (capability -> service list, from
utils.capabilities.CAPABILITY_SERVICES) -- the interface fetches this at startup to
populate the service buttons under a selected capability.

The POI layer (pois_used2pois_used_1.js, sourced from pois_used.gpkg) has a richer
schema feeding the object inspector and is handled separately.
"""

import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

import fiona

from core.config import ELECTRE_BOUNDARIES
from utils.capabilities import CAPABILITY_COLOR_STOPS, CAPABILITY_SERVICES, SERVICE_COLOR_STOPS

GPKG_PATH = Path("outputs/gpkg/Cagliari/Cagliari.gpkg")
OUTPUT_DIR = Path("interfaccia/layers")

# gpkg layer name -> qgis2web-era output basename (must match the json_<name>/
# style_<name> identifiers already wired into interfaccia/resources/city_config.js).
LAYER_NAME_MAP = {
    "capability_care": "Cagliari6capability_care_1",
    "capability_nutrition": "Cagliari6capability_nutrition_2",
    "capability_restorativeness": "Cagliari6capability_restorativeness_3",
    "service_care_services": "Cagliari6service_care_services_4",
    "service_cultural_activities": "Cagliari6service_cultural_activities_5",
    "service_diagnosis_and_prevention": "Cagliari6service_diagnosis_and_prevention_6",
    "service_eating_out": "Cagliari6service_eating_out_7",
    "service_emergency_services": "Cagliari6service_emergency_services_8",
    "service_food_access": "Cagliari6service_food_access_9",
    "service_medicines_and_supplies": "Cagliari6service_medicines_and_supplies_10",
    "service_nature_contact": "Cagliari6service_nature_contact_11",
    "service_quietness": "Cagliari6service_quietness_12",
    "service_scenic_views": "Cagliari6service_scenic_views_13",
    "service_sport_and_movement": "Cagliari6service_sport_and_movement_14",
    "zz_capability_grid": "Cagliari6zz_capability_grid_15",
    "zzz_capability_isobands_care": "Cagliari6zzz_capability_isobands_care_16",
    "zzz_capability_isobands_nutrition": "Cagliari6zzz_capability_isobands_nutrition_17",
    "zzz_capability_isobands_restorativeness": "Cagliari6zzz_capability_isobands_restorativeness_18",
    "zzzz_place_comuni": "Cagliari6zzzz_place_comuni_19",
    "zzzz_place_quartieri": "Cagliari6zzzz_place_quartieri_20",
}

POI_GPKG_PATH = Path("outputs/poi_exports/Cagliari/pois_used.gpkg")
POI_OUTPUT_NAME = "pois_used2pois_used_1"

STYLE_DIR = Path("interfaccia/styles")
CAPABILITY_CSV_PATH = Path("interfaccia/resources/capability.csv")

# capability name -> output basename, for the 3 capability grid layers.
CAPABILITY_STYLE_TARGETS = {
    "care": "Cagliari6capability_care_1",
    "nutrition": "Cagliari6capability_nutrition_2",
    "restorativeness": "Cagliari6capability_restorativeness_3",
}

# service name -> output basename. Limited to the 11 services that actually have a
# map layer wired into city_config.js's layerGlobals.services -- CAPABILITY_SERVICES
# also lists "impatient_and_rehabilitation" under care, but no Cagliari6service_*
# layer exists for it yet, so it's left out of both this map and capability.csv
# rather than exporting a service button with nothing behind it.
SERVICE_STYLE_TARGETS = {
    "care_services": "Cagliari6service_care_services_4",
    "cultural_activities": "Cagliari6service_cultural_activities_5",
    "diagnosis_and_prevention": "Cagliari6service_diagnosis_and_prevention_6",
    "eating_out": "Cagliari6service_eating_out_7",
    "emergency_services": "Cagliari6service_emergency_services_8",
    "food_access": "Cagliari6service_food_access_9",
    "medicines_and_supplies": "Cagliari6service_medicines_and_supplies_10",
    "nature_contact": "Cagliari6service_nature_contact_11",
    "quietness": "Cagliari6service_quietness_12",
    "scenic_views": "Cagliari6service_scenic_views_13",
    "sport_and_movement": "Cagliari6service_sport_and_movement_14",
}


def _layer_to_geojson(gpkg_path: Path, gpkg_layer: str, out_name: str) -> dict:
    """Read one gpkg layer into the qgis2web-shaped FeatureCollection.

    Adds a sequential 1-based "fid" (as string, matching qgis2web) only for layers
    whose gpkg schema doesn't already carry one -- the grid/isobands/place layers
    keep their real fid column instead.
    """
    features = []
    with fiona.open(gpkg_path, layer=gpkg_layer) as src:
        has_fid = "fid" in src.schema["properties"]
        for i, feat in enumerate(src, start=1):
            props = dict(feat["properties"])
            if has_fid:
                props["fid"] = str(props["fid"])
            else:
                props = {"fid": str(i), **props}
            geom = feat["geometry"]
            features.append({
                "type": "Feature",
                "properties": props,
                "geometry": {"type": geom["type"], "coordinates": geom["coordinates"]},
            })
    return {
        "type": "FeatureCollection",
        "name": out_name,
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }


def _hex_to_rgba(hex_color: str) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},1.0)"


def _write_step_style(out_path: Path, out_name: str, field: str, bounds: list[float], colors: list[str]) -> None:
    """Write a qgis2web-shaped style_<name> function: one flat fill color per
    [bounds[i], bounds[i+1]] step. bounds/colors come straight from the pipeline's
    own constants, so this always matches the gpkg's embedded QML styling.
    """
    branches = []
    for i, color in enumerate(colors):
        keyword = "if" if i == 0 else "} else if"
        branches.append(
            f'    {keyword} (value >= {bounds[i]:.6f} && value <= {bounds[i + 1]:.6f}) {{\n'
            f"            style = [ new ol.style.Style({{\n"
            f"        fill: new ol.style.Fill({{color: '{_hex_to_rgba(color)}'}}),\n"
            f"        text: createTextStyle(feature, resolution, labelText, labelFont,\n"
            f"                              labelFill, placement, bufferColor,\n"
            f"                              bufferWidth)\n"
            f"    }})]"
        )
    body = "\n".join(branches) + "\n                    };\n"
    content = f"""var size = 0;
var placement = 'point';

var style_{out_name} = function(feature, resolution){{
    var context = {{
        feature: feature,
        variables: {{}}
    }};

    var labelText = "";
    var value = feature.get("{field}");
    var labelFont = "10px, sans-serif";
    var labelFill = "#000000";
    var bufferColor = "";
    var bufferWidth = 0;
    var textAlign = "left";
    var offsetX = 0;
    var offsetY = 0;
    var placement = 'point';
    if ("" !== null) {{
        labelText = String("");
    }}
{body}
    return style;
}};
"""
    out_path.write_text(content, encoding="utf-8")
    print(f"Wrote {out_path}")


def _export_capability_styles() -> None:
    bounds = [0.0] + list(ELECTRE_BOUNDARIES) + [1.0]
    for capability, out_name in CAPABILITY_STYLE_TARGETS.items():
        _write_step_style(STYLE_DIR / f"{out_name}_style.js", out_name, f"grid_mean_{capability}", bounds, CAPABILITY_COLOR_STOPS)


def _export_service_styles() -> None:
    bounds = [i / 10 for i in range(11)]
    for service, out_name in SERVICE_STYLE_TARGETS.items():
        _write_step_style(STYLE_DIR / f"{out_name}_style.js", out_name, f"grid_mean_service_{service}", bounds, SERVICE_COLOR_STOPS)


def _export_capability_csv() -> None:
    lines = ["capability,services,electre_weight,veto_threshold,enabled"]
    for capability, services in CAPABILITY_SERVICES.items():
        if capability not in CAPABILITY_STYLE_TARGETS:
            continue
        kept = [s for s in services if s in SERVICE_STYLE_TARGETS]
        services_repr = "[" + ", ".join(f"'{s}'" for s in kept) + "]"
        lines.append(f'{capability},"{services_repr}"')
    CAPABILITY_CSV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {CAPABILITY_CSV_PATH}")


# Mirrors exports/poi_exports.py's _format_angular_coords -- duplicated rather than
# imported since that module pulls in the full pipeline dependency graph for one
# formatting helper. Recomputed from lon/lat rather than the gpkg's own stored
# angular_coords column, which has a pre-existing mojibake bug on the degree sign.
def _format_angular_coords(lat: float, lon: float) -> str:
    lat_hemi = "N" if lat >= 0 else "S"
    lon_hemi = "E" if lon >= 0 else "W"
    return f"{abs(lat):.6f}°{lat_hemi}, {abs(lon):.6f}°{lon_hemi}"


def _export_poi_layer(gpkg_path: Path, out_dir: Path, out_name: str) -> None:
    """Regenerate the POI point layer from pois_used.gpkg.

    poi_types/svc_map are JSON-encoded text columns in the gpkg (no native
    array/object type) -- decoded here so the browser's GeoJSON reader hands
    qgis2web.js real arrays/objects, matching what it expects from feature.get().
    Per-hexagon "power" is not included -- qgis2web.js sets it dynamically at
    runtime from the selected hexagon's hex_pois shard record.
    """
    features = []
    with fiona.open(gpkg_path, layer="pois_used") as src:
        for feat in src:
            props = dict(feat["properties"])
            lon, lat = props["lon"], props["lat"]
            out_props = {
                "id": props["id"],
                "lon": lon,
                "lat": lat,
                "angular_coords": _format_angular_coords(lat, lon),
                "poi_types": json.loads(props["poi_types"]),
                "svc_map": json.loads(props["svc_map"]),
            }
            geom = feat["geometry"]
            features.append({
                "type": "Feature",
                "properties": out_props,
                "geometry": {"type": geom["type"], "coordinates": geom["coordinates"]},
            })
    geojson = {
        "type": "FeatureCollection",
        "name": out_name,
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }
    body = json.dumps(geojson, ensure_ascii=False, separators=(",", ":"))
    out_path = out_dir / f"{out_name}.js"
    out_path.write_text(f"var json_{out_name} = {body};", encoding="utf-8")
    print(f"Wrote {out_path} ({len(features)} features)")


def main() -> None:
    for gpkg_layer, out_name in LAYER_NAME_MAP.items():
        geojson = _layer_to_geojson(GPKG_PATH, gpkg_layer, out_name)
        body = json.dumps(geojson, ensure_ascii=False, separators=(",", ":"))
        out_path = OUTPUT_DIR / f"{out_name}.js"
        out_path.write_text(f"var json_{out_name} = {body};", encoding="utf-8")
        print(f"Wrote {out_path} ({len(geojson['features'])} features)")

    _export_poi_layer(POI_GPKG_PATH, OUTPUT_DIR, POI_OUTPUT_NAME)
    _export_capability_styles()
    _export_service_styles()
    _export_capability_csv()


if __name__ == "__main__":
    main()
