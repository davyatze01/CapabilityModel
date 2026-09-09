import csv
import gc
import json
import lzma
import os
import pickle
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
from utils import services as serv

# v2: non_bus_blobs moved from one big in-memory object array (a single npz key
# holding every node's cache bytes at once) to individual per-node entries
# (non_bus_blobs/<node_id>.bin) streamed in and out of the archive one at a time.
# The old approach required holding the ENTIRE non-bus cache for every origin node
# in RAM simultaneously on both the write and the read side -- fine for a small
# city, but for a dense one (Paris: ~50MB/node, hundreds of thousands of POIs
# within radius per origin) that's hundreds of GB to low TB, far past any
# reasonable memory budget. v1 bundles are treated as a cache miss below (cheap
# and safe to recompute) rather than supporting both formats going forward.
ARTIFACT_SCHEMA_VERSION = 4

# Minimum fraction of the current run's origin nodes that must exist in the bundle for it
# to be accepted rather than triggering a full recompute (see load_impedance_bundle). Real
# OSM edits between builds routinely drop/add a handful of nodes; this tolerates that while
# still rejecting a genuinely wrong or unrelated bundle (near-zero overlap).
_MIN_ORIGIN_COVERAGE = 0.90


def _shuffle_bytes(arr: np.ndarray) -> bytes:
    """Group a fixed-width array's byte planes together (all byte 0s, then all byte 1s, ...).

    Impedances are float32 minutes spanning a narrow range, so their exponent bytes are
    nearly constant while their mantissa bytes are noise. Interleaved, every 4th byte is
    compressible and the compressor gets nothing; planed, the exponent bytes form long runs.
    Measured on real blobs: 1.94x with lzma vs 1.48x on the raw interleaved bytes. Exactly
    reversible -- this is a permutation, not a quantization.
    """
    v = arr.view(np.uint8).reshape(-1, arr.itemsize)
    return v.T.copy().tobytes()


def _unshuffle_bytes(buf: bytes, dtype, n: int) -> np.ndarray:
    itemsize = np.dtype(dtype).itemsize
    v = np.frombuffer(buf, dtype=np.uint8).reshape(itemsize, n)
    return np.ascontiguousarray(v.T).view(dtype).reshape(n)


def _pack_kept_idx(kept_idx: np.ndarray, catalog_size: int) -> bytes:
    """kept_idx (sorted int32 positions) -> one bit per catalog entry.

    Coverage runs ~47% of the catalog and the indices are monotonic, so a presence bitmask
    is ~15x smaller than the int32 list it replaces.
    """
    mask = np.zeros(catalog_size, dtype=bool)
    mask[kept_idx] = True
    return np.packbits(mask).tobytes()


def _unpack_kept_idx(buf: bytes, catalog_size: int) -> np.ndarray:
    mask = np.unpackbits(np.frombuffer(buf, dtype=np.uint8), count=catalog_size).astype(bool)
    return np.nonzero(mask)[0].astype(np.int32)


_SHUFFLE_CHUNK_ELEMS = 1 << 24    # 16M elements = 64 MB per chunk


def _write_shuffled_lzma(
    zf: zipfile.ZipFile, arcname: str, arr: np.ndarray, desc: str | None = None
) -> None:
    """Byte-shuffle `arr` in fixed-size chunks into a single lzma stream in the archive.

    Chunked so peak memory is one chunk, not the whole array -- a dense city's CSR indices
    run ~1.7 GB, and the old dense path already had to be written to stay off the anon heap
    (see _memmap_zip_member). The reader reverses with the same chunk size.

    Pass `desc` for a progress bar: lzma at preset=1 moves tens of MB/s, so a 1.7 GB array
    is minutes of otherwise-silent work. The bar is driven by bytes already consumed, which
    costs nothing to know.
    """
    comp = lzma.LZMACompressor(preset=1)
    bar = tqdm(
        total=arr.nbytes, desc=desc, unit="B", unit_scale=True, unit_divisor=1024,
        leave=False, disable=desc is None,
    )
    with zf.open(arcname, "w") as dst:
        for start in range(0, arr.size, _SHUFFLE_CHUNK_ELEMS):
            chunk = arr[start:start + _SHUFFLE_CHUNK_ELEMS]
            out = comp.compress(_shuffle_bytes(chunk))
            if out:
                dst.write(out)
            bar.update(chunk.nbytes)
        dst.write(comp.flush())
    bar.close()


