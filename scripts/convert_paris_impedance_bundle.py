"""One-time migration for an impedance bundle: schema 3 -> schema 4.

Schema 3 shipped the working non-bus cache verbatim -- `write_impedance_bundle` zipped each
scratch `.pkl` in as-is -- so the artifact carried a lot that is not impedance. Measured on
Paris (114 GB uncompressed): only ~32% of the bytes were impedance values. `poi_coords` alone
was 44 GB, and it is never user-facing geometry (exported POI coordinates come from the shared
catalog); it existed only to hash into a bus/subway matrix column and to run the radius test.
The bus and subway matrices were dense float32 despite being 86-88% exact zeros.

Schema 4 stores the impedances plus only the addressing needed to read them:
  - `poi_coords` -> `dest_col` (one column addressing BOTH matrices, since bus and subway
    route against the same destination set) + `in_radius`
  - `kept_idx` int32 list -> a presence bitmask over the shared catalog
  - dense matrices -> CSR
  - the whole payload byte-shuffled and lzma'd
Nothing is recomputed and nothing is quantized: every impedance value is copied across
bit-for-bit, and the CSR expands back to the identical dense matrix. This is a re-encoding,
not a re-route.

OLD_BUNDLE_PATH is only ever read, never modified or deleted. The result is written to a
brand-new NEW_BUNDLE_PATH so the original stays available to retry from if anything looks off.

Run directly from VS Code (Run Python File / F5), or: python scripts/convert_paris_impedance_bundle.py
"""

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

import numpy as np
from tqdm import tqdm

from core.config import PipelineConfig
from exports.artifact_bundle import (
    ARTIFACT_SCHEMA_VERSION,
    encode_blob,
    encode_matrix_csr,
    _memmap_zip_member,
)
from utils import services as serv
from utils.delta_g import _haversine_m_np

STUDY_CITY = "paris"
OLD_BUNDLE_PATH = str(_REPO_ROOT / "artifacts/mgp_boundary/impedances.npz")
NEW_BUNDLE_PATH = str(_REPO_ROOT / "artifacts/mgp_boundary/impedances_v4.npz")

_T0 = 0.0


def _log(msg: str) -> None:
    """Timestamped line so phases of a long run can be told apart in a scrollback."""
    el = time.monotonic() - _T0
    print(f"[Convert +{int(el) // 60:03d}:{int(el) % 60:02d}] {msg}", flush=True)


def _coord_col_map(dest_coords: np.ndarray) -> dict[tuple[float, float], int]:
    """{rounded (lat, lon) -> matrix column} built from a bundle's own dest coords.

    Rounding matches utils.delta_g._dest_col_map exactly, so a POI resolves to the same
    column here as it does in a live run.
    """
    return {
        (round(float(lat), 6), round(float(lon), 6)): idx
        for idx, (lat, lon) in enumerate(dest_coords)
    }


def _convert_blob(payload: dict, dest_map, radius_m) -> dict:
    """v10 blob (per-origin snapped poi_coords) -> v11 shape (resolved addressing).

    poi_coords was only ever used to reach a matrix column and to run the radius test, so
    both are resolved here and the geometry is dropped. No impedance is recomputed.
    """
    origin = payload["origin"]
    for items in payload["services"].values():
        for it in items:
            pc = it.pop("poi_coords")
            n = len(pc)
            dest_col = np.full(n, -1, dtype=np.int32)
            for i in range(n):
                # Python round(), not np.round(): they disagree on exact .5 ties and 5% of
                # these coords are ties -- a mismatch silently zeroes a POI's transit access.
                c = dest_map.get((round(float(pc[i, 0]), 6), round(float(pc[i, 1]), 6)))
                if c is not None:
                    dest_col[i] = c
            it["dest_col"] = dest_col
            it["in_radius"] = (
                _haversine_m_np(origin[0], origin[1], pc[:, 0], pc[:, 1]) <= radius_m
                if radius_m is not None
                else np.ones(n, dtype=bool)
            )
    return payload


