from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from core.config import PipelineConfig
from core.context import build_context
from stages.snapping_stage import run_snapping_stage, load_snap_checkpoint, write_snap_checkpoint
from routing.public_transport_routing_stage import run_public_transport_routing_stage
from routing.non_bus_routing_stage import run_non_bus_routing_stage
from stages.accessibility_stage import run_accessibility_stage
from stages.service_stage import run_service_stage
from stages.capability_stage import run_capability_stage
from exports.artifact_bundle import load_impedance_bundle, write_impedance_bundle
from exports.poi_exports import generate_poi_exports
from analysis.score_report import generate_score_report
from plotting.plot_shapefile import plot_all_experiments
from exports.generate_experiment_shapefiles import (
    GRID_TABLE_NAME,
    ISOBANDS_TABLE_NAMES,
    PLACE_COMUNI_TABLE_NAME,
    PLACE_QUARTIERI_TABLE_NAME,
    generate_combined_experiment_gpkg,
    generate_combined_experiment_shapefile,
)


def _in_flatpak_sandbox() -> bool:
    """True when this process is running inside a Flatpak sandbox (e.g. VS Code's
    integrated terminal on a Flatpak install). The host filesystem is then mounted at
    /run/host and host programs must be reached via `flatpak-spawn --host`."""
    return os.path.exists("/.flatpak-info")


def _host_cmd_prefix() -> list[str]:
    """Argv prefix to run a command on the host. Empty when not sandboxed (or when
    flatpak-spawn is unavailable, in which case we fall back to in-sandbox lookups)."""
    if _in_flatpak_sandbox() and shutil.which("flatpak-spawn"):
        return ["flatpak-spawn", "--host"]
    return []