def _read_shuffled_lzma(
    zf: zipfile.ZipFile, arcname: str, dtype, n: int, desc: str | None = None
) -> np.ndarray:
    """Inverse of _write_shuffled_lzma, filling the output array chunk by chunk."""
    itemsize = np.dtype(dtype).itemsize
    out = np.empty(n, dtype=dtype)
    dec = lzma.LZMADecompressor()
    buf = bytearray()
    pos = 0
    bar = tqdm(
        total=out.nbytes, desc=desc, unit="B", unit_scale=True, unit_divisor=1024,
        leave=False, disable=desc is None,
    )
    with zf.open(arcname) as src:
        while pos < n:
            raw = src.read(1 << 22)
            if not raw:
                break
            buf += dec.decompress(raw)
            while pos < n:
                take = min(_SHUFFLE_CHUNK_ELEMS, n - pos)
                need = take * itemsize
                if len(buf) < need:
                    break
                out[pos:pos + take] = _unshuffle_bytes(bytes(buf[:need]), dtype, take)
                del buf[:need]
                pos += take
                bar.update(need)
    bar.close()
    return out


def _memmap_zip_member(zip_path: str, arcname: str):
    """Memory-map an uncompressed .npy member of a zip in place, without copying it out.

    A ZIP_STORED member's bytes already sit
    contiguously in the archive in C order, so a 12.6 GB matrix can be read straight from
    the .npz at an offset instead of being extracted to a temp file first.
    """
    with zipfile.ZipFile(zip_path) as zf:
        zinfo = zf.getinfo(arcname)
        if zinfo.compress_type != zipfile.ZIP_STORED:
            raise ValueError(f"{arcname} is compressed; cannot memmap in place")
    with open(zip_path, "rb") as f:
        f.seek(zinfo.header_offset)
        local_header = f.read(30)
        fname_len, extra_len = struct.unpack("<HH", local_header[26:30])
        f.seek(zinfo.header_offset + 30 + fname_len + extra_len)
        version = npy_format.read_magic(f)
        shape, fortran, dtype = npy_format._read_array_header(f, version)
        offset = f.tell()
    if fortran:
        raise ValueError(f"{arcname} is Fortran-ordered; cannot memmap as C-order")
    return np.memmap(zip_path, dtype=dtype, mode="r", shape=shape, offset=offset)


_MATRIX_ROW_CHUNK = 512


def encode_matrix_csr(zf: zipfile.ZipFile, name: str, matrix) -> dict:
    """Write a dense impedance matrix into the archive as CSR; return its header entry.

    These matrices are 86-88% exact zeros (subway's median row has none at all), so the
    dense form spends ~22 GB storing the number 0. A zero here means "no service", which
    the accessibility stage already reads as zero decay (_matrix_decay's `vals > 0`), and
    CSR reconstructs it as exactly 0.0 -- lossless, not a threshold.

    Streams the source in row chunks so the dense matrix is never pulled into RAM: it
    arrives as a memmap whose pages must stay clean and reclaimable (see the note on
    write_impedance_bundle's `matrix`, and the 2026-07-31 cgroup incident).
    """
    n_rows, n_cols = matrix.shape
    # Two passes over the memmap (count, then fill), so the bar spans 2 * n_rows -- a dense
    # city reads ~25 GB here and would otherwise sit silent for minutes.
    bar = tqdm(total=n_rows * 2, desc=f"[Artifact] {name} -> CSR", unit="row",
               unit_scale=True, leave=False)
    counts = np.empty(n_rows, dtype=np.int64)
    for start in range(0, n_rows, _MATRIX_ROW_CHUNK):
        block = np.asarray(matrix[start:start + _MATRIX_ROW_CHUNK])
        counts[start:start + block.shape[0]] = np.count_nonzero(block, axis=1)
        bar.update(block.shape[0])
    indptr = np.concatenate(([0], np.cumsum(counts))).astype(np.int64)
    nnz = int(indptr[-1])

    indices = np.empty(nnz, dtype=np.int32)
    data = np.empty(nnz, dtype=np.float32)
    for start in range(0, n_rows, _MATRIX_ROW_CHUNK):
        block = np.asarray(matrix[start:start + _MATRIX_ROW_CHUNK])
        rows, cols = np.nonzero(block)          # row-major == CSR order
        lo, hi = int(indptr[start]), int(indptr[start + block.shape[0]])
        indices[lo:hi] = cols.astype(np.int32)
        data[lo:hi] = block[rows, cols]
        bar.update(block.shape[0])
    bar.close()

    _write_shuffled_lzma(zf, f"{name}_indices.bin", indices, desc=f"[Artifact] {name} indices")
    _write_shuffled_lzma(zf, f"{name}_data.bin", data, desc=f"[Artifact] {name} data")
    zf.writestr(f"{name}_indptr.bin", indptr.tobytes())
    return {"shape": [int(n_rows), int(n_cols)], "nnz": nnz}


