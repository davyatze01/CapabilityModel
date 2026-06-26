import argparse
import json
import math
import os
import sqlite3
import zipfile
from html import escape
from pathlib import Path
from typing import Any

from config import PipelineConfig


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect the POIs associated with one exported hexagon. "
            "Reads the per-hex JSONP from hex_pois/ or hex_pois.zip and joins it "
            "with pois_used.gpkg to produce easy-to-read debug outputs."
        )
    )
    parser.add_argument("hex_id", nargs="?", help="Hexagon id such as H0003_0017")
    parser.add_argument(
        "--poi-export-dir",
        default=None,
        help="Override the POI export directory. Defaults to PipelineConfig().poi_export_dir.",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Override the debug output directory. Defaults to <poi_export_dir>/debug_hex.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Also print the joined POI table to stdout.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Generate debug pages for every available hexagon plus an explorer index.",
    )
    return parser.parse_args()


def _default_paths(poi_export_dir_override: str | None) -> tuple[Path, Path, Path]:
    if poi_export_dir_override:
        export_dir = Path(poi_export_dir_override)
        gpkg_path = export_dir / "pois_used.gpkg"
        zip_path = export_dir / "hex_pois.zip"
        hex_dir = export_dir / "hex_pois"
        return export_dir, gpkg_path, hex_dir if hex_dir.is_dir() else zip_path

    cfg = PipelineConfig()
    export_dir = Path(cfg.poi_export_dir)
    gpkg_path = Path(cfg.poi_export_geopackage_path)
    hex_dir = Path(cfg.hex_pois_dir)
    zip_path = Path(cfg.hex_pois_zip_path)
    return export_dir, gpkg_path, hex_dir if hex_dir.is_dir() else zip_path


def _find_spatial_export_gpkg(export_dir: Path) -> Path | None:
    candidates = sorted((Path("outputs") / "export").glob("*_export.gpkg"))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    export_slug = export_dir.name.lower()
    for candidate in candidates:
        stem = candidate.stem.lower()
        if export_slug in stem:
            return candidate
    return candidates[-1]


def _load_hex_items(hex_id: str, hex_source: Path) -> list[dict[str, Any]]:
    filename = f"{hex_id}.js"
    if hex_source.is_dir():
        hex_path = hex_source / filename
        if not hex_path.exists():
            raise FileNotFoundError(f"Hex file not found: {hex_path}")
        text = hex_path.read_text(encoding="utf-8")
    else:
        if not hex_source.exists():
            raise FileNotFoundError(f"Hex archive not found: {hex_source}")
        with zipfile.ZipFile(hex_source) as zf:
            try:
                text = zf.read(filename).decode("utf-8")
            except KeyError as exc:
                raise FileNotFoundError(f"Hex file not found in zip: {filename}") from exc

    prefix = f'__onHexPois("{hex_id}",'
    suffix = ");"
    if not text.startswith(prefix) or not text.endswith(suffix):
        raise ValueError(f"Unexpected JSONP format for {filename}")
    payload = text[len(prefix):-len(suffix)]
    items = json.loads(payload)
    if not isinstance(items, list):
        raise ValueError(f"Unexpected payload type for {filename}: {type(items).__name__}")
    return items


def _list_hex_ids(hex_source: Path) -> list[str]:
    if hex_source.is_dir():
        return sorted(
            path.stem
            for path in hex_source.glob("H*.js")
            if path.is_file() and path.name != "index.js"
        )
    if not hex_source.exists():
        raise FileNotFoundError(f"Hex archive not found: {hex_source}")
    with zipfile.ZipFile(hex_source) as zf:
        return sorted(
            Path(name).stem
            for name in zf.namelist()
            if name.startswith("H") and name.endswith(".js")
        )


