import csv
import hashlib
import os
import pickle
import signal
import shutil
import subprocess
from pathlib import Path

import osmnx as ox
import pandas as pd
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
    
def _validate_bus_matrix_meta(path: str, departure_iso: str, origins_sig: str, destinations_sig: str) -> None:
    """Validate bus matrix metadata file against expected run signatures.

    Inputs:
    - path: metadata JSON path.
    - departure_iso: expected departure datetime string.
    - origins_sig: expected origin signature.
    - destinations_sig: expected destination signature.

    Outputs:
    - None. Raises RuntimeError when metadata is missing or incompatible.
    """
    if not os.path.isfile(path):
        raise RuntimeError(f"Missing bus matrix metadata while skip_routing=True. Expected file: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception as exc:
        raise RuntimeError(f"Failed to load bus matrix metadata: {path}") from exc

    if (
        meta.get("departure_iso") != departure_iso
        or meta.get("origins_sig") != origins_sig
        or meta.get("destinations_sig") != destinations_sig
    ):
        raise RuntimeError(
            "Bus matrix metadata is invalid or incompatible while skip_routing=True. "
            f"Meta file: {path}"
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


def _run_r5r_script(script_path: str, ctx: PipelineContext) -> None:
    """Run the external R routing script and stream its logs.

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
        for line in proc.stdout:
            print(line, end="", flush=True)
        return_code = proc.wait()
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, [rscript_exe, script_path])
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

def build_bus_impedance_cache(context: PipelineContext, force_rebuild: bool = False) -> None:
    """Build dense bus impedance matrix and index files from routing CSV.

    Inputs:
    - context: pipeline context with bus artifact paths.
    - force_rebuild: when True, rebuild matrix/indexes even if files exist.

    Outputs:
    - None. Writes matrix `.dat` and row/column index JSON files.
    """
    cfg = context.config
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
            total_time = row["total_time"]
            if total_time == "" or (row["routes"] in ["", "[WALK]"]):
                total_time = 0
            impedance_matrix[source_row][dest_col] = total_time
    impedance_matrix.flush()

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


def _write_bus_matrix_meta(path, departure_iso, origins_sig, destinations_sig):
    """Persist metadata used to validate matrix freshness across runs.

    Inputs:
    - path: metadata JSON destination.
    - departure_iso: run departure datetime string.
    - origins_sig: origin signature.
    - destinations_sig: destination signature.

    Outputs:
    - None. Writes metadata JSON to disk.
    """
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "departure_iso": departure_iso,
                "origins_sig": origins_sig,
                "destinations_sig": destinations_sig,
            },
            f,
        )


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


def _build_routing_cache_from_r5r_csv(
    csv_path: str,
    origin_id_to_coord: dict[str, tuple[float, float]],
    destination_id_to_coord: dict[str, tuple[float, float]],
    departure_iso: str,
    origins_sig: str,
    destinations_sig: str,
):
    """Build in-memory routing cache dictionary from expanded routing CSV.

    Inputs:
    - csv_path: expanded routing CSV path produced by R.
    - origin_id_to_coord: mapping from origin ids to rounded coordinates.
    - destination_id_to_coord: mapping from destination ids to rounded coordinates.
    - departure_iso: departure datetime string.
    - origins_sig: origin signature.
    - destinations_sig: destination signature.

    Outputs:
    - dict: routing cache payload with metadata and `(origin,destination)` route map.
    """
    routes = {}
    usecols = ["from_id", "to_id", "total_time", "wait_time", "routes"]

    reader = pd.read_csv(
        csv_path,
        usecols=lambda c: c in usecols,
        chunksize=200_000,
        engine="python",   # avoids C parser native crash path
    )

    for chunk in reader:
        for row in chunk.itertuples(index=False):
            from_id = str(row.from_id)
            to_id = str(row.to_id)

            origin_coord = origin_id_to_coord.get(from_id)
            destination_coord = destination_id_to_coord.get(to_id)
            if origin_coord is None or destination_coord is None:
                continue

            total_time = row.total_time
            wait_time = row.wait_time
            if pd.isna(total_time) or pd.isna(wait_time):
                continue

            total_time = float(total_time)
            wait_time = float(wait_time)
            travel_time = total_time - wait_time
            if travel_time < 0:
                continue

            routes[(origin_coord, destination_coord)] = {
                "travel_time": travel_time,
                "wait_time": wait_time,
                "impedance": total_time,
                "routes": getattr(row, "routes", None),
            }

    return {
        "departure_iso": departure_iso,
        "origins_sig": origins_sig,
        "destinations_sig": destinations_sig,
        "routes": routes,
    }


# This function runs the bus routing stage
def run_bus_routing_stage(ctx: PipelineContext, snap: SnappingStageResult) -> BusRoutingStageResult:
    """Execute transit routing stage and produce reusable bus routing artifacts.

    Inputs:
    - ctx: pipeline context with configuration and graph nodes.
    - snap: snapping results with bus destination candidates.

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
        for candidates in snap_info_for_key.values():
            if len(candidates) > 1:
                has_multi_snap_candidates = True
            for snapped, _ in candidates:
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
        build_bus_impedance_cache(ctx, force_rebuild=True)

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
        build_bus_impedance_cache(ctx, force_rebuild=False)

        print(f"Skipping routing build. Reusing cache: {routing_cache}")

        return BusRoutingStageResult(
            routing_csv=routing_csv,
            routing_pkl=routing_cache,
            routing_departure_iso=departure_iso,
            origins_sig=origins_sig,
            destinations_sig=destinations_sig,
        )

    
    origin_id_to_coord = {
        str(node_id): (round(float(data["y"]), COORD_ROUND), round(float(data["x"]), COORD_ROUND))
        for node_id, data in ctx.nodes_with_coords
    }
    destination_id_to_coord = {
        f"d{idx}": (round(float(lat), COORD_ROUND), round(float(lon), COORD_ROUND))
        for idx, (lat, lon) in enumerate(destinations)
    }

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

    build_bus_impedance_cache(ctx, force_rebuild=True)

    routing_payload = _build_routing_cache_from_r5r_csv(
        routing_csv,
        origin_id_to_coord=origin_id_to_coord,
        destination_id_to_coord=destination_id_to_coord,
        departure_iso=departure_iso,
        origins_sig=origins_sig,
        destinations_sig=destinations_sig,
    )
    _save_routing_cache(routing_cache, routing_payload)

    return BusRoutingStageResult(
        routing_csv=routing_csv,
        routing_pkl=routing_cache,
        routing_departure_iso=departure_iso,
        origins_sig=origins_sig,
        destinations_sig=destinations_sig,
    )



# compatibility shim
validate_routing_artifacts_for_skip = _validate_routing_artifacts_for_skip