def decode_matrix_csr(zf: zipfile.ZipFile, name: str, header: dict, dest_path: str):
    """Expand a CSR matrix back into the dense .dat memmap the pipeline reads."""
    n_rows, n_cols = header["shape"]
    nnz = int(header["nnz"])
    indptr = np.frombuffer(zf.read(f"{name}_indptr.bin"), dtype=np.int64)
    indices = _read_shuffled_lzma(zf, f"{name}_indices.bin", np.int32, nnz,
                                  desc=f"[Artifact] {name} indices")
    data = _read_shuffled_lzma(zf, f"{name}_data.bin", np.float32, nnz,
                               desc=f"[Artifact] {name} data")

    mat = np.memmap(dest_path, dtype=np.float32, mode="w+", shape=(n_rows, n_cols))
    bar = tqdm(total=n_rows, desc=f"[Artifact] {name} <- CSR", unit="row",
               unit_scale=True, leave=False)
    for start in range(0, n_rows, _MATRIX_ROW_CHUNK):
        end = min(start + _MATRIX_ROW_CHUNK, n_rows)
        block = np.zeros((end - start, n_cols), dtype=np.float32)
        for r in range(start, end):
            lo, hi = int(indptr[r]), int(indptr[r + 1])
            block[r - start, indices[lo:hi]] = data[lo:hi]
        mat[start:end] = block
        bar.update(end - start)
    bar.close()
    mat.flush()
    return (n_rows, n_cols)


_BLOB_MAGIC = b"NB4\x00"


def encode_blob(payload: dict, catalog_sizes: dict[str, int]) -> bytes:
    """Serialize one node's non-bus payload into the v4 compact form.

    Layout: magic, a 4-byte header length, a JSON header (node identity + a per-entry
    index of service/poi_type/n), then ONE lzma stream holding every entry's arrays back
    to back. One compression call per node rather than per array: the arrays are small and
    lzma needs a wide window to exploit the redundancy across them (measured 1.73x joined
    vs 1.40x per-array on a real blob).
    """
    index, parts = [], []
    for service, entries in payload["services"].items():
        for e in entries:
            poi_type = str(e["poi_type"])
            n = int(len(e["kept_idx"]))
            index.append({"service": service, "poi_type": poi_type, "n": n})
            parts.append(_pack_kept_idx(e["kept_idx"], catalog_sizes[poi_type]))
            parts.append(_shuffle_bytes(np.ascontiguousarray(e["dest_col"], dtype=np.int32)))
            parts.append(np.packbits(e["in_radius"]).tobytes())
            for k in ("imp_walk", "imp_bike", "imp_drive"):
                parts.append(_shuffle_bytes(np.ascontiguousarray(e[k], dtype=np.float32)))

    header = json.dumps({
        "node_id": payload["node_id"],
        "origin": [float(payload["origin"][0]), float(payload["origin"][1])],
        "poi_config_signature": payload["poi_config_signature"],
        "entries": index,
    }).encode("utf-8")
    body = lzma.compress(b"".join(parts), preset=1)
    return _BLOB_MAGIC + struct.pack("<I", len(header)) + header + body


