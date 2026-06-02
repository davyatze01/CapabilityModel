import csv
import json
import os
from pathlib import Path

import numpy as np

from pipeline_types import BusRoutingStageResult, NonBusRoutingStageResult, PipelineContext

ARTIFACT_SCHEMA_VERSION = 1


def load_impedance_bundle(
    ctx: PipelineContext,
) -> tuple[BusRoutingStageResult, NonBusRoutingStageResult] | None:
    path = ctx.config.impedance_artifact_path
    if not os.path.isfile(path):
        return None

    with np.load(path, allow_pickle=True) as z:
        data = {k: z[k] for k in z.files}

    node_ids = np.array(data["node_ids"]).astype(str)
    bus_matrix = np.array(data["bus_impedance_matrix"], dtype=np.float32)
    bus_dest_coords = np.array(data["bus_dest_coords"], dtype=np.float64)
    blobs = np.array(data["non_bus_blobs"], dtype=object)

    cfg = ctx.config
    Path(cfg.non_bus_cache_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.bus_impedance_matrix_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.bus_source_id_to_row_path).parent.mkdir(parents=True, exist_ok=True)

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

    mat = np.memmap(
        cfg.bus_impedance_matrix_path,
        dtype=np.float32,
        mode="w+",
        shape=bus_matrix.shape,
    )
    mat[:] = bus_matrix
    mat.flush()

    cache_paths: dict[str, str] = {}
    for idx, node_id in enumerate(node_ids.tolist()):
        out_path = os.path.join(cfg.non_bus_cache_dir, f"{node_id}.pkl")
        blob = blobs[idx]
        if not isinstance(blob, (bytes, bytearray)):
            raise ValueError(f"Invalid non_bus blob type at index {idx}: {type(blob)!r}")
        with open(out_path, "wb") as f:
            f.write(blob)
        cache_paths[str(node_id)] = out_path

    routing_departure_iso = str(np.array(data["routing_departure_iso"]).item())
    origins_sig = str(np.array(data["origins_sig"]).item())
    destinations_sig = str(np.array(data["destinations_sig"]).item())

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

    blobs = np.empty((len(node_ids),), dtype=object)
    for idx, node_id in enumerate(node_ids.tolist()):
        cache_path = non_bus.cache_paths.get(node_id)
        if cache_path is None:
            try:
                cache_path = non_bus.cache_paths.get(int(node_id))
            except Exception:
                cache_path = None
        if cache_path is None or not os.path.exists(cache_path):
            raise RuntimeError(f"Missing non-bus cache for node_id={node_id}")
        with open(cache_path, "rb") as f:
            blobs[idx] = f.read()

    Path(cfg.impedance_artifact_path).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        cfg.impedance_artifact_path,
        schema_version=np.array(ARTIFACT_SCHEMA_VERSION, dtype=np.int32),
        node_ids=node_ids,
        bus_impedance_matrix=matrix,
        bus_dest_coords=dest_coords,
        non_bus_blobs=blobs,
        routing_departure_iso=np.array(bus.routing_departure_iso, dtype=object),
        origins_sig=np.array(bus.origins_sig, dtype=object),
        destinations_sig=np.array(bus.destinations_sig, dtype=object),
    )
