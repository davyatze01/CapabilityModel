import atexit
import csv
import glob
import hashlib
import os
import pickle
import queue as _queue
import signal
import shutil
import subprocess
import threading
import time
from pathlib import Path

import osmnx as ox
import numpy as np
from tqdm import tqdm
from core.pipeline_types import BusRoutingStageResult, PipelineContext, SnappingStageResult
from stages.snapping_stage import build_selected_routing_destinations
from core.runtime_setup import _excluded_cpus
import json

COORD_ROUND = 6


class RscriptNotFoundError(RuntimeError):
    """Raised when Rscript isn't on PATH -- distinct from RuntimeError so callers can
    catch specifically this and offer to run scripts/setup_r.py, without swallowing
    unrelated routing failures."""

# Wall-clock stall watchdog for the R5 subprocess. r5r prints per-chunk progress
# (build_network, per-chunk routing with progress=TRUE, per-chunk mem reports), so a
# healthy run emits output regularly. If NOTHING is printed for this long, the JVM/R5 is
# almost certainly stuck — e.g. G1GC grinding just under the heap ceiling without ever
# throwing OutOfMemoryError, or R5 spinning on a pathological route search — which from the
# outside is indistinguishable from a hang. Per the project's fail-fast preference we kill
# it at a known ceiling instead of waiting indefinitely; the chunk-resume retry loop then
# either makes forward progress on relaunch or fails loudly once retries are exhausted.
# Override with CAP_R5_STALL_TIMEOUT_S (seconds); 0/negative disables the watchdog.
_R5_STALL_TIMEOUT_S_DEFAULT = 60.0

# build_network() (GTFS+OSM -> R5 transport network) is a single blocking r5r/Java call
# with no incremental progress output, unlike the per-chunk routing phase that follows it
# (progress=TRUE). A large network can legitimately take well over 60s to build with zero
# stdout in between, so the tight routing-phase stall timeout above would kill a healthy
# build. Give the build phase its own, much more generous ceiling instead of exempting it
# from the watchdog entirely (per the project's fail-fast preference, a hang there should
# still die at a known limit, just a longer one). The watchdog switches to the tight
# routing timeout once the "Routing transit mode:" line (utils/r5_routing.r) appears,
# which is printed immediately after build_network() returns.
# Override with CAP_R5_BUILD_TIMEOUT_S (seconds); 0/negative disables the build-phase watchdog.
_R5_BUILD_TIMEOUT_S_DEFAULT = 1800.0
_R5_ROUTING_STARTED_MARKER = "Routing transit mode:"

# The final combine (chunk CSVs -> one output matrix, utils/r5_routing.r) comes after the
# routing phase, so it would otherwise stay on the tight per-chunk stall timeout above.
# It streams one chunk at a time with progress every ~200 files, but under memory pressure
# (reclaim throttling near the cgroup cap) a healthy combine can legitimately go quiet for
# minutes — and killing it there is worse than useless: the relaunch skips every completed
# chunk and re-enters the same combine, looping forever. Switch to the generous build-phase
# timeout once this marker appears.
_R5_COMBINE_STARTED_MARKER = "Combining chunk files:"

# Module-level reference so atexit/signal handlers can always reach the running
# Rscript/JVM subprocess, even if the parent dies without the wait loop's own
# KeyboardInterrupt handler ever running (e.g. SIGTERM, or SIGHUP from closing
# the terminal). Left unguarded, a killed parent orphans the R5 JVM (and, under
# Flatpak, the flatpak-spawn --host process) — it keeps running and consuming
# its full heap, starving the next run before it even reaches snapping.
_ACTIVE_R5_PROC: subprocess.Popen | None = None


def _terminate_active_r5_proc() -> None:
    """Terminate any running R5 Rscript subprocess. Called by atexit and signal handlers."""
    global _ACTIVE_R5_PROC
    proc = _ACTIVE_R5_PROC
    if proc is None or proc.poll() is not None:
        return
    _ACTIVE_R5_PROC = None
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


atexit.register(_terminate_active_r5_proc)


def _r5_exit_signal_handler(signum, frame):
    """Terminate the active R5 subprocess and raise SystemExit so atexit also runs."""
    _terminate_active_r5_proc()
    raise SystemExit(1)


# SIGTERM covers `kill <pid>` and process managers. SIGHUP covers closing the
# terminal (e.g. a konsole tab) — Python has no default handler for either, so
# without this the process (and the R5 child) dies with no cleanup at all.
for _sig_name in ("SIGTERM", "SIGHUP"):
    _sig = getattr(signal, _sig_name, None)
    if _sig is not None:
        try:
            signal.signal(_sig, _r5_exit_signal_handler)
        except (OSError, ValueError):
            pass


def _prepare_r5r_data_bundle(cfg) -> str:
    """Ensure a city-scoped routing data folder contains exactly one OSM PBF and the
    configured GTFS zip file(s).

    r5r's build_network() scans its entire target directory for '*.pbf' and '*.zip'
    files, so every city gets its own subdirectory here to prevent another city's
    GTFS feed (e.g. Paris' IDFM-gtfs.zip sitting alongside Cagliari's GTFS.zip in the
    shared "gtfs" folder) from being silently pulled into the network build.
    """
    source_dir = cfg.routing_data_dir
    data_dir = os.path.join(source_dir, cfg.city_slug)
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    pbf_path = os.path.join(data_dir, f"{cfg.city_slug}.osm.pbf")
    if not os.path.isfile(pbf_path):
        legacy_pbf_path = os.path.join(source_dir, f"{cfg.city_slug}.osm.pbf")
        if os.path.isfile(legacy_pbf_path):
            shutil.copy2(legacy_pbf_path, pbf_path)
        elif cfg.osm_pbf_autobuild:
            print(
                "[Bus] OSM PBF missing. Building local extract from city_name via OSMnx...",
                flush=True,
            )
            _autobuild_pbf_from_place(cfg, pbf_path)
            print(f"[Bus] OSM PBF auto-built: {pbf_path}", flush=True)
        else:
            raise RuntimeError(
                "Missing OSM PBF for routing. "
                f"Expected: {pbf_path} (or {legacy_pbf_path}). Enable config.osm_pbf_autobuild."
            )

    if not cfg.gtfs_feeds:
        raise RuntimeError("config.gtfs_feeds is empty. Provide at least one GTFS zip path.")

    staged_gtfs = []
    for feed_path in cfg.gtfs_feeds:
        if not os.path.isfile(feed_path):
            raise RuntimeError(f"GTFS feed not found: {feed_path}")
        if Path(feed_path).suffix.lower() != ".zip":
            raise RuntimeError(f"GTFS feed must be a .zip file: {feed_path}")

        dst_path = os.path.join(data_dir, os.path.basename(feed_path))
        if os.path.abspath(feed_path) != os.path.abspath(dst_path):
            shutil.copy2(feed_path, dst_path)
        staged_gtfs.append(dst_path)

    if not staged_gtfs:
        raise RuntimeError(f"No GTFS feeds staged into {data_dir}.")

    staged_names = {os.path.basename(p) for p in staged_gtfs} | {os.path.basename(pbf_path)}
    for stray in Path(data_dir).glob("*"):
        if stray.name not in staged_names and stray.suffix.lower() in (".zip", ".pbf"):
            stray.unlink()

    _invalidate_stale_r5_network(data_dir, [pbf_path] + staged_gtfs)

    return data_dir


def _r5_network_cache_files(data_dir: str) -> list[str]:
    return [
        os.path.join(data_dir, "network.dat"),
        os.path.join(data_dir, "network_settings.json"),
        os.path.join(data_dir, "gtfs_errors.csv"),
    ]


def _invalidate_stale_r5_network(data_dir: str, input_paths: list[str]) -> None:
    """Delete a cached R5 network build (network.dat/network_settings.json) when the
    staged PBF/GTFS inputs it was built from have changed.

    r5r's build_network() reuses an existing network.dat whenever one is present in
    data_dir, regardless of whether the GTFS/PBF files there still match what it was
    built from -- it doesn't hash file contents, only checks that a cached network
    exists. That silently serves a stale transit network: swapping in a new GTFS zip
    (even reusing the same filename, e.g. a rebuilt gtfs_new_metro.zip) or changing
    which feeds are staged has no effect until this cache is cleared, and the failure
    mode is not "wrong answer" but a confusing r5r error like "no transit services on
    the selected date" when the stale network's calendar doesn't cover the requested
    departure date.

    A manifest of (basename, size, mtime_ns) per input file is written into data_dir
    and compared on every call; any mismatch (including "manifest missing", e.g. first
    run after r5r built the cache directly) clears the cache so the next build_network()
    call is forced to rebuild from what's actually on disk now.
    """
    manifest_path = os.path.join(data_dir, "_bundle_manifest.json")
    current = {
        os.path.basename(p): [os.path.getsize(p), os.stat(p).st_mtime_ns]
        for p in input_paths
    }

    previous = None
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                previous = json.load(f)
        except (OSError, json.JSONDecodeError):
            previous = None

    if previous != current:
        removed = [p for p in _r5_network_cache_files(data_dir) if os.path.isfile(p)]
        for p in removed:
            os.remove(p)
        if removed:
            print(
                f"[Bus] Staged GTFS/PBF inputs changed; cleared stale R5 network cache "
                f"({', '.join(os.path.basename(p) for p in removed)}) so build_network() "
                "rebuilds from the current files.",
                flush=True,
            )
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(current, f)


