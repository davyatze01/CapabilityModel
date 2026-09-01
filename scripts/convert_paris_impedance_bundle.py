"""One-time migration for Paris's impedance bundle: schema 2 -> schema 3.

Schema 2 per-node blobs carried their own source_keys/source_coords/walk_path_scores
(the same origin-invariant POI identity duplicated into every one of ~23k node blobs).
Schema 3 (see exports/artifact_bundle.py, routing/non_bus_routing_stage.py,
utils/delta_g.py) keeps that identity once, in a shared per-poi_type catalog, and has
each node reference it by kept_idx. This script converts the existing 750GB bundle to
the new layout by streaming it once -- no routing is recomputed -- and drops
natural=tree POIs (TYPEQU=='GI12') along the way, since config/poi_types.csv no longer
configures them.

OLD_BUNDLE_PATH is only ever read, never modified or deleted. The result is written to
a brand-new NEW_BUNDLE_PATH so the original stays available to retry from if anything
looks off. Do not run this at the same time as a live pipeline run against the same
city -- it writes to the same artifacts/mgp_boundary/bus (and subway) paths the live
non-bus/bus stages use.

Run directly from VS Code (Run Python File / F5), or: python scripts/convert_paris_impedance_bundle.py
"""
import csv
import json
import os
import pickle
import sys
import time
import zipfile
from pathlib import Path

# VS Code's "Run Python File" invokes this script directly, which puts this file's own
# folder (scripts/) on sys.path -- not the repo root -- so the project imports below
# would otherwise fail with ModuleNotFoundError regardless of how the script is run.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import geopandas as gpd
import numpy as np
from tqdm import tqdm

from core.config import PipelineConfig
from core.pipeline_types import BusRoutingStageResult, NonBusRoutingStageResult, PipelineContext
from exports.artifact_bundle import _restore_matrix_fast, write_impedance_bundle
from utils import services as serv
from utils.poi_identity import build_poi_source_key

OLD_BUNDLE_PATH = str(_REPO_ROOT / "artifacts/mgp_boundary/impedances.npz")
NEW_BUNDLE_PATH = str(_REPO_ROOT / "artifacts/mgp_boundary/impedances_v3.npz")
SCRATCH_CACHE_DIR = str(_REPO_ROOT / "artifacts/mgp_boundary/non_bus_convert_scratch")
TREE_SHAPEFILE = str(_REPO_ROOT / "Paris/POI_point.shp")
TREE_TYPEQU = "GI12"


def _build_tree_exclusion_set() -> set[str]:
    """Every source_key build_poi_source_key would produce for a TREE_TYPEQU row --
    the exact identifiers the old bundle's node blobs used, so they can be matched
    and dropped while streaming."""
    gdf = gpd.read_file(TREE_SHAPEFILE)
    trees = gdf[gdf["TYPEQU"] == TREE_TYPEQU]
    keys = {build_poi_source_key(dict(row), row.geometry) for _, row in trees.iterrows()}
    print(f"[Convert] {len(keys)} tree POIs ({TREE_TYPEQU}) will be dropped.", flush=True)
    return keys


def _restore_mode_matrix_to_disk(
    old_zip_path, z, label, matrix_arcname, dest_coords_key,
    matrix_path, source_id_to_row_path, dest_id_to_col_path, dest_csv_path,
    source_id_to_row,
) -> None:
    """Materialize one mode's (bus or subway) matrix + dest index/CSV to disk, in the
    exact shape write_impedance_bundle expects to read them back from -- the same
    shape load_impedance_bundle's own restore passes produce for a live bundle load."""
    dest_coords = np.asarray(z[dest_coords_key], dtype=np.float64)
    os.makedirs(os.path.dirname(matrix_path), exist_ok=True)

    with open(source_id_to_row_path, "w", encoding="utf-8") as f:
        json.dump(source_id_to_row, f)

    dest_id_to_col = {f"d{idx}": idx for idx in range(dest_coords.shape[0])}
    with open(dest_id_to_col_path, "w", encoding="utf-8") as f:
        json.dump(dest_id_to_col, f)

    with open(dest_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "lon", "lat"])
        for idx, (lat, lon) in enumerate(dest_coords.tolist()):
            writer.writerow([f"d{idx}", float(lon), float(lat)])

    shape = _restore_matrix_fast(old_zip_path, matrix_arcname, matrix_path)
    if shape is None:
        matrix = np.asarray(z[matrix_arcname[:-4]], dtype=np.float32)
        mat = np.memmap(matrix_path, dtype=np.float32, mode="w+", shape=matrix.shape)
        mat[:] = matrix
        mat.flush()
        del mat, matrix
    print(f"[Convert] Restored {label} matrix to {matrix_path}", flush=True)