def _fetch_poi_rows(gpkg_path: Path, poi_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not gpkg_path.exists():
        raise FileNotFoundError(f"POI GeoPackage not found: {gpkg_path}")
    if not poi_ids:
        return {}

    placeholders = ",".join("?" for _ in poi_ids)
    query = (
        "SELECT id, source_key, lon, lat, angular_coords, poi_types, svc_map "
        f"FROM pois_used WHERE id IN ({placeholders})"
    )

    with sqlite3.connect(gpkg_path) as conn:
        rows = conn.execute(query, poi_ids).fetchall()

    by_id: dict[int, dict[str, Any]] = {}
    for row in rows:
        by_id[int(row[0])] = {
            "id": int(row[0]),
            "source_key": str(row[1]),
            "lon": float(row[2]) if row[2] is not None else None,
            "lat": float(row[3]) if row[3] is not None else None,
            "angular_coords": str(row[4]) if row[4] is not None else "",
            "poi_types": str(row[5]) if row[5] is not None else "[]",
            "svc_map": str(row[6]) if row[6] is not None else "{}",
        }
    return by_id


def _join_hex_rows(hex_id: str, hex_items: list[dict[str, Any]], poi_rows: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    joined: list[dict[str, Any]] = []
    for item in hex_items:
        poi_id = int(item["i"])
        poi = poi_rows.get(poi_id)
        joined.append(
            {
                "hex_id": hex_id,
                "id": poi_id,
                "source_key": "" if poi is None else poi["source_key"],
                "lat": None if poi is None else poi["lat"],
                "lon": None if poi is None else poi["lon"],
                "angular_coords": "" if poi is None else poi["angular_coords"],
                "poi_types": "[]" if poi is None else poi["poi_types"],
                "svc_map": "{}" if poi is None else poi["svc_map"],
                "service_power": json.dumps(item.get("sp", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "capability_power": json.dumps(item.get("cp", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "missing_in_gpkg": poi is None,
            }
        )
    return joined


def _csv_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    if any(ch in text for ch in [",", "\"", "\n"]):
        text = '"' + text.replace('"', '""') + '"'
    return text


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = [
        "hex_id",
        "id",
        "source_key",
        "lat",
        "lon",
        "angular_coords",
        "poi_types",
        "svc_map",
        "service_power",
        "capability_power",
        "missing_in_gpkg",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(",".join(columns) + "\n")
        for row in rows:
            f.write(",".join(_csv_escape(row.get(col)) for col in columns) + "\n")


def _write_geojson(path: Path, rows: list[dict[str, Any]]) -> None:
    features = []
    for row in rows:
        lat = row.get("lat")
        lon = row.get("lon")
        if lat is None or lon is None:
            continue
        props = {k: v for k, v in row.items() if k not in {"lat", "lon"}}
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props,
            }
        )
    payload = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_grid_params(export_dir: Path) -> dict[str, Any] | None:
    candidates = [
        export_dir.parent.parent / "grid_params.json",
        export_dir.parent / "grid_params.json",
        Path("outputs") / "grid_params.json",
    ]
    for path in candidates:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return None


def _hex_geometry(hex_id: str, grid_params: dict[str, Any]) -> list[tuple[float, float]]:
    if not hex_id.startswith("H") or "_" not in hex_id:
        raise ValueError(f"Unsupported hex id format: {hex_id}")
    col_text, row_text = hex_id[1:].split("_", 1)
    col_idx = int(col_text)
    row_idx = int(row_text)

    min_x = float(grid_params["min_x"])
    min_y = float(grid_params["min_y"])
    m_per_deg_lon = float(grid_params["m_per_deg_lon"])
    m_per_deg_lat = 111320.0
    cell_size_m = float(grid_params["cell_size_m"])

    hex_radius_m = cell_size_m / math.cos(math.pi / 6.0)
    x_step = 1.5 * hex_radius_m
    y_step = math.sqrt(3.0) * hex_radius_m
    cx = min_x - hex_radius_m + (col_idx * x_step)
    cy = min_y - y_step + (0.0 if col_idx % 2 == 0 else y_step / 2.0) + (row_idx * y_step)

    vertices: list[tuple[float, float]] = []
    for k in range(6):
        angle = k * math.pi / 3.0
        vx = cx + hex_radius_m * math.cos(angle)
        vy = cy + hex_radius_m * math.sin(angle)
        vertices.append((vx / m_per_deg_lon, vy / m_per_deg_lat))
    vertices.append(vertices[0])
    return vertices


def _hex_centroid(hex_id: str, grid_params: dict[str, Any]) -> tuple[float, float]:
    vertices = _hex_geometry(hex_id, grid_params)
    coords = vertices[:-1]
    lon = sum(lon for lon, _ in coords) / len(coords)
    lat = sum(lat for _, lat in coords) / len(coords)
    return lat, lon


def _fetch_capability_points(gpkg_path: Path) -> list[dict[str, Any]]:
    if gpkg_path is None or not gpkg_path.exists():
        return []
    with sqlite3.connect(gpkg_path) as conn:
        rows = conn.execute(
            "SELECT node_id, lon, lat FROM capability_points "
            "WHERE lon IS NOT NULL AND lat IS NOT NULL"
        ).fetchall()
    return [
        {"node_id": str(row[0]), "lon": float(row[1]), "lat": float(row[2])}
        for row in rows
    ]


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return 2.0 * radius_m * math.asin(math.sqrt(a))


def _nearest_capability_point(
    centroid_lat: float,
    centroid_lon: float,
    points: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not points:
        return None
    best = None
    best_dist = float("inf")
    for point in points:
        dist = _haversine_m(centroid_lat, centroid_lon, float(point["lat"]), float(point["lon"]))
        if dist < best_dist:
            best = dict(point)
            best["distance_to_hex_centroid_m"] = dist
            best_dist = dist
    return best


def _write_hex_geojson(path: Path, hex_id: str, vertices: list[tuple[float, float]]) -> None:
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[list(coord) for coord in vertices]],
                },
                "properties": {"hex_id": hex_id},
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_html_map(
    path: Path,
    hex_id: str,
    rows: list[dict[str, Any]],
    vertices: list[tuple[float, float]] | None,
    node_point: dict[str, Any] | None,
) -> None:
    all_lons: list[float] = []
    all_lats: list[float] = []
    if vertices:
        all_lons.extend(lon for lon, _ in vertices)
        all_lats.extend(lat for _, lat in vertices)
    if node_point is not None:
        all_lons.append(float(node_point["lon"]))
        all_lats.append(float(node_point["lat"]))
    for row in rows:
        if row.get("lon") is not None and row.get("lat") is not None:
            all_lons.append(float(row["lon"]))
            all_lats.append(float(row["lat"]))
    center_lat = sum(all_lats) / len(all_lats) if all_lats else 0.0
    center_lon = sum(all_lons) / len(all_lons) if all_lons else 0.0

    list_markup = []
    for row in rows:
        list_markup.append(
            "<tr>"
            f"<td>{row['id']}</td>"
            f"<td>{escape(str(row['poi_types']))}</td>"
            f"<td>{escape(str(row['source_key']))}</td>"
            f"<td>{escape(str(row['service_power']))}</td>"
            f"<td>{escape(str(row['capability_power']))}</td>"
            "</tr>"
        )

    poi_js = json.dumps(
        [
            {
                "id": row["id"],
                "lat": row["lat"],
                "lon": row["lon"],
                "source_key": row["source_key"],
                "poi_types": row["poi_types"],
                "service_power": row["service_power"],
                "capability_power": row["capability_power"],
            }
            for row in rows
            if row.get("lat") is not None and row.get("lon") is not None
        ],
        ensure_ascii=False,
    )
    hex_js = json.dumps([[lat, lon] for lon, lat in vertices], ensure_ascii=False) if vertices else "null"
    node_js = json.dumps(
        {
            "node_id": None if node_point is None else node_point["node_id"],
            "lat": None if node_point is None else node_point["lat"],
            "lon": None if node_point is None else node_point["lon"],
            "distance_to_hex_centroid_m": None if node_point is None else node_point["distance_to_hex_centroid_m"],
        },
        ensure_ascii=False,
    )

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{hex_id} debug map</title>
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
    crossorigin=""
  >
  <style>
    body {{
      font-family: Arial, sans-serif;
      margin: 0;
      background: #f3f1ea;
      color: #1f2933;
    }}
    .wrap {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 20px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 28px;
    }}
    .sub {{
      margin: 0 0 16px;
      color: #52606d;
    }}
    .panel {{
      background: #fffdf8;
      border: 1px solid #d9d3c7;
      border-radius: 14px;
      padding: 14px;
      box-shadow: 0 8px 24px rgba(31, 41, 51, 0.08);
      margin-bottom: 18px;
    }}
    #map {{
      width: 100%;
      height: 700px;
      border-radius: 10px;
      overflow: hidden;
    }}
    .note {{
      margin: 10px 0 0;
      color: #52606d;
      font-size: 13px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      text-align: left;
      padding: 8px;
      border-bottom: 1px solid #ece6d8;
      vertical-align: top;
      word-break: break-word;
    }}
    th {{
      background: #f8f3e7;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>{hex_id}</h1>
    <p class="sub">POIs associated with this hexagon: {len(rows)}</p>
    <div class="panel">
      <div id="map"></div>
      <p class="note">The map uses OpenStreetMap tiles. An internet connection is needed for the basemap tiles to appear.</p>
    </div>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>POI id</th>
            <th>POI types</th>
            <th>Source key</th>
            <th>Service power</th>
            <th>Capability power</th>
          </tr>
        </thead>
        <tbody>
          {"".join(list_markup)}
        </tbody>
      </table>
    </div>
  </div>
  <script
    src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
    crossorigin=""
  ></script>
  <script>
    const map = L.map("map");
    L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }}).addTo(map);

    const poiData = {poi_js};
    const hexCoords = {hex_js};
    const nodePoint = {node_js};
    const bounds = [];

    if (hexCoords) {{
      const hexLayer = L.polygon(hexCoords, {{
        color: "#c26a00",
        weight: 3,
        fillColor: "#f1a340",
        fillOpacity: 0.18
      }}).addTo(map);
      hexLayer.bindTooltip("{escape(hex_id)}");
      bounds.push(...hexCoords);
    }}

    if (nodePoint && nodePoint.lat !== null && nodePoint.lon !== null) {{
      const nodeLatLng = [nodePoint.lat, nodePoint.lon];
      L.circle(nodeLatLng, {{
        radius: 15000,
        color: "#1d4ed8",
        weight: 2,
        fillColor: "#60a5fa",
        fillOpacity: 0.08
      }}).addTo(map);
      L.circleMarker(nodeLatLng, {{
        radius: 7,
        color: "#1e3a8a",
        weight: 2,
        fillColor: "#2563eb",
        fillOpacity: 1
      }}).addTo(map).bindPopup(
        `<strong>Representative node</strong><br>` +
        `node_id=${{nodePoint.node_id}}<br>` +
        `distance to hex centroid=${{nodePoint.distance_to_hex_centroid_m?.toFixed(1)}} m`
      );
      bounds.push(nodeLatLng);
    }}

    for (const poi of poiData) {{
      const latlng = [poi.lat, poi.lon];
      bounds.push(latlng);
      L.circleMarker(latlng, {{
        radius: 5,
        color: "#0f766e",
        weight: 1,
        fillColor: "#14b8a6",
        fillOpacity: 0.95
      }}).addTo(map).bindPopup(
        `<strong>POI ${{poi.id}}</strong><br>` +
        `${{poi.source_key}}<br>` +
        `types=${{poi.poi_types}}<br>` +
        `service_power=${{poi.service_power}}<br>` +
        `capability_power=${{poi.capability_power}}`
      );
    }}

    if (bounds.length) {{
      map.fitBounds(bounds, {{ padding: [30, 30] }});
    }} else {{
      map.setView([{center_lat}, {center_lon}], 13);
    }}
  </script>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