def _download_regional_extract(cfg) -> str:
    """Download (or reuse) the regional Geofabrik PBF named by cfg.osm_extract_url.

    The file is cached under <routing_data_dir>/_extracts and verified against the
    publisher's .md5 before first use. Interrupted downloads land in a '.part' file
    (resumed via HTTP Range on the next run) and are never visible as a complete
    extract. The checksum also catches a resume that spans a Geofabrik daily update
    (the '-latest' redirect target changes), which would otherwise splice two
    different files together.
    """
    import urllib.error
    import urllib.request

    url = cfg.osm_extract_url
    cache_dir = os.path.join(cfg.routing_data_dir, "_extracts")
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    final_path = os.path.join(cache_dir, os.path.basename(url))
    if os.path.isfile(final_path):
        print(f"[Bus] Reusing cached regional extract: {final_path}", flush=True)
        return final_path

    part_path = final_path + ".part"
    offset = os.path.getsize(part_path) if os.path.isfile(part_path) else 0
    req = urllib.request.Request(url)
    if offset:
        req.add_header("Range", f"bytes={offset}-")
        print(f"[Bus] Resuming OSM extract download at {offset / 1e6:.0f} MB", flush=True)
    else:
        print(f"[Bus] Downloading regional OSM extract: {url}", flush=True)

    # timeout=60 bounds every socket read: a stalled mirror raises instead of hanging.
    resp = urllib.request.urlopen(req, timeout=60)
    if offset and resp.status != 206:
        # Server ignored the Range request; start over.
        offset = 0
    with resp, open(part_path, "ab" if offset else "wb") as out:
        total = offset + int(resp.headers.get("Content-Length") or 0)
        done = offset
        next_report = 0
        while True:
            chunk = resp.read(8 * 1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if done >= next_report:
                total_txt = f"/{total / 1e6:.0f}" if total else ""
                print(f"[Bus] OSM extract download: {done / 1e6:.0f}{total_txt} MB", flush=True)
                next_report = done + 200 * 1_000_000

    md5_expected = None
    try:
        with urllib.request.urlopen(url + ".md5", timeout=60) as resp:
            md5_expected = resp.read().decode().split()[0]
    except (urllib.error.URLError, IndexError):
        print("[Bus] No .md5 published for extract; skipping checksum.", flush=True)
    if md5_expected:
        digest = hashlib.md5()
        with open(part_path, "rb") as f:
            for chunk in iter(lambda: f.read(16 * 1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != md5_expected:
            os.remove(part_path)
            raise RuntimeError(
                f"Checksum mismatch for downloaded OSM extract {url}. "
                "Partial file deleted; re-run to download it again."
            )

    os.replace(part_path, final_path)
    return final_path


def _study_area_bbox_wgs84(cfg) -> tuple[float, float, float, float]:
    """West/south/east/north WGS84 bounds of the study area, padded by
    cfg.osm_extract_buffer_m so routing near the boundary sees the surrounding network."""
    import math

    import geopandas as gpd

    if cfg.use_shapefile:
        gdf = gpd.read_file(os.path.join("shapefile_base", cfg.name_shapefile))
        if gdf.crs is None:
            raise ValueError(f"Missing CRS in {cfg.name_shapefile}")
        gdf = gdf.to_crs(epsg=4326)
    else:
        gdf = ox.geocode_to_gdf(cfg.city_name)
    west, south, east, north = gdf.total_bounds

    lat = (south + north) / 2.0
    dlat = cfg.osm_extract_buffer_m / 111_320.0
    dlon = dlat / max(math.cos(math.radians(lat)), 0.01)
    return west - dlon, south - dlat, east + dlon, north + dlat


def _autobuild_pbf_from_place(cfg, pbf_path: str) -> None:
    """Clip the study-area PBF out of a regional Geofabrik extract with osmium.

    Deliberately avoids the former OSMnx graph -> XML -> PBF round-trip: that built
    the whole network and XML tree in RAM (segfault-prone on large cities) and,
    because save_graph_xml requires all_oneway=True, handed R5 falsified oneway
    tags. Here the data stays real, compressed PBF end to end and osmium streams it
    with bounded memory.
    """
    osmium_exe = shutil.which("osmium")
    if osmium_exe is None:
        raise RuntimeError(
            "osmium-tool is required to clip the OSM extract. "
            "Install it with: sudo dnf install osmium-tool"
        )
    if not cfg.osm_extract_url:
        raise RuntimeError(
            "config.osm_extract_url is empty: set the regional Geofabrik PBF URL for "
            "this city (e.g. https://download.geofabrik.de/europe/italy/isole-latest.osm.pbf) "
            "or export CAP_OSM_EXTRACT_URL."
        )

    regional_path = _download_regional_extract(cfg)
    west, south, east, north = _study_area_bbox_wgs84(cfg)
    print(
        f"[Bus] Clipping {os.path.basename(regional_path)} to bbox "
        f"({west:.4f}, {south:.4f}, {east:.4f}, {north:.4f})...",
        flush=True,
    )
    subprocess.run(
        [
            osmium_exe, "extract",
            "--bbox", f"{west},{south},{east},{north}",
            "--set-bounds", "--strategy", "complete_ways",
            regional_path, "-o", pbf_path, "--overwrite",
        ],
        check=True,
        timeout=1800,
    )

def _load_routing_cache(path: str):
    """Load persisted routing cache payload from pickle.

    Inputs:
    - path: file path to routing pickle cache.

    Outputs:
    - dict-like payload containing routing metadata and routes.
    """
    with open(path, "rb") as f:
        return pickle.load(f)


def _is_valid_routing_cache(payload, departure_iso: str, origins_sig: str | None = None,
                            destinations_sig: str | None = None) -> bool:
    """Validate routing cache schema and optional run signatures.

    Inputs:
    - payload: object loaded from routing cache file.
    - departure_iso: expected departure datetime string.
    - origins_sig: optional expected origin signature.
    - destinations_sig: optional expected destination signature.

    Outputs:
    - bool: True when cache is compatible with the expected run context.
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("departure_iso") != departure_iso:
        return False
    
    routes = payload.get("routes")
    if not isinstance(routes, dict):
        return False
    
    if origins_sig is not None and payload.get("origins_sig") != origins_sig:
        return False
    
    if destinations_sig is not None and payload.get("destinations_sig") != destinations_sig:
        return False
    
    return True

def _coords_signature(coords: list[tuple[float, float]]) -> str:
    """Hash ordered coordinates to identify exact routing inputs across runs.

    Inputs:
    - coords: ordered (lat, lon) list.

    Outputs:
    - str: stable SHA1 signature of the rounded coordinate sequence.
    """
    h = hashlib.sha1()
    for lat, lon in coords:
        h.update(f"{round(float(lat), COORD_ROUND)},{round(float(lon), COORD_ROUND)};".encode("ascii"))
    return h.hexdigest()


def _write_r5r_point_inputs(
        nodes_with_coords,
        destinations,
        origins_csv,
        destinations_csv,
):
    """Write origin/destination CSV files consumed by the R routing script.

    Inputs:
    - nodes_with_coords: iterable of graph node ids with x/y coordinates.
    - destinations: selected destination coordinates as (lat, lon).
    - origins_csv: output path for origins CSV.
    - destinations_csv: output path for destinations CSV.

    Outputs:
    - None. Writes CSV files to disk.
    """
    Path(origins_csv).parent.mkdir(parents=True, exist_ok=True)

    with open(origins_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "lon", "lat"])
        for node_id, data in nodes_with_coords:
            writer.writerow([str(node_id), float(data["x"]), float(data["y"])])

    with open(destinations_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "lon", "lat"])
        for idx, (lat, lon) in enumerate(destinations):
            writer.writerow([f"d{idx}", float(lon), float(lat)])


def _write_empty_routing_csv(path: str) -> None:
    """Write an empty expanded-routing CSV with the expected schema."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "from_id",
                "to_id",
                "departure_time",
                "draw_number",
                "access_time",
                "wait_time",
                "ride_time",
                "transfer_time",
                "egress_time",
                "routes",
                "n_rides",
                "total_time",
            ]
        )


def _write_dummy_destination_csv(path: str) -> None:
    """Write one placeholder destination so matrix artifacts remain non-empty."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "lon", "lat"])
        writer.writerow(["d0", 0.0, 0.0])


def _resolve_r5r_java_home() -> str | None:
    """Locate a Java 21 home for r5r, which targets Java 21 and breaks on newer JDKs.

    The default system Java here is OpenJDK 25, which r5r/rJava reject. Resolution order:
    1. CAP_JAVA_HOME, if set (explicit override; must point at a JDK).
    2. The Fedora-conventional /usr/lib/jvm/java-21-openjdk symlink.
    3. Any /usr/lib/jvm/java-21* or *-21-* directory (other distros / versioned paths).

    Returns the JAVA_HOME path, or None if no Java 21 is found (caller leaves env untouched
    and lets R surface its own error).
    """
    override = os.environ.get("CAP_JAVA_HOME", "").strip()
    if override:
        return override

    home = os.path.expanduser("~")
    candidates = ["/usr/lib/jvm/java-21-openjdk"]
    for pattern in (
        "/usr/lib/jvm/java-21*", "/usr/lib/jvm/*-21-*", "/usr/lib/jvm/jdk-21*",
        # Fedora 44 has no Java 21 package, so it's commonly a home/tarball install.
        os.path.join(home, "jdks", "jdk-21*"), os.path.join(home, "jdks", "*-21*"),
        os.path.join(home, ".sdkman", "candidates", "java", "21*"),
    ):
        candidates.extend(sorted(glob.glob(pattern)))
    for path in candidates:
        if os.path.isfile(os.path.join(path, "bin", "java")):
            return path
    return None


def _routing_taskset_prefix(on_host: bool) -> list[str]:
    """Build a `taskset -c <good_cpus>` prefix to keep routing off known-bad CPUs.

    The Python affinity mask (runtime_setup._apply_cpu_affinity_exclusions) is
    inherited by normal forked children, but NOT by `flatpak-spawn --host`, which
    spawns the process on the host outside this process tree. Pinning the JVM
    explicitly with taskset honours CAP_EXCLUDE_CPUS in both launch paths and
    covers the worker threads R5 spawns.

    Inputs:
    - on_host: True when the command runs on the host via flatpak-spawn (taskset
      is then resolved on the host); False to resolve taskset in this environment.

    Outputs:
    - list of command tokens, or [] if no exclusions apply or taskset is missing.
    """
    bad = _excluded_cpus()
    if not bad:
        return []
    total = os.cpu_count() or 0
    keep = sorted(set(range(total)) - bad)
    if not keep or len(keep) == total:
        return []
    cpu_list = ",".join(str(c) for c in keep)
    if on_host:
        # Resolved by flatpak-spawn --host on the host (util-linux is standard there).
        return ["taskset", "-c", cpu_list]
    exe = shutil.which("taskset")
    if not exe:
        print(
            "[Bus Routing] taskset not found; cannot pin routing off excluded CPUs "
            f"{sorted(bad)}. Routing may run on a faulty core.",
            flush=True,
        )
        return []
    return [exe, "-c", cpu_list]


def _resolve_transit_mode(cfg, transport_type: str) -> str:
    """Map a transport_type to the r5r transit mode passed to the R script.

    Subway/metro route only the second modality's layer, using whichever r5r mode string
    matches that feed's actual GTFS route_type (cfg.subway_transit_mode -- "SUBWAY" for a
    true route_type=1 metro like Paris' IDFM, "TRAM" for a route_type=0 light rail like
    Cagliari's Metrocagliari; see the field's docstring in core/config.py). Bus routes only
    the bus layer when a second modality is also being routed for this city, so the two
    don't double-count on a combined feed; otherwise bus falls back to the generic "TRANSIT"
    to preserve the original single-feed behaviour for cities with no second modality.
    """
    if transport_type in ("subway", "metro"):
        return getattr(cfg, "subway_transit_mode", "SUBWAY")
    if getattr(cfg, "enable_subway", False):
        return "BUS"
    return "TRANSIT"


def _run_r5r_script(
    script_path: str,
    ctx: PipelineContext,
    transport_type: str = "bus",
    origins_csv: str | None = None,
    destinations_csv: str | None = None,
    output_csv: str | None = None,
    chunk_dir: str | None = None,
) -> None:
    """Run the external R routing script and keep shell output concise.

    Inputs:
    - script_path: path to the R script entrypoint.
    - transport_type: modality being routed ("bus" or "subway"/"metro"); selects the
      per-mode artifact paths and the r5r transit mode.
    - origins_csv/destinations_csv/output_csv/chunk_dir: optional overrides for the
      default cfg.public_transport_paths(transport_type)-derived locations, used to run
      R5 against a subset of origins/destinations in isolation (e.g. partial-reuse
      "brand new origins only" jobs) without touching the default single-shot paths.

    Outputs:
    - None. Raises if Rscript fails or is not available.
    """
    _in_flatpak = os.path.isfile("/.flatpak-info")
    if _in_flatpak:
        # Inside the VS Code Flatpak sandbox the host's Rscript ELF can't exec directly
        # (its glibc/linker don't exist in the runtime). Use flatpak-spawn --host instead.
        rscript_exe = shutil.which("flatpak-spawn")
        if rscript_exe is None:
            raise RuntimeError(
                "Running inside a Flatpak but flatpak-spawn is not available. "
                "Cannot launch host Rscript. Run the pipeline from a regular terminal instead."
            )
    else:
        rscript_exe = shutil.which("Rscript")
        if not rscript_exe:
            raise RscriptNotFoundError(
                "Rscript executable not found. Run `python scripts/setup_r.py` to check/install "
                "R, the r5r/data.table packages, and verify a Java 21 JVM is available."
            )
    cfg = ctx.config
    r5_data_path = _prepare_r5r_data_bundle(cfg)
    env = os.environ.copy()

    # r5r runs R5 on the JVM and only supports Java 21; the system default here is Java 25,
    # which it rejects. Pin JAVA_HOME (and prepend its bin to PATH) for the R subprocess so the
    # right JVM is used regardless of the shell's default java.
    java_home = _resolve_r5r_java_home()
    if java_home:
        env["JAVA_HOME"] = java_home
        env["PATH"] = os.path.join(java_home, "bin") + os.pathsep + env.get("PATH", "")
        # JAVA_HOME alone is not enough: R's etc/ldpaths sets R_JAVA_LD_LIBRARY_PATH (only if
        # unset) to whatever JVM `R CMD javareconf` detected — here the system Java 25 — and
        # prepends it to LD_LIBRARY_PATH, so rJava's dlopen("libjvm.so") loads Java 25 and r5r
        # aborts with "requires Java-SE Development Kit 21". Exporting it ourselves to Java 21's
        # lib/server pre-empts that default and makes rJava load the right JVM, with no root/
        # javareconf change needed.
        env["R_JAVA_LD_LIBRARY_PATH"] = os.path.join(java_home, "lib", "server")
        print(f"[Bus Routing] Using Java 21 for r5r: {java_home}", flush=True)
    else:
        print(
            "[Bus Routing] No Java 21 found (looked for /usr/lib/jvm/java-21-openjdk; set "
            "CAP_JAVA_HOME to override). r5r may fail on the default JVM.",
            flush=True,
        )

    paths = cfg.public_transport_paths(transport_type)
    env["R5_DATA_PATH"] = r5_data_path
    env["R5_ORIGINS_PATH"] = origins_csv or paths["routing_origins_input"]
    env["R5_DEST_PATH"] = destinations_csv or paths["routing_destinations_input"]
    resolved_output_csv = output_csv or paths["routing_matrix"]
    env["R5_OUTPUT_PATH"] = resolved_output_csv
    env["R5_CHUNK_DIR"] = chunk_dir or os.path.join(os.path.dirname(paths["routing_matrix"]), "r5r_chunks")
    env["R5_DEPARTURE_DATETIME"] = (cfg.subway_departure_dt if transport_type in ("subway", "metro") else cfg.bus_departure_dt).strftime("%Y-%m-%d %H:%M:%S")
    # If we are routing subway or metro we are using the subway time, otherwise the bus time. This fix was needed since Cagliari's CTM and metrocagliari data
    # do not overlap in time
    env["R5_TRANSIT_MODE"] = _resolve_transit_mode(cfg, transport_type)
    budget_gb = float(os.environ.get("CAP_MEM_BUDGET_GB", "0") or "0")
    # JVM heap for r5r. Smaller than the raw budget so Python + R side-tables fit
    # alongside the JVM; G1GC pause times scale with heap size, so keeping it
    # bounded avoids multi-minute stop-the-world pauses between chunk calls.
    if "R5_JVM_MAX_HEAP_GB" not in env:
        if budget_gb >= 40:
            jvm_heap_gb = 16
        else:
            # No cgroup budget — derive from machine RAM: leave ~15 GB for OS,
            # Python, and R side-tables. Clamp to [8, 16].
            try:
                import psutil as _psutil
                total_gb = int(_psutil.virtual_memory().total / (1024 ** 3))
            except Exception:
                total_gb = 32
            jvm_heap_gb = max(8, min(16, total_gb - 15))
        env["R5_JVM_MAX_HEAP_GB"] = str(jvm_heap_gb)
        print(
            f"[Bus Routing] R5_JVM_MAX_HEAP_GB={jvm_heap_gb} "
            f"(budget={budget_gb:.0f}GB)",
            flush=True,
        )

    # Origin-primary chunking (see utils/r5_routing.r). R5's transit search (RAPTOR)
    # runs once per origin per departure minute over the whole network and is
    # essentially independent of the destination count — so splitting destinations
    # into many chunks re-runs that expensive search once per chunk (Paris: 268k
    # destinations at the old size 500 => 537 redundant passes per origin batch).
    # We instead keep ALL destinations in a single call and chunk *origins* small
    # enough that the expanded (origins × destinations × time-window) table fits the
    # JVM heap. Peak heap for one call scales with origins × destinations: measured
    # on Cagliari (200 × 2000 ≈ 6.95 GB) ⇒ ~1.74e-5 GB per origin-destination pair
    # at time_window=60. We spend ~35% of the heap on that table, leaving headroom
    # for R side-tables and the off-heap/native (Arrow/JNI) memory G1GC can't
    # reclaim. Both sizes are overridable via the R5_ORIGIN_CHUNK_SIZE /
    # R5_DEST_CHUNK_SIZE env vars for tuning against the per-chunk [mem] logs.
    if "R5_ORIGIN_CHUNK_SIZE" not in env or "R5_DEST_CHUNK_SIZE" not in env:
        gb_per_od = 1.74e-5
        heap_gb = float(env.get("R5_JVM_MAX_HEAP_GB", "16") or "16")
        od_budget = max(1, int((0.35 * heap_gb) / gb_per_od))
        # Hard R5 constraint: expanded_travel_time_matrix(breakdown=TRUE) computes
        # detailed path breakdowns via PathResult, which throws "Number of detailed
        # path destinations exceeds limit of 5000" for any call with >5000
        # destinations. We need the breakdown columns downstream, so this cap is
        # mandatory, not tuning — a destination chunk can never exceed it however
        # much heap we have. (Enforced defensively again in utils/r5_routing.r.)
        max_breakdown_dests = 5000
        try:
            with open(env["R5_DEST_PATH"], encoding="utf-8") as _df:
                n_dests = max(1, sum(1 for _ in _df) - 1)  # minus header row
        except OSError:
            n_dests = od_budget  # unknown → assume a single full destination chunk
        # Largest destination chunk that respects both the heap budget and the R5
        # breakdown limit.
        max_dest_per_call = min(od_budget, max_breakdown_dests)
        if n_dests <= max_dest_per_call:
            # All destinations fit in one call for >=1 origin: no destination
            # chunking at all (redundancy factor 1); size origins to fill the budget.
            dest_chunk_size = n_dests
            origin_chunk_size = max(1, od_budget // n_dests)
        else:
            # Destinations exceed a single call: route against the largest allowed
            # destination chunk and size origins so origins × dest_chunk still fills
            # the heap budget (so we don't waste it running 1 origin at a time).
            dest_chunk_size = max_dest_per_call
            origin_chunk_size = max(1, od_budget // dest_chunk_size)
        env.setdefault("R5_ORIGIN_CHUNK_SIZE", str(origin_chunk_size))
        env.setdefault("R5_DEST_CHUNK_SIZE", str(dest_chunk_size))
        n_dest_chunks = -(-n_dests // int(env["R5_DEST_CHUNK_SIZE"]))
        print(
            f"[{transport_type.capitalize()} Routing] origin_chunk={env['R5_ORIGIN_CHUNK_SIZE']} "
            f"dest_chunk={env['R5_DEST_CHUNK_SIZE']} "
            f"(dests={n_dests}, heap={heap_gb:.0f}GB, budget={budget_gb:.0f}GB, "
            f"dest_chunks={n_dest_chunks} vs {-(-n_dests // 500)} at old size 500)",
            flush=True,
        )

    # Ensure the host Rscript finds user-installed packages (e.g. r5r installed to ~/R/…).
    # R_LIBS_USER must point at the *versioned* per-user library
    # (~/R/<platform>-library/<x.y>), not at ~/R itself, or library(r5r) won't find it.
    if "R_LIBS_USER" not in env:
        lib_candidates = sorted(
            glob.glob(os.path.expanduser("~/R/*-library/*")), reverse=True
        )
        if lib_candidates:
            env["R_LIBS_USER"] = lib_candidates[0]

    if _in_flatpak:
        # The host Rscript ELF can't exec inside the Flatpak runtime (wrong glibc/linker).
        # Use flatpak-spawn --host and pass only the vars we explicitly set so we don't
        # blow past argument length limits with the full sandbox environment.
        spawn_env_keys = [
            "JAVA_HOME", "R_JAVA_LD_LIBRARY_PATH", "PATH",
            "R_LIBS_USER",
            "R5_DATA_PATH", "R5_ORIGINS_PATH", "R5_DEST_PATH",
            "R5_OUTPUT_PATH", "R5_CHUNK_DIR", "R5_DEPARTURE_DATETIME",
            "R5_TRANSIT_MODE", "R5_ORIGIN_CHUNK_SIZE", "R5_DEST_CHUNK_SIZE",
            "R5_JVM_MAX_HEAP_GB",
        ]
        env_flags = [f"--env={k}={env[k]}" for k in spawn_env_keys if k in env]
        cmd = (
            [rscript_exe, "--host"] + env_flags
            + _routing_taskset_prefix(on_host=True)
            + ["Rscript", script_path]
        )
        popen_kwargs: dict = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    elif os.name == "nt":
        cmd = [rscript_exe, script_path]
        popen_kwargs = dict(
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        cmd = _routing_taskset_prefix(on_host=False) + [rscript_exe, script_path]
        popen_kwargs = dict(env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    # A SIGSEGV inside the JVM/rJava (exit 139, or -11 from Popen) tends to be a
    # non-deterministic crash mid-run on this hardware. The R script resumes from
    # completed chunks, so relaunching makes forward progress instead of losing
    # everything. Other non-zero exits are real errors and are not retried.
    try:
        stall_timeout_s = float(os.environ.get("CAP_R5_STALL_TIMEOUT_S", "") or _R5_STALL_TIMEOUT_S_DEFAULT)
    except ValueError:
        stall_timeout_s = _R5_STALL_TIMEOUT_S_DEFAULT
    try:
        build_timeout_s = float(os.environ.get("CAP_R5_BUILD_TIMEOUT_S", "") or _R5_BUILD_TIMEOUT_S_DEFAULT)
    except ValueError:
        build_timeout_s = _R5_BUILD_TIMEOUT_S_DEFAULT
    if stall_timeout_s > 0:
        print(
            f"[Bus Routing] Stall watchdog armed: will kill Rscript after "
            f"{stall_timeout_s:.0f}s with no output once routing starts "
            f"(CAP_R5_STALL_TIMEOUT_S); network build phase gets "
            f"{build_timeout_s:.0f}s (CAP_R5_BUILD_TIMEOUT_S).",
            flush=True,
        )

    global _ACTIVE_R5_PROC
    max_attempts = 64
    for attempt in range(1, max_attempts + 1):
        proc = subprocess.Popen(cmd, **popen_kwargs)
        _ACTIVE_R5_PROC = proc

        # Read stdout on a daemon thread so the parent can enforce a wall-clock
        # stall timeout: a blocking `for line in proc.stdout` gives us no way to
        # notice that the child has gone silent (hung) rather than exited.
        assert proc.stdout is not None
        line_q: "_queue.Queue[str | None]" = _queue.Queue()

        def _pump(stream, q):
            try:
                for ln in stream:
                    q.put(ln)
            finally:
                q.put(None)  # EOF sentinel

        reader = threading.Thread(target=_pump, args=(proc.stdout, line_q), daemon=True)
        reader.start()

        captured_lines: list[str] = []
        stalled = False
        routing_started = False
        combining_started = False
        last_output = time.monotonic()
        try:
            while True:
                # Tight timeout only during per-chunk routing (regular progress output);
                # network build and the final combine get the generous one.
                in_routing_phase = routing_started and not combining_started
                active_timeout_s = stall_timeout_s if in_routing_phase else build_timeout_s
                try:
                    line = line_q.get(timeout=1.0)
                except _queue.Empty:
                    if active_timeout_s > 0 and (time.monotonic() - last_output) > active_timeout_s:
                        if proc.poll() is None:
                            stalled = True
                            if combining_started:
                                phase = "chunk combine"
                            elif routing_started:
                                phase = "routing"
                            else:
                                phase = "network build"
                            print(
                                f"[Bus Routing] No output from Rscript ({phase} phase) for "
                                f"{active_timeout_s:.0f}s; treating as a hang and killing it.",
                                flush=True,
                            )
                            try:
                                proc.terminate()
                                proc.wait(timeout=10)
                            except Exception:
                                try:
                                    proc.kill()
                                except Exception:
                                    pass
                            break
                    continue
                if line is None:
                    break  # reader hit EOF; child has closed stdout
                print(line, end="", flush=True)  # live R5 per-chunk progress so the run isn't mistaken for stuck
                if not routing_started and _R5_ROUTING_STARTED_MARKER in line:
                    routing_started = True
                if not combining_started and _R5_COMBINE_STARTED_MARKER in line:
                    combining_started = True
                captured_lines.append(line.rstrip("\n"))
                if len(captured_lines) > 50:
                    captured_lines.pop(0)
                last_output = time.monotonic()
            return_code = proc.wait()
        except KeyboardInterrupt:
            # Forward interruption to child so Ctrl+C actually stops Rscript.
            if proc.poll() is None:
                try:
                    if os.name == "nt":
                        proc.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
            raise
        finally:
            _ACTIVE_R5_PROC = None

        if not stalled and return_code == 0:
            return

        tail = "\n".join(captured_lines[-20:])
        # A stall or an rJava SIGSEGV are both non-deterministic mid-run failures on this
        # hardware; the R script resumes from completed chunks, so relaunching makes forward
        # progress. Other non-zero exits are real errors and are not retried.
        retryable = stalled or return_code in (139, -11)
        if retryable and attempt < max_attempts:
            reason = "hung (no output)" if stalled else f"crashed with SIGSEGV (exit {return_code})"
            print(
                f"[Bus Routing] Rscript {reason} on attempt {attempt}/{max_attempts}; "
                "resuming from completed chunks...",
                flush=True,
            )
            continue
        if stalled:
            raise RuntimeError(
                f"Rscript hung with no output (stall watchdog: {build_timeout_s:.0f}s during "
                f"network build, {stall_timeout_s:.0f}s during routing) and did not recover "
                f"after {max_attempts} attempts. Last output lines:\n{tail}"
            )
        raise RuntimeError(
            f"Rscript failed with exit code {return_code}. Last output lines:\n{tail}"
        )

class _ByteTrackingTextFile:
    """Wraps a text-mode file object, tracking UTF-8 bytes consumed via iteration.

    csv.reader/DictReader only need an object supporting the iterator protocol
    (__next__ returning a str per call); this proxies that while accumulating a byte
    count as each line is pulled. Needed instead of `f.tell()` because a text-mode
    file raises "telling position disabled by next() call" once iteration has
    advanced past the first line -- there's no way to get a byte-accurate read
    position mid-iteration other than tracking it ourselves line by line.
    """

    def __init__(self, fileobj):
        self._f = fileobj
        self.bytes_read = 0

    def __iter__(self):
        return self

    def __next__(self):
        line = next(self._f)
        self.bytes_read += len(line.encode("utf-8"))
        return line


def build_bus_impedance_cache(context: PipelineContext, force_rebuild: bool = False, transport_type: str = "bus") -> None:
    """Build dense bus impedance matrix and index files from routing CSV.

    Inputs:
    - context: pipeline context with bus artifact paths.
    - force_rebuild: when True, rebuild matrix/indexes even if files exist.
    - transport_type: public transport mode ("bus", "metro", or "train").

    Outputs:
    - None. Writes matrix `.dat` and row/column index JSON files.
    """
    cfg = context.config
    paths = cfg.public_transport_paths(transport_type)
    ticket_price = {"bus": cfg.bus_ticket_price, "metro": cfg.metro_ticket_price, "train": cfg.train_ticket_price}.get(transport_type, cfg.bus_ticket_price)
    gamma = (cfg.time_indifference_bus - cfg.vot * ticket_price) / cfg.time_indifference_bus
    source_id_to_row = {}
    dest_id_to_col = {}

    matrix_path = Path(paths["impedance_matrix"])
    Path(matrix_path).parent.mkdir(parents=True, exist_ok=True)
    source_index_path = Path(paths["source_id_to_row"])
    dest_index_path = Path(paths["dest_id_to_col"])

    if (
        not force_rebuild
        and matrix_path.is_file()
        and matrix_path.stat().st_size > 0
        and source_index_path.is_file()
        and dest_index_path.is_file()
    ):
        return

    # Read the source csv and create correspondance between id and row in the csv
    with open(paths["routing_origins_input"], newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row_idx, row in enumerate(reader):
            source_id = row["id"]
            source_id_to_row[source_id] = row_idx

    # Read the destination csv and create correspondance between id and row in the csv
    with open(paths["routing_destinations_input"], newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for col_idx, row in enumerate(reader):
            dest_id = row["id"]
            dest_id_to_col[dest_id] = col_idx


    n_rows = len(source_id_to_row)
    n_cols = len(dest_id_to_col)
    # The memmap below creates the .dat at full size before any values are
    # written, so a crash mid-fill leaves a valid-looking matrix file. The index
    # JSONs are written only after a complete fill and the resume guard above
    # requires all three files — but stale JSONs from an earlier complete build
    # would defeat that guard. Remove them first so a partial matrix can never
    # pass the resume check.
    source_index_path.unlink(missing_ok=True)
    dest_index_path.unlink(missing_ok=True)
    impedance_matrix = np.memmap(
        paths["impedance_matrix"],
        dtype=np.float32,
        mode="w+",
        shape=(n_rows, n_cols),
    )
    impedance_matrix[:] = 0

    # This CSV is R5's expanded travel-time matrix and can be huge (observed: 601 MB /
    # 10.7M rows for Cagliari, 17.4 GB for Paris). The row-by-row parse below used to run
    # completely silently -- the only print was a single summary line after the entire
    # file was consumed, which for a multi-GB file is indistinguishable from a hang. Track
    # progress by bytes read (cheap: file size is known upfront, no separate row-counting
    # pass needed) and update the bar every 200k rows rather than every row, so the
    # progress signal itself doesn't add per-row overhead to the hot loop.
    matrix_csv_path = paths["routing_matrix"]
    matrix_csv_size = os.path.getsize(matrix_csv_path)
    print(
        f"[{transport_type.capitalize()}] Reading routing matrix CSV "
        f"({matrix_csv_size / (1024 ** 3):.2f} GB)...",
        flush=True,
    )
    t_start = time.monotonic()
    progress_bar = tqdm(
        total=matrix_csv_size,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc=f"{transport_type.capitalize()} impedance matrix",
        mininterval=1,
        disable=not cfg.enable_progress,
    )
    bytes_seen = 0
    with open(matrix_csv_path, newline="", encoding="utf-8") as raw_f:
        f = _ByteTrackingTextFile(raw_f)
        reader = csv.DictReader(f)
        missing_from_ids = set()
        missing_to_ids = set()
        for row_idx, row in enumerate(reader):
            from_id = row["from_id"]
            to_id = row["to_id"]
            source_row = source_id_to_row.get(from_id)
            dest_col = dest_id_to_col.get(to_id)
            if source_row is None:
                missing_from_ids.add(from_id)
                continue
            if dest_col is None:
                missing_to_ids.add(to_id)
                continue
            total_time_raw = row.get("total_time", "")
            wait_time_raw = row.get("wait_time", "")
            if total_time_raw == "" or (row["routes"] in ["", "[WALK]"]):
                impedance = 0.0
            else:
                try:
                    wait_time    = float(wait_time_raw) if wait_time_raw != "" else 0.0
                    ride_time    = float(row.get("ride_time",     "") or 0.0)
                    access_time  = float(row.get("access_time",   "") or 0.0)
                    transfer_time = float(row.get("transfer_time","") or 0.0)
                    egress_time  = float(row.get("egress_time",   "") or 0.0)
                except ValueError:
                    impedance = 0.0
                else:
                    impedance = (
                        access_time + wait_time + gamma * ride_time
                        + transfer_time + egress_time
                        + float(cfg.vot) * ticket_price
                    )
                    if impedance < 0:
                        impedance = 0.0
            impedance_matrix[source_row][dest_col] = impedance
            if row_idx % 200_000 == 0:
                pos = f.bytes_read
                progress_bar.update(pos - bytes_seen)
                bytes_seen = pos
                rows_per_s = (row_idx + 1) / max(time.monotonic() - t_start, 1e-9)
                progress_bar.set_postfix({"rows/s": f"{rows_per_s:,.0f}"}, refresh=False)
        progress_bar.update(f.bytes_read - bytes_seen)
    progress_bar.close()
    impedance_matrix.flush()
    nonzero_count = int(np.count_nonzero(impedance_matrix))
    total_count = int(impedance_matrix.size)
    elapsed_s = time.monotonic() - t_start
    print(
        f"[{transport_type.capitalize()}] Built {transport_type} impedance matrix: "
        f"nonzero={nonzero_count}/{total_count} zero={total_count - nonzero_count} "
        f"rows={row_idx + 1:,} elapsed={elapsed_s:.0f}s",
        flush=True,
    )

    if missing_from_ids or missing_to_ids:
        sample_from = sorted(missing_from_ids)[:5]
        sample_to = sorted(missing_to_ids)[:5]
        raise RuntimeError(
            "Routing CSV contains origin/destination IDs not present in the current run inputs. "
            "This usually means stale or mixed chunk files were reused. "
            f"Unknown from_id count={len(missing_from_ids)} sample={sample_from}; "
            f"unknown to_id count={len(missing_to_ids)} sample={sample_to}."
        )

    with open(paths["source_id_to_row"], 'w') as f:
        json.dump(source_id_to_row, f)

    with open(paths["dest_id_to_col"], 'w') as f:
        json.dump(dest_id_to_col, f)

def _save_routing_cache(path: str, payload) -> None:
    """Atomically persist routing cache payload to pickle.

    Inputs:
    - path: destination pickle path.
    - payload: routing cache payload dictionary.

    Outputs:
    - None. Writes pickle file with atomic replace.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp_path, path)


