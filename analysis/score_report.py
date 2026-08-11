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
import multiprocessing as mp
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
from utils import graphml
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


def _precompute_poi_weights(
    poi_by_id: dict[int, dict],
    drop_set: dict[str, set[str]],
) -> dict[int, tuple[dict[str, float], dict[str, float]]]:
    """Per POI, the linear service_power/capability_power weight: power = acc_val * weight.

    Both powers are linear in the POI's own accessibility value (service_power sums
    singleton weights over poi_types; capability_power is a fixed linear combination of
    service_power via the ELECTRE weights), so the weight itself depends only on
    poi_types/source_key/drop_set -- never on which node or accessibility value is asking.
    The same POI is reachable from, and re-scored by, many nodes, so computing this once
    here (instead of inside the per-(node, POI) hot loop) turns that loop from a ~12-service
    x ~poi_types membership scan into a couple of dict lookups per POI.
    """
    weights: dict[int, tuple[dict[str, float], dict[str, float]]] = {}
    for poi_id, poi_info in poi_by_id.items():
        source_key = poi_info["source_key"]
        poi_types: list[str] = poi_info["poi_types"]

        svc_w: dict[str, float] = {}
        for service in serv.SERVICE_KEYS:
            singletons = serv.SERVICE_SINGLETON_M.get(service, {})
            w = 0.0
            for pt in poi_types:
                if pt not in singletons:
                    continue
                if source_key in drop_set.get(pt, ()):
                    continue
                w += float(singletons[pt])
            if w > 0.0:
                svc_w[service] = w

        cap_w: dict[str, float] = {}
        for capability, cap_services in cap_mod.CAPABILITY_SERVICES.items():
            cap_weights = cap_mod.CAP_ELECTRE_W[capability]
            w = sum(svc_w.get(svc, 0.0) * float(cap_weights.get(svc, 0.0)) for svc in cap_services)
            if w > 0.0:
                cap_w[capability] = w

        if svc_w or cap_w:
            weights[poi_id] = (svc_w, cap_w)
    return weights


# Populated in each worker process by _init_score_worker (via Pool initargs, pickled once
# per worker at pool startup, not per task).
_WORKER_POI_WEIGHTS: dict[int, tuple[dict[str, float], dict[str, float]]] = {}
_WORKER_POI_BY_NODE_DIR: str = ""


def _init_score_worker(
    poi_weights: dict[int, tuple[dict[str, float], dict[str, float]]],
    poi_by_node_dir: str,
) -> None:
    global _WORKER_POI_WEIGHTS, _WORKER_POI_BY_NODE_DIR
    _WORKER_POI_WEIGHTS = poi_weights
    _WORKER_POI_BY_NODE_DIR = poi_by_node_dir


def _score_node_worker(args: tuple) -> tuple[str, list[dict]] | None:
    """Score one node: load its sparse accessibility, apply the precomputed per-POI
    weights, and return its hexagon's POI entries (or None if it contributes nothing)."""
    node_id, hex_id = args
    acc_by_poi_id = _load_node_sparse(node_id, _WORKER_POI_BY_NODE_DIR)
    if not acc_by_poi_id:
        return None

    poi_entries: list[dict] = []
    for poi_id, acc_val in acc_by_poi_id.items():
        weights = _WORKER_POI_WEIGHTS.get(poi_id)
        if weights is None:
            continue
        svc_w, cap_w = weights
        entry: dict = {"i": poi_id}
        if svc_w:
            entry["sp"] = {k: round(acc_val * w, 7) for k, w in svc_w.items()}
        if cap_w:
            entry["cp"] = {k: round(acc_val * w, 7) for k, w in cap_w.items()}
        if "sp" in entry or "cp" in entry:
            poi_entries.append(entry)

    if not poi_entries:
        return None
    poi_entries.sort(key=lambda e: e["i"])
    return hex_id, poi_entries


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
    # resume=True: if a prior run's node-scoring loop finished cleanly and left
    # its part files + completion marker behind (see mark_scoring_complete),
    # skip straight to finalize() instead of repeating the scoring loop, which
    # is the expensive part of this script (hours, one npz read + POI-power
    # computation per node).
    writer = HexShardWriter(cfg.hex_pois_dir, cfg.artifact_slug, scale_powers=True, resume=True)

    if writer.resumed:
        print(
            f"[ScoreReport] Resuming from a completed scoring pass "
            f"({writer.hex_count} hexagons already scored) -- skipping straight to finalize.",
            flush=True,
        )
    else:
        nodes = ctx.nodes_with_coords
        node_args = [(node_id, data.get("hex_id") or str(node_id)) for node_id, data in nodes]
        total = len(node_args)
        skipped = 0

        print("[ScoreReport] Precomputing per-POI service/capability weights...", flush=True)
        poi_weights = _precompute_poi_weights(poi_by_id, drop_set)

        # Node scoring is embarrassingly parallel (each node reads its own .npz and is
        # otherwise independent), so it's farmed out to a worker pool the same way the
        # routing/accessibility stages are -- this is normally the slowest part of this
        # script by far. ctx.workers is already sized for this machine's cores/memory by
        # build_context, but capped again here by score_report_max_workers (see its
        # definition in config.py) since this pool hasn't been proven safe yet at full
        # worker count the way non_bus_max_workers was tuned down after a real OOM.
        # Only the main process touches `writer` (shard part-file appends aren't safe to
        # parallelize), so workers just return each node's computed entries.
        pool_workers = max(1, min(ctx.workers, ctx.config.score_report_max_workers, total or 1))
        chunksize = max(1, total // (pool_workers * 4)) if total else 1

        # Workers here only read per-node .npz files and the precomputed weights dict --
        # they never touch a NetworkX graph. But this script commonly runs at the tail of
        # a long-lived main.py process, where the full mode graphs non_bus_routing_stage
        # loaded earlier can still be cached in-process. Forking a pool without releasing
        # them first inherits that multi-GB baseline copy-on-write, which turns into N
        # private copies as each worker's refcounting touches pages -- this is the exact
        # OOM pattern non_bus_max_workers exists to bound in that stage's own pool; clear
        # it here too instead of re-learning that lesson in a second stage.
        graphml.clear_mode_graph_cache()
        progress = tqdm(
            total=total,
            desc="[ScoreReport] scoring nodes",
            unit="node",
            file=sys.stderr,
            mininterval=0.5,
            dynamic_ncols=True,
        )
        with mp.Pool(
            processes=pool_workers,
            initializer=_init_score_worker,
            initargs=(poi_weights, poi_by_node_dir),
        ) as pool:
            for idx, result in enumerate(
                pool.imap_unordered(_score_node_worker, node_args, chunksize=chunksize), start=1
            ):
                if result is None:
                    skipped += 1
                else:
                    hex_id, poi_entries = result
                    writer.add_hexagon(hex_id, poi_entries)
                progress.update(1)
                if idx % 500 == 0:
                    progress.set_postfix_str(f"{writer.hex_count} hexagons, {skipped} empty")
        progress.close()
        writer.mark_scoring_complete()

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
