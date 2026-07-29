"""Post-pipeline score report generator.

Reads on-disk artifacts written by the main pipeline and produces the sharded
hexagon-POI store (schema hexagon_poi_powers_v2, see hex_shard_writer.py):
deflate+base64 JSONP shard files, each holding a block of hexagons with
compact-encoded per-POI service/capability powers.

Run after the main pipeline has completed:
    python score_report.py [--city paris]

Memory strategy: per-POI accessibility data is read one node at a time from small
per-node .npz files written by the accessibility stage; no large structure is ever
held in full.  POI metadata is loaded from the GPKG (source_key, poi_types, svc_map).
"""

import json
import os
import sqlite3
import sys
from typing import TYPE_CHECKING

import numpy as np
from tqdm import tqdm

from core.config import PipelineConfig
from core.context import build_context
from exports.hex_shard_writer import HexShardWriter
from utils import capabilities as cap_mod
from utils import services as serv

if TYPE_CHECKING:
    from core.pipeline_types import PipelineContext


def _read_poi_table(gpkg_path: str) -> tuple[dict[int, dict], dict[str, int]]:
    """Return (poi_by_id, source_key_to_id) from the POI export GeoPackage.

    poi_by_id[id] = {"source_key": str, "poi_types": [str,...], "svc_map": {str:[str,...]}}
    source_key_to_id[source_key] = id (integer)
    """
    poi_by_id: dict[int, dict] = {}
    source_key_to_id: dict[str, int] = {}
    if not os.path.exists(gpkg_path):
        raise FileNotFoundError(f"POI GeoPackage not found: {gpkg_path}")
    con = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
    try:
        for source_key, pid, poi_types_json, svc_map_json in con.execute(
            "SELECT source_key, id, poi_types, svc_map FROM pois_used"
        ):
            pid = int(pid)
            poi_types: list[str] = json.loads(poi_types_json) if poi_types_json else []
            svc_map: dict[str, list[str]] = json.loads(svc_map_json) if svc_map_json else {}
            poi_by_id[pid] = {
                "source_key": source_key,
                "poi_types": poi_types,
                "svc_map": svc_map,
            }
            source_key_to_id[str(source_key)] = pid
    finally:
        con.close()
    return poi_by_id, source_key_to_id


def _load_drop_set(drop_path: str) -> dict[str, set[str]]:
    """Load poi_ownership_drop.json as {poi_type: set(source_keys)}."""
    if not os.path.exists(drop_path):
        return {}
    try:
        with open(drop_path, encoding="utf-8") as f:
            raw: dict[str, list[str]] = json.load(f)
        return {pt: set(keys) for pt, keys in raw.items()}
    except Exception:
        return {}


def _load_node_sparse(node_id, poi_by_node_dir: str) -> dict[int, float]:
    """Load per-node compressed .npz → {poi_id: accessibility_value}.

    Returns empty dict if the file is absent (node had no reachable POIs or
    was a cache-hit from a run before this feature was added).
    """
    path = os.path.join(poi_by_node_dir, f"{node_id}.npz")
    if not os.path.exists(path):
        return {}
    try:
        data = np.load(path)
        return {int(pid): float(val) for pid, val in zip(data["poi_ids"], data["values"])}
    except Exception:
        return {}


def _compute_poi_powers(
    poi_id: int,
    poi_info: dict,
    acc_val: float,
    drop_set: dict[str, set[str]],
) -> tuple[dict[str, float], dict[str, float]]:
    """Compute service_power and capability_power for one POI given its accessibility.

    Mirrors poi_exports._poi_powers using exact per-POI accessibility (not poi_type fallback).
    Applies ownership dedup: skips poi_type contributions where this POI is in the drop set.
    """
    source_key = poi_info["source_key"]
    poi_types: list[str] = poi_info["poi_types"]

    service_power: dict[str, float] = {}
    for service in serv.SERVICE_KEYS:
        singletons = serv.SERVICE_SINGLETON_M.get(service, {})
        sp = 0.0
        for pt in poi_types:
            if pt not in singletons:
                continue
            if source_key in drop_set.get(pt, ()):
                continue
            sp += acc_val * float(singletons[pt])
        if sp > 0.0:
            service_power[service] = round(sp, 7)

    capability_power: dict[str, float] = {}
    for capability, cap_services in cap_mod.CAPABILITY_SERVICES.items():
        weights = cap_mod.CAP_ELECTRE_W[capability]
        cp = sum(
            service_power.get(svc, 0.0) * float(weights.get(svc, 0.0))
            for svc in cap_services
        )
        if cp > 0.0:
            capability_power[capability] = round(cp, 7)

    return service_power, capability_power


def _migrate_json_to_sparse(json_path: str, poi_by_node_dir: str, source_key_to_id: dict[str, int]) -> None:
    """One-time migration: convert the old monolithic access_poi_by_node.json into per-node .npz files.

    Only called when the old JSON exists but the per-node directory is absent/empty.
    Loads the JSON fully into memory — only practical for smaller cities (< ~20 GB).
    For very large cities (Paris) re-run the main pipeline instead.
    """
    size_gb = os.path.getsize(json_path) / 1e9
    print(
        f"[ScoreReport] Migrating {json_path} ({size_gb:.1f} GB) to per-node sparse files.\n"
        f"  This loads the full file into memory once.  For very large cities (> 20 GB) "
        f"re-run the main pipeline instead.",
        flush=True,
    )
    with open(json_path, encoding="utf-8") as f:
        data: dict[str, dict[str, float]] = json.load(f)

    os.makedirs(poi_by_node_dir, exist_ok=True)
    count = 0
    for node_id_str, poi_map in data.items():
        poi_ids_list: list[int] = []
        values_list: list[float] = []
        for sk, val in poi_map.items():
            if val > 0.0:
                pid = source_key_to_id.get(sk)
                if pid is not None:
                    poi_ids_list.append(pid)
                    values_list.append(val)
        if poi_ids_list:
            np.savez_compressed(
                os.path.join(poi_by_node_dir, f"{node_id_str}.npz"),
                poi_ids=np.array(poi_ids_list, dtype=np.uint32),
                values=np.array(values_list, dtype=np.float32),
            )
            count += 1
    print(f"[ScoreReport] Migration complete: {count} per-node sparse files written.", flush=True)


