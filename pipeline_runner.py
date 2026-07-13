from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from config import PipelineConfig
from context import build_context
from snapping_stage import run_snapping_stage, load_snap_checkpoint, write_snap_checkpoint
from public_transport_routing_stage import run_public_transport_routing_stage
from non_bus_routing_stage import run_non_bus_routing_stage
from accessibility_stage import run_accessibility_stage
from service_stage import run_service_stage
from capability_stage import run_capability_stage
from artifact_bundle import load_impedance_bundle, write_impedance_bundle
from poi_exports import generate_poi_exports
from score_report import generate_score_report
from plot_shapefile import plot_all_experiments
from generate_experiment_shapefiles import (
    GRID_TABLE_NAME,
    ISOBANDS_TABLE_NAMES,
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
    grid_field_literal = repr(str(cfg.qgis_autostyle_field))
    iso_table_names_literal = repr(ISOBANDS_TABLE_NAMES)
    iso_default_capability_literal = repr(str(cfg.qgis_autostyle_field).replace("capability_", ""))
    ramp_literal = repr(str(cfg.qgis_autostyle_ramp))
    basemap_flag = "True" if cfg.qgis_autostyle_basemap else "False"

    # ── Per-service monochrome heatmap specs ────────────────────────────────────
    # Every service is colored using its *capability's* signature color, not a color
    # of its own: all services feeding "nutrition" share the nutrition color map, all
    # services feeding "care" share the care color map, etc. This is computed once per
    # capability and reused for every service under it.
    capability_grid_colors = {
        "nutrition": "#FFA200",
        "care": "#EB4CCC",
        "restorativeness": "#006BFF",
    }
    from utils.capabilities import CAPABILITY_SERVICES

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
    # hues at t=0.2, 0.4, 0.6, 0.8, and the full color at 1.0. With the hatch grid now
    # drawn on top, this plain white->color ramp reads more clearly than a
    # higher-contrast floor did, so intensity is judged relative to true white.
    from matplotlib.colors import LinearSegmentedColormap

    _STOP_FRACTIONS = [0.2, 0.4, 0.6, 0.8, 1.0]
    _MIN_BLEND_FLOOR = 0.0

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
    for _capability, _services in CAPABILITY_SERVICES.items():
        _color_hex = capability_grid_colors.get(_capability)
        if not _color_hex:
            continue
        _shade_cmap = LinearSegmentedColormap.from_list(f"{_capability}_shades", ["white", _color_hex])
        _stop_hexes = []
        for _frac in _STOP_FRACTIONS:
            _blend = _MIN_BLEND_FLOOR + (1.0 - _MIN_BLEND_FLOOR) * _frac
            _r, _g, _b, _ = _shade_cmap(_blend)
            _stop_hexes.append(
                "#{:02x}{:02x}{:02x}".format(round(_r * 255), round(_g * 255), round(_b * 255))
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


    script = f"""
from qgis.core import (
    QgsApplication,
    QgsClassificationEqualInterval,
    QgsCoordinateReferenceSystem,
    QgsFillSymbol,
    QgsGradientColorRamp,
    QgsCategorizedSymbolRenderer,
    QgsGraduatedSymbolRenderer,
    QgsLinePatternFillSymbolLayer,
    QgsProperty,
    QgsReferencedRectangle,
    QgsRendererCategory,
    QgsRendererRange,
    QgsProject,
    QgsRasterLayer,
    QgsSingleSymbolRenderer,
    QgsSymbolLayer,
    QgsStyle,
    QgsUnitTypes,
    QgsVectorLayer,
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

electre_bounds = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
electre_labels = ["Very Low (0.0–0.2)", "Low (0.2–0.4)", "Medium (0.4–0.6)", "High (0.6–0.8)", "Very High (0.8–1.0)"]
n_cls = len(electre_labels)

grid_layer = QgsVectorLayer({gpkg_literal} + "|layername={GRID_TABLE_NAME}", "Capability grid", "ogr")
if not grid_layer.isValid():
    grid_layer = QgsVectorLayer({grid_sidecar_literal} + "|layername={GRID_TABLE_NAME}", "Capability grid", "ogr")
if grid_layer.isValid():
    # The colored (graduated) layer shows measured cells only. Hull-fill cells have null
    # values so they wouldn't be colored anyway; the separate outline layer below draws every
    # cell's border, so the filled study-area shape/perimeter still reads clearly.
    if "has_data" in [field.name() for field in grid_layer.fields()]:
        grid_layer.setSubsetString("has_data = 1")

    grid_available_fields = [field.name() for field in grid_layer.fields()]
    preferred_grid_fields = [
        "grid_mean_" + {grid_field_literal}.replace("capability_", ""),
        "grid_mean_care",
        "grid_mean_restorativeness",
        "grid_mean_nutrition",
    ]
    grid_field_name = next((name for name in preferred_grid_fields if name in grid_available_fields), None)
    if grid_field_name is None:
        grid_field_name = next((f for f in grid_available_fields if f.startswith("grid_mean_")), None)
    if grid_field_name is None:
        grid_field_name = grid_available_fields[0] if grid_available_fields else "grid_mean"
    grid_ramp = QgsStyle.defaultStyle().colorRamp({ramp_literal})
    if grid_ramp is None:
        grid_ramp = QgsGradientColorRamp(QColor("#440154"), QColor("#FDE725"))
    # No fill color: each class is a diagonal hatch of parallel lines, sparse at
    # Very Low and packed tight at Very High, so density (line spacing) alone encodes
    # the level. The stroke width is kept thin and constant across all classes -- an
    # earlier version scaled width up with density (via a coverage fraction), which at
    # the tightest (Very High) spacing left barely any gap between strokes: the lines
    # merged into what looked like a solid black mass, hiding the service color grid
    # underneath entirely. Keeping width fixed means even the densest class still
    # leaves most of the cell showing its actual fill color.
    #
    # Spacing and width are expressed in METERS-in-map-units (RenderMetersInMapUnits),
    # NOT millimeters, so the pattern is locked to the hexagon: every cell shows the
    # same number of stripes at every zoom level (the strokes get thicker on screen as
    # you zoom in, but the count per cell is invariant). Millimeters would instead hold
    # the on-screen stroke width constant and change how many stripes fall inside a
    # cell as you zoom. The spacing ladder is derived from the ground cell size so the
    # density reads the same regardless of the configured grid resolution.
    grid_cell_size_m = {repr(float(cfg.qgis_grid_cell_size_m))}
    # FEW lines, THICK strokes. Keeping the line count low (~2 lines for Very Low up to
    # ~5 for Very High) avoids the "grey wash" that many thin lines produce when they
    # blur together at the whole-map view. The level is carried mostly by stroke
    # THICKNESS: at full extent a cell reads as one gray tone equal to the ink coverage
    # (width / spacing), and thick strokes give a wide, legible tone ramp (~8% -> ~62%)
    # while keeping only a handful of clearly separated lines per cell.
    #
    # Line COUNT per class via spacing (fraction of cell): ~2 -> 5 lines per cell.
    hatch_distance_fractions = [1 / 2, 1 / 2.5, 1 / 3, 1 / 4, 1 / 5]
    # Line THICKNESS per class (fraction of cell). Coverage = width / spacing runs
    # ~8% / 15% / 25% / 40% / 62% -- Very Low a couple of thin lines, Very High a few
    # thick ones (still < solid, so a gap survives for the service color underneath).
    hatch_line_width_fractions = [1 / 25, 1 / 17, 1 / 12, 1 / 10, 1 / 8]
    ranges_grid = []
    for i, lbl in enumerate(electre_labels):
        hatch = QgsLinePatternFillSymbolLayer()
        hatch.setLineAngle(45)
        distance_m = grid_cell_size_m * hatch_distance_fractions[i]
        hatch.setDistance(distance_m)
        hatch.setLineWidth(grid_cell_size_m * hatch_line_width_fractions[i])
        hatch.setColor(QColor("#000000"))
        # Render spacing/width/offset as meters-at-scale so they track ground
        # distance (constant stripes per hexagon), independent of the layer CRS.
        hatch.setOutputUnit(QgsUnitTypes.RenderMetersInMapUnits)
        sym = QgsFillSymbol()
        sym.changeSymbolLayer(0, hatch)
        ranges_grid.append(QgsRendererRange(electre_bounds[i], electre_bounds[i + 1], sym, lbl))
    if ranges_grid:
        grid_renderer = QgsGraduatedSymbolRenderer(grid_field_name, ranges_grid)
    else:
        grid_renderer = QgsGraduatedSymbolRenderer()
        grid_renderer.setClassAttribute(grid_field_name)
        grid_renderer.setMode(QgsGraduatedSymbolRenderer.EqualInterval)
        grid_renderer.updateClasses(grid_layer, n_cls)
        grid_renderer.updateColorRamp(grid_ramp)

    grid_layer.setRenderer(grid_renderer)

    grid_layer.setOpacity(float({repr(float(cfg.qgis_grid_opacity))}))
    project.addMapLayer(grid_layer)

    grid_outline_layer = QgsVectorLayer({gpkg_literal} + "|layername={GRID_TABLE_NAME}", "Capability grid outline", "ogr")
    if not grid_outline_layer.isValid():
        grid_outline_layer = QgsVectorLayer({grid_sidecar_literal} + "|layername={GRID_TABLE_NAME}", "Capability grid outline", "ogr")
    if grid_outline_layer.isValid():
        outline_symbol = QgsFillSymbol.createSimple(
            {{
                "style": "no",
                "outline_color": {repr(str(cfg.qgis_grid_outline_color))},
                "outline_width": {repr(str(float(cfg.qgis_grid_outline_width)))},
            }}
        )
        grid_outline_layer.setRenderer(QgsSingleSymbolRenderer(outline_symbol))
        project.addMapLayer(grid_outline_layer)

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
        # layer panel. Inserted at a fixed index below the capability hatch grid and
        # its outline (both added earlier, so they occupy indices 0-1) so the hatch
        # pattern stays legible on top of the service color fill when toggled on.
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
        # capability_grid fallback it must stay non-default (the hatch grid owns
        # that table's default) and is reachable via "Load Style from database".
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
        # Insert below the hatch grid + outline (moved to indices 0-1 afterwards) but
        # above the toggled-off service grids.
        _iso_node = project.layerTreeRoot().insertLayer(2, iso_layer)
        _iso_node.setItemVisibilityChecked(_capability == iso_default_capability)
        # Embed as the layer's default gpkg style so a bare drag-in is styled too.
        iso_layer.saveStyleToDatabase("default", "", True, "")
        iso_layers[_capability] = iso_layer

    # Force the hatch grid (and its outline) to the very top of the layer tree,
    # above every per-service color grid. The hatch layer has no base fill --
    # only the diagonal lines -- specifically so a service grid underneath
    # still shows through the gaps; that only works if the hatch is drawn last
    # (topmost). This is done explicitly, after every other layer has been
    # added, instead of relying on the insertion order above, since QGIS's
    # default legend-position for newly added layers is easy to get backwards
    # and silently bury the hatch under the solid, more opaque service fills.
    _root = project.layerTreeRoot()
    for _top_layer in (grid_layer, grid_outline_layer):
        if not _top_layer.isValid():
            continue
        _node = _root.findLayer(_top_layer.id())
        if _node is None:
            continue
        _clone = _node.clone()
        # Start unchecked: the first-open sample view is basemap + iso bands +
        # one service grid only. The hatch grid and outline stay one click away
        # at the top of the panel, and still render above everything when on.
        _clone.setItemVisibilityChecked(False)
        _parent = _node.parent()
        _parent.insertChildNode(0, _clone)
        _parent.removeChildNode(_node)

# Embed the capability-grid hatch style into the GeoPackage layer_styles table
# as the default, so the file is self-describing: a colleague opening the .gpkg
# directly gets the same renderer without needing this project file.
if grid_layer.isValid():
    grid_layer.saveStyleToDatabase(
        "default", "", True, ""
    )

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
