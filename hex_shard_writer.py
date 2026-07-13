"""Sharded, compressed storage for per-hexagon POI power records.

Replaces the one-JSONP-file-per-hexagon layout (schema hexagon_poi_powers_v1)
with bucketed shard files (schema hexagon_poi_powers_v2):

- Hexagons are grouped into shards by a pure function of the hex id
  (spatial blocks of SHARD_BLOCK x SHARD_BLOCK grid cells), so a reader can
  compute the shard filename directly from the hex id — O(1) retrieval, no
  lookup table needed.
- Records are compact-encoded: service/capability names are replaced by
  indices into ordered key lists stored once in the manifest, and power
  values are quantized to integers (value * SCALE, ~4 significant digits).
- Each shard's JSON payload is zlib-deflated and base64-wrapped in a JSONP
  call (`__onHexShardZ`), so the offline interface can still load it over
  file:// via <script> injection and inflate it in the browser.

Writing is streaming and memory-bounded (fail-fast friendly):
- pass 1: each hexagon is encoded and appended to its shard's `.part` temp
  file the moment it is produced — memory is O(one hexagon);
- pass 2 (finalize): shards are assembled one at a time — memory is
  O(one shard).

Record wire format inside a shard payload:
    {"<hex_id>": [<record>, ...], ...}
    record = poi_id (int, id-only export)
           | [poi_id, [svc_idx, q, svc_idx, q, ...], [cap_idx, q, ...]]
    where q = round(value * SCALE).
"""

import base64
import json
import os
import re
import shutil
import zipfile
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from utils import capabilities as cap_mod
from utils import services as serv

SCHEMA = "hexagon_poi_powers_v2"
# Hexes per shard side (4x4 grid cells per shard). Kept small on purpose: with
# thousands of POI records per hexagon, a decoded shard is the browser-side
# memory unit — 16 hexes/shard keeps one shard in the tens of MB of JS heap.
SHARD_BLOCK = 4
SCALE = 10000  # quantization: stored int = round(value * SCALE)
_HEX_ID_RE = re.compile(r"^H(\d+)_(\d+)$")

MANIFEST_CALLBACK = "__onHexPoisManifest"
SHARD_CALLBACK = "__onHexShardZ"


def shard_name_for_hex(hex_id: str, block: int = SHARD_BLOCK) -> str:
    """Pure function hex_id -> shard basename (no extension).

    Mirrored in hex_shard_loader.js (shardNameForHex) — keep in sync.
    """
    m = _HEX_ID_RE.match(hex_id)
    if m:
        a = int(m.group(1)) // block
        b = int(m.group(2)) // block
        return f"s{a:04d}_{b:04d}"
    # Fallback for non-grid ids: deterministic string hash, same in JS.
    h = 0
    for ch in hex_id:
        h = (h * 31 + ord(ch)) % 4096
    return f"sx{h:04d}"


def service_keys() -> list[str]:
    return list(serv.SERVICE_KEYS)


def capability_keys() -> list[str]:
    return list(cap_mod.CAPABILITY_SERVICES.keys())


def encode_records(
    entries: list[dict[str, Any]],
    svc_index: dict[str, int],
    cap_index: dict[str, int],
    scale: int = SCALE,
) -> list[Any]:
    """Encode [{"i": id, "sp": {...}, "cp": {...}}, ...] into compact records."""
    records: list[Any] = []
    for entry in entries:
        poi_id = int(entry["i"] if "i" in entry else entry["id"])
        sp_flat: list[int] = []
        for key, val in (entry.get("sp") or {}).items():
            q = int(round(float(val) * scale))
            if q > 0:
                sp_flat.extend((svc_index[key], q))
        cp_flat: list[int] = []
        for key, val in (entry.get("cp") or {}).items():
            q = int(round(float(val) * scale))
            if q > 0:
                cp_flat.extend((cap_index[key], q))
        if sp_flat or cp_flat:
            records.append([poi_id, sp_flat, cp_flat])
        else:
            records.append(poi_id)
    return records


