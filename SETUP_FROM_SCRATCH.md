# CapabilityModel - Setup From Scratch (Windows)

This is a minimal, clean-room checklist to run the pipeline on a new machine.

## 1) Prerequisites

- Windows 10/11
- Python 3.12 (64-bit)
- Git
- QGIS 3.40 LTR (optional but recommended for map styling/auto-open)

Note: this guide assumes you already have an `impedances.npz` file, so R/r5r is not required.

## 2) Clone the repository

```powershell
git clone https://github.com/davyatze01/CapabilityModel
cd CapabilityModel
git checkout qgis-export
```

## 3) Create and activate a virtual environment

```powershell
python -m venv .venv
.venv\Scripts\activate
```

## 4) Install Python dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 5) Verify required input data

Make sure these exist in the repo:

- `shapefile_base/` contains the study area shapefile
- `gtfs/` contains your GTFS zip files
- `config/poi_types.csv` is present
- `artifacts/<artifact_slug>/impedances.npz` is present

## 6) Configure run settings

Edit `config.py` if needed:

- `name_shapefile`
- `open_qgis_after_run`
- `qgis_autostyle_field` (default: `capability_care`)
- `gtfs_feeds` (only needed when regenerating impedances)

If you want QGIS to auto-open and style the output, set:

- `open_qgis_after_run = True`
- `qgis_bin_path = "C:/Program Files/QGIS 3.40.14/bin/qgis-ltr-bin.exe"` (optional, autodetect is used if not set)

## 7) First run

```powershell
python main.py
```

Outputs are written to:

- `outputs/gpkg/<artifact_slug>/<artifact_slug>.gpkg`
- `outputs/shapefiles/<artifact_slug>/<artifact_slug>.shp`

## 8) If QGIS is installed

The run will generate a QGIS project and open it automatically. The project is saved at:

- `outputs/qgis/<artifact_slug>/capability.qgz`

## 9) Common issues

- Missing `osmnx` or other Python packages: run `pip install -r requirements.txt` in the venv you are using.
- QGIS not opening: set `QGIS_BIN_PATH` env var or `qgis_bin_path` in config.