def generate_score_report(city: str | None = None, ctx: "PipelineContext | None" = None) -> None:
    """Write per-hexagon score .js files (id + service_power + capability_power).

    Pass `ctx` when calling from an in-process pipeline run that already built a
    PipelineContext (avoids rebuilding the graph/node sample). Otherwise (e.g. CLI
    invocation) a fresh context is built from `city`/CAP_STUDY_CITY.
    """
    cfg = ctx.config if ctx is not None else PipelineConfig(study_city=city or os.environ.get("CAP_STUDY_CITY", "paris"))
    print(f"[ScoreReport] city={cfg.city_name}  gpkg={cfg.poi_export_geopackage_path}", flush=True)

    poi_by_node_dir = cfg.accessibility_poi_by_node_dir

    print("[ScoreReport] Reading POI table...", flush=True)
    poi_by_id, source_key_to_id = _read_poi_table(cfg.poi_export_geopackage_path)
    print(f"[ScoreReport] {len(poi_by_id)} POIs loaded.", flush=True)

    # Auto-migrate from the old monolithic JSON if the per-node directory is absent.
    if not os.path.isdir(poi_by_node_dir) or not any(
        f.endswith(".npz") for f in os.listdir(poi_by_node_dir)
    ):
        old_json = cfg.accessibility_poi_by_node_path
        if os.path.exists(old_json):
            _migrate_json_to_sparse(old_json, poi_by_node_dir, source_key_to_id)
        else:
            raise FileNotFoundError(
                f"Neither the per-node directory ({poi_by_node_dir}) nor the legacy JSON "
                f"({old_json}) found.\n"
                "Run the main pipeline so accessibility_stage writes the per-node .npz files."
            )

    drop_set = _load_drop_set(cfg.poi_ownership_drop_path)
    if ctx is None:
        # Building the context loads the routing graph and can run for a while
        # with no output -- announce it so the terminal never looks stuck.
        print("[ScoreReport] Building context (loading graph + node sample)...", flush=True)
        ctx = build_context(cfg)
        print("[ScoreReport] Context ready.", flush=True)

    # Streaming shard writer: each hexagon is flushed to its shard part file as
    # soon as it is computed, so memory never scales with hexagon count.
    # scale_powers: remap every exported sp/cp to its per-key empirical quantile
    # so the values spread across [0, 1] (they otherwise cluster too tightly to
    # tell apart). Also dumps power_scaling.json next to the store for the
    # scaling dashboard. See utils/power_scaling.py.
    writer = HexShardWriter(cfg.hex_pois_dir, cfg.artifact_slug, scale_powers=True)

    nodes = ctx.nodes_with_coords
    total = len(nodes) if hasattr(nodes, "__len__") else None
    skipped = 0
    # Live progress bar over the node loop (each node reads its own .npz and
    # computes per-POI powers). Drives off the known node count so it shows a
    # real ETA instead of an occasional print, and updates in place in the
    # VS Code terminal. Matches the tqdm style used elsewhere in the pipeline.
    progress = tqdm(
        total=total,
        desc="[ScoreReport] scoring nodes",
        unit="node",
        file=sys.stderr,
        mininterval=0.5,
        dynamic_ncols=True,
    )
    for idx, (node_id, data) in enumerate(nodes, start=1):
        hex_id: str = data.get("hex_id") or str(node_id)
        acc_by_poi_id = _load_node_sparse(node_id, poi_by_node_dir)
        if not acc_by_poi_id:
            skipped += 1
        else:
            poi_entries: list[dict] = []
            for poi_id, acc_val in acc_by_poi_id.items():
                poi_info = poi_by_id.get(poi_id)
                if poi_info is None:
                    continue
                sp, cp = _compute_poi_powers(poi_id, poi_info, acc_val, drop_set)
                if sp or cp:
                    entry: dict = {"i": poi_id}
                    if sp:
                        entry["sp"] = sp
                    if cp:
                        entry["cp"] = cp
                    poi_entries.append(entry)

            if poi_entries:
                poi_entries.sort(key=lambda e: e["i"])
                writer.add_hexagon(hex_id, poi_entries)

        progress.update(1)
        if idx % 500 == 0:
            progress.set_postfix_str(f"{writer.hex_count} hexagons, {skipped} empty")
    progress.close()

    print(f"[ScoreReport] Finalizing {writer.hex_count} hexagons into shards (fitting scaler + writing)...", flush=True)
    info = writer.finalize(zip_path=cfg.hex_pois_zip_path)
    print(
        f"[ScoreReport] Done. hex_pois_dir={info['hex_pois_dir']}  "
        f"shards={info['shard_count']}  zip={info['hex_pois_zip_path']}",
        flush=True,
    )


if __name__ == "__main__":
    # Run directly (VS Code "Run Python File" / F5), no CLI args needed.
    # Change the city here to run a different one.
    generate_score_report(city="cagliari")
