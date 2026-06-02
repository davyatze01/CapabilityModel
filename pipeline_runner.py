from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from config import PipelineConfig
from context import build_context
from snapping_stage import run_snapping_stage
from bus_routing_stage import run_bus_routing_stage
from non_bus_routing_stage import run_non_bus_routing_stage
from accessibility_stage import run_accessibility_stage
from service_stage import run_service_stage
from capability_stage import run_capability_stage
from artifact_bundle import load_impedance_bundle, write_impedance_bundle
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

    # Mac detection
    if os.name != "nt":
        mac_candidates = [
            "/Applications/QGIS.app/Contents/MacOS/QGIS",
            "/Applications/QGIS-LTR.app/Contents/MacOS/QGIS",
            "/usr/local/bin/qgis",
            "/opt/homebrew/bin/qgis",
        ]
        for candidate in mac_candidates:
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


def _resolve_qgis_python_launcher(qgis_exe: str) -> str | None:
    qgis_bin_dir = Path(qgis_exe).parent
    
    # Windows launcher candidates
    windows_candidates = [
        qgis_bin_dir / "python-qgis-ltr.bat",
        qgis_bin_dir / "python-qgis.bat",
    ]
    for candidate in windows_candidates:
        if candidate.exists():
            return str(candidate)
    
    # Mac launcher candidates
    qgis_contents_dir = qgis_bin_dir.parent
    qgis_app_dir = qgis_contents_dir.parent
    mac_candidates = [
        qgis_bin_dir / "python-qgis-ltr",
        qgis_bin_dir / "python-qgis",
        qgis_bin_dir / "python3",
        qgis_bin_dir / "bin" / "python3",
        qgis_contents_dir / "Resources" / "python" / "bin" / "python3",
        qgis_contents_dir / "Resources" / "bin" / "python3",
        qgis_app_dir / "Contents" / "MacOS" / "bin" / "python3",
    ]
    for candidate in mac_candidates:
        if candidate.exists():
            return str(candidate)
    
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

    gpkg_literal = repr(str(gpkg_path))
    grid_sidecar_path = gpkg_path.with_name(f"{gpkg_path.stem}_grid{gpkg_path.suffix}")
    grid_sidecar_literal = repr(str(grid_sidecar_path))
    output_literal = repr(str(output_path))
    field_literal = repr(str(cfg.qgis_autostyle_field))
    grid_field_literal = repr(str(cfg.qgis_autostyle_field))
    ramp_literal = repr(str(cfg.qgis_autostyle_ramp))
    basemap_flag = "True" if cfg.qgis_autostyle_basemap else "False"
    classes_count = int(cfg.qgis_autostyle_classes)

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
if field_name:
    renderer = QgsGraduatedSymbolRenderer()
    renderer.setClassAttribute(field_name)
    renderer.setMode(QgsGraduatedSymbolRenderer.EqualInterval)
    renderer.updateClasses(layer, int({classes_count}))

    ramp = QgsStyle.defaultStyle().colorRamp({ramp_literal})
    if ramp is None:
        ramp = QgsGradientColorRamp(QColor("#440154"), QColor("#FDE725"))
    renderer.updateColorRamp(ramp)
    layer.setRenderer(renderer)

grid_layer = QgsVectorLayer({gpkg_literal} + "|layername=capability_grid", "Capability grid", "ogr")
if not grid_layer.isValid():
    grid_layer = QgsVectorLayer({grid_sidecar_literal} + "|layername=capability_grid", "Capability grid", "ogr")
if grid_layer.isValid():
    if "has_data" in [field.name() for field in grid_layer.fields()]:
        grid_layer.setSubsetString("has_data = 1")

    grid_available_fields = [field.name() for field in grid_layer.fields()]
    preferred_grid_fields = [
        "grid_mean_" + {grid_field_literal}.replace("capability_", ""),
        "grid_mean_care",
        "grid_mean_restorativeness",
        "grid_mean_nutrition",
        "grid_mean",
    ]
    grid_field_name = next((name for name in preferred_grid_fields if name in grid_available_fields), "grid_mean")
    grid_ramp = QgsStyle.defaultStyle().colorRamp({ramp_literal})
    if grid_ramp is None:
        grid_ramp = QgsGradientColorRamp(QColor("#440154"), QColor("#FDE725"))
    grid_renderer = QgsGraduatedSymbolRenderer()
    grid_renderer.setClassAttribute(grid_field_name)

    # Keep grid classification aligned with the point-layer value scale.
    ranges = []
    if field_name:
        idx = layer.fields().indexFromName(field_name)
        vmin = layer.minimumValue(idx)
        vmax = layer.maximumValue(idx)
        if vmin is not None and vmax is not None and float(vmax) > float(vmin):
            step = (float(vmax) - float(vmin)) / float(int({classes_count}))
            for i in range(int({classes_count})):
                lower = float(vmin) + i * step
                upper = float(vmin) + (i + 1) * step if i < int({classes_count}) - 1 else float(vmax)
                symbol = QgsSymbol.defaultSymbol(grid_layer.geometryType())
                if symbol is None:
                    continue
                color = grid_ramp.color(float(i) / max(1, int({classes_count}) - 1))
                symbol.setColor(color)
                ranges.append(QgsRendererRange(lower, upper, symbol, f"{{lower:.4f}} - {{upper:.4f}}"))

    if ranges:
        grid_renderer = QgsGraduatedSymbolRenderer(grid_field_name, ranges)
    else:
        grid_renderer.setMode(QgsGraduatedSymbolRenderer.EqualInterval)
        grid_renderer.updateClasses(grid_layer, int({classes_count}))
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

project.write({output_literal})
app.exitQgis()
"""

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        script_path = handle.name

    try:
        subprocess.run([python_launcher, script_path], check=True)
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

    subprocess.Popen(args)
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

    print("[Stage] Bus Routing", flush=True)
    bus = run_bus_routing_stage(ctx, snap)

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

    print("[Stage] Service Aggregation", flush=True)
    svc = run_service_stage(ctx, acc)

    print("[Stage] Capability Aggregation", flush=True)
    cap = run_capability_stage(ctx, svc)

    print(
        "[Output] Capability CSV files: "
        + ", ".join(str(path) for path in cap.output_paths.values()),
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
