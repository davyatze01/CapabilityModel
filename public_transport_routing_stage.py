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
from pipeline_types import BusRoutingStageResult, PipelineContext, SnappingStageResult
from snapping_stage import build_selected_routing_destinations
from runtime_setup import _excluded_cpus
import json

COORD_ROUND = 6

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

    return data_dir


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

def _validate_routing_artifacts_for_skip(skip_routing: bool, cache_path: str, departure_iso: str,
    origins_sig: str | None = None, destinations_sig: str | None = None,):
    """Fail fast when skip mode is enabled but routing cache is missing/incompatible.

    Inputs:
    - skip_routing: whether the stage is configured to reuse existing artifacts.
    - cache_path: routing pickle path to validate.
    - departure_iso: expected departure datetime string.
    - origins_sig: optional expected origin signature.
    - destinations_sig: optional expected destination signature.

    Outputs:
    - None. Raises RuntimeError on invalid artifacts.
    """
    if not skip_routing:
        return
    if not os.path.isfile(cache_path):
        raise RuntimeError(
            "Missing routing cache while skip_routing=True. "
            f"Expected file: {cache_path}"
        )
    try:
        payload = _load_routing_cache(cache_path)
    except Exception as exc:
        raise RuntimeError(f"Failed to load routing cache: {cache_path}") from exc
    if not _is_valid_routing_cache(payload, departure_iso=departure_iso, origins_sig=origins_sig, destinations_sig=destinations_sig):
        raise RuntimeError(
            "Routing cache is invalid or incompatible while skip_routing=True. "
            f"Cache file: {cache_path}"
        )

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

    Subway/metro route only the subway layer. Bus routes only the bus layer when a second
    modality (subway) is also being routed for this city, so the two don't double-count on a
    combined feed; otherwise bus falls back to the generic "TRANSIT" to preserve the original
    single-feed behaviour for cities like Cagliari.
    """
    if transport_type in ("subway", "metro"):
        return "SUBWAY"
    if getattr(cfg, "enable_subway", False):
        return "BUS"
    return "TRANSIT"


def _run_r5r_script(script_path: str, ctx: PipelineContext, transport_type: str = "bus") -> None:
    """Run the external R routing script and keep shell output concise.

    Inputs:
    - script_path: path to the R script entrypoint.
    - transport_type: modality being routed ("bus" or "subway"/"metro"); selects the
      per-mode artifact paths and the r5r transit mode.

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
            raise RuntimeError(
                "Rscript executable not found. Add Rscript to your PATH so the pipeline can run the R routing script."
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
    env["R5_ORIGINS_PATH"] = paths["routing_origins_input"]
    env["R5_DEST_PATH"] = paths["routing_destinations_input"]
    env["R5_OUTPUT_PATH"] = paths["routing_matrix"]
    env["R5_CHUNK_DIR"] = os.path.join(os.path.dirname(paths["routing_matrix"]), "r5r_chunks")
    env["R5_DEPARTURE_DATETIME"] = cfg.bus_departure_dt.strftime("%Y-%m-%d %H:%M:%S")
    env["R5_TRANSIT_MODE"] = _resolve_transit_mode(cfg, transport_type)
    # Destination chunk size controls how many destinations the R script passes to
    # expanded_travel_time_matrix per call. Each call holds ~origins×dests×60 rows in the
    # JVM heap + R data.table simultaneously.
    budget_gb = float(os.environ.get("CAP_MEM_BUDGET_GB", "0") or "0")
    if "R5_DEST_CHUNK_SIZE" not in env:
        # Measured on Cagliari (200 origins x 2000 dests, ~9.9M expanded rows): a single
        # chunk peaks at ~6.95GB JVM heap / ~12.5GB RSS against a ~15.6GB heap ceiling, and
        # RSS doesn't fully return to baseline after cleanup (G1GC frees the Java heap but
        # lazily uncommits it back to the OS). Two such chunks back-to-back land close
        # enough to the ceiling that G1 falls into repeated expensive full GCs trying to
        # free room for the next allocation — indistinguishable from a hang, not a crash.
        # 500 destinations cuts peak per-chunk usage roughly 4x, leaving real headroom
        # regardless of whether RSS fully unwinds between chunks.
        dest_chunk_size = 500
        env["R5_DEST_CHUNK_SIZE"] = str(dest_chunk_size)
        print(
            f"[Bus Routing] R5_DEST_CHUNK_SIZE={dest_chunk_size} "
            f"(budget={budget_gb:.0f}GB)",
            flush=True,
        )
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
            "R5_TRANSIT_MODE", "R5_DEST_CHUNK_SIZE", "R5_JVM_MAX_HEAP_GB",
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
        last_output = time.monotonic()
        try:
            while True:
                active_timeout_s = stall_timeout_s if routing_started else build_timeout_s
                try:
                    line = line_q.get(timeout=1.0)
                except _queue.Empty:
                    if active_timeout_s > 0 and (time.monotonic() - last_output) > active_timeout_s:
                        if proc.poll() is None:
                            stalled = True
                            phase = "routing" if routing_started else "network build"
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

    with open(paths["routing_matrix"], newline="", encoding="utf-8") as f:
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
    impedance_matrix.flush()
    nonzero_count = int(np.count_nonzero(impedance_matrix))
    total_count = int(impedance_matrix.size)
    print(
        f"[{transport_type.capitalize()}] Built {transport_type} impedance matrix: "
        f"nonzero={nonzero_count}/{total_count} zero={total_count - nonzero_count}",
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


def _pt_destinations_signature(origins, poi_bus_snap_info_by_type, radius_m) -> str:
    """Fingerprint the inputs that determine the selected routing destinations.

    The selection output depends only on the origins, the snapped candidate coordinates,
    and the global radius, so hashing those (rounded to COORD_ROUND, in a deterministic
    order) yields a key that changes exactly when the destination set would.
    """
    h = hashlib.sha1()
    h.update(f"schema={_PT_DEST_CACHE_SCHEMA_VERSION}|radius={radius_m}|".encode("ascii"))
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


def _select_pt_destinations(ctx, snap, cfg, origins, global_radius_m):
    """Select the unique reachable set of routing destinations from the snap candidates.

    Single-candidate POIs contribute their snapped node directly; multi-candidate POIs
    pick, per origin, the closest candidate (build_selected_routing_destinations). A final
    BallTree radius pre-filter drops destinations no origin can reach within the global
    radius. This is the O(origins × POIs) work the destination cache exists to avoid
    repeating on restart.
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

    if global_radius_m is not None and unique_snapped_coords:
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
        unique_snapped_coords = filtered_coords

    return list(unique_snapped_coords)


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
    departure_iso = cfg.bus_departure_dt.isoformat()
    routing_csv = paths["routing_matrix"]
    routing_cache = paths["routing_cache"]

    origins = [(data["y"], data["x"]) for _, data in ctx.nodes_with_coords]
    origins_sig = _coords_signature(origins)

    # skip_routing: reuse the previously computed routing artifacts wholesale. The
    # destination selection below only exists to feed R5, which we are not running here,
    # so it is skipped entirely — avoiding its O(origins × POIs) cost. Validate the cache
    # on departure + origins; the matrix being reused was built for a specific destination
    # set, so recover that set's signature from the cached metadata rather than
    # recomputing it, keeping downstream stages (accessibility meta) consistent.
    if cfg.skip_routing:
        _validate_routing_artifacts_for_skip(
            cfg.skip_routing,
            routing_cache,
            departure_iso,
            origins_sig=origins_sig,
        )
        build_bus_impedance_cache(ctx, force_rebuild=True, transport_type=transport_type)

        print(f"[{transport_type.capitalize()}] Skipping routing build; reusing cache: {routing_cache}", flush=True)

        try:
            destinations_sig = str(_load_routing_cache(routing_cache).get("destinations_sig", ""))
        except Exception:
            destinations_sig = ""
        return BusRoutingStageResult(
            routing_csv=routing_csv,
            routing_pkl=routing_cache,
            routing_departure_iso=departure_iso,
            origins_sig=origins_sig,
            destinations_sig=destinations_sig,
        )

    from utils import services as serv
    global_radius_m = serv.get_global_radius_m(cfg)

    # Destination-selection cache: the selected destinations are a pure function of the
    # origins, the public-transport snap candidates, and the global radius. Persist them
    # keyed by a signature of those inputs so a restart (e.g. after a crash in a later
    # stage) skips the expensive O(origins × POIs) selection + radius pre-filter. Shared
    # across bus/subway, whose selection inputs are identical.
    dest_cache_key = _pt_destinations_signature(origins, snap.poi_bus_snap_info_by_type, global_radius_m)
    destinations = _load_pt_destination_cache(cfg.pt_destination_cache_path, dest_cache_key)
    if destinations is not None:
        print(
            f"[{transport_type.capitalize()}] Loaded {len(destinations)} selected "
            "destinations from cache; skipping selection.",
            flush=True,
        )
    else:
        destinations = _select_pt_destinations(ctx, snap, cfg, origins, global_radius_m)
        _save_pt_destination_cache(cfg.pt_destination_cache_path, dest_cache_key, destinations)

    destinations_sig = _coords_signature(destinations)

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



# compatibility shim
validate_routing_artifacts_for_skip = _validate_routing_artifacts_for_skip
