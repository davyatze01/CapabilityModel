"""Bundle the raw data behind robustness_report.html / sensitivity_report.html into one zip."""
import datetime as dt
import os
import zipfile
from pathlib import Path

ZIP_NAME = "sensitivity_robustness_raw_data.zip"
_SENSITIVITY = [
    ("sensitivity_oat.csv", "OAT: % of nodes changing ELECTRE class per (parameter, value, capability)"),
    ("sensitivity_oat_levels.csv", "OAT: % of nodes at each class per (parameter, value, capability, level)"),
    ("sensitivity_weights.csv", "Weight perturbation: % of nodes changing class per Dirichlet draw"),
    ("sensitivity_upstream/upstream_summary.csv", "Upstream: % of nodes changed vs baseline per config"),
    ("sensitivity_upstream/upstream_service_deltas.csv", "Upstream: mean |delta| per (config, service)"),
    ("sensitivity_upstream/upstream_node_dropout.csv", "Upstream: nodes gaining/losing all reachable POIs per config"),
    ("sensitivity_upstream/upstream_level_distribution.csv", "Upstream: % of nodes at each class per (config, capability, level)"),
]
_ROBUSTNESS = [
    ("robustness_node_stability.csv", "Robustness: modal class + hit-rate per (node, capability, sigma)"),
    ("robustness_node_stability_wide.csv", "Robustness: same, one column per capability"),
    ("capability_stability.gpkg", "Robustness: per-node stability layer for QGIS"),
    ("capability_stability.qml", "QGIS style for capability_stability.gpkg"),
]


def write_raw_data_zip(slug: str, source_csv, robustness: bool, sensitivity: bool) -> Path | None:
    out_dir = Path("outputs/debug") / slug
    wanted = (_ROBUSTNESS if robustness else []) + (_SENSITIVITY if sensitivity else [])
    present = [(rel, desc) for rel, desc in wanted if (out_dir / rel).exists()]
    if not present:
        return None
    readme = [
        f"Raw data behind robustness_report.html / sensitivity_report.html for {slug}.",
        f"Generated {dt.datetime.now():%Y-%m-%d %H:%M} by main.py from {source_csv}.",
        "",
    ] + [f"{Path(rel).name:40s} {desc}" for rel, desc in present]
    zip_path = out_dir / ZIP_NAME
    tmp_path = zip_path.with_suffix(".zip.tmp")  # never leave a half-written zip behind on a crash
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("sensitivity_robustness_raw_data/README.txt", "\n".join(readme) + "\n")
        for rel, _ in present:
            zf.write(out_dir / rel, f"sensitivity_robustness_raw_data/{Path(rel).name}")
    os.replace(tmp_path, zip_path)
    return zip_path