def decode_blob(buf: bytes, catalog_sizes: dict[str, int], schema_version: int) -> dict:
    """Inverse of encode_blob: rebuild the exact dict the non-bus cache holds."""
    if buf[:4] != _BLOB_MAGIC:
        raise ValueError("not a v4 non-bus blob")
    (hlen,) = struct.unpack("<I", buf[4:8])
    header = json.loads(buf[8:8 + hlen])
    raw = lzma.decompress(buf[8 + hlen:])

    pos = 0
    services: dict[str, list] = {}
    for item in header["entries"]:
        poi_type, n = item["poi_type"], int(item["n"])
        csize = catalog_sizes[poi_type]
        nb = (csize + 7) // 8
        kept_idx = _unpack_kept_idx(raw[pos:pos + nb], csize); pos += nb
        dest_col = _unshuffle_bytes(raw[pos:pos + 4 * n], np.int32, n); pos += 4 * n
        rb = (n + 7) // 8
        in_radius = np.unpackbits(
            np.frombuffer(raw[pos:pos + rb], dtype=np.uint8), count=n
        ).astype(bool); pos += rb
        entry = {"poi_type": poi_type, "kept_idx": kept_idx,
                 "dest_col": dest_col, "in_radius": in_radius}
        for k in ("imp_walk", "imp_bike", "imp_drive"):
            entry[k] = _unshuffle_bytes(raw[pos:pos + 4 * n], np.float32, n); pos += 4 * n
        services.setdefault(item["service"], []).append(entry)

    return {
        "schema_version": schema_version,
        "poi_config_signature": header["poi_config_signature"],
        "node_id": header["node_id"],
        "origin": tuple(header["origin"]),
        "services": services,
    }


def _mode_matrix_path(cfg, mode: str) -> str:
    return cfg.bus_impedance_matrix_path if mode == "bus" else cfg.subway_impedance_matrix_path


def _write_mode_sidecars(cfg, mode: str, source_id_to_row: dict, dest_coords) -> None:
    """Write the row/column index files and destinations CSV a restored mode needs.

    These are the same files the live routing stages produce, so downstream code reads one
    layout regardless of whether the run routed or restored from a bundle.
    """
    if mode == "bus":
        src_path = cfg.bus_source_id_to_row_path
        col_path = cfg.bus_dest_id_to_col_path
        csv_path = cfg.bus_routing_destinations_input_path
    else:
        src_path = cfg.subway_source_id_to_row_path
        col_path = cfg.subway_dest_id_to_col_path
        csv_path = cfg.subway_routing_destinations_input_path

    Path(_mode_matrix_path(cfg, mode)).parent.mkdir(parents=True, exist_ok=True)
    Path(src_path).parent.mkdir(parents=True, exist_ok=True)

    with open(src_path, "w", encoding="utf-8") as f:
        json.dump(source_id_to_row, f)
    with open(col_path, "w", encoding="utf-8") as f:
        json.dump({f"d{idx}": idx for idx in range(dest_coords.shape[0])}, f)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "lon", "lat"])
        for idx, (lat, lon) in enumerate(dest_coords.tolist()):
            writer.writerow([f"d{idx}", float(lon), float(lat)])