def decode_records(
    records: list[Any],
    services: list[str],
    capabilities: list[str],
    scale: int = SCALE,
) -> list[dict[str, Any]]:
    """Inverse of encode_records -> [{"i": id, "sp": {...}, "cp": {...}}, ...]."""
    out: list[dict[str, Any]] = []
    for rec in records:
        if isinstance(rec, (int, float)):
            out.append({"i": int(rec)})
            continue
        poi_id, sp_flat, cp_flat = rec
        item: dict[str, Any] = {"i": int(poi_id)}
        if sp_flat:
            item["sp"] = {
                services[sp_flat[k]]: sp_flat[k + 1] / scale
                for k in range(0, len(sp_flat), 2)
            }
        if cp_flat:
            item["cp"] = {
                capabilities[cp_flat[k]]: cp_flat[k + 1] / scale
                for k in range(0, len(cp_flat), 2)
            }
        out.append(item)
    return out


class HexShardWriter:
    """Streaming writer for the sharded hex-POI store.

    Usage:
        writer = HexShardWriter(out_dir, slug)
        for each hexagon: writer.add_hexagon(hex_id, entries)
        info = writer.finalize(zip_path=...)
    """

    def __init__(self, out_dir: str, slug: str, block: int = SHARD_BLOCK, scale: int = SCALE):
        self.out_dir = out_dir
        self.slug = slug
        self.block = block
        self.scale = scale
        self.services = service_keys()
        self.capabilities = capability_keys()
        self._svc_index = {k: i for i, k in enumerate(self.services)}
        self._cap_index = {k: i for i, k in enumerate(self.capabilities)}
        self._hex_ids: list[str] = []
        self._tmp_dir = os.path.join(out_dir, "_parts_tmp")

        # Regenerate from scratch so hexagons/shards removed since a prior run
        # (including old v1 per-hex files) don't linger.
        if os.path.isdir(out_dir):
            shutil.rmtree(out_dir)
        os.makedirs(self._tmp_dir, exist_ok=True)

    def add_hexagon(self, hex_id: str, entries: list[dict[str, Any]]) -> None:
        """Encode one hexagon's entries and append them to its shard part file.

        Memory stays O(one hexagon): nothing is retained except the hex id
        (needed for the manifest).
        """
        if not entries:
            return
        records = encode_records(entries, self._svc_index, self._cap_index, self.scale)
        line = json.dumps([hex_id, records], ensure_ascii=False, separators=(",", ":"))
        shard = shard_name_for_hex(hex_id, self.block)
        with open(os.path.join(self._tmp_dir, f"{shard}.part"), "a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")
        self._hex_ids.append(hex_id)

    @property
    def hex_count(self) -> int:
        return len(self._hex_ids)

    def finalize(self, zip_path: str | None = None) -> dict[str, Any]:
        """Assemble each shard (one at a time), write the manifest, optionally zip."""
        shard_files: list[str] = []
        for part_name in sorted(os.listdir(self._tmp_dir)):
            if not part_name.endswith(".part"):
                continue
            shard = part_name[: -len(".part")]
            hexmap: dict[str, list[Any]] = {}
            with open(os.path.join(self._tmp_dir, part_name), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    hex_id, records = json.loads(line)
                    hexmap[hex_id] = records
            payload = json.dumps(hexmap, ensure_ascii=False, separators=(",", ":"))
            packed = base64.b64encode(zlib.compress(payload.encode("utf-8"), 9)).decode("ascii")
            out_name = f"{shard}.js"
            with open(os.path.join(self.out_dir, out_name), "w", encoding="utf-8") as f:
                f.write(f'{SHARD_CALLBACK}("{shard}","{packed}");')
            shard_files.append(out_name)
        shutil.rmtree(self._tmp_dir)

        manifest = {
            "schema": SCHEMA,
            "slug": self.slug,
            "count": len(self._hex_ids),
            "shard_count": len(shard_files),
            "shard": {"block": self.block},
            "encoding": "deflate-base64",
            "scale": self.scale,
            "services": self.services,
            "capabilities": self.capabilities,
            "hex_ids": sorted(self._hex_ids),
        }
        manifest_body = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(self.out_dir, "index.js"), "w", encoding="utf-8") as f:
            f.write(f"{MANIFEST_CALLBACK}({manifest_body});")

        if zip_path:
            # Shard payloads are already deflated; store instead of recompressing.
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
                for name in sorted(os.listdir(self.out_dir)):
                    if name.endswith(".js"):
                        zf.write(os.path.join(self.out_dir, name), arcname=name)

        return {
            "hex_pois_dir": self.out_dir,
            "hex_pois_zip_path": zip_path,
            "hex_pois_count": len(self._hex_ids),
            "shard_count": len(shard_files),
        }