def _write_explorer_index(path: Path, items: list[dict[str, Any]]) -> None:
    rows = []
    for item in items:
        rows.append(
            "<tr>"
            f"<td><a href=\"{escape(item['html_name'])}\">{escape(item['hex_id'])}</a></td>"
            f"<td>{item['poi_count']}</td>"
            f"<td>{escape(item['node_id']) if item['node_id'] else ''}</td>"
            f"<td>{escape(item['node_latlon']) if item['node_latlon'] else ''}</td>"
            f"<td>{escape(item['poi_types'])}</td>"
            "</tr>"
        )
    items_js = json.dumps(items, ensure_ascii=False)
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Hex Debug Explorer</title>
  <style>
    body {{
      margin: 0;
      font-family: Arial, sans-serif;
      background: #f3f1ea;
      color: #1f2933;
    }}
    .wrap {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 20px;
    }}
    .panel {{
      background: #fffdf8;
      border: 1px solid #d9d3c7;
      border-radius: 14px;
      padding: 16px;
      box-shadow: 0 8px 24px rgba(31, 41, 51, 0.08);
      margin-bottom: 18px;
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 30px;
    }}
    .sub {{
      margin: 0 0 14px;
      color: #52606d;
    }}
    input, select {{
      padding: 10px 12px;
      border: 1px solid #cfc7b5;
      border-radius: 10px;
      font-size: 14px;
      background: white;
    }}
    .controls {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .controls input {{
      min-width: 280px;
    }}
    .open-link {{
      display: inline-block;
      padding: 10px 14px;
      border-radius: 10px;
      background: #0f766e;
      color: white;
      text-decoration: none;
      font-weight: 700;
    }}
    iframe {{
      width: 100%;
      height: 860px;
      border: 0;
      border-radius: 12px;
      background: white;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      text-align: left;
      padding: 8px;
      border-bottom: 1px solid #ece6d8;
      vertical-align: top;
      word-break: break-word;
    }}
    th {{
      background: #f8f3e7;
    }}
    a {{
      color: #0f766e;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="panel">
      <h1>Hex Debug Explorer</h1>
      <p class="sub">Browse every hexagon debug page, including the representative node, 15 km circle, and associated POIs.</p>
      <div class="controls">
        <input id="search" type="search" placeholder="Filter by hex id, node id, or POI type">
        <select id="hex-select"></select>
        <a id="open-link" class="open-link" href="#" target="_blank" rel="noopener">Open Current Page</a>
      </div>
    </div>
    <div class="panel">
      <iframe id="viewer" title="Hex debug viewer"></iframe>
    </div>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>Hex</th>
            <th>POIs</th>
            <th>Node id</th>
            <th>Node coords</th>
            <th>POI types</th>
          </tr>
        </thead>
        <tbody id="hex-table">
          {"".join(rows)}
        </tbody>
      </table>
    </div>
  </div>
  <script>
    const items = {items_js};
    const searchInput = document.getElementById("search");
    const select = document.getElementById("hex-select");
    const viewer = document.getElementById("viewer");
    const openLink = document.getElementById("open-link");
    const tableBody = document.getElementById("hex-table");

    function filteredItems() {{
      const needle = searchInput.value.trim().toLowerCase();
      if (!needle) return items;
      return items.filter(item =>
        item.hex_id.toLowerCase().includes(needle) ||
        (item.node_id || "").toLowerCase().includes(needle) ||
        item.poi_types.toLowerCase().includes(needle)
      );
    }}

    function renderTable(list) {{
      tableBody.innerHTML = list.map(item => `
        <tr>
          <td><a href="${{item.html_name}}" data-hex="${{item.hex_id}}">${{item.hex_id}}</a></td>
          <td>${{item.poi_count}}</td>
          <td>${{item.node_id || ""}}</td>
          <td>${{item.node_latlon || ""}}</td>
          <td>${{item.poi_types}}</td>
        </tr>
      `).join("");
      for (const link of tableBody.querySelectorAll("a[data-hex]")) {{
        link.addEventListener("click", (event) => {{
          event.preventDefault();
          setCurrent(link.getAttribute("data-hex"));
        }});
      }}
    }}

    function renderSelect(list) {{
      const current = select.value;
      select.innerHTML = list.map(item => `
        <option value="${{item.hex_id}}">${{item.hex_id}} (${{item.poi_count}} POIs)</option>
      `).join("");
      if (list.length === 0) {{
        viewer.removeAttribute("src");
        openLink.removeAttribute("href");
        return;
      }}
      const target = list.some(item => item.hex_id === current) ? current : list[0].hex_id;
      select.value = target;
      setCurrent(target, false);
    }}

    function setCurrent(hexId, syncSelect = true) {{
      const item = items.find(entry => entry.hex_id === hexId);
      if (!item) return;
      viewer.src = item.html_name;
      openLink.href = item.html_name;
      if (syncSelect) select.value = hexId;
    }}

    searchInput.addEventListener("input", () => {{
      const list = filteredItems();
      renderTable(list);
      renderSelect(list);
    }});

    select.addEventListener("change", () => setCurrent(select.value, false));

    renderTable(items);
    renderSelect(items);
  </script>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


def _build_hex_outputs(
    hex_id: str,
    export_dir: Path,
    gpkg_path: Path,
    hex_source: Path,
    out_dir: Path,
    grid_params: dict[str, Any] | None,
    capability_points: list[dict[str, Any]],
) -> dict[str, Any]:
    hex_items = _load_hex_items(hex_id, hex_source)
    poi_ids = [int(item["i"]) for item in hex_items]
    poi_rows = _fetch_poi_rows(gpkg_path, poi_ids)
    joined_rows = _join_hex_rows(hex_id, hex_items, poi_rows)

    csv_path = out_dir / f"{hex_id}.csv"
    geojson_path = out_dir / f"{hex_id}.geojson"
    ids_path = out_dir / f"{hex_id}_ids.txt"
    html_path = out_dir / f"{hex_id}.html"
    hex_geojson_path = out_dir / f"{hex_id}_hex.geojson"
    node_geojson_path = out_dir / f"{hex_id}_node.geojson"

    _write_csv(csv_path, joined_rows)
    _write_geojson(geojson_path, joined_rows)
    ids_path.write_text(",".join(str(pid) for pid in poi_ids) + "\n", encoding="utf-8")

    vertices = None
    node_point = None
    if grid_params is not None:
        try:
            vertices = _hex_geometry(hex_id, grid_params)
            _write_hex_geojson(hex_geojson_path, hex_id, vertices)
        except Exception:
            vertices = None
        try:
            centroid_lat, centroid_lon = _hex_centroid(hex_id, grid_params)
            node_point = _nearest_capability_point(centroid_lat, centroid_lon, capability_points)
            if node_point is not None:
                node_payload = {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Point",
                                "coordinates": [node_point["lon"], node_point["lat"]],
                            },
                            "properties": node_point,
                        }
                    ],
                }
                node_geojson_path.write_text(json.dumps(node_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            node_point = None

    _write_html_map(html_path, hex_id, joined_rows, vertices, node_point)

    poi_types = sorted(
        {
            poi_type
            for row in joined_rows
            for poi_type in json.loads(row["poi_types"])
        }
    )

    result = {
        "hex_id": hex_id,
        "poi_count": len(joined_rows),
        "hex_source": str(hex_source),
        "gpkg": str(gpkg_path),
        "csv": str(csv_path),
        "geojson": str(geojson_path),
        "html": str(html_path),
        "html_name": html_path.name,
        "ids": str(ids_path),
        "qgis_filter": f"\"id\" IN ({','.join(str(pid) for pid in poi_ids)})",
        "node_id": None,
        "node_latlon": None,
        "hex_geojson": str(hex_geojson_path) if vertices is not None else None,
        "node_geojson": None,
        "poi_types": ", ".join(poi_types),
        "rows": joined_rows,
    }
    if node_point is not None:
        result["node_id"] = str(node_point["node_id"])
        result["node_latlon"] = f"{node_point['lat']:.6f}, {node_point['lon']:.6f}"
        result["node_geojson"] = str(node_geojson_path)
        result["node_summary"] = (
            f"{node_point['node_id']} @ ({node_point['lat']}, {node_point['lon']}) "
            f"[{node_point['distance_to_hex_centroid_m']:.1f} m from hex centroid]"
        )
    return result


def _print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No POIs associated with this hexagon.")
        return
    print("id | lat | lon | poi_types | source_key")
    for row in rows:
        print(
            f"{row['id']} | {row['lat']} | {row['lon']} | "
            f"{row['poi_types']} | {row['source_key']}"
        )


def main() -> None:
    args = _parse_args()
    export_dir, gpkg_path, hex_source = _default_paths(args.poi_export_dir)
    out_dir = Path(args.out_dir) if args.out_dir else export_dir / "debug_hex"
    out_dir.mkdir(parents=True, exist_ok=True)
    grid_params = _load_grid_params(export_dir)
    spatial_gpkg = _find_spatial_export_gpkg(export_dir)
    capability_points = _fetch_capability_points(spatial_gpkg) if spatial_gpkg is not None else []

    if args.all:
        hex_ids = _list_hex_ids(hex_source)
        if not hex_ids:
            raise ValueError("No hexagon files were found.")
        explorer_items = []
        for idx, hex_id in enumerate(hex_ids, start=1):
            result = _build_hex_outputs(
                hex_id,
                export_dir,
                gpkg_path,
                hex_source,
                out_dir,
                grid_params,
                capability_points,
            )
            explorer_items.append(
                {
                    "hex_id": result["hex_id"],
                    "html_name": result["html_name"],
                    "poi_count": result["poi_count"],
                    "node_id": result["node_id"],
                    "node_latlon": result["node_latlon"],
                    "poi_types": result["poi_types"],
                }
            )
            if idx % 100 == 0 or idx == len(hex_ids):
                print(f"[Explorer] generated {idx}/{len(hex_ids)} hex pages", flush=True)
        index_path = out_dir / "index.html"
        _write_explorer_index(index_path, explorer_items)
        print(f"hex_count={len(hex_ids)}")
        print(f"explorer={index_path}")
        return

    if not args.hex_id:
        raise ValueError("Provide a hex_id or use --all.")
    result = _build_hex_outputs(
        args.hex_id.strip(),
        export_dir,
        gpkg_path,
        hex_source,
        out_dir,
        grid_params,
        capability_points,
    )

    print(f"hex_id={result['hex_id']}")
    print(f"poi_count={result['poi_count']}")
    print(f"hex_source={result['hex_source']}")
    print(f"gpkg={result['gpkg']}")
    print(f"csv={result['csv']}")
    print(f"geojson={result['geojson']}")
    print(f"html={result['html']}")
    if result["hex_geojson"] is not None:
        print(f"hex_geojson={result['hex_geojson']}")
    if result["node_geojson"] is not None:
        print(f"node_geojson={result['node_geojson']}")
        print(f"node={result['node_summary']}")
    print(f"ids={result['ids']}")
    print(f'qgis_filter={result["qgis_filter"]}')

    if args.stdout:
        print()
        _print_table(result["rows"])


if __name__ == "__main__":
    main()