def load_impedance_bundle(
    ctx: PipelineContext,
) -> tuple[BusRoutingStageResult, NonBusRoutingStageResult] | None:
    path = ctx.config.impedance_artifact_path
    if not os.path.isfile(path):
        return None
    cfg = ctx.config

    with zipfile.ZipFile(path) as zf:
        if "manifest.json" not in set(zf.namelist()):
            print("[Artifact] Bundle predates schema 4; recomputing.", flush=True)
            return None
        man = json.loads(zf.read("manifest.json"))

    if int(man["schema_version"]) != ARTIFACT_SCHEMA_VERSION:
        print(
            f"[Artifact] Bundle schema_version={man['schema_version']} != "
            f"{ARTIFACT_SCHEMA_VERSION}; recomputing.",
            flush=True,
        )
        return None
    if cfg.enable_subway and not man["has_subway"]:
        print(
            "[Artifact] Bundle has no subway data but subway is enabled; recomputing.",
            flush=True,
        )
        return None

    # The bundle's routing data is keyed by origin node ID (accessibility_stage looks up
    # source_id_to_row.get(node_id), not by list position), so what actually matters for
    # correctness is ID coverage, not an exact ordered match. An ordered coordinate hash
    # (the old check) failed on ANY reordering -- which a fresh OSM download produces
    # routinely, since Overpass element order isn't guaranteed stable across requests, even
    # when the underlying node set is unchanged. Reject only when coverage is low enough to
    # suggest a genuinely different/wrong bundle; a handful of nodes missing (real OSM edits
    # between builds) just means those specific nodes get zero transit/non-bus accessibility
    # this run, same as any other legitimately-unreachable node -- not silently wrong data
    # for the nodes that ARE covered.
    current_node_ids = {str(node_id) for node_id, _ in ctx.nodes_with_coords}
    bundle_node_ids = {str(n) for n in man["node_ids"]}
    covered = current_node_ids & bundle_node_ids
    coverage = len(covered) / len(current_node_ids) if current_node_ids else 0.0
    if coverage < _MIN_ORIGIN_COVERAGE:
        print(
            f"[Artifact] Bundle covers only {coverage:.1%} of the current node set "
            f"(need >= {_MIN_ORIGIN_COVERAGE:.0%}); recomputing.",
            flush=True,
        )
        return None
    missing = current_node_ids - bundle_node_ids
    if missing:
        print(
            f"[Artifact] Bundle origins cover {coverage:.1%} of the current node set -- "
            f"{len(missing)} current node(s) have no bundle entry and will get zero "
            "transit/non-bus accessibility this run.",
            flush=True,
        )

    node_ids = [str(n) for n in man["node_ids"]]
    Path(cfg.non_bus_cache_dir).mkdir(parents=True, exist_ok=True)
    _t0 = time.monotonic()

    with zipfile.ZipFile(path) as zf:
        catalog_ptr = np.load(zf.open("poi_catalog_ptr.npy"))
        catalog_src_keys = np.load(zf.open("poi_catalog_src_keys.npy"))
        catalog_coords = np.load(zf.open("poi_catalog_source_coords.npy"))
        catalog_types = man["poi_catalog_types"]

        # Reconstitute the shared catalog where the live pipeline writes it, so the
        # accessibility stage has one place to read it from either way.
        poi_catalog = {
            poi_type: {
                "src_keys": catalog_src_keys[catalog_ptr[i]:catalog_ptr[i + 1]],
                "source_coords": catalog_coords[catalog_ptr[i]:catalog_ptr[i + 1]],
            }
            for i, poi_type in enumerate(catalog_types)
        }
        Path(cfg.non_bus_poi_catalog_path).parent.mkdir(parents=True, exist_ok=True)
        with open(cfg.non_bus_poi_catalog_path, "wb") as f:
            pickle.dump(poi_catalog, f, protocol=pickle.HIGHEST_PROTOCOL)
        catalog_sizes = {
            t: int(catalog_ptr[i + 1] - catalog_ptr[i]) for i, t in enumerate(catalog_types)
        }
        del catalog_src_keys, catalog_coords, poi_catalog

        source_id_to_row = {node_id: idx for idx, node_id in enumerate(node_ids)}
        for mode in ("bus", "subway"):
            if mode == "subway" and not cfg.enable_subway:
                continue
            dest_coords = np.load(zf.open(f"{mode}_dest_coords.npy"))
            _write_mode_sidecars(cfg, mode, source_id_to_row, dest_coords)
            print(f"[Artifact] Expanding {mode} matrix from CSR ...", flush=True)
            shape = decode_matrix_csr(
                zf, mode, man["matrices"][mode], _mode_matrix_path(cfg, mode)
            )
            print(
                f"[Artifact]   {mode} {shape} restored, "
                f"nnz={man['matrices'][mode]['nnz']}",
                flush=True,
            )

        schema_version = int(man["non_bus_cache_schema_version"])
        cache_paths: dict[str, str] = {}
        _thread_local = threading.local()

        def _restore_one(node_id: str) -> tuple[str, str]:
            zf_local = getattr(_thread_local, "zf", None)
            if zf_local is None:
                zf_local = zipfile.ZipFile(path)
                _thread_local.zf = zf_local
            payload = decode_blob(
                zf_local.read(f"non_bus_blobs/{node_id}.bin"), catalog_sizes, schema_version
            )
            out_path = os.path.join(cfg.non_bus_cache_dir, f"{node_id}.pkl")
            with open(out_path, "wb") as f:
                pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
            with open(out_path + ".v", "w") as fv:     # lets _has_valid_non_bus_cache
                fv.write(str(schema_version))          # skip a full unpickle later
            return str(node_id), out_path

        # CPU-bound now (lzma + unshuffle) rather than pure byte-copying, so this tracks
        # core count instead of the old 4x oversubscription for I/O.
        max_workers = min(16, os.cpu_count() or 4)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_restore_one, n): n for n in node_ids}
            for fut in tqdm(
                as_completed(futures), total=len(futures),
                desc="[Artifact] Restoring non-bus caches", unit="node",
            ):
                node_id, out_path = fut.result()
                cache_paths[node_id] = out_path

    gc.collect()
    print(
        f"[Artifact] Restored {len(cache_paths)} nodes in {time.monotonic() - _t0:.1f}s.",
        flush=True,
    )

    bus = BusRoutingStageResult(
        routing_csv=cfg.bus_routing_matrix_path,
        routing_pkl=cfg.bus_routing_cache_path,
        routing_departure_iso=man["routing_departure_iso"],
        origins_sig=man["origins_sig"],
        destinations_sig=man["destinations_sig"],
    )
    non_bus = NonBusRoutingStageResult(
        cache_paths=cache_paths,
        cached_nodes=len(cache_paths),
        computed_nodes=0,
    )
    return bus, non_bus