# Bump when the destination-cache payload layout or its signature inputs change, so old
# caches are treated as stale instead of silently reused.
_PT_DEST_CACHE_SCHEMA_VERSION = 1


def _hash_origins_and_snap(h, origins, poi_bus_snap_info_by_type) -> None:
    """Fold the origins and snapped candidate coordinates into hash `h`.

    These are exactly the inputs the expensive per-origin candidate selection depends on
    (the radius is NOT one of them -- it enters only as a post-filter). Coordinates are
    rounded to COORD_ROUND and folded in a deterministic order so the digest changes
    exactly when the selection output would.
    """
    for lat, lon in origins:
        h.update(f"{round(float(lat), COORD_ROUND)},{round(float(lon), COORD_ROUND)};".encode("ascii"))
    h.update(b"|snap|")
    for poi_key in sorted(poi_bus_snap_info_by_type.keys(), key=repr):
        snap_info_for_key = poi_bus_snap_info_by_type[poi_key]
        for source_key in sorted(snap_info_for_key.keys(), key=repr):
            snap_info = snap_info_for_key[source_key]
            candidates = snap_info.get("candidates") if isinstance(snap_info, dict) else snap_info
            if not isinstance(candidates, (list, tuple)):
                continue
            for cand in candidates:
                if not isinstance(cand, (list, tuple)) or len(cand) < 2:
                    continue
                snapped = cand[0]
                if not isinstance(snapped, (list, tuple)) or len(snapped) < 2:
                    continue
                h.update(
                    f"{round(float(snapped[0]), COORD_ROUND)},"
                    f"{round(float(snapped[1]), COORD_ROUND)};".encode("ascii")
                )