# ---------------------------------------------------------------------------
# Read side (used by inspect_hex_pois.py and verification scripts)
# ---------------------------------------------------------------------------


def _read_source_file(source: Path, name: str) -> str:
    """Read one file from either the shard directory or the hex_pois zip."""
    if source.is_dir():
        path = source / name
        if not path.exists():
            raise FileNotFoundError(f"Hex shard file not found: {path}")
        return path.read_text(encoding="utf-8")
    if not source.exists():
        raise FileNotFoundError(f"Hex archive not found: {source}")
    with zipfile.ZipFile(source) as zf:
        try:
            return zf.read(name).decode("utf-8")
        except KeyError as exc:
            raise FileNotFoundError(f"File not found in zip: {name}") from exc


def _strip_jsonp(text: str, callback: str) -> str:
    """Return the raw argument list of a JSONP call (text between the parens)."""
    if not text.startswith(callback + "(") or not text.endswith(");"):
        raise ValueError(f"Unexpected JSONP format (expected {callback}(...);)")
    return text[len(callback) + 1 : -2]


def read_manifest(source: str | Path) -> dict[str, Any]:
    source = Path(source)
    payload = json.loads(_strip_jsonp(_read_source_file(source, "index.js"), MANIFEST_CALLBACK))
    if payload.get("schema") != SCHEMA:
        raise ValueError(
            f"Unsupported hex_pois schema {payload.get('schema')!r}; expected {SCHEMA}. "
            "Regenerate the export (score_report.py / the pipeline POI export)."
        )
    return payload


@lru_cache(maxsize=4)
def _load_shard_cached(source_str: str, shard: str) -> dict[str, list[Any]]:
    return load_shard(source_str, shard)


@lru_cache(maxsize=4)
def _read_manifest_cached(source_str: str) -> dict[str, Any]:
    return read_manifest(source_str)


def load_shard(source: str | Path, shard: str) -> dict[str, list[Any]]:
    """Load one shard -> {hex_id: [raw records]} (still compact-encoded)."""
    source = Path(source)
    inner = _strip_jsonp(_read_source_file(source, f"{shard}.js"), SHARD_CALLBACK)
    # JSONP args are `"<shard>","<base64>"` — wrap in [] to parse as JSON.
    args = json.loads(f"[{inner}]")
    if not isinstance(args, list) or len(args) != 2:
        raise ValueError(f"Unexpected shard payload for {shard}")
    packed = args[1]
    payload = zlib.decompress(base64.b64decode(packed)).decode("utf-8")
    return json.loads(payload)


def list_hex_ids(source: str | Path) -> list[str]:
    return list(read_manifest(source)["hex_ids"])


def load_hex_items(hex_id: str, source: str | Path, manifest: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Load and decode one hexagon's records: [{"i": id, "sp": {...}, "cp": {...}}, ...]."""
    source = Path(source)
    if manifest is None:
        manifest = _read_manifest_cached(str(source))
    shard = shard_name_for_hex(hex_id, int(manifest["shard"]["block"]))
    # Bounded cache: repeated lookups (e.g. inspect_hex_pois --all) hit the same
    # shard ~block^2 times in a row; caching a few shards avoids re-inflating.
    hexmap = _load_shard_cached(str(source), shard)
    if hex_id not in hexmap:
        raise FileNotFoundError(f"Hexagon {hex_id} not found in shard {shard}")
    return decode_records(
        hexmap[hex_id],
        manifest["services"],
        manifest["capabilities"],
        int(manifest["scale"]),
    )