def convert() -> None:
    cfg = PipelineConfig(study_city="paris")
    cfg.impedance_artifact_path = NEW_BUNDLE_PATH
    cfg.non_bus_cache_dir = SCRATCH_CACHE_DIR
    os.makedirs(SCRATCH_CACHE_DIR, exist_ok=True)

    tree_keys = _build_tree_exclusion_set()

    print(f"[Convert] Reading scalars from {OLD_BUNDLE_PATH} ...", flush=True)
    with np.load(OLD_BUNDLE_PATH, allow_pickle=True) as z:
        node_ids = np.array(z["node_ids"]).astype(str).tolist()
        routing_departure_iso = str(np.array(z["routing_departure_iso"]).item())
        origins_sig = str(np.array(z["origins_sig"]).item())
        destinations_sig = str(np.array(z["destinations_sig"]).item())
        has_subway = "subway_impedance_matrix" in z.files

        source_id_to_row = {node_id: idx for idx, node_id in enumerate(node_ids)}

        _restore_mode_matrix_to_disk(
            OLD_BUNDLE_PATH, z, "bus", "bus_impedance_matrix.npy", "bus_dest_coords",
            cfg.bus_impedance_matrix_path, cfg.bus_source_id_to_row_path,
            cfg.bus_dest_id_to_col_path, cfg.bus_routing_destinations_input_path,
            source_id_to_row,
        )
        if has_subway:
            _restore_mode_matrix_to_disk(
                OLD_BUNDLE_PATH, z, "subway", "subway_impedance_matrix.npy", "subway_dest_coords",
                cfg.subway_impedance_matrix_path, cfg.subway_source_id_to_row_path,
                cfg.subway_dest_id_to_col_path, cfg.subway_routing_destinations_input_path,
                source_id_to_row,
            )

    # Incrementally-assigned per-poi_type catalog: key string -> index, plus the
    # parallel coord list. A key's index never changes once assigned, so one
    # streaming pass over the nodes (in any order) is enough to build it.
    catalog_index: dict[str, dict[str, int]] = {}
    catalog_coords: dict[str, list[tuple[float, float]]] = {}
    cache_paths: dict[str, str] = {}

    print(f"[Convert] Streaming {len(node_ids)} node blobs from {OLD_BUNDLE_PATH} ...", flush=True)
    _t0 = time.monotonic()
    dropped_count = 0
    kept_count = 0
    with zipfile.ZipFile(OLD_BUNDLE_PATH) as zf:
        for node_id in tqdm(node_ids, desc="[Convert] Nodes", unit="node"):
            with zf.open(f"non_bus_blobs/{node_id}.bin") as f:
                old_payload = pickle.load(f)

            new_services = {}
            for service, entries in old_payload.get("services", {}).items():
                new_entries = []
                for entry in entries:
                    poi_type = str(entry["poi_type"])
                    old_source_keys = entry.get("source_keys", []) or []
                    old_source_coords = entry.get("source_coords", []) or []
                    old_poi_coords = entry.get("poi_coords", []) or []
                    old_imp_walk = entry.get("imp_walk", []) or []
                    old_imp_bike = entry.get("imp_bike", []) or []
                    old_imp_drive = entry.get("imp_drive", []) or []

                    idx_map = catalog_index.setdefault(poi_type, {})
                    coord_list = catalog_coords.setdefault(poi_type, [])

                    kept_idx_list: list[int] = []
                    kept_poi_coords: list[tuple[float, float]] = []
                    kept_imp_walk: list[float | None] = []
                    kept_imp_bike: list[float | None] = []
                    kept_imp_drive: list[float | None] = []
                    for i in range(len(old_source_keys)):
                        key = str(old_source_keys[i])
                        if key in tree_keys:
                            dropped_count += 1
                            continue
                        kept_count += 1
                        cat_idx = idx_map.get(key)
                        if cat_idx is None:
                            cat_idx = len(coord_list)
                            idx_map[key] = cat_idx
                            sc = old_source_coords[i] if i < len(old_source_coords) else (0.0, 0.0)
                            coord_list.append((float(sc[0]), float(sc[1])))
                        kept_idx_list.append(cat_idx)
                        kept_poi_coords.append(old_poi_coords[i] if i < len(old_poi_coords) else (0.0, 0.0))
                        kept_imp_walk.append(old_imp_walk[i] if i < len(old_imp_walk) else None)
                        kept_imp_bike.append(old_imp_bike[i] if i < len(old_imp_bike) else None)
                        kept_imp_drive.append(old_imp_drive[i] if i < len(old_imp_drive) else None)

                    n_kept = len(kept_idx_list)
                    new_entries.append({
                        "poi_type": poi_type,
                        "kept_idx": np.asarray(kept_idx_list, dtype=np.int32),
                        "poi_coords": np.asarray(kept_poi_coords, dtype=np.float64).reshape(n_kept, 2),
                        "imp_walk": np.asarray(
                            [v if v is not None else np.nan for v in kept_imp_walk], dtype=np.float32
                        ),
                        "imp_bike": np.asarray(
                            [v if v is not None else np.nan for v in kept_imp_bike], dtype=np.float32
                        ),
                        "imp_drive": np.asarray(
                            [v if v is not None else np.nan for v in kept_imp_drive], dtype=np.float32
                        ),
                    })
                new_services[service] = new_entries

            new_payload = {
                "schema_version": cfg.non_bus_cache_schema_version,
                "poi_config_signature": serv.config_signature(),
                "node_id": node_id,
                "origin": old_payload.get("origin"),
                "services": new_services,
            }
            out_path = os.path.join(SCRATCH_CACHE_DIR, f"{node_id}.pkl")
            with open(out_path, "wb") as f:
                pickle.dump(new_payload, f, protocol=pickle.HIGHEST_PROTOCOL)
            cache_paths[node_id] = out_path

    _elapsed = time.monotonic() - _t0
    print(
        f"[Convert] Streamed {len(node_ids)} nodes in {_elapsed / 60:.1f} min "
        f"(kept {kept_count} POI refs, dropped {dropped_count} tree POI refs).",
        flush=True,
    )

    poi_catalog = {
        poi_type: {
            "src_keys": np.asarray(list(idx_map.keys()), dtype="S"),
            "source_coords": np.asarray(catalog_coords[poi_type], dtype=np.float64),
        }
        for poi_type, idx_map in catalog_index.items()
    }
    total_catalog_pois = sum(len(v["src_keys"]) for v in poi_catalog.values())
    print(
        f"[Convert] Shared catalog: {total_catalog_pois} unique POIs across "
        f"{len(poi_catalog)} poi_types.",
        flush=True,
    )

    ctx = PipelineContext(
        config=cfg,
        graph=None,
        nodes_with_coords=[(node_id, {}) for node_id in node_ids],
        workers=1,
        output_paths={},
        capability_services={},
    )
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
        poi_catalog=poi_catalog,
    )

    print(f"[Convert] Writing new bundle to {NEW_BUNDLE_PATH} ...", flush=True)
    write_impedance_bundle(ctx, bus, non_bus)

    old_size_gb = os.path.getsize(OLD_BUNDLE_PATH) / (1 << 30)
    new_size_gb = os.path.getsize(NEW_BUNDLE_PATH) / (1 << 30)
    print(
        f"[Convert] Done. {OLD_BUNDLE_PATH} ({old_size_gb:.1f} GB) -> "
        f"{NEW_BUNDLE_PATH} ({new_size_gb:.1f} GB). Original left untouched -- "
        "verify the new bundle, then manually delete the old file and the scratch "
        f"cache dir ({SCRATCH_CACHE_DIR}) once satisfied.",
        flush=True,
    )


if __name__ == "__main__":
    convert()
