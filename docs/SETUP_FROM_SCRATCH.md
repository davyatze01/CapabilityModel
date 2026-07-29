# CapabilityModel - Setup From Scratch (Windows)

A clean-room checklist to run the **full** pipeline (including bus routing and QGIS) on a new machine.

## 1) Prerequisites

| Tool | Version | Needed for |
|------|---------|-----------|
| Windows 10/11 | — | — |
| Python (64-bit) | 3.12 | the whole pipeline |
| Git | any | cloning |
| **R** (with `Rscript` on `PATH`) | 4.x | bus routing (r5r) |
| **Java JDK** | **21** | r5r / R5 engine (set `JAVA_HOME`) |
| **QGIS LTR** | 3.40 | auto-styled map output |

A default run that only needs capability scores can skip R/Java/QGIS **if** you bring a
prebuilt `artifacts/<slug>/impedances.npz`. The steps below assume you want bus routing and
QGIS, i.e. the full setup.

## 2) Clone the repository

```powershell
git clone https://github.com/davyatze01/CapabilityModel
cd CapabilityModel
git checkout qgis-export
```

## 3) Python environment + dependencies

Standard (pip):

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Faster alternative ([uv](https://github.com/astral-sh/uv) resolves the stack in seconds):

```powershell
pip install uv
uv venv ; .venv\Scripts\activate
uv pip install -r requirements.txt
```

Note: `main.py` also auto-installs anything missing from `requirements.txt` on first run, so
running it in a fresh venv is enough in a pinch.

## 4) System dependencies for bus routing (R5r)

Only needed when regenerating bus impedances (no `impedances.npz` / bus route cache present).
The bus stage calls `Rscript utils/r5_routing.r`.

1. **Java JDK 21** (required by current r5r) — install Eclipse Temurin 21 and set `JAVA_HOME`,
   or easiest, let R install it: `install.packages("rJavaEnv"); rJavaEnv::java_quick_install(version = 21)`.
   r5r reserves 16 GB heap (`-Xmx16G`), so keep ~16 GB RAM free.
2. **R + Rscript on PATH** — verify with `Rscript --version`.
3. **R packages** — in an R console:
   ```r
   install.packages(c("r5r", "data.table"))
   ```
   The R5 engine jar is downloaded automatically by `download_r5()` on first use.
4. **OSM → PBF backend** (one of): `osmium` CLI, `osmconvert` CLI, or the Python binding
   (simplest): `python -m pip install osmium`.
5. **GTFS feeds** — put your transit `*.zip` files in `gtfs/`.

If `impedances.npz` (or the bus route cache) already exists, none of section 4 is used.

## 5) QGIS (auto-styled map output)

- Install **QGIS 3.40 LTR**. The run autodetects `qgis-ltr-bin.exe` under `C:\Program Files\QGIS*`.
- Optional explicit path in `config.py`:
  - `open_qgis_after_run = True`
  - `qgis_bin_path = "C:/Program Files/QGIS 3.40.14/bin/qgis-ltr-bin.exe"`
  - or set the `QGIS_BIN_PATH` environment variable.
- Without QGIS the pipeline still completes; it just skips opening the map project.

## 6) Required input data

In the clone, `graph/` (OSM graphs), `artifacts/` and the POI GeoJSON caches are **gitignored**,
so they are not in a fresh checkout:

- OSM **graphs** and **POIs** auto-download from OpenStreetMap on first run (internet required).
- `artifacts/<artifact_slug>/impedances.npz` is **not** in the repo — copy it over to skip bus
  regeneration, or let section 4 regenerate it.
- `shapefile_base/` study-area shapefile and `config/poi_types.csv` are tracked in the repo.
- `gtfs/` transit zips are only needed for bus regeneration.

## 7) Configure run settings

Edit the knobs at the top of `main.py`:

- `study_city` (e.g. `"cagliari"`)
- `SAFE_MODE` — `True` for gentle execution (1 math thread/process, ~half the cores,
  below-normal priority). Results are identical; only scheduling changes.
- `WORKER_COUNT` — `None` = automatic; `1` = single-process run.

Edit `config.py` if needed: `name_shapefile`, `gtfs_feeds` (bus regeneration),
`open_qgis_after_run`, `qgis_autostyle_field` (default `capability_care`).

## 8) Run

```powershell
python main.py
```

Outputs:

- `outputs/gpkg/<artifact_slug>/<artifact_slug>.gpkg`
- `outputs/shapefiles/<artifact_slug>/<artifact_slug>.shp`
- `outputs/qgis/<artifact_slug>/capability.qgz` (opened automatically if QGIS is installed)

## 9) Common issues

- **Missing Python packages**: `pip install -r requirements.txt` in the active venv.
- **`Rscript executable not found`**: add R's `bin` to `PATH` (bus regeneration only).
- **r5r/Java errors**: confirm JDK **21** is installed and `JAVA_HOME` points to it; ensure
  ~16 GB RAM is free.
- **Could not convert OSM XML to PBF**: install a backend — easiest `python -m pip install osmium`.
- **QGIS not opening**: set `qgis_bin_path` in `config.py` or the `QGIS_BIN_PATH` env var.
