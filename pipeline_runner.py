from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from config import PipelineConfig
from context import build_context
from snapping_stage import run_snapping_stage
from public_transport_routing_stage import run_public_transport_routing_stage
from non_bus_routing_stage import run_non_bus_routing_stage
from accessibility_stage import run_accessibility_stage
from service_stage import run_service_stage
from capability_stage import run_capability_stage
from artifact_bundle import load_impedance_bundle, write_impedance_bundle
from poi_exports import generate_poi_exports
from plot_shapefile import plot_all_experiments
from generate_experiment_shapefiles import (
    generate_combined_experiment_gpkg,
    generate_combined_experiment_shapefile,
)


def _resolve_qgis_executable(cfg: PipelineConfig) -> str | None:
    if cfg.qgis_bin_path:
        return cfg.qgis_bin_path

    env_path = os.getenv("QGIS_BIN_PATH")
    if env_path:
        return env_path

    qgis_on_path = shutil.which("qgis-ltr-bin.exe") or shutil.which("qgis-bin.exe") or shutil.which("qgis")
    if qgis_on_path:
        return qgis_on_path

    # macOS and Linux detection (non-Windows). Covers distro packages, snap, and flatpak —
    # the flatpak/snap export wrappers are single executables that forward args (the project
    # path), so they work with the same Popen([exe, project]) launch path below.
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
            if os.path.exists(candidate):
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