def _pt_selection_signature(origins, poi_bus_snap_info_by_type) -> str:
    """Radius-INDEPENDENT fingerprint of the per-origin candidate selection.

    The O(origins × POIs) selection (build_selected_routing_destinations) depends only on
    the origins and snap candidates, never on the radius, so this key stays stable across a
    radius change -- letting a sensitivity sweep over poi_radius_m reuse the cached selection
    instead of recomputing it. Radius is applied afterwards as a cheap BallTree post-filter.
    """
    h = hashlib.sha1()
    h.update(f"selection|schema={_PT_DEST_CACHE_SCHEMA_VERSION}|".encode("ascii"))
    _hash_origins_and_snap(h, origins, poi_bus_snap_info_by_type)
    return h.hexdigest()


def _pt_destinations_signature(origins, poi_bus_snap_info_by_type, radius_m) -> str:
    """Fingerprint of the FINAL radius-filtered destination set.

    Same inputs as the selection signature plus the global radius, so it changes exactly
    when the post-filtered destination set would.
    """
    h = hashlib.sha1()
    h.update(f"schema={_PT_DEST_CACHE_SCHEMA_VERSION}|radius={radius_m}|".encode("ascii"))
    _hash_origins_and_snap(h, origins, poi_bus_snap_info_by_type)
    return h.hexdigest()


