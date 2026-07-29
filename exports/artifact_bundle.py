import csv
import gc
import json
import os
import shutil
import time
import zipfile
from pathlib import Path

import numpy as np
from tqdm import tqdm

from core.pipeline_types import BusRoutingStageResult, NonBusRoutingStageResult, PipelineContext

# v2: non_bus_blobs moved from one big in-memory object array (a single npz key
# holding every node's cache bytes at once) to individual per-node entries
# (non_bus_blobs/<node_id>.bin) streamed in and out of the archive one at a time.
# The old approach required holding the ENTIRE non-bus cache for every origin node
# in RAM simultaneously on both the write and the read side -- fine for a small
# city, but for a dense one (Paris: ~50MB/node, hundreds of thousands of POIs
# within radius per origin) that's hundreds of GB to low TB, far past any
# reasonable memory budget. v1 bundles are treated as a cache miss below (cheap
# and safe to recompute) rather than supporting both formats going forward.
ARTIFACT_SCHEMA_VERSION = 2


def load_impedance_bundle(
    ctx: PipelineContext,
) -> tuple[BusRoutingStageResult, NonBusRoutingStageResult] | None:
    path = ctx.config.impedance_artifact_path
    if not os.path.isfile(path):
        return None

    cfg = ctx.config

    with np.load(path, allow_pickle=True) as z_probe:
        bundle_schema_version = int(np.array(z_probe["schema_version"])) if "schema_version" in z_probe.files else 1
        if bundle_schema_version != ARTIFACT_SCHEMA_VERSION:
            print(
                f"[Artifact] Bundle schema_version={bundle_schema_version} != {ARTIFACT_SCHEMA_VERSION}; "
                "recomputing.",
                flush=True,
            )
            return None
        # If subway is enabled for this city but the cached bundle predates subway support, treat
        # it as a miss so the pipeline re-routes both modes and rewrites a subway-aware bundle.
        if cfg.enable_subway and "subway_impedance_matrix" not in z_probe.files:
            print(
                "[Artifact] Bundle has no subway data but subway is enabled; recomputing.",
                flush=True,
            )
            return None
        # The bundle's routing data is keyed to the exact origin node set it was built
        # against (baked in as origins_sig, the same signature run_public_transport_routing_stage
        # uses to detect a stale routing cache). Unlike that stage's own cache check, this
        # load path used to skip straight past it with no validation at all: it would restore
        # whatever bundle existed on disk regardless of whether the CURRENT ctx.nodes_with_coords
        # (e.g. after a boundary/study-area fix that changes which nodes are in scope) still
        # matches it, silently mixing a stale, narrower routing matrix with a freshly-rebuilt,
        # wider node set -- nodes outside the old bundle's coverage then get no transit
        # accessibility at all, with no error or warning. Reject the bundle here so a mismatch
        # falls through to a real (now partial-reuse-capable, see public_transport_routing_stage)
        # routing run instead of silently corrupting downstream accessibility.
        bundle_origins_sig = str(np.array(z_probe["origins_sig"]).item()) if "origins_sig" in z_probe.files else None
        if bundle_origins_sig is not None:
            from routing.public_transport_routing_stage import _coords_signature
            current_origins_sig = _coords_signature(
                [(data["y"], data["x"]) for _, data in ctx.nodes_with_coords]
            )
            if bundle_origins_sig != current_origins_sig:
                print(
                    "[Artifact] Bundle origins_sig doesn't match the current node set "
                    f"(bundle={bundle_origins_sig[:12]}... current={current_origins_sig[:12]}...); "
                    "recomputing.",
                    flush=True,
                )
                return None

    Path(cfg.non_bus_cache_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.bus_impedance_matrix_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.bus_source_id_to_row_path).parent.mkdir(parents=True, exist_ok=True)
    if cfg.enable_subway:
        Path(cfg.subway_impedance_matrix_path).parent.mkdir(parents=True, exist_ok=True)

    # ── Pass 1: load everything except blobs, write bus matrix, then free RAM ──
    # Loading all npz keys at once (bus matrix + blobs) can exhaust RAM for large
    # cities. We instead load scalars and the bus matrix first, flush them to disk,
    # and only then load the blob array so peak RSS = max(bus_matrix, blobs) rather
    # than bus_matrix + blobs.
    with np.load(path, allow_pickle=True) as z:
        node_ids = np.array(z["node_ids"]).astype(str)
        bus_dest_coords = np.array(z["bus_dest_coords"], dtype=np.float64)
        routing_departure_iso = str(np.array(z["routing_departure_iso"]).item())
        origins_sig = str(np.array(z["origins_sig"]).item())
        destinations_sig = str(np.array(z["destinations_sig"]).item())
        bus_matrix = np.array(z["bus_impedance_matrix"], dtype=np.float32)

    source_id_to_row = {str(node_id): idx for idx, node_id in enumerate(node_ids.tolist())}
    with open(cfg.bus_source_id_to_row_path, "w", encoding="utf-8") as f:
        json.dump(source_id_to_row, f)

    dest_id_to_col = {f"d{idx}": idx for idx in range(bus_dest_coords.shape[0])}
    with open(cfg.bus_dest_id_to_col_path, "w", encoding="utf-8") as f:
        json.dump(dest_id_to_col, f)

    with open(cfg.bus_routing_destinations_input_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "lon", "lat"])
        for idx, (lat, lon) in enumerate(bus_dest_coords.tolist()):
            writer.writerow([f"d{idx}", float(lon), float(lat)])
    del bus_dest_coords

    mat = np.memmap(
        cfg.bus_impedance_matrix_path,
        dtype=np.float32,
        mode="w+",
        shape=bus_matrix.shape,
    )
    mat[:] = bus_matrix
    mat.flush()
    nonzero_count = int(np.count_nonzero(bus_matrix))
    total_count = int(bus_matrix.size)
    print(
        f"[Artifact] Loaded impedance bundle: bus_impedance_nonzero={nonzero_count}/{total_count} "
        f"bus_impedance_zero={total_count - nonzero_count}",
        flush=True,
    )
    del mat, bus_matrix
    gc.collect()

    # ── Pass 1b: restore the optional subway matrix (kept separate to bound peak RSS) ──
    if cfg.enable_subway:
        with np.load(path, allow_pickle=True) as z:
            subway_dest_coords = np.array(z["subway_dest_coords"], dtype=np.float64)
            subway_matrix = np.array(z["subway_impedance_matrix"], dtype=np.float32)

        # Origins are the same graph nodes as bus, so the source index mirrors node_ids.
        with open(cfg.subway_source_id_to_row_path, "w", encoding="utf-8") as f:
            json.dump(source_id_to_row, f)

        subway_dest_id_to_col = {f"d{idx}": idx for idx in range(subway_dest_coords.shape[0])}
        with open(cfg.subway_dest_id_to_col_path, "w", encoding="utf-8") as f:
            json.dump(subway_dest_id_to_col, f)

        with open(cfg.subway_routing_destinations_input_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "lon", "lat"])
            for idx, (lat, lon) in enumerate(subway_dest_coords.tolist()):
                writer.writerow([f"d{idx}", float(lon), float(lat)])
        del subway_dest_coords

        sub_mem = np.memmap(
            cfg.subway_impedance_matrix_path, dtype=np.float32, mode="w+", shape=subway_matrix.shape,
        )
        sub_mem[:] = subway_matrix
        sub_mem.flush()
        sub_nonzero = int(np.count_nonzero(subway_matrix))
        print(
            f"[Artifact] Loaded subway impedance: nonzero={sub_nonzero}/{subway_matrix.size} "
            f"zero={subway_matrix.size - sub_nonzero}",
            flush=True,
        )
        del sub_mem, subway_matrix
        gc.collect()

    # ── Pass 2: stream each node's blob straight from the archive to its cache file ──
    # Each node lives in its own zip entry (non_bus_blobs/<node_id>.bin), read via
    # ZipFile.open() -- a streaming, chunked file-like object -- instead of the old
    # single "non_bus_blobs" array key that required every node's bytes to be
    # decompressed and held in RAM at once before any of them could be written out.
    # Peak memory here is bounded by one copy buffer, not by total cache size.
    cache_paths: dict[str, str] = {}
    schema_version_str = str(ctx.config.non_bus_cache_schema_version)
    node_ids_list = node_ids.tolist()
    with zipfile.ZipFile(path) as zf:
        for node_id in tqdm(node_ids_list, desc="[Artifact] Restoring non-bus caches", unit="node"):
            out_path = os.path.join(cfg.non_bus_cache_dir, f"{node_id}.pkl")
            arcname = f"non_bus_blobs/{node_id}.bin"
            with zf.open(arcname) as src, open(out_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            # Write sidecar so _has_valid_non_bus_cache skips full unpickling.
            try:
                with open(out_path + ".v", "w") as fv:
                    fv.write(schema_version_str)
            except OSError:
                pass
            cache_paths[str(node_id)] = out_path
    gc.collect()

    bus = BusRoutingStageResult(
        routing_csv=cfg.bus_routing_matrix_path,
        routing_pkl=cfg.bus_routing_cache_path,
        routing_departure_iso=routing_departure_iso,
        origins_sig=origins_sig,
        destinations_sig=destinations_sig,
    )
    non_bus = NonBusRoutingStageResult(
        cache_paths=cache_paths,
        cached_nodes=len(cache_paths),
        computed_nodes=0,
    )
    return bus, non_bus


def write_impedance_bundle(
    ctx: PipelineContext,
    bus: BusRoutingStageResult,
    non_bus: NonBusRoutingStageResult,
) -> None:
    cfg = ctx.config
    node_ids = np.array([str(node_id) for node_id, _ in ctx.nodes_with_coords], dtype=object)

    with open(cfg.bus_dest_id_to_col_path, encoding="utf-8") as f:
        dest_id_to_col_raw = json.load(f)
    dest_id_to_col = {str(k): int(v) for k, v in dest_id_to_col_raw.items()}
    n_dest = len(dest_id_to_col)
    dest_coords = np.zeros((n_dest, 2), dtype=np.float64)
    with open(cfg.bus_routing_destinations_input_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            idx = dest_id_to_col.get(str(row["id"]))
            if idx is None:
                continue
            dest_coords[idx, 0] = float(row["lat"])
            dest_coords[idx, 1] = float(row["lon"])

    with open(cfg.bus_source_id_to_row_path, encoding="utf-8") as f:
        source_id_to_row_raw = json.load(f)
    source_id_to_row = {str(k): int(v) for k, v in source_id_to_row_raw.items()}
    n_rows = len(source_id_to_row)

    matrix_mem = np.memmap(
        cfg.bus_impedance_matrix_path,
        dtype=np.float32,
        mode="r",
        shape=(n_rows, n_dest),
    )
    matrix = np.array(matrix_mem, dtype=np.float32)
    nonzero_count = int(np.count_nonzero(matrix))
    total_count = int(matrix.size)
    print(
        f"[Artifact] Restored bus impedance matrix: nonzero={nonzero_count}/{total_count} "
        f"zero={total_count - nonzero_count}",
        flush=True,
    )

    # Only resolve+validate each node's cache path here (cheap: path strings, not
    # file contents). The actual bytes are streamed straight from these files into
    # the archive below, one node at a time, so peak memory never holds more than
    # one node's blob regardless of how many GB the full non-bus cache adds up to.
    node_cache_paths: list[str] = []
    for node_id in node_ids.tolist():
        cache_path = non_bus.cache_paths.get(node_id)
        if cache_path is None:
            try:
                cache_path = non_bus.cache_paths.get(int(node_id))
            except Exception:
                cache_path = None
        if cache_path is None or not os.path.exists(cache_path):
            raise RuntimeError(f"Missing non-bus cache for node_id={node_id}")
        node_cache_paths.append(cache_path)

    save_kwargs = dict(
        schema_version=np.array(ARTIFACT_SCHEMA_VERSION, dtype=np.int32),
        node_ids=node_ids,
        bus_impedance_matrix=matrix,
        bus_dest_coords=dest_coords,
        routing_departure_iso=np.array(bus.routing_departure_iso, dtype=object),
        origins_sig=np.array(bus.origins_sig, dtype=object),
        destinations_sig=np.array(bus.destinations_sig, dtype=object),
    )

    # Optional subway modality: stored additively (no schema bump) so existing bundles
    # without these keys keep loading unchanged.
    if cfg.enable_subway:
        subway_matrix, subway_dest_coords = _read_mode_matrix(
            cfg.subway_impedance_matrix_path,
            cfg.subway_source_id_to_row_path,
            cfg.subway_dest_id_to_col_path,
            cfg.subway_routing_destinations_input_path,
        )
        save_kwargs["subway_impedance_matrix"] = subway_matrix
        save_kwargs["subway_dest_coords"] = subway_dest_coords
        sub_nonzero = int(np.count_nonzero(subway_matrix))
        print(
            f"[Artifact] Restored subway impedance matrix: nonzero={sub_nonzero}/{subway_matrix.size} "
            f"zero={subway_matrix.size - sub_nonzero}",
            flush=True,
        )

    Path(cfg.impedance_artifact_path).parent.mkdir(parents=True, exist_ok=True)
    # Plain savez, not savez_compressed: compression builds each array's compressed
    # representation fully in memory before writing it into the zip -- fine for the
    # small arrays here, but exactly the kind of extra full-size copy that must be
    # avoided for non_bus_blobs below. Uncompressed writes closer to bytes-in ==
    # bytes-out, trading disk space for memory headroom.
    _write_start = time.monotonic()
    print(f"[Artifact] Writing impedance bundle to {cfg.impedance_artifact_path} ...", flush=True)
    np.savez(cfg.impedance_artifact_path, **save_kwargs)

    # Stream each node's non-bus cache file directly into the archive as its own zip
    # entry (non_bus_blobs/<node_id>.bin) instead of building one big in-memory
    # object array first: ZipFile.write() reads+writes the source file in chunks, so
    # peak memory here is one copy buffer, not the total non-bus cache size (which,
    # for a dense city, can be hundreds of GB to low TB -- see ARTIFACT_SCHEMA_VERSION
    # comment above). A single archive file is preserved so the bundle stays a
    # portable, compute-once artifact you can copy to another machine.
    with zipfile.ZipFile(cfg.impedance_artifact_path, mode="a", allowZip64=True) as zf:
        for node_id, cache_path in zip(
            tqdm(node_ids.tolist(), desc="[Artifact] Writing non-bus caches", unit="node"),
            node_cache_paths,
        ):
            zf.write(cache_path, arcname=f"non_bus_blobs/{node_id}.bin")

    _write_elapsed = time.monotonic() - _write_start
    _artifact_size_gb = os.path.getsize(cfg.impedance_artifact_path) / (1 << 30)
    print(
        f"[Artifact] Wrote impedance bundle: {_artifact_size_gb:.2f} GB in {_write_elapsed:.1f}s.",
        flush=True,
    )


def _read_mode_matrix(matrix_path, source_id_to_row_path, dest_id_to_col_path, dest_csv_path):
    """Load a public-transport impedance matrix + its destination coords from disk.

    Returns (matrix: float32 [n_rows, n_dest], dest_coords: float64 [n_dest, 2] (lat, lon)).
    """
    with open(dest_id_to_col_path, encoding="utf-8") as f:
        dest_id_to_col = {str(k): int(v) for k, v in json.load(f).items()}
    n_dest = len(dest_id_to_col)
    dest_coords = np.zeros((n_dest, 2), dtype=np.float64)
    with open(dest_csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            idx = dest_id_to_col.get(str(row["id"]))
            if idx is None:
                continue
            dest_coords[idx, 0] = float(row["lat"])
            dest_coords[idx, 1] = float(row["lon"])

    with open(source_id_to_row_path, encoding="utf-8") as f:
        n_rows = len(json.load(f))

    matrix_mem = np.memmap(matrix_path, dtype=np.float32, mode="r", shape=(n_rows, n_dest))
    return np.array(matrix_mem, dtype=np.float32), dest_coords
