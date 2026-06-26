import csv
import glob
import hashlib
import os
import pickle
import signal
import shutil
import subprocess
from pathlib import Path

import osmnx as ox
import numpy as np
from pipeline_types import BusRoutingStageResult, PipelineContext, SnappingStageResult
from snapping_stage import build_selected_routing_destinations
import json

COORD_ROUND = 6


def _prepare_r5r_data_bundle(cfg) -> str:
    """Ensure routing data folder contains one OSM PBF and one or more GTFS zip files."""
    data_dir = cfg.routing_data_dir
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    pbf_path = os.path.join(data_dir, f"{cfg.city_slug}.osm.pbf")
    if not os.path.isfile(pbf_path):
        if cfg.osm_pbf_autobuild:
            print(
                "[Bus] OSM PBF missing. Building local extract from city_name via OSMnx...",
                flush=True,
            )
            _autobuild_pbf_from_place(cfg, pbf_path)
            print(f"[Bus] OSM PBF auto-built: {pbf_path}", flush=True)
        else:
            raise RuntimeError(
                "Missing OSM PBF for routing. "
                f"Expected: {pbf_path}. Enable config.osm_pbf_autobuild."
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

    return data_dir


def _autobuild_pbf_from_place(cfg, pbf_path: str) -> None:
    """Build a place-scoped OSM extract and convert it to PBF for r5r."""
    xml_path = os.path.join(cfg.routing_data_dir, f"{cfg.city_slug}.osm.xml")

    # Build unsimplified graph so OSM export keeps a closer representation of ways.
    graph = ox.graph_from_place(
        cfg.city_name,
        network_type=cfg.osm_autobuild_network_type,
        simplify=cfg.osm_autobuild_simplify,
        retain_all=cfg.osm_autobuild_retain_all,
    )
    ox.io.save_graph_xml(graph, filepath=xml_path)

    osmium_exe = shutil.which("osmium")
    if osmium_exe:
        subprocess.run(
            [osmium_exe, "cat", xml_path, "-o", pbf_path, "--overwrite"],
            check=True,
        )
        return

    osmconvert_exe = shutil.which("osmconvert")
    if osmconvert_exe:
        subprocess.run(
            [osmconvert_exe, xml_path, f"-o={pbf_path}"],
            check=True,
        )
        return

    # Fallback: use Python osmium bindings (pyosmium) when CLI tools are unavailable.
    try:
        import osmium
    except Exception:
        osmium = None

    if osmium is not None:
        class _CopyToPbf(osmium.SimpleHandler):
            def __init__(self, writer):
                super().__init__()
                self._writer = writer

            def node(self, n):
                self._writer.add_node(n)

            def way(self, w):
                self._writer.add_way(w)

            def relation(self, r):
                self._writer.add_relation(r)

        writer = osmium.SimpleWriter(pbf_path)
        try:
            handler = _CopyToPbf(writer)
            handler.apply_file(xml_path, locations=False)
        finally:
            writer.close()
        return

    raise RuntimeError(
        "Could not convert auto-generated OSM XML to PBF. "
        "None of these conversion backends is available: "
        "'osmium' CLI, 'osmconvert' CLI, Python package 'osmium'. "
        "Install one of them, for example: `python -m pip install osmium`. "
        f"Generated XML at: {xml_path}"
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


def _run_r5r_script(script_path: str, ctx: PipelineContext) -> None:
    """Run the external R routing script and keep shell output concise.

    Inputs:
    - script_path: path to the R script entrypoint.

    Outputs:
    - None. Raises if Rscript fails or is not available.
    """
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

    env["R5_DATA_PATH"] = r5_data_path
    env["R5_ORIGINS_PATH"] = cfg.bus_routing_origins_input_path
    env["R5_DEST_PATH"] = cfg.bus_routing_destinations_input_path
    env["R5_OUTPUT_PATH"] = cfg.bus_routing_matrix_path
    env["R5_CHUNK_DIR"] = os.path.join(os.path.dirname(cfg.bus_routing_matrix_path), "r5r_chunks")
    env["R5_DEPARTURE_DATETIME"] = cfg.bus_departure_dt.strftime("%Y-%m-%d %H:%M:%S")

    if os.name == "nt":
        proc = subprocess.Popen(
            [rscript_exe, script_path],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        proc = subprocess.Popen(
            [rscript_exe, script_path],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    try:
        assert proc.stdout is not None
        captured_lines: list[str] = []
        for line in proc.stdout:
            captured_lines.append(line.rstrip("\n"))
            if len(captured_lines) > 50:
                captured_lines.pop(0)
        return_code = proc.wait()
        if return_code != 0:
            tail = "\n".join(captured_lines[-20:])
            raise RuntimeError(
                f"Rscript failed with exit code {return_code}. Last output lines:\n{tail}"
            )
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
    ticket_price = {"bus": cfg.bus_ticket_price, "metro": cfg.metro_ticket_price, "train": cfg.train_ticket_price}.get(transport_type, cfg.bus_ticket_price)
    gamma = (cfg.time_indifference_bus - cfg.vot * ticket_price) / cfg.time_indifference_bus
    source_id_to_row = {}
    dest_id_to_col = {}

    matrix_path = Path(cfg.bus_impedance_matrix_path)
    Path(matrix_path).parent.mkdir(parents=True, exist_ok=True)
    source_index_path = Path(cfg.bus_source_id_to_row_path)
    dest_index_path = Path(cfg.bus_dest_id_to_col_path)

    if (
        not force_rebuild
        and matrix_path.is_file()
        and matrix_path.stat().st_size > 0
        and source_index_path.is_file()
        and dest_index_path.is_file()
    ):
        return

    # Read the source csv and create correspondance between id and row in the csv
    with open(cfg.bus_routing_origins_input_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row_idx, row in enumerate(reader):
            source_id = row["id"]
            source_id_to_row[source_id] = row_idx

    # Read the destination csv and create correspondance between id and row in the csv
    with open(cfg.bus_routing_destinations_input_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for col_idx, row in enumerate(reader):
            dest_id = row["id"]
            dest_id_to_col[dest_id] = col_idx

        
    n_rows = len(source_id_to_row)
    n_cols = len(dest_id_to_col)
    impedance_matrix = np.memmap(
        cfg.bus_impedance_matrix_path,
        dtype=np.float32,
        mode="w+",
        shape=(n_rows, n_cols),
    )
    impedance_matrix[:] = 0

    with open(cfg.bus_routing_matrix_path, newline="", encoding="utf-8") as f:
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
        f"[Bus] Built bus impedance matrix: nonzero={nonzero_count}/{total_count} "
        f"zero={total_count - nonzero_count}",
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

    with open(cfg.bus_source_id_to_row_path, 'w') as f:
        json.dump(source_id_to_row, f)
    
    with open(cfg.bus_dest_id_to_col_path, 'w') as f:
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
    departure_iso = cfg.bus_departure_dt.isoformat()
    routing_csv = cfg.bus_routing_matrix_path
    routing_cache = cfg.bus_routing_cache_path
    
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

    origins = [(data["y"], data["x"]) for _, data in ctx.nodes_with_coords]

    from utils import services as serv
    global_radius_m = serv.get_global_radius_m(cfg)
    if global_radius_m is not None and unique_snapped_coords:
        from utils.delta_g import _haversine_m
        filtered_coords = [
            dest for dest in unique_snapped_coords
            if any(_haversine_m(o[0], o[1], dest[0], dest[1]) <= global_radius_m for o in origins)
        ]
        print(
            f"[Bus] Destination radius pre-filter: radius={global_radius_m/1000:.1f} km  "
            f"kept={len(filtered_coords)}/{len(unique_snapped_coords)}",
            flush=True,
        )
        unique_snapped_coords = filtered_coords

    destinations = list(unique_snapped_coords)
    origins_sig = _coords_signature(origins)
    destinations_sig = _coords_signature(destinations)

    # No snapped destinations for this city: skip R routing and emit empty-safe artifacts.
    if not destinations:
        print("[Bus] No snapped destinations found. Writing empty bus artifacts and skipping R routing.", flush=True)

        _write_r5r_point_inputs(
            ctx.nodes_with_coords,
            [],
            cfg.bus_routing_origins_input_path,
            cfg.bus_routing_destinations_input_path,
        )
        _write_dummy_destination_csv(cfg.bus_routing_destinations_input_path)
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


    if cfg.skip_routing:
        _validate_routing_artifacts_for_skip(
            cfg.skip_routing,
            routing_cache,
            departure_iso,
            origins_sig=origins_sig,
            destinations_sig=destinations_sig,
        )
        build_bus_impedance_cache(ctx, force_rebuild=True, transport_type=transport_type)

        print(f"[Bus] Skipping routing build; reusing cache: {routing_cache}", flush=True)

        return BusRoutingStageResult(
            routing_csv=routing_csv,
            routing_pkl=routing_cache,
            routing_departure_iso=departure_iso,
            origins_sig=origins_sig,
            destinations_sig=destinations_sig,
        )

    
    r_origins_csv = cfg.bus_routing_origins_input_path
    r_dest_csv = cfg.bus_routing_destinations_input_path

    _write_r5r_point_inputs(
        ctx.nodes_with_coords,
        destinations,
        r_origins_csv,
        r_dest_csv,
    )

    r_script_path = os.path.join("utils", "r5_routing.r")
    chunk_dir = os.path.join(os.path.dirname(cfg.bus_routing_matrix_path), "r5r_chunks")
    if os.path.isdir(chunk_dir):
        shutil.rmtree(chunk_dir)
    print("[Bus] Launching Rscript...", flush=True)
    _run_r5r_script(r_script_path, ctx)
    print("[Bus] Rscript completed. Building routing cache from CSV...", flush=True)

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