def _load_pt_destination_cache(path: str, signature: str):
    """Return cached selected destinations when the signature matches, else None.

    Honours CAP_IGNORE_SNAP_CACHE, the same bypass switch used by the snap caches.
    """
    if os.environ.get("CAP_IGNORE_SNAP_CACHE"):
        return None
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            payload = pickle.load(f)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("signature") != signature:
        return None
    destinations = payload.get("destinations")
    if not isinstance(destinations, list):
        return None
    return [(float(lat), float(lon)) for lat, lon in destinations]


def _save_pt_destination_cache(path: str, signature: str, destinations) -> None:
    """Persist selected destinations keyed by their input signature (atomic write)."""
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "wb") as f:
        pickle.dump(
            {"signature": signature, "destinations": list(destinations)},
            f,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    os.replace(tmp_path, path)


def _load_pt_destination_cache_raw(path: str) -> list[tuple[float, float]] | None:
    """Load a previous run's selected destinations regardless of signature.

    Unlike _load_pt_destination_cache, this doesn't require knowing the old
    run's signature (which depended on the old, now-superseded origin set) --
    it's used purely to recover the OLD destination list's (lat, lon) order,
    needed to resolve old "d{idx}" labels back to real coordinates when
    reusing a prior run's routing results (see _partition_pt_reuse_work).
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            payload = pickle.load(f)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    destinations = payload.get("destinations")
    if not isinstance(destinations, list) or not destinations:
        return None
    try:
        return [(float(lat), float(lon)) for lat, lon in destinations]
    except (TypeError, ValueError):
        return None


def _scan_old_routing_matrix_origins(matrix_csv_path: str, enable_progress: bool = True) -> set[str] | None:
    """Return the distinct from_id values (origin node ids) in a prior run's
    routing matrix CSV, streamed (these files can be multi-GB -- e.g. 17GB for
    Paris -- see the comment in build_bus_impedance_cache), or None if the
    file is unusable. Byte-driven progress bar so a multi-minute scan on a
    huge file isn't silent (see CLAUDE.md: this bit the project for real once
    already, on a routing CSV of this exact scale).
    """
    if not matrix_csv_path or not os.path.isfile(matrix_csv_path) or os.path.getsize(matrix_csv_path) == 0:
        return None
    matrix_csv_size = os.path.getsize(matrix_csv_path)
    origins: set[str] = set()
    try:
        with open(matrix_csv_path, newline="", encoding="utf-8") as raw_f:
            f = _ByteTrackingTextFile(raw_f)
            reader = csv.DictReader(f)
            if reader.fieldnames is None or "from_id" not in reader.fieldnames:
                return None
            with tqdm(
                total=matrix_csv_size, unit="B", unit_scale=True, unit_divisor=1024,
                desc="Scanning prior routing matrix for reusable origins",
                mininterval=1, disable=not enable_progress,
            ) as bar:
                for row_idx, row in enumerate(reader):
                    origins.add(row["from_id"])
                    if row_idx % 200_000 == 0:
                        bar.update(f.bytes_read - bar.n)
                bar.update(f.bytes_read - bar.n)
    except Exception:
        return None
    return origins or None


def _partition_pt_reuse_work(
    old_matrix_csv: str,
    old_destinations: list[tuple[float, float]] | None,
    new_origin_ids: set[str],
    new_destinations: list[tuple[float, float]],
    enable_progress: bool = True,
):
    """Work out what a prior run's routing results can safely cover for a new,
    larger origin/destination set, and what genuinely needs (re-)routing.

    Returns None if the prior run's artifacts aren't usable for reuse (first
    run for this city, missing/corrupt files, no overlap, etc.) -- callers
    should fall back to a plain full routing run in that case, which is
    always correct, just potentially slower.

    Otherwise returns a dict:
      - old_origins: set[str] of origin ids covered by the prior run
      - carried_origins: old_origins & new_origin_ids (safe to reuse for)
      - brand_new_origins: new_origin_ids - old_origins (need full routing)
      - old_to_new_dest_id: dict mapping old "d{idx}" label -> new "d{idx}"
        label, for old destinations whose coordinate is still present in the
        new destination list (only these old rows are safely reusable)
      - new_destinations_only: set[str] of new "d{idx}" labels with no
        matching old destination -- carried_origins still need routing to
        these, since the prior run never computed them.

    Destinations are matched by coordinate (rounded to COORD_ROUND), not by
    "d{idx}" label -- that label is just the destination's index in each
    run's own destination list, not a stable identity across runs (unlike
    origins, which are real graph node ids).
    """
    old_origins = _scan_old_routing_matrix_origins(old_matrix_csv, enable_progress=enable_progress)
    if not old_origins:
        return None
    carried_origins = old_origins & new_origin_ids
    if not carried_origins:
        # No overlap at all -- e.g. a totally different city/study area.
        # Nothing to reuse; a full run is no more expensive than trying.
        return None

    if not old_destinations:
        return None

    new_dest_id_by_coord = {
        (round(lat, COORD_ROUND), round(lon, COORD_ROUND)): f"d{idx}"
        for idx, (lat, lon) in enumerate(new_destinations)
    }
    old_to_new_dest_id: dict[str, str] = {}
    for old_idx, (lat, lon) in enumerate(old_destinations):
        new_id = new_dest_id_by_coord.get((round(lat, COORD_ROUND), round(lon, COORD_ROUND)))
        if new_id is not None:
            old_to_new_dest_id[f"d{old_idx}"] = new_id

    if not old_to_new_dest_id:
        return None

    brand_new_origins = new_origin_ids - old_origins
    new_destinations_only = set(new_dest_id_by_coord.values()) - set(old_to_new_dest_id.values())

    return {
        "old_origins": old_origins,
        "carried_origins": carried_origins,
        "brand_new_origins": brand_new_origins,
        "old_to_new_dest_id": old_to_new_dest_id,
        "new_destinations_only": new_destinations_only,
    }


def _stream_reuse_old_routing_rows(
    old_matrix_csv: str,
    carried_origins: set[str],
    old_to_new_dest_id: dict[str, str],
    out_csv: str,
    fieldnames: list[str],
    enable_progress: bool = True,
) -> int:
    """Stream old_matrix_csv, keep only rows for carried-over origins whose
    destination still exists in the new run, rewrite their to_id to the new
    run's label for that same destination, and write them to out_csv.

    Streamed both ways (the input can be multi-GB, e.g. 17GB for Paris) so
    peak memory is one row, not the whole file. Byte-driven progress bar for
    the same reason as _scan_old_routing_matrix_origins. Returns the number
    of rows written.
    """
    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    in_size = os.path.getsize(old_matrix_csv)
    n_written = 0
    with open(old_matrix_csv, newline="", encoding="utf-8") as raw_fin, \
         open(out_csv, "w", newline="", encoding="utf-8") as fout:
        fin = _ByteTrackingTextFile(raw_fin)
        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()
        with tqdm(
            total=in_size, unit="B", unit_scale=True, unit_divisor=1024,
            desc=f"Extracting reusable rows from {os.path.basename(old_matrix_csv)}",
            mininterval=1, disable=not enable_progress,
        ) as bar:
            for row_idx, row in enumerate(reader):
                if row["from_id"] in carried_origins:
                    new_dest_id = old_to_new_dest_id.get(row["to_id"])
                    if new_dest_id is not None:
                        row["to_id"] = new_dest_id
                        writer.writerow(row)
                        n_written += 1
                if row_idx % 200_000 == 0:
                    bar.update(fin.bytes_read - bar.n)
            bar.update(fin.bytes_read - bar.n)
    return n_written


def _build_pt_selection(ctx, snap, cfg):
    """Select the unique candidate destination nodes -- the expensive, radius-INDEPENDENT part.

    Single-candidate POIs contribute their snapped node directly; multi-candidate POIs pick,
    per origin, the closest candidate (build_selected_routing_destinations). This is the
    O(origins × POIs) work; it does not depend on the radius, so its result is cached keyed
    on origins+snap alone (see _pt_selection_signature) and reused across radius changes.
    """
    all_candidate_snapped_coords = set()
    has_multi_snap_candidates = False
    for snap_info_for_key in snap.poi_bus_snap_info_by_type.values():
        for snap_info in snap_info_for_key.values():
            candidates = snap_info.get("candidates") if isinstance(snap_info, dict) else snap_info
            if not isinstance(candidates, (list, tuple)):
                continue
            if len(candidates) > 1:
                has_multi_snap_candidates = True
            for cand in candidates:
                if not isinstance(cand, (list, tuple)) or len(cand) < 2:
                    continue
                snapped = cand[0]
                if not isinstance(snapped, (list, tuple)) or len(snapped) < 2:
                    continue
                all_candidate_snapped_coords.add(tuple(snapped))

    if has_multi_snap_candidates:
        unique_snapped_coords = build_selected_routing_destinations(
            ctx.nodes_with_coords,
            snap.poi_bus_snap_info_by_type,
            cfg.enable_progress,
        )
        print(
            "Bus destination reduction: "
            f"candidate_nodes={len(all_candidate_snapped_coords)} "
            f"selected_nodes={len(unique_snapped_coords)}"
        )
    else:
        unique_snapped_coords = sorted(all_candidate_snapped_coords)

    return list(unique_snapped_coords)


def _apply_radius_prefilter(unique_snapped_coords, origins, global_radius_m):
    """Drop selected destinations no origin can reach within the global radius.

    The cheap, radius-DEPENDENT half of destination selection: one BallTree pass over the
    already-selected nodes. Run fresh every run so a radius change reuses the cached
    selection (see _build_pt_selection) and only redoes this sub-second filter.
    """
    if global_radius_m is None or not unique_snapped_coords:
        return list(unique_snapped_coords)

    from sklearn.neighbors import BallTree
    dest_arr = np.radians(np.array(unique_snapped_coords, dtype=np.float64))   # (D, 2)
    origin_arr = np.radians(np.array(origins, dtype=np.float64))               # (O, 2)
    # BallTree with haversine uses unit-sphere distances; divide radius by Earth radius.
    tree = BallTree(dest_arr, metric="haversine")
    radius_rad = global_radius_m / 6_371_000.0
    reachable: set[int] = set()
    for idx_list in tree.query_radius(origin_arr, r=radius_rad, return_distance=False):
        reachable.update(idx_list.tolist())
    filtered_coords = [unique_snapped_coords[i] for i in sorted(reachable)]
    print(
        f"[Bus] Destination radius pre-filter: radius={global_radius_m/1000:.1f} km  "
        f"kept={len(filtered_coords)}/{len(unique_snapped_coords)}",
        flush=True,
    )
    return filtered_coords


def _select_pt_destinations(ctx, snap, cfg, origins, global_radius_m):
    """Build the radius-filtered routing destination set (selection + radius post-filter)."""
    unique_snapped_coords = _build_pt_selection(ctx, snap, cfg)
    return _apply_radius_prefilter(unique_snapped_coords, origins, global_radius_m)


# This function runs the bus routing stage
def run_public_transport_routing_stage(ctx: PipelineContext, snap: SnappingStageResult, transport_type: str = "bus") -> BusRoutingStageResult:
    """Execute transit routing stage and produce reusable public transport routing artifacts.

    Inputs:
    - ctx: pipeline context with configuration and graph nodes.
    - snap: snapping results with public transport destination candidates.
    - transport_type: type of public transport being routed (e.g. "bus", "tram", "metro").

    Outputs:
    - BusRoutingStageResult: paths and signatures for downstream stages.
    """
    cfg = ctx.config
    paths = cfg.public_transport_paths(transport_type)
    departure_iso = (cfg.subway_departure_dt if transport_type in ("subway","metro") else cfg.bus_departure_dt).isoformat()
    routing_csv = paths["routing_matrix"]
    routing_cache = paths["routing_cache"]

    origins = [(data["y"], data["x"]) for _, data in ctx.nodes_with_coords]
    origins_sig = _coords_signature(origins)

    # Snapshot whatever destinations a PRIOR run selected, before the destination-selection
    # cache below is (possibly) overwritten with this run's own destinations -- needed later
    # to resolve old "d{idx}" labels in a stale routing_matrix.csv back to real coordinates
    # for partial reuse. Bypasses the signature check deliberately: an old run's signature is
    # keyed on its own (now superseded) origin set, so it will never match the new one, but
    # the destination list itself is still exactly what assigned those old labels.
    _prior_destinations_snapshot = _load_pt_destination_cache_raw(cfg.pt_destination_cache_path)

    from utils import services as serv
    global_radius_m = serv.get_global_radius_m(cfg)

    # Two-layer destination cache. The final destinations are a pure function of origins,
    # snap candidates, and the global radius -- but the expensive O(origins × POIs) selection
    # depends only on origins + snap, NOT the radius, which enters solely as a cheap BallTree
    # post-filter. Splitting the cache accordingly lets a radius change (e.g. a poi_radius_m
    # sensitivity sweep) reuse the selection and redo only the sub-second filter, instead of
    # recomputing the whole thing. Both layers are shared across bus/subway.
    dest_cache_key = _pt_destinations_signature(origins, snap.poi_bus_snap_info_by_type, global_radius_m)
    destinations = _load_pt_destination_cache(cfg.pt_destination_cache_path, dest_cache_key)
    if destinations is not None:
        print(
            f"[{transport_type.capitalize()}] Loaded {len(destinations)} radius-filtered "
            "destinations from cache; skipping selection.",
            flush=True,
        )
    else:
        # Layer 1: radius-independent selection (the 12h O(origins × POIs) part).
        selection_key = _pt_selection_signature(origins, snap.poi_bus_snap_info_by_type)
        selected_nodes = _load_pt_destination_cache(cfg.pt_selection_cache_path, selection_key)
        if selected_nodes is not None:
            print(
                f"[{transport_type.capitalize()}] Loaded {len(selected_nodes)} selected "
                "destination nodes from cache; skipping selection (radius-independent).",
                flush=True,
            )
        else:
            selected_nodes = _build_pt_selection(ctx, snap, cfg)
            _save_pt_destination_cache(cfg.pt_selection_cache_path, selection_key, selected_nodes)
        # Layer 2: cheap radius post-filter, recomputed whenever the radius changes.
        destinations = _apply_radius_prefilter(selected_nodes, origins, global_radius_m)
        _save_pt_destination_cache(cfg.pt_destination_cache_path, dest_cache_key, destinations)

    destinations_sig = _coords_signature(destinations)

    # Auto-skip: if a routing cache already exists and matches this exact run (same
    # departure time, origins, and destination set), the R5/Rscript launch and the
    # impedance-matrix rebuild are both redundant. This replaces the old skip_routing
    # manual flag, which required the operator to know in advance whether a matching
    # cache existed on disk and raised a hard error if they guessed wrong. Both checks
    # here are cheap regardless of city size: routing_cache is a small pickle (actual
    # impedances live in the memmap, not this file), and build_bus_impedance_cache's
    # own existence check (force_rebuild=False) is a few stat() calls, not a rebuild.
    cache_is_current = False
    if os.path.isfile(routing_cache):
        try:
            payload = _load_routing_cache(routing_cache)
            cache_is_current = _is_valid_routing_cache(
                payload,
                departure_iso=departure_iso,
                origins_sig=origins_sig,
                destinations_sig=destinations_sig,
            )
        except Exception:
            cache_is_current = False
    if cache_is_current:
        build_bus_impedance_cache(ctx, force_rebuild=False, transport_type=transport_type)
        print(
            f"[{transport_type.capitalize()}] Routing artifacts already up to date "
            "(departure/origins/destinations unchanged); skipping R5 routing.",
            flush=True,
        )
        return BusRoutingStageResult(
            routing_csv=routing_csv,
            routing_pkl=routing_cache,
            routing_departure_iso=departure_iso,
            origins_sig=origins_sig,
            destinations_sig=destinations_sig,
        )

    # No snapped destinations for this city: skip R routing and emit empty-safe artifacts.
    if not destinations:
        print(f"[{transport_type.capitalize()}] No snapped destinations found. Writing empty artifacts and skipping R routing.", flush=True)

        _write_r5r_point_inputs(
            ctx.nodes_with_coords,
            [],
            paths["routing_origins_input"],
            paths["routing_destinations_input"],
        )
        _write_dummy_destination_csv(paths["routing_destinations_input"])
        _write_empty_routing_csv(routing_csv)
        build_bus_impedance_cache(ctx, force_rebuild=True, transport_type=transport_type)

        _save_routing_cache(
            routing_cache,
            {
                "departure_iso": departure_iso,
                "origins_sig": origins_sig,
                "destinations_sig": destinations_sig,
                "routes": {},
            },
        )
        return BusRoutingStageResult(
            routing_csv=routing_csv,
            routing_pkl=routing_cache,
            routing_departure_iso=departure_iso,
            origins_sig=origins_sig,
            destinations_sig=destinations_sig,
        )

    
    r_origins_csv = paths["routing_origins_input"]
    r_dest_csv = paths["routing_destinations_input"]

    # Work out whether a prior run's routing_matrix.csv (still on disk at routing_csv, not
    # yet overwritten) can partially cover this run's larger origin/destination set, before
    # anything below overwrites it. See _partition_pt_reuse_work's docstring for the matching
    # rules; None means "not usable" (first run, no overlap, corrupt/missing data, etc.) and
    # we fall straight through to the existing, unmodified full-recompute path.
    new_origin_ids = {str(node_id) for node_id, _ in ctx.nodes_with_coords}
    reuse_plan = None
    if os.path.isfile(routing_csv) and os.path.getsize(routing_csv) > 0:
        try:
            reuse_plan = _partition_pt_reuse_work(
                routing_csv, _prior_destinations_snapshot, new_origin_ids, destinations,
                enable_progress=cfg.enable_progress,
            )
        except Exception as exc:
            print(
                f"[{transport_type.capitalize()}] Could not evaluate partial R5 reuse "
                f"({exc}); falling back to a full routing run.",
                flush=True,
            )
            reuse_plan = None

    _write_r5r_point_inputs(
        ctx.nodes_with_coords,
        destinations,
        r_origins_csv,
        r_dest_csv,
    )

    r_script_path = os.path.join("utils", "r5_routing.r")
    # Deliberately do NOT wipe the chunk dir here. The R script resumes by skipping any
    # already-completed chunk_*.csv file it finds in r5r_chunks/ for this city+transport_type
    # (no fingerprint check against the origin/dest CSVs or routing params -- see
    # utils/r5_routing.r). Wiping here would defeat that resume across process restarts — a
    # re-run would always start from chunk 1 even though the completed chunks were still
    # valid. Bus and subway use separate chunk dirs (distinct routing_matrix paths), so
    # leaving them intact can't cross-contaminate. If you deliberately change chunk sizes,
    # destinations, or departure time for a city+transport_type, clear its chunk dir yourself.

    if reuse_plan is not None:
        n_carried = len(reuse_plan["carried_origins"])
        n_brand_new = len(reuse_plan["brand_new_origins"])
        print(
            f"[{transport_type.capitalize()}] Partial R5 reuse: {n_carried} origins carried "
            f"over from the prior run, {n_brand_new} brand-new origins need routing "
            f"({len(reuse_plan['new_destinations_only'])} destinations are new to the "
            "carried-over origins).",
            flush=True,
        )
        matrix_fieldnames = [
            "from_id", "to_id", "departure_time", "draw_number", "access_time", "wait_time",
            "ride_time", "transfer_time", "egress_time", "routes", "n_rides", "total_time",
        ]
        work_dir = os.path.dirname(routing_csv)
        reused_csv = os.path.join(work_dir, f"r5r_reused_{transport_type}.csv")
        n_reused = _stream_reuse_old_routing_rows(
            routing_csv, reuse_plan["carried_origins"], reuse_plan["old_to_new_dest_id"],
            reused_csv, matrix_fieldnames, enable_progress=cfg.enable_progress,
        )
        print(f"[{transport_type.capitalize()}] Reused {n_reused} previously-routed rows.", flush=True)

        fragment_csvs = [reused_csv]

        if reuse_plan["brand_new_origins"]:
            job_a_origins_csv = os.path.join(work_dir, f"r5r_jobA_origins_{transport_type}.csv")
            job_a_output_csv = os.path.join(work_dir, f"r5r_jobA_output_{transport_type}.csv")
            job_a_chunk_dir = os.path.join(work_dir, f"r5r_chunks_jobA_{transport_type}")
            # Job A routes to the full new destination set, so it can reuse r_dest_csv
            # (already written above via the standard _write_r5r_point_inputs call) as-is --
            # only the origins subset differs.
            _write_r5r_point_inputs(
                [(node_id, data) for node_id, data in ctx.nodes_with_coords
                 if str(node_id) in reuse_plan["brand_new_origins"]],
                destinations,
                job_a_origins_csv,
                r_dest_csv,
            )
            print(f"[{transport_type.capitalize()}] Launching Rscript (job A: brand-new origins)...", flush=True)
            _run_r5r_script(
                r_script_path, ctx, transport_type=transport_type,
                origins_csv=job_a_origins_csv, destinations_csv=r_dest_csv,
                output_csv=job_a_output_csv, chunk_dir=job_a_chunk_dir,
            )
            fragment_csvs.append(job_a_output_csv)

        if reuse_plan["carried_origins"] and reuse_plan["new_destinations_only"]:
            new_dest_id_by_coord = {
                f"d{idx}": (lat, lon) for idx, (lat, lon) in enumerate(destinations)
            }
            # Job B gets its own destinations CSV containing only this subset, so
            # _write_r5r_point_inputs will label them positionally (d0, d1, ...) local to
            # that subset -- NOT the same "d{idx}" labels used in the global destinations
            # list / r_dest_csv. local_to_global_dest_id undoes that after routing so the
            # output's to_id values match what build_bus_impedance_cache expects (it reads
            # r_dest_csv, the global file, to build its id->column mapping).
            ordered_global_ids = sorted(reuse_plan["new_destinations_only"], key=lambda s: int(s[1:]))
            job_b_destinations = [new_dest_id_by_coord[d_id] for d_id in ordered_global_ids]
            local_to_global_dest_id = {f"d{i}": d_id for i, d_id in enumerate(ordered_global_ids)}
            job_b_origins_csv = os.path.join(work_dir, f"r5r_jobB_origins_{transport_type}.csv")
            job_b_dest_csv = os.path.join(work_dir, f"r5r_jobB_dest_{transport_type}.csv")
            job_b_output_csv_raw = os.path.join(work_dir, f"r5r_jobB_output_raw_{transport_type}.csv")
            job_b_output_csv = os.path.join(work_dir, f"r5r_jobB_output_{transport_type}.csv")
            job_b_chunk_dir = os.path.join(work_dir, f"r5r_chunks_jobB_{transport_type}")
            _write_r5r_point_inputs(
                [(node_id, data) for node_id, data in ctx.nodes_with_coords
                 if str(node_id) in reuse_plan["carried_origins"]],
                job_b_destinations,
                job_b_origins_csv,
                job_b_dest_csv,
            )
            print(
                f"[{transport_type.capitalize()}] Launching Rscript (job B: carried-over "
                "origins x new-only destinations)...",
                flush=True,
            )
            _run_r5r_script(
                r_script_path, ctx, transport_type=transport_type,
                origins_csv=job_b_origins_csv, destinations_csv=job_b_dest_csv,
                output_csv=job_b_output_csv_raw, chunk_dir=job_b_chunk_dir,
            )
            _stream_reuse_old_routing_rows(
                job_b_output_csv_raw, reuse_plan["carried_origins"], local_to_global_dest_id,
                job_b_output_csv, matrix_fieldnames, enable_progress=cfg.enable_progress,
            )
            fragment_csvs.append(job_b_output_csv)

        existing_fragments = [
            p for p in fragment_csvs if os.path.isfile(p) and os.path.getsize(p) > 0
        ]
        total_frag_size = sum(os.path.getsize(p) for p in existing_fragments)
        with open(routing_csv, "w", newline="", encoding="utf-8") as fout, tqdm(
            total=total_frag_size, unit="B", unit_scale=True, unit_divisor=1024,
            desc=f"Combining {transport_type} routing fragments", mininterval=1,
            disable=not cfg.enable_progress,
        ) as bar:
            writer = csv.DictWriter(fout, fieldnames=matrix_fieldnames)
            writer.writeheader()
            bytes_done_prior_fragments = 0
            for frag_path in existing_fragments:
                with open(frag_path, newline="", encoding="utf-8") as raw_fin:
                    fin = _ByteTrackingTextFile(raw_fin)
                    reader = csv.DictReader(fin)
                    for row_idx, row in enumerate(reader):
                        writer.writerow(row)
                        if row_idx % 200_000 == 0:
                            bar.update(bytes_done_prior_fragments + fin.bytes_read - bar.n)
                    bar.update(bytes_done_prior_fragments + fin.bytes_read - bar.n)
                bytes_done_prior_fragments += os.path.getsize(frag_path)
        print(f"[{transport_type.capitalize()}] Combined reused + newly-routed rows into {routing_csv}.", flush=True)
    else:
        print(f"[{transport_type.capitalize()}] Launching Rscript...", flush=True)
        _run_r5r_script(r_script_path, ctx, transport_type=transport_type)
    print(f"[{transport_type.capitalize()}] Rscript completed. Building routing cache from CSV...", flush=True)

    build_bus_impedance_cache(ctx, force_rebuild=True, transport_type=transport_type)

    # Save a lightweight metadata pkl — actual impedances live in the .dat memmap.
    # Materialising all routes in memory is prohibitive for large cities.
    _save_routing_cache(routing_cache, {
        "departure_iso": departure_iso,
        "origins_sig": origins_sig,
        "destinations_sig": destinations_sig,
        "routes": {},
    })

    return BusRoutingStageResult(
        routing_csv=routing_csv,
        routing_pkl=routing_cache,
        routing_departure_iso=departure_iso,
        origins_sig=origins_sig,
        destinations_sig=destinations_sig,
    )