def _read_mode_dest_coords(cfg, mode: str) -> np.ndarray:
    """Read one mode's routed destinations CSV into an (n_dest, 2) lat/lon array,
    ordered by matrix column."""
    if mode == "bus":
        col_path = cfg.bus_dest_id_to_col_path
        csv_path = cfg.bus_routing_destinations_input_path
    else:
        col_path = cfg.subway_dest_id_to_col_path
        csv_path = cfg.subway_routing_destinations_input_path

    with open(col_path, encoding="utf-8") as f:
        dest_id_to_col = {str(k): int(v) for k, v in json.load(f).items()}
    dest_coords = np.zeros((len(dest_id_to_col), 2), dtype=np.float64)
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            idx = dest_id_to_col.get(str(row["id"]))
            if idx is not None:
                dest_coords[idx, 0] = float(row["lat"])
                dest_coords[idx, 1] = float(row["lon"])
    return dest_coords


def write_impedance_bundle(
    ctx: PipelineContext,
    bus: BusRoutingStageResult,
    non_bus: NonBusRoutingStageResult,
) -> None:
    cfg = ctx.config
    node_ids = [str(node_id) for node_id, _ in ctx.nodes_with_coords]

    catalog_types = sorted(non_bus.poi_catalog.keys())
    catalog_ptr = [0]
    src_keys_parts, coords_parts = [], []
    for poi_type in catalog_types:
        bundle = non_bus.poi_catalog[poi_type]
        src_keys_parts.append(np.asarray(bundle["src_keys"]))
        coords_parts.append(np.asarray(bundle["source_coords"], dtype=np.float64).reshape(-1, 2))
        catalog_ptr.append(catalog_ptr[-1] + len(bundle["src_keys"]))
    catalog_ptr = np.asarray(catalog_ptr, dtype=np.int64)
    catalog_sizes = {
        t: int(catalog_ptr[i + 1] - catalog_ptr[i]) for i, t in enumerate(catalog_types)
    }

    # Only resolve+validate each node's cache path here (cheap: path strings, not file
    # contents). The bytes are read one node at a time below, so peak memory never holds
    # more than one window of blobs regardless of how many GB the full cache adds up to.
    node_cache_paths = []
    for node_id in node_ids:
        cache_path = non_bus.cache_paths.get(node_id)
        if cache_path is None:
            try:
                cache_path = non_bus.cache_paths.get(int(node_id))
            except Exception:
                cache_path = None
        if cache_path is None or not os.path.exists(cache_path):
            raise RuntimeError(f"Missing non-bus cache for node_id={node_id}")
        node_cache_paths.append(cache_path)

    Path(cfg.impedance_artifact_path).parent.mkdir(parents=True, exist_ok=True)
    _t0 = time.monotonic()
    print(f"[Artifact] Writing impedance bundle to {cfg.impedance_artifact_path} ...", flush=True)

    matrix_headers: dict[str, dict] = {}
    with zipfile.ZipFile(cfg.impedance_artifact_path, "w", allowZip64=True) as zf:
        for mode in ("bus", "subway"):
            if mode == "subway" and not cfg.enable_subway:
                continue
            dest_coords = _read_mode_dest_coords(cfg, mode)
            # Keep this a memmap, not an np.array() copy: each matrix is ~12.6 GB for a
            # dense city and encode_matrix_csr streams it in row chunks, so its pages stay
            # clean and reclaimable instead of un-reclaimable anon (2026-07-31 cgroup kill).
            matrix = np.memmap(
                _mode_matrix_path(cfg, mode), dtype=np.float32, mode="r",
                shape=(len(node_ids), dest_coords.shape[0]),
            )
            print(f"[Artifact] {mode} matrix {matrix.shape} -> CSR ...", flush=True)
            matrix_headers[mode] = encode_matrix_csr(zf, mode, matrix)
            nnz = matrix_headers[mode]["nnz"]
            print(f"[Artifact]   nnz={nnz} ({nnz / matrix.size:.1%} of dense)", flush=True)
            del matrix
            with zf.open(f"{mode}_dest_coords.npy", "w") as f:
                np.save(f, dest_coords)

        for name, arr in (
            ("poi_catalog_src_keys",
             np.concatenate(src_keys_parts) if src_keys_parts else np.asarray([], dtype="S1")),
            ("poi_catalog_source_coords",
             np.concatenate(coords_parts, axis=0) if coords_parts else np.empty((0, 2), dtype=np.float64)),
            ("poi_catalog_ptr", catalog_ptr),
        ):
            with zf.open(f"{name}.npy", "w") as f:
                np.save(f, arr)

        zf.writestr("manifest.json", json.dumps({
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "non_bus_cache_schema_version": int(cfg.non_bus_cache_schema_version),
            "node_ids": node_ids,
            "routing_departure_iso": bus.routing_departure_iso,
            "origins_sig": bus.origins_sig,
            "destinations_sig": bus.destinations_sig,
            "poi_catalog_types": catalog_types,
            "poi_radius_m": serv.get_global_radius_m(cfg),
            "has_subway": bool(cfg.enable_subway),
            "matrices": matrix_headers,
        }))

        # Encode in parallel (lzma releases the GIL) but write serially -- a ZipFile is not
        # thread-safe. Windowed rather than one big map so peak memory is one window of
        # encoded blobs, not all of them at once.
        def _encode_one(cache_path: str) -> bytes:
            with open(cache_path, "rb") as f:
                return encode_blob(pickle.load(f), catalog_sizes)

        max_workers = min(16, os.cpu_count() or 4)
        window = max_workers * 4
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            bar = tqdm(total=len(node_ids), desc="[Artifact] Writing non-bus caches", unit="node")
            for start in range(0, len(node_ids), window):
                chunk_ids = node_ids[start:start + window]
                chunk_paths = node_cache_paths[start:start + window]
                for node_id, payload in zip(chunk_ids, pool.map(_encode_one, chunk_paths)):
                    zf.writestr(f"non_bus_blobs/{node_id}.bin", payload)
                    bar.update(1)
            bar.close()

    size_gb = os.path.getsize(cfg.impedance_artifact_path) / (1 << 30)
    print(
        f"[Artifact] Wrote impedance bundle: {size_gb:.2f} GB in {time.monotonic() - _t0:.1f}s.",
        flush=True,
    )