def _python_has_pyqgis(python_path: str) -> bool:
    """Return True if the given interpreter can import PyQGIS (qgis.core)."""
    try:
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
    qgis_bin_dir = Path(qgis_exe).parent

    # Windows: the .bat launchers set up the QGIS environment before invoking python, so they
    # are the canonical entry point — return them directly without an import probe.
    windows_candidates = [
        qgis_bin_dir / "python-qgis-ltr.bat",
        qgis_bin_dir / "python-qgis.bat",
    ]
    for candidate in windows_candidates:
        if candidate.exists():
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
    which_py = shutil.which("python3")
    if which_py:
        linux_candidates.append(Path(which_py))

    # Crucially, only accept an interpreter that can actually import PyQGIS. The previous code
    # returned the first python3 it found, which on Linux could be one *without* the bindings
    # (silently breaking the styling step). The import probe makes this correct by construction.
    seen: set[str] = set()
    for candidate in mac_candidates + linux_candidates:
        path = str(candidate)
        if path in seen or not candidate.exists():
            continue
        seen.add(path)
        if _python_has_pyqgis(path):
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
    field_literal = repr(str(cfg.qgis_autostyle_field))
    grid_field_literal = repr(str(cfg.qgis_autostyle_field))
    ramp_literal = repr(str(cfg.qgis_autostyle_ramp))
    basemap_flag = "True" if cfg.qgis_autostyle_basemap else "False"


    script = f"""
from qgis.core import (
    QgsApplication,
    QgsClassificationEqualInterval,
    QgsCoordinateReferenceSystem,
    QgsFillSymbol,
    QgsGradientColorRamp,
    QgsGraduatedSymbolRenderer,
    QgsReferencedRectangle,
    QgsRendererRange,
    QgsProject,
    QgsRasterLayer,
    QgsSingleSymbolRenderer,
    QgsSymbol,
    QgsStyle,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor

app = QgsApplication([], False)
app.initQgis()

project = QgsProject.instance()

project_crs = QgsCoordinateReferenceSystem("EPSG:3857")
project.setCrs(project_crs)

if {basemap_flag}:
    osm_uri = "type=xyz&url=https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png&zmin=0&zmax=19&crs=EPSG:3857"
    osm_layer = QgsRasterLayer(osm_uri, "OSM", "wms")
    if osm_layer.isValid():
        project.addMapLayer(osm_layer)

layer = QgsVectorLayer({gpkg_literal}, "Capability model output", "ogr")
if not layer.isValid():
    layer = QgsVectorLayer({gpkg_literal} + "|layername=capability_points", "Capability model output", "ogr")
if not layer.isValid():
    raise SystemExit("Failed to load GeoPackage layer: " + {gpkg_literal})

layer_crs = QgsCoordinateReferenceSystem("EPSG:4326")
layer.setCrs(layer_crs)
project.addMapLayer(layer)

# Persist project startup extent so QGIS opens centered on the output layer.
layer_extent = layer.extent()
if not layer_extent.isEmpty():
    project.viewSettings().setDefaultViewExtent(
        QgsReferencedRectangle(layer_extent, layer.crs())
    )

available_fields = [field.name() for field in layer.fields()]
preferred_fields = [{field_literal}, "capability_care", "capability_restorativeness", "capability_nutrition", "value"]
field_name = next((name for name in preferred_fields if name in available_fields), None)

electre_bounds = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
electre_labels = ["Very Low (0.0–0.2)", "Low (0.2–0.4)", "Medium (0.4–0.6)", "High (0.6–0.8)", "Very High (0.8–1.0)"]
n_cls = len(electre_labels)

if field_name:
    ramp = QgsStyle.defaultStyle().colorRamp({ramp_literal})
    if ramp is None:
        ramp = QgsGradientColorRamp(QColor("#440154"), QColor("#FDE725"))
    ranges_pt = []
    for i, lbl in enumerate(electre_labels):
        sym = QgsSymbol.defaultSymbol(layer.geometryType())
        if sym is None:
            continue
        sym.setColor(ramp.color(float(i) / max(1, n_cls - 1)))
        ranges_pt.append(QgsRendererRange(electre_bounds[i], electre_bounds[i + 1], sym, lbl))
    renderer = QgsGraduatedSymbolRenderer(field_name, ranges_pt)
    layer.setRenderer(renderer)

grid_layer = QgsVectorLayer({gpkg_literal} + "|layername=capability_grid", "Capability grid", "ogr")
if not grid_layer.isValid():
    grid_layer = QgsVectorLayer({grid_sidecar_literal} + "|layername=capability_grid", "Capability grid", "ogr")
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
    ranges_grid = []
    for i, lbl in enumerate(electre_labels):
        sym = QgsSymbol.defaultSymbol(grid_layer.geometryType())
        if sym is None:
            continue
        sym.setColor(grid_ramp.color(float(i) / max(1, n_cls - 1)))
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

    grid_outline_layer = QgsVectorLayer({gpkg_literal} + "|layername=capability_grid", "Capability grid outline", "ogr")
    if not grid_outline_layer.isValid():
        grid_outline_layer = QgsVectorLayer({grid_sidecar_literal} + "|layername=capability_grid", "Capability grid outline", "ogr")
    if grid_outline_layer.isValid():
        outline_symbol = QgsFillSymbol.createSimple(
            {{
                "style": "no",
                "outline_color": "90,90,90,170",
                "outline_width": "0.2",
            }}
        )
        grid_outline_layer.setRenderer(QgsSingleSymbolRenderer(outline_symbol))
        project.addMapLayer(grid_outline_layer)

# Embed styles into the GeoPackage layer_styles table so the file is
# self-describing: a colleague opening the .gpkg directly gets the same
# graduated renderer without needing this project file.
for styled_layer in [layer, grid_layer]:
    if styled_layer.isValid():
        styled_layer.saveStyleToDatabase(
            "default", "", True, ""
        )

try:
    project.setFilePathStorage(QgsProject.Relative)
except Exception:
    pass
project.write({output_literal})
app.exitQgis()
"""

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        script_path = handle.name

    try:
        # Project generation needs no GUI; force the offscreen Qt platform so it also works
        # over SSH / on headless machines without a display. setdefault lets an explicit
        # QT_QPA_PLATFORM override win.
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

    args = [qgis_exe]
    if project_path:
        args.append(project_path)
    elif gpkg_path is not None:
        args.append(str(gpkg_path))

    if len(args) == 1:
        print("[QGIS] Skipping launch: no project or output path to open.", flush=True)
        return

    # Detach so QGIS keeps running independently once the pipeline exits, and don't let it
    # inherit/clutter our stdio. start_new_session is POSIX-only.
    popen_kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    subprocess.Popen(args, **popen_kwargs)
    print(f"[QGIS] Launched QGIS: {qgis_exe}", flush=True)


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
    snap = run_snapping_stage(ctx)

    print("[Stage] POI Export (pre-routing)", flush=True)
    pre_route_poi_exports = generate_poi_exports(ctx, snap=snap)
    print(
        "[Output] Pre-routing POI geopackage: "
        + ", ".join(str(path) for path in pre_route_poi_exports.values()),
        flush=True,
    )

    print("[Stage] Bus Routing", flush=True)
    bus = run_public_transport_routing_stage(ctx, snap, transport_type="bus")

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