def convert() -> None:
    global _T0
    _T0 = time.monotonic()

    cfg = PipelineConfig(study_city=STUDY_CITY)
    radius_m = serv.get_global_radius_m(cfg)
    target_cache_version = int(cfg.non_bus_cache_schema_version)

    old_size = os.path.getsize(OLD_BUNDLE_PATH)
    _log(f"Source {OLD_BUNDLE_PATH} ({old_size / (1 << 30):.1f} GB)")
    _log(f"Target {NEW_BUNDLE_PATH}")
    _log("Reading metadata ...")
    with np.load(OLD_BUNDLE_PATH, allow_pickle=True) as z:
        node_ids = np.array(z["node_ids"]).astype(str).tolist()
        routing_departure_iso = str(np.array(z["routing_departure_iso"]).item())
        origins_sig = str(np.array(z["origins_sig"]).item())
        destinations_sig = str(np.array(z["destinations_sig"]).item())
        catalog_types = np.array(z["poi_catalog_types"]).astype(str).tolist()
        catalog_ptr = np.asarray(z["poi_catalog_ptr"], dtype=np.int64)
        catalog_src_keys = np.asarray(z["poi_catalog_src_keys"])
        catalog_source_coords = np.asarray(z["poi_catalog_source_coords"], dtype=np.float64)
        bus_dest_coords = np.asarray(z["bus_dest_coords"], dtype=np.float64)
        has_subway = "subway_impedance_matrix" in z.files
        subway_dest_coords = (
            np.asarray(z["subway_dest_coords"], dtype=np.float64) if has_subway else None
        )

    catalog_sizes = {
        poi_type: int(catalog_ptr[i + 1] - catalog_ptr[i])
        for i, poi_type in enumerate(catalog_types)
    }

    # The column map comes from the bundle's own dest coords, so the conversion needs
    # nothing but the input file.
    dest_map = _coord_col_map(bus_dest_coords)
    if subway_dest_coords is not None and _coord_col_map(subway_dest_coords) != dest_map:
        raise RuntimeError(
            "Subway destinations differ from bus destinations, so one dest_col cannot "
            "address both matrices. Schema 4 assumes a shared destination set."
        )
    _log(
        f"{len(node_ids)} nodes | {len(dest_map)} destinations | "
        f"{sum(catalog_sizes.values())} catalog POIs | radius {radius_m} m"
    )

    matrix_headers: dict[str, dict] = {}
    modes = [("bus", "bus_impedance_matrix.npy")]
    if has_subway:
        modes.append(("subway", "subway_impedance_matrix.npy"))

    with zipfile.ZipFile(OLD_BUNDLE_PATH) as src, \
            zipfile.ZipFile(NEW_BUNDLE_PATH, "w", allowZip64=True) as dst:

        _log(f"Phase 1/3: {len(modes)} matrices -> CSR")
        for name, arcname in modes:
            matrix = _memmap_zip_member(OLD_BUNDLE_PATH, arcname)
            dense_gb = matrix.size * 4 / (1 << 30)
            _log(f"  {name}: {matrix.shape} dense ({dense_gb:.1f} GB) ...")
            matrix_headers[name] = encode_matrix_csr(dst, name, matrix)
            nnz = matrix_headers[name]["nnz"]
            _log(f"  {name}: nnz={nnz} ({nnz / matrix.size:.1%} of dense)")
            del matrix

        _log(f"Phase 2/3: metadata ({len(catalog_types)} poi_types)")
        for arr_name, arr in (
            ("poi_catalog_src_keys", catalog_src_keys),
            ("poi_catalog_source_coords", catalog_source_coords),
            ("poi_catalog_ptr", catalog_ptr),
            ("bus_dest_coords", bus_dest_coords),
        ):
            with dst.open(f"{arr_name}.npy", "w") as f:
                np.save(f, arr)
        if subway_dest_coords is not None:
            with dst.open("subway_dest_coords.npy", "w") as f:
                np.save(f, subway_dest_coords)

        dst.writestr("manifest.json", json.dumps({
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "non_bus_cache_schema_version": target_cache_version,
            "node_ids": node_ids,
            "routing_departure_iso": routing_departure_iso,
            "origins_sig": origins_sig,
            "destinations_sig": destinations_sig,
            "poi_catalog_types": catalog_types,
            "poi_radius_m": radius_m,
            "has_subway": has_subway,
            "matrices": matrix_headers,
        }))

        _log(f"Phase 3/3: {len(node_ids)} node blobs")
        bytes_in = bytes_out = 0
        # smoothing well below tqdm's 0.3 default: blob sizes vary a lot, and over 23k items
        # a slow-moving average gives a far more usable ETA than one that tracks the last few.
        bar = tqdm(node_ids, desc="[Convert] blobs", unit="node", smoothing=0.05)
        for i, node_id in enumerate(bar):
            raw = src.read(f"non_bus_blobs/{node_id}.bin")
            payload = _convert_blob(pickle.loads(raw), dest_map, radius_m)
            payload["schema_version"] = target_cache_version
            enc = encode_blob(payload, catalog_sizes)
            dst.writestr(f"non_bus_blobs/{node_id}.bin", enc)
            bytes_in += len(raw)
            bytes_out += len(enc)
            if i % 200 == 0:       # postfix formatting isn't free; keep it off the hot path
                bar.set_postfix_str(
                    f"{bytes_in / (1 << 30):.1f}->{bytes_out / (1 << 30):.1f} GB "
                    f"({bytes_in / max(bytes_out, 1):.2f}x)"
                )
        bar.close()

    old_gb = old_size / (1 << 30)
    new_gb = os.path.getsize(NEW_BUNDLE_PATH) / (1 << 30)
    matrix_gb = (new_gb * (1 << 30) - bytes_out) / (1 << 30)
    _log(f"Done in {(time.monotonic() - _T0) / 60:.1f} min")
    _log(f"  matrices + metadata: {matrix_gb:6.2f} GB")
    _log(f"  blobs:               {bytes_out / (1 << 30):6.2f} GB "
         f"(from {bytes_in / (1 << 30):.1f} GB, {bytes_in / max(bytes_out, 1):.2f}x)")
    _log(f"  total:               {old_gb:.1f} GB -> {new_gb:.1f} GB "
         f"({old_gb / new_gb:.2f}x smaller)")
    _log(f"Original left untouched at {OLD_BUNDLE_PATH}; verify the new bundle loads "
         "before deleting it.")


if __name__ == "__main__":
    convert()