def _which(name: str, prefix: list[str]) -> str | None:
    """Locate an executable on PATH. When `prefix` is set, resolves against the host's
    PATH via flatpak-spawn instead of the sandbox's."""
    if prefix:
        try:
            result = subprocess.run(
                prefix + ["sh", "-c", f"command -v {name}"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            out = result.stdout.strip()
            return out.splitlines()[0] if result.returncode == 0 and out else None
        except Exception:
            return None
    return shutil.which(name)


def _path_exists(path: str, prefix: list[str]) -> bool:
    """Existence check that targets the host filesystem when `prefix` is set."""
    if prefix:
        try:
            return subprocess.run(prefix + ["test", "-e", path], timeout=15).returncode == 0
        except Exception:
            return False
    return os.path.exists(path)


def _resolve_qgis_executable(cfg: PipelineConfig) -> str | None:
    if cfg.qgis_bin_path:
        return cfg.qgis_bin_path

    env_path = os.getenv("QGIS_BIN_PATH")
    if env_path:
        return env_path

    prefix = _host_cmd_prefix()

    # Look on PATH first (the host's PATH when running inside a Flatpak sandbox).
    for name in ("qgis-ltr-bin.exe", "qgis-bin.exe", "qgis"):
        found = _which(name, prefix)
        if found:
            return found

    # macOS and Linux detection (non-Windows). Covers distro packages, snap, and flatpak —
    # the flatpak/snap export wrappers are single executables that forward args (the project
    # path), so they work with the same Popen([exe, project]) launch path below. When we are
    # ourselves inside a sandbox, these paths are probed on the host via `prefix`.
    if os.name != "nt":
        home = os.path.expanduser("~")
        unix_candidates = [
            # macOS app bundles
            "/Applications/QGIS.app/Contents/MacOS/QGIS",
            "/Applications/QGIS-LTR.app/Contents/MacOS/QGIS",
            "/opt/homebrew/bin/qgis",
            # Linux: distro packages
            "/usr/bin/qgis",
            "/usr/local/bin/qgis",
            # Linux: snap
            "/snap/bin/qgis",
            # Linux: flatpak export wrappers (system + per-user)
            "/var/lib/flatpak/exports/bin/org.qgis.qgis",
            os.path.join(home, ".local/share/flatpak/exports/bin/org.qgis.qgis"),
        ]
        for candidate in unix_candidates:
            if _path_exists(candidate, prefix):
                return candidate
        return None

    # Windows detection
    candidates: list[str] = []
    program_files = os.environ.get("ProgramFiles", "C:\\Program Files")
    for base in [program_files, os.path.join(program_files, "QGIS")]:
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            if not name.lower().startswith("qgis"):
                continue
            exe_path = os.path.join(base, name, "bin", "qgis-ltr-bin.exe")
            if os.path.exists(exe_path):
                candidates.append(exe_path)
            exe_path = os.path.join(base, name, "bin", "qgis-bin.exe")
            if os.path.exists(exe_path):
                candidates.append(exe_path)

    if not candidates:
        return None

    candidates.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    return candidates[0]


def _python_has_pyqgis(python_path: str, prefix: list[str] | None = None) -> bool:
    """Return True if the given interpreter can import PyQGIS (qgis.core). When `prefix`
    is set the probe runs on the host (the interpreter and its bindings live there)."""
    if prefix is None:
        prefix = _host_cmd_prefix()
    try:
        if prefix:
            # flatpak-spawn doesn't forward our env; pass the one var we care about explicitly.
            result = subprocess.run(
                prefix + ["--env=QT_QPA_PLATFORM=offscreen", python_path, "-c", "import qgis.core"],
                capture_output=True,
                timeout=60,
            )
        else:
            env = dict(os.environ)
            env.setdefault("QT_QPA_PLATFORM", "offscreen")  # don't need a display just to import
            result = subprocess.run(
                [python_path, "-c", "import qgis.core"],
                capture_output=True,
                timeout=60,
                env=env,
            )
        return result.returncode == 0
    except Exception:
        return False


def _resolve_qgis_python_launcher(qgis_exe: str) -> str | None:
    prefix = _host_cmd_prefix()
    qgis_bin_dir = Path(qgis_exe).parent

    # Windows: the .bat launchers set up the QGIS environment before invoking python, so they
    # are the canonical entry point — return them directly without an import probe.
    windows_candidates = [
        qgis_bin_dir / "python-qgis-ltr.bat",
        qgis_bin_dir / "python-qgis.bat",
    ]
    for candidate in windows_candidates:
        if _path_exists(str(candidate), prefix):
            return str(candidate)

    # macOS bundled launchers/interpreters.
    qgis_contents_dir = qgis_bin_dir.parent
    qgis_app_dir = qgis_contents_dir.parent
    mac_candidates = [
        qgis_bin_dir / "python-qgis-ltr",
        qgis_bin_dir / "python-qgis",
        qgis_bin_dir / "bin" / "python3",
        qgis_contents_dir / "Resources" / "python" / "bin" / "python3",
        qgis_contents_dir / "Resources" / "bin" / "python3",
        qgis_app_dir / "Contents" / "MacOS" / "bin" / "python3",
    ]

    # Linux: PyQGIS is installed into a system python by the distro/snap/flatpak, not a
    # dedicated launcher. Candidates, checked in order of preference.
    linux_candidates: list[Path] = [qgis_bin_dir / "python3", Path("/usr/bin/python3")]
    which_py = _which("python3", prefix)
    if which_py:
        linux_candidates.append(Path(which_py))

    # Crucially, only accept an interpreter that can actually import PyQGIS. The previous code
    # returned the first python3 it found, which on Linux could be one *without* the bindings
    # (silently breaking the styling step). The import probe makes this correct by construction.
    seen: set[str] = set()
    for candidate in mac_candidates + linux_candidates:
        path = str(candidate)
        if path in seen or not _path_exists(path, prefix):
            continue
        seen.add(path)
        if _python_has_pyqgis(path, prefix):
            return path

    return None


def _build_qgis_project(cfg: PipelineConfig, gpkg_path: Path, qgis_exe: str) -> Path | None:
    if cfg.qgis_project_path:
        return None

    output_path = Path("outputs") / "qgis" / cfg.artifact_slug / "capability.qgz"
    
    if not cfg.qgis_autostyle_project:
        # Return the project path even if we're not auto-styling
        # (it may have been created previously)
        return output_path if output_path.exists() else None

    output_path.parent.mkdir(parents=True, exist_ok=True)

    python_launcher = _resolve_qgis_python_launcher(qgis_exe)
    if python_launcher is None:
        print("[QGIS] Skipping project generation: python-qgis launcher not found.", flush=True)
        # Still return the path if it exists
        return output_path if output_path.exists() else None

    gpkg_literal = repr(str(gpkg_path.resolve()))
    grid_sidecar_path = gpkg_path.with_name(f"{gpkg_path.stem}_grid{gpkg_path.suffix}")
    grid_sidecar_literal = repr(str(grid_sidecar_path.resolve()))
    output_literal = repr(str(output_path.resolve()))
    iso_table_names_literal = repr(ISOBANDS_TABLE_NAMES)
    iso_default_capability_literal = repr(str(cfg.qgis_autostyle_field).replace("capability_", ""))
    basemap_flag = "True" if cfg.qgis_autostyle_basemap else "False"

    # ── Per-service monochrome heatmap specs ────────────────────────────────────
    # Every service is colored using its *capability's* signature color, not a color
    # of its own: all services feeding "nutrition" share the nutrition color map, all
    # services feeding "care" share the care color map, etc. This is computed once per
    # capability and reused for every service under it.
    from utils.capabilities import (
        CAPABILITY_COLORS,
        CAPABILITY_SERVICES,
        ELECTRE_BOUNDS,
        ELECTRE_LABELS,
        ISO_BAND_WIDTHS_MM,
        capability_shade_hexes,
    )

    capability_grid_colors = CAPABILITY_COLORS

    import json as _json_cfg
    service_labels: dict[str, str] = {}
    _labels_path = Path("config") / "service_labels.json"
    try:
        with open(_labels_path, encoding="utf-8") as _lf:
            service_labels = dict(_json_cfg.load(_lf))
    except (FileNotFoundError, ValueError):
        pass

    # Grid fields carry the service scores as grid_mean_service_<service> (see
    # _build_capability_grid). Precompute one spec per service, including the full
    # fill-color expression, entirely here in Python. Building the expression as a
    # plain string (rather than concatenating pieces of it inside the generated QGIS
    # script) avoids nested string-escaping bugs, and baking the 5 stop colors
    # directly into the expression as literal hex constants avoids depending on
    # QgsStyle.defaultStyle() — a color ramp registered there during headless project
    # generation is only visible to that same process's QGIS user profile, and is NOT
    # embedded in the project/GeoPackage, so it can silently fail to resolve (falling
    # back to the symbol's default fill color) when the project is later opened by a
    # different QGIS instance.
    #
    # The color map for a capability is 5 explicit colors, linearly interpolated from
    # white (t=0) to the capability's own color (t=1.0): four progressively lighter
    # hues at t=0.2, 0.4, 0.6, 0.8, and the full color at 1.0 (see
    # utils.capabilities.capability_shade_hexes -- the single source of truth shared
    # with the standalone legend generator). The per-service grids interpolate
    # continuously between these 5 stops; the per-capability grids (below) snap each
    # cell to one of the 5 discrete shades by ELECTRE class.
    _STOP_FRACTIONS = [0.2, 0.4, 0.6, 0.8, 1.0]

    def _stop_color_expr(field_name: str, stops: list[str], fractions: list[float]) -> str:
        value_expr = f'clamp(0, "{field_name}", 1)'
        clauses = [f"WHEN {value_expr} <= {fractions[0]} THEN '{stops[0]}'"]
        for i in range(1, len(fractions)):
            lo, hi = fractions[i - 1], fractions[i]
            ratio_expr = f"(({value_expr} - {lo}) / {hi - lo} * 100)"
            clauses.append(
                f"WHEN {value_expr} <= {hi} THEN color_mix('{stops[i - 1]}', '{stops[i]}', {ratio_expr})"
            )
        return "CASE " + " ".join(clauses) + f" ELSE '{stops[-1]}' END"

    service_grid_specs: list[dict[str, object]] = []
    # One discrete-shade colored grid layer per capability, mirroring the iso-band
    # and per-service layers: its own gpkg view (capability_<name>) so a plain
    # import is styled, filled with the 5 ELECTRE-class shades of the capability's
    # signature color. This replaces the old single diagonal-hatch grid layer.
    capability_grid_specs: list[dict[str, object]] = []
    for _capability, _services in CAPABILITY_SERVICES.items():
        _color_hex = capability_grid_colors.get(_capability)
        if not _color_hex:
            continue
        _stop_hexes = capability_shade_hexes(_color_hex)
        capability_grid_specs.append(
            {
                "capability": _capability,
                "field": f"grid_mean_{_capability}",
                # Per-capability GeoPackage view (see _create_service_grid_views):
                # its own table name lets it carry its 5-shade style as a default,
                # so plain gpkg imports show the capability grids styled.
                "layer": f"capability_{_capability}",
                "label": f"Capability grid: {_capability}",
                "color_hex": _color_hex,
                "shades": _stop_hexes,
            }
        )
        for _service in _services:
            _field_name = f"grid_mean_service_{_service}"
            service_grid_specs.append(
                {
                    "field": _field_name,
                    # Per-service GeoPackage view (see _create_service_grid_views):
                    # its own table name lets it carry its color map as a default
                    # style, so plain gpkg imports show the service grids styled.
                    "layer": f"service_{_service}",
                    "label": service_labels.get(_service, _service),
                    "fill_expr": _stop_color_expr(_field_name, _stop_hexes, _STOP_FRACTIONS),
                    # Static base color for the symbol, distinct from the per-feature
                    # data-defined fill: the layer-tree icon/legend swatch has no feature
                    # to evaluate the expression against, so it always renders this base
                    # color instead. Without it every service layer's icon defaults to the
                    # same color, making them indistinguishable in the Layers panel.
                    "color_hex": _color_hex,
                    # Which capability this service feeds: used by the generated
                    # script to pick the sample service grid shown on first open.
                    "capability": _capability,
                }
            )
    service_grid_specs_literal = repr(service_grid_specs)
    capability_grid_specs_literal = repr(capability_grid_specs)
    electre_bounds_literal = repr(list(ELECTRE_BOUNDS))
    electre_labels_literal = repr(list(ELECTRE_LABELS))
    iso_band_widths_literal = repr(list(ISO_BAND_WIDTHS_MM))


    script = f"""
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsFillSymbol,
    QgsCategorizedSymbolRenderer,
    QgsGraduatedSymbolRenderer,
    QgsNullSymbolRenderer,
    QgsPalLayerSettings,
    QgsProperty,
    QgsReferencedRectangle,
    QgsRendererCategory,
    QgsRendererRange,
    QgsProject,
    QgsRasterLayer,
    QgsSingleSymbolRenderer,
    QgsSymbolLayer,
    QgsTextFormat,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtGui import QColor

app = QgsApplication([], False)
app.initQgis()

project = QgsProject.instance()

project_crs = QgsCoordinateReferenceSystem("EPSG:3857")
project.setCrs(project_crs)

if {basemap_flag}:
    basemap_uri = "type=xyz&url=https://basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}.png&zmin=0&zmax=20&crs=EPSG:3857"
    basemap_layer = QgsRasterLayer(basemap_uri, "CartoDB Positron", "wms")
    if basemap_layer.isValid():
        project.addMapLayer(basemap_layer)

# The old per-node layer (one node per hexagon, 5-class colors) is no longer
# part of the output, and capability_points is unregistered as a gpkg layer
# (its table survives for debug tooling, but OGR no longer lists it), so the
# hex grid is loaded here only to center the project view.
extent_layer = QgsVectorLayer({gpkg_literal} + "|layername={GRID_TABLE_NAME}", "capability_grid_extent", "ogr")
if not extent_layer.isValid():
    extent_layer = QgsVectorLayer({grid_sidecar_literal} + "|layername={GRID_TABLE_NAME}", "capability_grid_extent", "ogr")
if extent_layer.isValid():
    extent_layer.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    layer_extent = extent_layer.extent()
    if not layer_extent.isEmpty():
        project.viewSettings().setDefaultViewExtent(
            QgsReferencedRectangle(layer_extent, extent_layer.crs())
        )
# Unlike every other layer, this one is never added to the project, so Python
# would otherwise destroy it at interpreter shutdown -- after exitQgis() has
# torn down GDAL -- and the late GDALClose segfaults. Drop it here instead.
del extent_layer

electre_bounds = {electre_bounds_literal}
electre_labels = {electre_labels_literal}
n_cls = len(electre_labels)
capability_grid_specs = {capability_grid_specs_literal}
iso_default_capability = {iso_default_capability_literal}
grid_outline_color = {repr(str(cfg.qgis_grid_outline_color))}
grid_outline_width = {repr(str(float(cfg.qgis_grid_outline_width)))}
grid_opacity = float({repr(float(cfg.qgis_grid_opacity))})

# Probe the shared grid table once for its field list -- used to skip a
# capability/service whose column is absent and to gate the service loop below.
_grid_probe = QgsVectorLayer({gpkg_literal} + "|layername={GRID_TABLE_NAME}", "grid_probe", "ogr")
if not _grid_probe.isValid():
    _grid_probe = QgsVectorLayer({grid_sidecar_literal} + "|layername={GRID_TABLE_NAME}", "grid_probe", "ogr")
grid_available_fields = [f.name() for f in _grid_probe.fields()] if _grid_probe.isValid() else []
del _grid_probe

if grid_available_fields:
    # Shared outline layer: draws EVERY cell's border (including hull-fill cells
    # with no measured value), so the study-area shape/perimeter reads even where
    # no colored capability grid covers a cell.
    grid_outline_layer = QgsVectorLayer({gpkg_literal} + "|layername={GRID_TABLE_NAME}", "Capability grid outline", "ogr")
    if not grid_outline_layer.isValid():
        grid_outline_layer = QgsVectorLayer({grid_sidecar_literal} + "|layername={GRID_TABLE_NAME}", "Capability grid outline", "ogr")
    if grid_outline_layer.isValid():
        outline_symbol = QgsFillSymbol.createSimple(
            {{
                "style": "no",
                "outline_color": grid_outline_color,
                "outline_width": grid_outline_width,
            }}
        )
        grid_outline_layer.setRenderer(QgsSingleSymbolRenderer(outline_symbol))
        project.addMapLayer(grid_outline_layer)

    # ── Per-capability colored grids ─────────────────────────────────────────────
    # One graduated layer per capability (care/nutrition/restorativeness), each cell
    # filled with the 5 discrete ELECTRE-class shades of the capability's signature
    # color (white->color, snapped by class -- see utils.capabilities). This replaces
    # the old single diagonal-hatch grid layer. Each layer prefers its own gpkg view
    # (capability_<name>, has_data=1 baked in, own default style) and falls back to
    # the shared grid table with a subset filter. Every cell carries the same thin
    # outline as the shared outline layer so borders read at any zoom.
    capability_grid_layers = {{}}
    for _spec in capability_grid_specs:
        _field = _spec["field"]
        _shades = _spec["shades"]
        _cap = _spec["capability"]
        _name = "Capability grid: " + _cap
        _cap_is_view = True
        _cap_layer = QgsVectorLayer({gpkg_literal} + "|layername=" + _spec["layer"], _name, "ogr")
        if not _cap_layer.isValid():
            _cap_layer = QgsVectorLayer({grid_sidecar_literal} + "|layername=" + _spec["layer"], _name, "ogr")
        if not _cap_layer.isValid():
            _cap_is_view = False
            _cap_layer = QgsVectorLayer({gpkg_literal} + "|layername={GRID_TABLE_NAME}", _name, "ogr")
            if not _cap_layer.isValid():
                _cap_layer = QgsVectorLayer({grid_sidecar_literal} + "|layername={GRID_TABLE_NAME}", _name, "ogr")
        if not _cap_layer.isValid():
            continue
        if _field not in [f.name() for f in _cap_layer.fields()]:
            continue
        if not _cap_is_view and "has_data" in [f.name() for f in _cap_layer.fields()]:
            _cap_layer.setSubsetString("has_data = 1")

        _ranges = []
        for _i, _lbl in enumerate(electre_labels):
            # No hexagon borders on the colored capability grids -- the separate
            # "Capability grid outline" layer carries the cell edges when wanted.
            _sym = QgsFillSymbol.createSimple(
                {{
                    "style": "solid",
                    "color": _shades[_i],
                    "outline_style": "no",
                }}
            )
            _ranges.append(QgsRendererRange(electre_bounds[_i], electre_bounds[_i + 1], _sym, _lbl))
        _cap_layer.setRenderer(QgsGraduatedSymbolRenderer(_field, _ranges))
        _cap_layer.setOpacity(grid_opacity)
        project.addMapLayer(_cap_layer, False)
        _cap_node = project.layerTreeRoot().insertLayer(2, _cap_layer)
        # Only the default capability's grid starts visible (same rule the iso
        # bands use); the others are one click away in the layer panel.
        _cap_node.setItemVisibilityChecked(_cap == iso_default_capability)
        # The per-capability view carries this 5-shade renderer as its DEFAULT gpkg
        # style so a plain import is styled; on the shared-table fallback it stays
        # non-default (reachable via "Load Style from database").
        _cap_layer.saveStyleToDatabase(_cap, "", _cap_is_view, "")
        capability_grid_layers[_cap] = _cap_layer

    # ── Per-service monochrome heatmap grids ─────────────────────────────────────
    # One grid layer per service, each a continuous heatmap fixed on [0, 1] using its
    # capability's color map: 4 progressively lighter hues of the capability color at
    # 0.2/0.4/0.6/0.8, and the full signature color at 1.0 (nutrition=#FFA200,
    # care=#EB4CCC, restorativeness=#006BFF). All services under the same capability
    # share this exact map. Each spec's "fill_expr" (built in Python, see above) is a
    # self-contained CASE/color_mix expression with the 5 stop colors baked in as
    # literal hex constants — no named color ramp to register or resolve, so the
    # coloring works regardless of which QGIS instance/profile opens the project.
    service_grid_specs = {service_grid_specs_literal}
    grid_service_fields = set(f for f in grid_available_fields if f.startswith("grid_mean_service_"))
    # Sample visualization on first open: exactly one service grid starts
    # checked -- the first available service of the default capability (the same
    # capability whose iso bands start visible below).
    _default_service_shown = False
    for spec in service_grid_specs:
        svc_field = spec["field"]
        if svc_field not in grid_service_fields:
            continue
        svc_name = "Service grid: " + spec["label"]
        # Prefer the per-service view (own table name -> own default style on
        # plain gpkg imports, has_data=1 filter baked in). Fall back to the
        # capability_grid table with a subset filter if the views are missing.
        svc_is_view = True
        svc_layer = QgsVectorLayer({gpkg_literal} + "|layername=" + spec["layer"], svc_name, "ogr")
        if not svc_layer.isValid():
            svc_layer = QgsVectorLayer({grid_sidecar_literal} + "|layername=" + spec["layer"], svc_name, "ogr")
        if not svc_layer.isValid():
            svc_is_view = False
            svc_layer = QgsVectorLayer({gpkg_literal} + "|layername={GRID_TABLE_NAME}", svc_name, "ogr")
            if not svc_layer.isValid():
                svc_layer = QgsVectorLayer({grid_sidecar_literal} + "|layername={GRID_TABLE_NAME}", svc_name, "ogr")
        if not svc_layer.isValid():
            continue
        if not svc_is_view and "has_data" in [field.name() for field in svc_layer.fields()]:
            svc_layer.setSubsetString("has_data = 1")

        svc_symbol = QgsFillSymbol.createSimple({{"style": "solid", "outline_style": "no"}})
        # Base color shown by the layer-tree icon/legend swatch (not evaluated per
        # feature); the data-defined override below still drives the actual map fill.
        svc_symbol.setColor(QColor(spec["color_hex"]))
        svc_symbol.symbolLayer(0).setDataDefinedProperty(
            QgsSymbolLayer.PropertyFillColor, QgsProperty.fromExpression(spec["fill_expr"])
        )
        svc_layer.setRenderer(QgsSingleSymbolRenderer(svc_symbol))
        svc_layer.setOpacity(float({repr(float(cfg.qgis_service_grid_opacity))}))
        # All but the sample service start off so the initial map isn't a stack of 11
        # overlapping heatmaps; the user toggles the service of interest on in the
        # layer panel. Inserted at a fixed index below the capability grids and the
        # shared outline (both added earlier), and repositioned explicitly at the end.
        project.addMapLayer(svc_layer, False)
        _svc_node = project.layerTreeRoot().insertLayer(2, svc_layer)
        if not _default_service_shown and spec.get("capability") == {iso_default_capability_literal}:
            _svc_node.setItemVisibilityChecked(True)
            _default_service_shown = True
        else:
            _svc_node.setItemVisibilityChecked(False)

        # Embed the service color map into the GeoPackage layer_styles table.
        # On the per-service view it is saved as the view's DEFAULT style, so a
        # plain gpkg import shows every service grid already styled. On the
        # capability_grid fallback it must stay non-default (the shared table's
        # default is the capability grid) and is reachable via "Load Style from database".
        svc_layer.saveStyleToDatabase(spec["label"], "", svc_is_view, "")

    # ── Capability iso-value bands ───────────────────────────────────────────────
    # One region per ELECTRE class band (Very Low .. Very High), built by dissolving
    # the grid's own hexagon cells in utils/isolines.py -- so band boundaries run
    # exactly along hexagon edges, not a smoothed contour. Unfilled: only the
    # boundary is drawn, in black, with the stroke getting THICKER at higher levels
    # so the level reads from line weight alone. One such layer per capability
    # (nutrition/care/restorativeness), all added to the project so any of them can
    # be toggled on independently; only the capability currently driving the grid's
    # coloring starts visible.
    iso_band_widths_mm = [
        ("Very Low", 0.3),
        ("Low", 0.6),
        ("Medium", 0.9),
        ("High", 1.3),
        ("Very High", 1.8),
    ]
    iso_table_names = {iso_table_names_literal}
    iso_default_capability = {iso_default_capability_literal}
    iso_layers = {{}}
    for _capability, _iso_table in iso_table_names.items():
        _iso_label = f"Capability bands: {{_capability}}"
        iso_layer = QgsVectorLayer({gpkg_literal} + "|layername=" + _iso_table, _iso_label, "ogr")
        if not iso_layer.isValid():
            iso_layer = QgsVectorLayer({grid_sidecar_literal} + "|layername=" + _iso_table, _iso_label, "ogr")
        if not iso_layer.isValid():
            continue
        _iso_categories = []
        for _band_label, _band_width_mm in iso_band_widths_mm:
            _band_symbol = QgsFillSymbol.createSimple(
                {{"style": "no", "outline_color": "#000000", "outline_width": str(_band_width_mm)}}
            )
            _iso_categories.append(QgsRendererCategory(_band_label, _band_symbol, _band_label))
        iso_layer.setRenderer(QgsCategorizedSymbolRenderer("class_name", _iso_categories))
        project.addMapLayer(iso_layer, False)
        # Inserted here, then repositioned explicitly at the end so the iso bands
        # sit at the very top, above the colored grids and service grids.
        _iso_node = project.layerTreeRoot().insertLayer(2, iso_layer)
        _iso_node.setItemVisibilityChecked(_capability == iso_default_capability)
        # Embed as the layer's default gpkg style so a bare drag-in is styled too.
        iso_layer.saveStyleToDatabase("default", "", True, "")
        iso_layers[_capability] = iso_layer

    # Force the final stacking order top -> bottom: iso bands, then the shared
    # cell outline, then the per-capability colored grids, then everything else
    # (per-service color grids, basemap) below that. Done as one explicit pass at
    # the end, after every layer has been added, instead of relying on insertion
    # order above -- QGIS's default legend position for newly added layers is easy
    # to get backwards, and the iso bands + outline need to sit above the colored
    # grids so their borders/bands read on top of the fills.
    #
    # Processed bottom-of-this-group first, top-of-this-group last: each
    # insertChildNode(0, ...) pushes everything already placed down by one, so
    # the last layer processed ends up topmost.
    _root = project.layerTreeRoot()
    _grid_layers = [grid_outline_layer] + list(capability_grid_layers.values())
    _reposition_top_to_bottom = list(iso_layers.values()) + _grid_layers
    for _layer in reversed(_reposition_top_to_bottom):
        if _layer is None or not _layer.isValid():
            continue
        _node = _root.findLayer(_layer.id())
        if _node is None:
            continue
        _clone = _node.clone()
        if _layer is grid_outline_layer:
            # The shared outline (all cells, incl. no-data hull fill) starts
            # unchecked -- the per-cell borders on the colored capability grids
            # already carry the cell edges for measured cells. It stays one click
            # away at the top of the panel for showing the full study-area shape.
            _clone.setItemVisibilityChecked(False)
        _parent = _node.parent()
        _parent.insertChildNode(0, _clone)
        _parent.removeChildNode(_node)

    # Give the shared grid table an OUTLINE-ONLY default gpkg style. This table
    # covers every cell (incl. has_data=0 hull fill), so its natural role is the
    # study-area outline, not another colored fill. It used to carry the default
    # capability's 5-shade renderer, but that made a full-gpkg drag-in show TWO
    # identical pink grids -- this base table AND the capability_<default> view,
    # both colored by the same field. An outline-only default de-duplicates that:
    # the per-capability views (capability_care/nutrition/restorativeness) are the
    # colored fills; this table is just the borders. (A table with no default at
    # all would instead render as a random opaque solid fill on drag-in.)
    _tbl_layer = QgsVectorLayer({gpkg_literal} + "|layername={GRID_TABLE_NAME}", "grid_default_style", "ogr")
    if _tbl_layer.isValid():
        _tbl_outline = QgsFillSymbol.createSimple(
            {{
                "style": "no",
                "outline_color": grid_outline_color,
                "outline_width": grid_outline_width,
            }}
        )
        _tbl_layer.setRenderer(QgsSingleSymbolRenderer(_tbl_outline))
        _tbl_layer.saveStyleToDatabase("default", "", True, "")
    del _tbl_layer

# ── Place labels (comuni + quartieri) ────────────────────────────────────────
# Two label-only point layers, one per kind, the way Google Maps labels an area:
# comuni (towns/municipalities) in normal case, quartieri (neighbourhoods) in
# UPPERCASE. The display casing is already baked into the "label" column (see
# utils/place_labels.py). Each layer uses a single STATIC text style (no
# data-defined expressions) because the qgis2web / OpenLayers exporter ignores
# expression-based styling -- a split, statically-styled pair survives the export
# while a single data-defined layer would flatten to plain text.
#
# Placed at the TOP of the layer tree: in OpenLayers, label draw order follows
# layer order (unlike desktop QGIS, where labels always draw on top), so the
# labels must sit above the grids to render over them in the web export.
#
# Size is in POINTS (fixed screen size). Map-unit sizing would scale the text
# with zoom like the hexagons, but the qgis2web/OpenLayers exporter does NOT
# honor map-unit label sizes -- it read the metre value as pixels and rendered
# giant overlapping text -- so points is the only reliable choice for the web
# export. Font family is pinned to "Open Sans" so QGIS and the browser render
# the same face instead of each substituting its own default.
# (table name, display name, text color, size in points)
_place_label_specs = [
    ({PLACE_COMUNI_TABLE_NAME!r}, "Comuni (labels)", "#1a1a1a", 9.0),
    ({PLACE_QUARTIERI_TABLE_NAME!r}, "Quartieri (labels)", "#333333", 8.0),
]
for _pl_tbl, _pl_disp, _pl_col, _pl_size in _place_label_specs:
    _pl = QgsVectorLayer({gpkg_literal} + "|layername=" + _pl_tbl, _pl_disp, "ogr")
    if not _pl.isValid():
        _pl = QgsVectorLayer({grid_sidecar_literal} + "|layername=" + _pl_tbl, _pl_disp, "ogr")
    if not _pl.isValid():
        continue
    # Label-only: null-symbol renderer draws no marker; labeling still renders.
    _pl.setRenderer(QgsNullSymbolRenderer())

    _pl_settings = QgsPalLayerSettings()
    _pl_settings.fieldName = "label"
    _pl_settings.placement = QgsPalLayerSettings.AroundPoint

    _pl_fmt = QgsTextFormat()
    _pl_fmt.setSize(_pl_size)
    _pl_fmt.setColor(QColor(_pl_col))
    # Bold static "Open Sans" (the regular weight washed out against saturated
    # fills, and an explicit family keeps QGIS and the browser in sync).
    _pl_font = _pl_fmt.font()
    _pl_font.setFamily("Open Sans")
    _pl_font.setBold(True)
    _pl_fmt.setFont(_pl_font)
    # Thin white halo just wide enough to separate text from the basemap/fills.
    _pl_buf = _pl_fmt.buffer()
    _pl_buf.setEnabled(True)
    _pl_buf.setSize(0.4)
    _pl_buf.setColor(QColor("#ffffff"))
    _pl_fmt.setBuffer(_pl_buf)
    _pl_settings.setFormat(_pl_fmt)

    _pl.setLabeling(QgsVectorLayerSimpleLabeling(_pl_settings))
    _pl.setLabelsEnabled(True)

    project.addMapLayer(_pl, False)
    _pl_node = project.layerTreeRoot().insertLayer(0, _pl)
    _pl_node.setItemVisibilityChecked(True)
    # Embed as the layer's default gpkg style so a bare drag-in is labelled too.
    _pl.saveStyleToDatabase("default", "", True, "")

try:
    project.setFilePathStorage(QgsProject.Relative)
except Exception:
    pass
project.write({output_literal})
app.exitQgis()
"""

    prefix = _host_cmd_prefix()

    # When we run the launcher on the host (Flatpak sandbox case), the host process cannot see
    # our sandbox /tmp. Write the script under the (host-visible) output dir at an absolute path
    # so both sides resolve it identically. Otherwise a normal tempfile is fine.
    if prefix:
        script_path = str((output_path.parent / "_generate_qgis_project.py").resolve())
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(script)
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
            handle.write(script)
            script_path = handle.name

    try:
        # Project generation needs no GUI; force the offscreen Qt platform so it also works
        # over SSH / on headless machines without a display. setdefault lets an explicit
        # QT_QPA_PLATFORM override win.
        if prefix:
            subprocess.run(
                prefix + ["--env=QT_QPA_PLATFORM=offscreen", python_launcher, script_path],
                check=True,
            )
        else:
            gen_env = dict(os.environ)
            gen_env.setdefault("QT_QPA_PLATFORM", "offscreen")
            subprocess.run([python_launcher, script_path], check=True, env=gen_env)
    except subprocess.CalledProcessError as exc:
        print(f"[QGIS] Failed to generate project: {exc}", flush=True)
        # Still return the path if it exists from a previous run
        return output_path if output_path.exists() else None

    return output_path


def _maybe_open_qgis(cfg: PipelineConfig, gpkg_path: Path | None) -> None:
    if not cfg.open_qgis_after_run:
        return

    qgis_exe = _resolve_qgis_executable(cfg)
    if not qgis_exe:
        raise RuntimeError(
            "[QGIS] Launch requested but no executable detected. "
            "Set qgis_bin_path or QGIS_BIN_PATH, or add QGIS to PATH."
        )

    project_path: str | None = None
    
    # First, try to build/generate the auto-styled project
    if gpkg_path is not None:
        generated_project = _build_qgis_project(cfg, gpkg_path, qgis_exe)
        if generated_project is not None:
            project_path = str(generated_project)
    
    # If no auto-generated project, check for a pre-existing auto-generated one
    if not project_path and gpkg_path is not None:
        auto_project_path = Path("outputs") / "qgis" / cfg.artifact_slug / "capability.qgz"
        if auto_project_path.exists():
            project_path = str(auto_project_path)

    # Finally, fall back to custom project path if set
    if not project_path and cfg.qgis_project_path:
        project_path = cfg.qgis_project_path

    prefix = _host_cmd_prefix()

    args = [qgis_exe]
    # Use absolute paths: when launched on the host via flatpak-spawn the working directory
    # may differ, so a relative project/gpkg path would not resolve.
    if project_path:
        args.append(str(Path(project_path).resolve()))
    elif gpkg_path is not None:
        args.append(str(gpkg_path.resolve()))

    if len(args) == 1:
        print("[QGIS] Skipping launch: no project or output path to open.", flush=True)
        return

    # Detach so QGIS keeps running independently once the pipeline exits, and don't let it
    # inherit/clutter our stdio. start_new_session is POSIX-only.
    popen_kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    subprocess.Popen(prefix + args, **popen_kwargs)
    launched_via = " (via flatpak-spawn --host)" if prefix else ""
    print(f"[QGIS] Launched QGIS: {qgis_exe}{launched_via}", flush=True)


def get_or_compute_impedances(ctx, cfg):
    """
    Load the impedance bundle if available.
    Otherwise run snapping, bus routing, and non-bus routing.
    """

    loaded = load_impedance_bundle(ctx)

    if loaded is not None:
        print(
            "[Artifact] Loaded impedance bundle. "
            "Skipping snapping, bus routing, and non-bus routing.",
            flush=True,
        )
        bus, non_bus = loaded
        return bus, non_bus

    print(
        "[Artifact] No valid impedance bundle found; recomputing impedances.",
        flush=True,
    )

    print("[Stage] Snapping", flush=True)
    # A crash in a later stage (e.g. bus routing) leaves no impedance bundle, so this
    # function re-runs from snapping. The snap checkpoint lets that restart skip the
    # stage's uncached per-run work (POI dedup, radius filter, CSR loads) entirely.
    snap = load_snap_checkpoint(ctx)
    if snap is not None:
        print("[Snap] Loaded snapping checkpoint; skipping snapping recompute.", flush=True)
    else:
        snap = run_snapping_stage(ctx)
        write_snap_checkpoint(ctx, snap)

    print("[Stage] POI Export (pre-routing)", flush=True)
    pre_route_poi_exports = generate_poi_exports(ctx, snap=snap)
    print(
        "[Output] Pre-routing POI geopackage: "
        + ", ".join(str(path) for path in pre_route_poi_exports.values()),
        flush=True,
    )

    print("[Stage] Bus Routing", flush=True)
    bus = run_public_transport_routing_stage(ctx, snap, transport_type="bus")

    # Subway is routed as an independent second modality when enabled (e.g. France/IDFM).
    if cfg.enable_subway:
        print("[Stage] Subway Routing", flush=True)
        run_public_transport_routing_stage(ctx, snap, transport_type="metro")

    print("[Stage] Non-Bus Routing", flush=True)
    non_bus = run_non_bus_routing_stage(ctx, snap)

    write_impedance_bundle(ctx, bus, non_bus)

    print(
        f"[Artifact] Wrote impedance bundle: {cfg.impedance_artifact_path}",
        flush=True,
    )

    return bus, non_bus


def compute_capabilities_from_impedances(ctx, cfg, bus, non_bus):
    """
    Run the post-routing part of the capability model:
    accessibility → services → capabilities.
    """

    print("[Stage] Accessibility", flush=True)
    acc = run_accessibility_stage(ctx, non_bus, bus)

    print("[Stage] POI Export (post-routing)", flush=True)
    poi_exports = generate_poi_exports(ctx, non_bus=non_bus, acc=acc)

    print("[Stage] Score Report (hex_pois service/capability power)", flush=True)
    generate_score_report(ctx=ctx)

    print("[Stage] Service Aggregation", flush=True)
    svc = run_service_stage(ctx, acc)

    print("[Stage] Capability Aggregation", flush=True)
    cap = run_capability_stage(ctx, svc)

    print(
        "[Output] Capability CSV files: "
        + ", ".join(str(path) for path in cap.output_paths.values()),
        flush=True,
    )
    print(
        "[Output] POI export files: "
        + ", ".join(str(path) for path in poi_exports.values()),
        flush=True,
    )

    return cap


def generate_spatial_outputs(cfg, cap):
    """
    Generate the spatial output layer from the capability CSV files.
    """

    run_experiment_paths = list(cap.output_paths.values())

    generated_plots = plot_all_experiments(
        csv_paths=run_experiment_paths,
        k_meters=10,
    )

    print(f"[Output] Generated plots: {len(generated_plots)}", flush=True)

    shapefile_output_path = None
    try:
        shapefile_output_path = generate_combined_experiment_shapefile(
            run_experiment_paths,
            output_path=Path("outputs")
            / "shapefiles"
            / cfg.artifact_slug
            / f"{cfg.artifact_slug}.shp",
        )
        print(f"[Output] Combined shapefile: {shapefile_output_path}", flush=True)
        if not Path(shapefile_output_path).exists():
            print("[Output] Warning: shapefile path does not exist after export.", flush=True)
    except Exception as exc:
        print(f"[Output] Failed to write shapefile: {exc}", flush=True)

    gpkg_output_path = Path("outputs") / "gpkg" / cfg.artifact_slug / f"{cfg.artifact_slug}.gpkg"
    try:
        gpkg_output_path = generate_combined_experiment_gpkg(
            run_experiment_paths,
            output_path=gpkg_output_path,
            grid_enabled=cfg.qgis_grid_enabled,
            grid_cell_size_m=cfg.qgis_grid_cell_size_m,
            grid_capability_field=cfg.qgis_autostyle_field,
            grid_max_cells=cfg.qgis_grid_max_cells,
            grid_fill_hull=cfg.qgis_grid_fill_hull,
            grid_hull_buffer_m=cfg.qgis_grid_hull_buffer_m,
            grid_hull_ratio=cfg.qgis_grid_hull_ratio,
            grid_params_path=os.path.join(cfg.poi_export_dir, "grid_params.json"),
            grid_exclude_water=cfg.qgis_grid_exclude_water,
            grid_water_cache_path=os.path.join(cfg.poi_export_dir, "water_mask.gpkg"),
            place_labels_enabled=cfg.qgis_place_labels_enabled,
            place_labels_cache_path=os.path.join(cfg.poi_export_dir, "place_labels.gpkg"),
        )
        print(f"[Output] Combined GeoPackage: {gpkg_output_path}", flush=True)
    except Exception as exc:
        print(f"[Output] Failed to write GeoPackage: {exc}", flush=True)
        gpkg_output_path = None

    _maybe_open_qgis(cfg, gpkg_output_path)

    return {
        "shapefile_path": shapefile_output_path,
        "gpkg_path": gpkg_output_path,
    }


def run_full_pipeline(cfg=None):
    """
    Full runtime:
    snapping → routing → impedance bundle → accessibility → services → capabilities.
    This is what main.py should call.
    """

    if cfg is None:
        cfg = PipelineConfig()

    ctx = build_context(cfg)

    bus, non_bus = get_or_compute_impedances(ctx, cfg)
    cap = compute_capabilities_from_impedances(ctx, cfg, bus, non_bus)
    spatial_outputs = generate_spatial_outputs(cfg, cap)

    return {
        "capability_csv_paths": cap.output_paths,
        "spatial_output_path": spatial_outputs["gpkg_path"] or spatial_outputs["shapefile_path"],
        "spatial_output_shapefile_path": spatial_outputs["shapefile_path"],
        "spatial_output_gpkg_path": spatial_outputs["gpkg_path"],
    }


def run_post_impedance_pipeline(cfg=None):
    """
    QGIS-friendly runtime:
    assumes the impedance bundle already exists and runs only:
    accessibility → services → capabilities → spatial output.
    """

    if cfg is None:
        cfg = PipelineConfig()

    ctx = build_context(cfg)

    loaded = load_impedance_bundle(ctx)

    if loaded is None:
        raise FileNotFoundError(
            f"No valid impedance bundle found at: {cfg.impedance_artifact_path}"
        )

    bus, non_bus = loaded

    cap = compute_capabilities_from_impedances(ctx, cfg, bus, non_bus)
    spatial_outputs = generate_spatial_outputs(cfg, cap)

    return {
        "capability_csv_paths": cap.output_paths,
        "spatial_output_path": spatial_outputs["gpkg_path"] or spatial_outputs["shapefile_path"],
        "spatial_output_shapefile_path": spatial_outputs["shapefile_path"],
        "spatial_output_gpkg_path": spatial_outputs["gpkg_path"],
    }
