import csv
import gc
import json
import os
import pickle
import shutil
import struct
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from numpy.lib import format as npy_format
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
ARTIFACT_SCHEMA_VERSION = 3


def _restore_matrix_fast(zip_path: str, arcname: str, dest_path: str):
    """Copy a ZIP_STORED (uncompressed) float32 .npy member's raw bytes straight to
    `dest_path` as a flat binary file, without ever materializing the array in RAM.

    np.load()/NpzFile.__getitem__ always decompresses a member fully into a new
    numpy array before you can even read its .shape -- for a 24.7 GB Paris matrix
    that's a full-size heap allocation just to then copy it again into the memmap
    file. np.savez writes members uncompressed (ZIP_STORED) and C-contiguous, so
    the raw bytes on disk already ARE the memmap's target layout; we only need to
    locate where they start (past the zip local-file-header and the .npy header)
    and stream them straight across in fixed-size chunks.

    Returns the array's shape on success, or None if the fast path doesn't apply
    (compressed member, non-C-contiguous, unexpected dtype, or any parsing
    surprise), so the caller can fall back to the safe np.load path.
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zinfo = zf.getinfo(arcname)
            if zinfo.compress_type != zipfile.ZIP_STORED:
                return None
            with open(zip_path, "rb") as f:
                f.seek(zinfo.header_offset)
                local_header = f.read(30)
                if local_header[:4] != b"PK\x03\x04":
                    return None
                fname_len, extra_len = struct.unpack("<HH", local_header[26:30])
                f.seek(zinfo.header_offset + 30 + fname_len + extra_len)
                major, _minor = npy_format.read_magic(f)
                if major == 1:
                    shape, fortran_order, dtype = npy_format.read_array_header_1_0(f)
                else:
                    shape, fortran_order, dtype = npy_format.read_array_header_2_0(f)
                if fortran_order or dtype != np.dtype(np.float32):
                    return None
                data_offset = f.tell()
                nbytes = int(np.prod(shape)) * dtype.itemsize
                f.seek(data_offset)
                with open(dest_path, "wb") as fdst:
                    remaining = nbytes
                    chunk_size = 1 << 26  # 64 MB
                    while remaining > 0:
                        buf = f.read(min(chunk_size, remaining))
                        if not buf:
                            raise IOError("unexpected EOF while copying matrix bytes")
                        fdst.write(buf)
                        remaining -= len(buf)
        return shape
    except Exception as exc:
        print(f"[Artifact] Fast matrix restore for {arcname} failed ({exc}); falling back.", flush=True)
        return None


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
    print(f"[Artifact] Restoring bus matrix from {path} ...", flush=True)
    _t_pass1 = time.monotonic()
    with np.load(path, allow_pickle=True) as z:
        node_ids = np.array(z["node_ids"]).astype(str)
        bus_dest_coords = np.asarray(z["bus_dest_coords"], dtype=np.float64)
        routing_departure_iso = str(np.array(z["routing_departure_iso"]).item())
        origins_sig = str(np.array(z["origins_sig"]).item())
        destinations_sig = str(np.array(z["destinations_sig"]).item())
        catalog_types = np.array(z["poi_catalog_types"]).astype(str).tolist()
        catalog_ptr = np.asarray(z["poi_catalog_ptr"], dtype=np.int64)
        catalog_src_keys = np.asarray(z["poi_catalog_src_keys"])
        catalog_source_coords = np.asarray(z["poi_catalog_source_coords"], dtype=np.float64)

    # Reconstitute the shared per-poi_type catalog to the same path the live pipeline
    # writes it to, so the accessibility stage has one place to read it from either way.
    poi_catalog = {
        poi_type: {
            "src_keys": catalog_src_keys[catalog_ptr[i]:catalog_ptr[i + 1]],
            "source_coords": catalog_source_coords[catalog_ptr[i]:catalog_ptr[i + 1]],
        }
        for i, poi_type in enumerate(catalog_types)
    }
    Path(cfg.non_bus_poi_catalog_path).parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.non_bus_poi_catalog_path, "wb") as f:
        pickle.dump(poi_catalog, f, protocol=pickle.HIGHEST_PROTOCOL)
    del catalog_src_keys, catalog_source_coords, poi_catalog

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

    bus_shape = _restore_matrix_fast(path, "bus_impedance_matrix.npy", cfg.bus_impedance_matrix_path)
    if bus_shape is None:
        # Fallback: compressed or non-C-contiguous member -- safe but RAM-heavier.
        # asarray (not array): the stored dtype already matches, so this avoids a second
        # full-size copy of a matrix that can be tens of GB (Paris: ~24.7 GB) -- np.array()
        # always copies even when the dtype is already correct, which was doubling peak RSS
        # at exactly this line and pushing it past the run's memory cgroup cap (2026-08-03).
        with np.load(path, allow_pickle=True) as z:
            bus_matrix = np.asarray(z["bus_impedance_matrix"], dtype=np.float32)
        mat = np.memmap(
            cfg.bus_impedance_matrix_path,
            dtype=np.float32,
            mode="w+",
            shape=bus_matrix.shape,
        )
        mat[:] = bus_matrix
        mat.flush()
        bus_shape = bus_matrix.shape
        del mat, bus_matrix
        gc.collect()

    mat_ro = np.memmap(cfg.bus_impedance_matrix_path, dtype=np.float32, mode="r", shape=bus_shape)
    nonzero_count = int(np.count_nonzero(mat_ro))
    total_count = int(mat_ro.size)
    print(
        f"[Artifact] Loaded impedance bundle: bus_impedance_nonzero={nonzero_count}/{total_count} "
        f"bus_impedance_zero={total_count - nonzero_count} "
        f"({time.monotonic() - _t_pass1:.1f}s)",
        flush=True,
    )
    del mat_ro
    gc.collect()

    # ── Pass 1b: restore the optional subway matrix (kept separate to bound peak RSS) ──
    if cfg.enable_subway:
        with np.load(path, allow_pickle=True) as z:
            subway_dest_coords = np.asarray(z["subway_dest_coords"], dtype=np.float64)

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

        subway_shape = _restore_matrix_fast(
            path, "subway_impedance_matrix.npy", cfg.subway_impedance_matrix_path
        )
        if subway_shape is None:
            # Fallback: same reasoning as the bus matrix above -- asarray avoids a
            # redundant full copy, but a full decompress-into-RAM is still unavoidable
            # here since the fast byte-range path didn't apply.
            with np.load(path, allow_pickle=True) as z:
                subway_matrix = np.asarray(z["subway_impedance_matrix"], dtype=np.float32)
            sub_mem = np.memmap(
                cfg.subway_impedance_matrix_path, dtype=np.float32, mode="w+", shape=subway_matrix.shape,
            )
            sub_mem[:] = subway_matrix
            sub_mem.flush()
            subway_shape = subway_matrix.shape
            del sub_mem, subway_matrix
            gc.collect()

        sub_mem_ro = np.memmap(cfg.subway_impedance_matrix_path, dtype=np.float32, mode="r", shape=subway_shape)
        sub_nonzero = int(np.count_nonzero(sub_mem_ro))
        print(
            f"[Artifact] Loaded subway impedance: nonzero={sub_nonzero}/{sub_mem_ro.size} "
            f"zero={sub_mem_ro.size - sub_nonzero}",
            flush=True,
        )
        del sub_mem_ro
        gc.collect()

    # ── Pass 2: stream each node's blob straight from the archive to its cache file ──
    # Each node lives in its own zip entry (non_bus_blobs/<node_id>.bin) -- these
    # ~23k entries are fully independent I/O units (this is where 90%+ of a dense
    # city's bundle bytes live, e.g. Paris: ~704 GB of ~751 GB total), so restoring
    # them one at a time on a single thread badly under-uses NVMe queue depth. Each
    # worker thread opens its own ZipFile handle onto `path` rather than sharing one:
    # zipfile.ZipFile serializes reads through its shared file object's internal
    # lock, which would otherwise flatten this back down to effectively one reader.
    # Peak memory stays bounded by (workers x one copy buffer), not by total cache size.
    cache_paths: dict[str, str] = {}
    schema_version_str = str(ctx.config.non_bus_cache_schema_version)
    node_ids_list = node_ids.tolist()

    _thread_local = threading.local()

    def _restore_one(node_id: str) -> tuple[str, str]:
        zf = getattr(_thread_local, "zf", None)
        if zf is None:
            zf = zipfile.ZipFile(path)
            _thread_local.zf = zf
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
        return str(node_id), out_path

    max_workers = min(32, (os.cpu_count() or 4) * 4)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_restore_one, node_id): node_id for node_id in node_ids_list}
        for future in tqdm(
            as_completed(futures), total=len(futures), desc="[Artifact] Restoring non-bus caches", unit="node"
        ):
            node_id, out_path = future.result()
            cache_paths[node_id] = out_path
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

    # Keep the matrix as a read-only memmap rather than np.array()-copying it into RAM.
    # For a dense city each matrix is ~n_rows*n_dest*4 bytes (Paris bus ~24.7 GB), and the
    # bundle holds the bus AND subway matrix at once -- two full anon copies (~49 GB) blew
    # past the 45 GB cgroup cap here (2026-07-31). count_nonzero and np.savez both stream
    # the memmap in chunks, so its pages stay clean, file-backed and reclaimable (verified:
    # savez adds no anon copy) instead of un-reclaimable anon.
    matrix = np.memmap(
        cfg.bus_impedance_matrix_path,
        dtype=np.float32,
        mode="r",
        shape=(n_rows, n_dest),
    )
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

    # Flatten the per-poi_type catalog (origin-invariant POI identity) into one CSR-style
    # block, same pattern as build_snap_compact's cand_ptr/cand_coords -- written once
    # here instead of duplicated into every node's blob.
    catalog_types = sorted(non_bus.poi_catalog.keys())
    catalog_ptr = [0]
    catalog_src_keys_parts = []
    catalog_source_coords_parts = []
    for poi_type in catalog_types:
        bundle = non_bus.poi_catalog[poi_type]
        catalog_src_keys_parts.append(np.asarray(bundle["src_keys"]))
        catalog_source_coords_parts.append(
            np.asarray(bundle["source_coords"], dtype=np.float64).reshape(-1, 2)
        )
        catalog_ptr.append(catalog_ptr[-1] + len(bundle["src_keys"]))

    save_kwargs = dict(
        schema_version=np.array(ARTIFACT_SCHEMA_VERSION, dtype=np.int32),
        node_ids=node_ids,
        bus_impedance_matrix=matrix,
        bus_dest_coords=dest_coords,
        routing_departure_iso=np.array(bus.routing_departure_iso, dtype=object),
        origins_sig=np.array(bus.origins_sig, dtype=object),
        destinations_sig=np.array(bus.destinations_sig, dtype=object),
        poi_catalog_types=np.array(catalog_types, dtype=object),
        poi_catalog_ptr=np.array(catalog_ptr, dtype=np.int64),
        poi_catalog_src_keys=(
            np.concatenate(catalog_src_keys_parts) if catalog_src_keys_parts else np.asarray([], dtype="S1")
        ),
        poi_catalog_source_coords=(
            np.concatenate(catalog_source_coords_parts, axis=0)
            if catalog_source_coords_parts
            else np.empty((0, 2), dtype=np.float64)
        ),
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

    # Same reasoning as the bus matrix above: keep this a memmap, not an anon copy.
    matrix = np.memmap(matrix_path, dtype=np.float32, mode="r", shape=(n_rows, n_dest))
    return matrix, dest_coords
