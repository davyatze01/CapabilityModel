import csv
import hashlib
import json
import re
import ast
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_CSV_PATH = Path(__file__).resolve().parents[1] / "config" / "poi_types.csv"
SERVICES_CSV_PATH = Path(__file__).resolve().parents[1] / "config" / "services.csv"


@dataclass(frozen=True)
class PoiQuery:
    service: str
    poi_type: str
    tags: dict[str, Any] | list[dict[str, Any]] | None = None
    labels: tuple[str, ...] = ()


def _config_error(row_num: int | None, column: str | None, message: str) -> ValueError:
    """Build standardized configuration error with CSV location context.

    Inputs:
    - row_num: optional CSV row number.
    - column: optional CSV column name.
    - message: validation message.

    Outputs:
    - ValueError ready to be raised by callers.
    """
    where = f"path={CONFIG_CSV_PATH}"
    if row_num is not None:
        where += f" row={row_num}"
    if column is not None:
        where += f" column={column}"
    return ValueError(f"Invalid POI config CSV ({where}): {message}")


def _parse_json_cell(raw: str, row_num: int, column: str) -> Any:
    """Parse JSON from a CSV cell and raise contextual errors.

    Inputs:
    - raw: raw JSON text from CSV cell.
    - row_num: source row number.
    - column: source column name.

    Outputs:
    - parsed JSON value.
    """
    try:
        return json.loads(raw)
    except Exception as exc:
        raise _config_error(row_num, column, f"expected valid JSON, got {raw!r}") from exc


def _normalize_tags_dict(tags: Any, row_num: int) -> dict[str, Any]:
    """Normalize one tags-dict clause into validated key/value shapes.

    Inputs:
    - tags: parsed JSON value expected to be a dictionary.
    - row_num: source CSV row number.

    Outputs:
    - normalized dict with string keys and values as bool/str/list[str].
    """
    if not isinstance(tags, dict) or not tags:
        raise _config_error(row_num, "tags", "expected non-empty JSON object")
    out: dict[str, Any] = {}
    for key, value in tags.items():
        key_s = str(key).strip()
        if not key_s:
            raise _config_error(row_num, "tags", f"invalid empty tag key: {key!r}")
        if isinstance(value, list):
            if not value:
                raise _config_error(row_num, "tags", f"tag {key_s!r} has empty array")
            vals: list[str] = []
            for item in value:
                item_s = str(item).strip()
                if not item_s:
                    raise _config_error(row_num, "tags", f"tag {key_s!r} has empty value in array")
                vals.append(item_s)
            out[key_s] = vals
        elif isinstance(value, bool):
            out[key_s] = value
        else:
            value_s = str(value).strip()
            if not value_s:
                raise _config_error(row_num, "tags", f"tag {key_s!r} has empty scalar value")
            out[key_s] = value_s
    return out


def _normalize_tags_cell(tags_raw: Any, row_num: int) -> dict[str, Any] | list[dict[str, Any]]:
    """Normalize tags payload supporting AND dict or OR list-of-dicts.

    Inputs:
    - tags_raw: parsed JSON from CSV `tags` column.
    - row_num: source CSV row number.

    Outputs:
    - dict for one AND clause, or list[dict] for OR across clauses.
    """
    if isinstance(tags_raw, dict):
        return _normalize_tags_dict(tags_raw, row_num)
    if isinstance(tags_raw, list):
        if not tags_raw:
            raise _config_error(row_num, "tags", "expected non-empty JSON array of objects")
        clauses: list[dict[str, Any]] = []
        for i, clause in enumerate(tags_raw):
            try:
                clauses.append(_normalize_tags_dict(clause, row_num))
            except ValueError as exc:
                raise _config_error(row_num, "tags", f"invalid OR clause at index {i}: {exc}") from exc
        return clauses
    raise _config_error(row_num, "tags", f"expected JSON object or array of objects, got {type(tags_raw).__name__}")


def _validate_required_columns(fieldnames: list[str] | None) -> None:
    """Ensure required POI configuration columns are present.

    Inputs:
    - fieldnames: header field list from CSV reader.

    Outputs:
    - None. Raises ValueError if required columns are missing.
    """
    required = ["poi_type", "decay_constant", "tags", "services"]
    if fieldnames is None:
        raise _config_error(None, None, f"missing header row, expected columns {required}")
    missing = [c for c in required if c not in fieldnames]
    if missing:
        raise _config_error(None, None, f"missing required columns {missing}; found {fieldnames}")


def _load_rows() -> list[dict[str, Any]]:
    """Load and validate POI configuration rows from CSV.

    Inputs:
    - none.

    Outputs:
    - list of normalized row dictionaries with parsed numeric/JSON fields.
    """
    if not CONFIG_CSV_PATH.is_file():
        raise _config_error(None, None, f"file not found: {CONFIG_CSV_PATH}")

    parsed_rows: list[dict[str, Any]] = []
    seen_poi_types: set[str] = set()

    with CONFIG_CSV_PATH.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames) if reader.fieldnames is not None else None
        _validate_required_columns(fieldnames)
        for idx, row in enumerate(reader, start=2):
            poi_type = (row.get("poi_type") or "").strip()
            if not poi_type:
                raise _config_error(idx, "poi_type", "expected non-empty string")
            if poi_type in seen_poi_types:
                raise _config_error(idx, "poi_type", f"duplicate poi_type {poi_type!r}")
            seen_poi_types.add(poi_type)

            try:
                decay_constant = float((row.get("decay_constant") or "").strip())
            except Exception as exc:
                raise _config_error(idx, "decay_constant", f"expected float, got {row.get('decay_constant')!r}") from exc

            tags_raw = _parse_json_cell(row.get("tags") or "", idx, "tags")
            tags = _normalize_tags_cell(tags_raw, idx)

            labels_raw = (row.get("labels") or "").strip()
            if labels_raw:
                labels = _parse_json_cell(labels_raw, idx, "labels")
                if not isinstance(labels, list) or not labels:
                    raise _config_error(idx, "labels", "expected non-empty JSON array of strings")
                if not all(isinstance(label, str) and label.strip() for label in labels):
                    raise _config_error(idx, "labels", "expected non-empty strings in array")
                normalized_labels = [label.strip() for label in labels]
                if len(set(normalized_labels)) != len(normalized_labels):
                    raise _config_error(idx, "labels", f"duplicate labels in row: {normalized_labels}")
            else:
                normalized_labels = [poi_type]

            services = _parse_json_cell(row.get("services") or "", idx, "services")
            if not isinstance(services, list) or not services:
                raise _config_error(idx, "services", "expected non-empty JSON array of strings")
            if not all(isinstance(s, str) and s.strip() for s in services):
                raise _config_error(idx, "services", "expected non-empty strings in array")
            services = [s.strip() for s in services]
            if len(set(services)) != len(services):
                raise _config_error(idx, "services", f"duplicate service names in row: {services}")
            for s in services:
                if not s.isidentifier():
                    raise _config_error(idx, "services", f"service name must be a valid identifier, got {s!r}")

            interactions_cell = (row.get("choquet_interactions") or "").strip()
            if interactions_cell:
                interactions_normalized = re.sub(r"\bNone\b", "null", interactions_cell)
                choquet_interactions = _parse_json_cell(interactions_normalized, idx, "choquet_interactions")
                if not isinstance(choquet_interactions, list) or not choquet_interactions:
                    raise _config_error(idx, "choquet_interactions", "expected non-empty JSON-like array")
            else:
                choquet_interactions = None

            parsed_rows.append(
                {
                    "poi_type": poi_type,
                    "decay_constant": decay_constant,
                    "tags": tags,
                    "labels": tuple(normalized_labels),
                    "services": services,
                    "choquet_interactions": choquet_interactions,
                }
            )

    return parsed_rows


def _parse_python_list_cell(raw: str, row_num: int, column: str) -> list[Any]:
    try:
        parsed = ast.literal_eval(raw)
    except Exception as exc:
        raise ValueError(
            f"Invalid services config CSV (path={SERVICES_CSV_PATH} row={row_num} column={column}): "
            f"expected Python list literal, got {raw!r}"
        ) from exc
    if not isinstance(parsed, list) or not parsed:
        raise ValueError(
            f"Invalid services config CSV (path={SERVICES_CSV_PATH} row={row_num} column={column}): "
            "expected non-empty list"
        )
    return parsed


def _load_service_weights(
    valid_poi_types: set[str],
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]], dict[str, list[str]]]:
    if not SERVICES_CSV_PATH.is_file():
        raise ValueError(f"Invalid services config CSV (path={SERVICES_CSV_PATH}): file not found")

    service_singleton_m: "OrderedDict[str, dict[str, float]]" = OrderedDict()
    contribution_constants: "OrderedDict[str, dict[str, float]]" = OrderedDict()
    service_poi_order: "OrderedDict[str, list[str]]" = OrderedDict()

    with SERVICES_CSV_PATH.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames) if reader.fieldnames is not None else None
        required = ["service", "poi_types", "choquet_capacity", "contribution_constant"]
        if fieldnames is None:
            raise ValueError(
                f"Invalid services config CSV (path={SERVICES_CSV_PATH}): missing header row, expected {required}"
            )
        missing = [c for c in required if c not in fieldnames]
        if missing:
            raise ValueError(
                f"Invalid services config CSV (path={SERVICES_CSV_PATH}): missing required columns {missing}; found {fieldnames}"
            )

        for idx, row in enumerate(reader, start=2):
            service = (row.get("service") or "").strip()
            if not service:
                raise ValueError(
                    f"Invalid services config CSV (path={SERVICES_CSV_PATH} row={idx} column=service): expected non-empty string"
                )
            poi_types_raw = (row.get("poi_types") or "").strip()
            choquet_raw = (row.get("choquet_capacity") or "").strip()
            contribution_raw = (row.get("contribution_constant") or "").strip()
            poi_types = _parse_python_list_cell(poi_types_raw, idx, "poi_types")
            choquet_values = _parse_python_list_cell(choquet_raw, idx, "choquet_capacity")
            contribution_values = _parse_python_list_cell(contribution_raw, idx, "contribution_constant")

            if len(poi_types) != len(choquet_values) or len(poi_types) != len(contribution_values):
                raise ValueError(
                    f"Invalid services config CSV (path={SERVICES_CSV_PATH} row={idx}): length mismatch "
                    f"poi_types={len(poi_types)} choquet_capacity={len(choquet_values)} "
                    f"contribution_constant={len(contribution_values)}"
                )
            poi_types_clean = [str(p).strip() for p in poi_types]
            if not all(poi_types_clean):
                raise ValueError(
                    f"Invalid services config CSV (path={SERVICES_CSV_PATH} row={idx} column=poi_types): expected non-empty strings"
                )
            if len(set(poi_types_clean)) != len(poi_types_clean):
                raise ValueError(
                    f"Invalid services config CSV (path={SERVICES_CSV_PATH} row={idx} column=poi_types): duplicate POI types"
                )
            for poi in poi_types_clean:
                if poi not in valid_poi_types:
                    raise ValueError(
                        f"Invalid services config CSV (path={SERVICES_CSV_PATH} row={idx} column=poi_types): "
                        f"unknown poi_type {poi!r}"
                    )

            service_poi_order[service] = poi_types_clean
            service_singleton_m[service] = {}
            contribution_constants[service] = {}
            for poi, cap_v, contrib_v in zip(poi_types_clean, choquet_values, contribution_values):
                service_singleton_m[service][poi] = float(cap_v)
                contribution_constants[service][poi] = float(contrib_v)

    return dict(service_singleton_m), dict(contribution_constants), dict(service_poi_order)


def _build_runtime_structures(rows: list[dict[str, Any]]):
    """Build runtime lookup structures used by service/capability stages.

    Inputs:
    - rows: validated row dictionaries from `_load_rows`.

    Outputs:
    - tuple containing service queries, singleton measures, decay constants, and contribution constants.
    """
    poi_by_type: dict[str, dict[str, Any]] = {r["poi_type"]: r for r in rows}
    valid_poi_types = set(poi_by_type.keys())
    service_singleton_m, contribution_constants, service_poi_order = _load_service_weights(valid_poi_types)
    service_poi_queries: "OrderedDict[str, list[PoiQuery]]" = OrderedDict()
    decay_constants: dict[str, float] = {}
    interaction_rows: "OrderedDict[str, dict[str, list[Any] | None]]" = OrderedDict()
    seen_pairs: set[tuple[str, str]] = set()

    for row in rows:
        poi_type = row["poi_type"]
        decay_constants[poi_type] = float(row["decay_constant"])
    for service, ordered_poi_types in service_poi_order.items():
        for poi_type in ordered_poi_types:
            row = poi_by_type[poi_type]
            tags_raw = row["tags"]
            if isinstance(tags_raw, dict):
                tags = dict(tags_raw)
            elif isinstance(tags_raw, list):
                tags = [dict(clause) for clause in tags_raw]
            else:
                raise _config_error(None, "tags", f"unexpected normalized tags type: {type(tags_raw).__name__}")
            labels = tuple(row.get("labels", (poi_type,)))
            if service not in row["services"]:
                raise _config_error(
                    None,
                    "services",
                    f"service {service!r} in services.csv is not listed for poi_type {poi_type!r} in poi_types.csv",
                )
            interactions_row = row.get("choquet_interactions")
            pair = (service, poi_type)
            if pair in seen_pairs:
                raise _config_error(None, None, f"duplicate (service, poi_type) pair generated: {pair}")
            seen_pairs.add(pair)
            service_poi_queries.setdefault(service, []).append(
                PoiQuery(service=service, poi_type=poi_type, tags=tags, labels=labels)
            )
            interaction_rows.setdefault(service, {})[poi_type] = interactions_row

    service_pairwise_m: "OrderedDict[str, dict[tuple[str, str], float]]" = OrderedDict()
    for service, queries in service_poi_queries.items():
        poi_types = [q.poi_type for q in queries]
        n = len(poi_types)
        pair_values: dict[tuple[int, int], list[float]] = {}
        for i, poi in enumerate(poi_types):
            row_vals = interaction_rows.get(service, {}).get(poi)
            if row_vals is None:
                # Backward-compatible default: no pairwise interactions.
                row_vals = [None if j == i else 0.0 for j in range(n)]
            if len(row_vals) != n:
                raise _config_error(
                    None,
                    "choquet_interactions",
                    f"service {service!r}, poi_type {poi!r}: expected length {n}, got {len(row_vals)}",
                )
            for j, raw_val in enumerate(row_vals):
                if i == j:
                    if raw_val is not None:
                        raise _config_error(
                            None,
                            "choquet_interactions",
                            f"service {service!r}, poi_type {poi!r}: diagonal entry at index {j} must be None/null",
                        )
                    continue
                if raw_val is None:
                    raise _config_error(
                        None,
                        "choquet_interactions",
                        f"service {service!r}, poi_type {poi!r}: off-diagonal entry at index {j} cannot be None/null",
                    )
                try:
                    value = float(raw_val)
                except Exception as exc:
                    raise _config_error(
                        None,
                        "choquet_interactions",
                        f"service {service!r}, poi_type {poi!r}, index {j}: expected numeric, got {raw_val!r}",
                    ) from exc
                key = (i, j) if i < j else (j, i)
                pair_values.setdefault(key, []).append(value)

        service_pairwise_m[service] = {}
        for i in range(n):
            for j in range(i + 1, n):
                vals = pair_values.get((i, j), [])
                if not vals:
                    pair_val = 0.0
                else:
                    # Use the mean when both directional entries are provided.
                    pair_val = float(sum(vals) / len(vals))
                service_pairwise_m[service][(poi_types[i], poi_types[j])] = float(pair_val)

    return (
        dict(service_poi_queries),
        dict(service_singleton_m),
        decay_constants,
        dict(contribution_constants),
        dict(service_pairwise_m),
    )


def _bootstrap_compatibility_checks() -> None:
    """Validate consistency between configured services and capability mappings.

    Inputs:
    - none.

    Outputs:
    - None. Raises ValueError if compatibility checks fail.
    """
    try:
        from utils import capabilities as _cap
    except Exception as exc:
        raise _config_error(None, None, f"failed to import utils.capabilities for compatibility checks: {exc}") from exc

    capability_services = set()
    for services in _cap.CAPABILITY_SERVICES.values():
        capability_services.update(services)

    for service in capability_services:
        if service not in SERVICE_POI_QUERIES:
            raise _config_error(None, None, f"service {service!r} referenced by capabilities has no POIs in CSV")
        if not SERVICE_POI_QUERIES[service]:
            raise _config_error(None, None, f"service {service!r} has empty POI list")

    for service, queries in SERVICE_POI_QUERIES.items():
        for q in queries:
            if q.poi_type not in SERVICE_SINGLETON_M.get(service, {}):
                raise _config_error(None, None, f"missing contribution weight for ({service}, {q.poi_type})")


def get_decay_constant(poi_type: str) -> float:
    """Return decay constant configured for a POI type.

    Inputs:
    - poi_type: POI type key.

    Outputs:
    - float decay constant.
    """
    return POI_DECAY_CONSTANTS[poi_type]


def get_contribution_constant(poi_type: str, service: str | None = None) -> float:
    """Return contribution constant for a POI type, optionally scoped by service.

    Inputs:
    - poi_type: POI type key.
    - service: optional service key to disambiguate per-service constants.

    Outputs:
    - float contribution constant.
    """
    if service is not None:
        return SERVICE_CONTRIBUTION_CONSTANTS[service][poi_type]
    matches = []
    for svc, weights in SERVICE_CONTRIBUTION_CONSTANTS.items():
        if poi_type in weights:
            matches.append(float(weights[poi_type]))
    if not matches:
        raise KeyError(f"No contribution_constant found for poi_type={poi_type!r}")
    first = matches[0]
    if any(abs(v - first) > 1e-12 for v in matches[1:]):
        raise ValueError(
            f"Contribution constants differ across services for poi_type={poi_type!r}; "
            "call get_contribution_constant(poi_type, service=...)"
        )
    return first


def _compute_config_signature() -> str:
    """Compute deterministic signature for current POI config CSV bytes.

    Inputs:
    - none.

    Outputs:
    - SHA1 hex digest for `config/poi_types.csv`.
    """
    data = CONFIG_CSV_PATH.read_bytes() + b"\n--services--\n" + SERVICES_CSV_PATH.read_bytes()
    return hashlib.sha1(data).hexdigest()


_ROWS = _load_rows()
(
    SERVICE_POI_QUERIES,
    SERVICE_SINGLETON_M,
    POI_DECAY_CONSTANTS,
    SERVICE_CONTRIBUTION_CONSTANTS,
    SERVICE_PAIRWISE_M,
) = _build_runtime_structures(_ROWS)
SERVICE_KEYS = list(SERVICE_POI_QUERIES.keys())
_bootstrap_compatibility_checks()
POI_CONFIG_SIGNATURE = _compute_config_signature()


def get_service_queries(service: str) -> list[PoiQuery]:
    """Get ordered POI queries that contribute to one service.

    Inputs:
    - service: service key.

    Outputs:
    - list of `PoiQuery` definitions.
    """
    return SERVICE_POI_QUERIES[service]


def config_signature() -> str:
    """Return current POI configuration signature.

    Inputs:
    - none.

    Outputs:
    - SHA1 signature string of current CSV bytes.
    """
    return POI_CONFIG_SIGNATURE


def get_service_poi_types() -> dict[str, list[str]]:
    """Return POI-type lists grouped by service.

    Inputs:
    - none.

    Outputs:
    - mapping service -> ordered list of POI types.
    """
    return {
        service: [q.poi_type for q in queries]
        for service, queries in SERVICE_POI_QUERIES.items()
    }


def all_queries() -> list[PoiQuery]:
    """Return flat list of all service-specific POI queries.

    Inputs:
    - none.

    Outputs:
    - list of `PoiQuery`.
    """
    return [q for group in SERVICE_POI_QUERIES.values() for q in group]


def unique_query_keys() -> list[PoiQuery]:
    """Return deduplicated POI queries across services.

    Inputs:
    - none.

    Outputs:
    - list of unique `PoiQuery`, preserving first-seen order.
    """
    seen = set()
    out = []
    for q in all_queries():
        key = query_key(q)
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


def _freeze_json_like(value: Any) -> Any:
    """Convert JSON-like values to hashable immutable structures.

    Inputs:
    - value: scalar, list, or dict parsed from JSON.

    Outputs:
    - hashable canonical representation preserving value semantics.
    """
    if isinstance(value, dict):
        return tuple((str(k), _freeze_json_like(v)) for k, v in sorted(value.items(), key=lambda kv: str(kv[0])))
    if isinstance(value, list):
        return tuple(_freeze_json_like(v) for v in value)
    return value


def query_key(q: PoiQuery) -> tuple[str, tuple | None]:
    """Build hashable key for a POI query.

    Inputs:
    - q: POI query object.

    Outputs:
    - tuple key using poi_type and normalized tags.
    """
    tags_key = None
    if q.tags:
        tags_key = _freeze_json_like(q.tags)
    return (q.poi_type, tags_key)


def _service_idx_map(service: str) -> dict[str, int]:
    """Create POI-type to index mapping for one service.

    Inputs:
    - service: service key.

    Outputs:
    - dict mapping POI type -> positional index.
    """
    return {q.poi_type: i for i, q in enumerate(SERVICE_POI_QUERIES[service])}


def _service_idx_map_optional(service: str) -> dict[str, int]:
    """Return service index map when available, else empty mapping.

    Inputs:
    - service: service key.

    Outputs:
    - dict mapping POI type -> positional index, or empty dict when missing.
    """
    if service not in SERVICE_POI_QUERIES:
        return {}
    return _service_idx_map(service)


SPORT_AND_MOVEMENT_IDX = _service_idx_map("sport_and_movement")
SCENIC_VIEWS_IDX = _service_idx_map("scenic_views")
QUIETNESS_IDX = _service_idx_map("quietness")
CULTURAL_ACTIVITIES_IDX = _service_idx_map("cultural_activities")
NATURE_CONTACT_IDX = _service_idx_map("nature_contact")
EATING_OUT_IDX = _service_idx_map("eating_out")
FOOD_ACCESS_IDX = _service_idx_map_optional("food_access")
MEDICINES_AND_SUPPLIES_IDX = _service_idx_map("medicines_and_supplies")
DIAGNOSIS_AND_PREVENTION_IDX = _service_idx_map_optional("diagnosis_and_prevention")
EMERGENCY_SERVICES_IDX = _service_idx_map("emergency_services")
CARE_SERVICES_IDX = _service_idx_map("care_services")
IMPATIENT_AND_REHABILITATION_SERVICE_IDX = _service_idx_map_optional("impatient_and_rehabilitation")

# Backward-compatible aliases for legacy service names.
FRESH_FOOD_ACCESS_IDX = FOOD_ACCESS_IDX
READY_FOOD_ACCESS_IDX = FOOD_ACCESS_IDX
IMPATIENT_AND_REHABILITATION_IDX = IMPATIENT_AND_REHABILITATION_SERVICE_IDX
IMPATIENT_AND_CARE_IDX = IMPATIENT_AND_REHABILITATION_SERVICE_IDX
REHABILITATION_SERVICES_IDX = IMPATIENT_AND_REHABILITATION_SERVICE_IDX


def cap(S, service):
    """Compute fuzzy measure for a subset of POI types in one service.

    Inputs:
    - S: subset/list of POI types.
    - service: service key.

    Outputs:
    - float fuzzy measure used by Choquet aggregation.
    """
    if len(S) == 0:
        return 0.0
    mu = 0.0
    subset = list(S)
    for i, poi_i in enumerate(subset):
        mu += float(SERVICE_SINGLETON_M[service][poi_i])
        for poi_j in subset[i + 1 :]:
            pair = (poi_i, poi_j) if poi_i < poi_j else (poi_j, poi_i)
            mu += float(SERVICE_PAIRWISE_M.get(service, {}).get(pair, 0.0))
    return mu


def choquet_integral(x, service):
    """Aggregate POI-type accessibility values into one service score.

    Inputs:
    - x: POI accessibility values aligned to service POI order.
    - service: service key.

    Outputs:
    - float aggregated service score.
    """
    n = len(x)
    if n == 0:
        return 0.0
    # Accessibility inputs are expected in [0, 1], but clamp defensively.
    x_clamped = [max(0.0, min(1.0, float(v))) for v in x]
    order = sorted(range(n), key=lambda i: x_clamped[i])
    x_sorted = [x_clamped[i] for i in order]
    poi_types = [q.poi_type for q in SERVICE_POI_QUERIES[service]]

    total = 0.0
    prev = 0.0
    for j in range(n):
        tail = [poi_types[i] for i in order[j:]]
        total += (x_sorted[j] - prev) * cap(tail, service)
        prev = x_sorted[j]

    # Normalize by measure of the full set so service scores are bounded in [0, 1].
    mu_full = float(cap(poi_types, service))
    if mu_full <= 0.0:
        return 0.0
    normalized = total / mu_full
    return max(0.0, min(1.0, normalized))


def choquet_integral_details(x, service):
    """Return a detailed Choquet integral breakdown for debugging."""
    n = len(x)
    if n == 0:
        return {
            "service": service,
            "input": [],
            "sorted_indices": [],
            "sorted_values": [],
            "poi_types": [],
            "steps": [],
            "mu_full": 0.0,
            "total": 0.0,
            "normalized": 0.0,
        }

    x_clamped = [max(0.0, min(1.0, float(v))) for v in x]
    order = sorted(range(n), key=lambda i: x_clamped[i])
    x_sorted = [x_clamped[i] for i in order]
    poi_types = [q.poi_type for q in SERVICE_POI_QUERIES[service]]

    steps = []
    total = 0.0
    prev = 0.0
    for j in range(n):
        tail_indices = order[j:]
        tail = [poi_types[i] for i in tail_indices]
        tail_cap = float(cap(tail, service))
        delta = float(x_sorted[j] - prev)
        term = delta * tail_cap
        total += term
        steps.append(
            {
                "j": j,
                "index": int(order[j]),
                "value": float(x_sorted[j]),
                "prev": float(prev),
                "delta": float(delta),
                "tail": tail,
                "capacity": tail_cap,
                "term": float(term),
                "running_total": float(total),
            }
        )
        prev = x_sorted[j]

    mu_full = float(cap(poi_types, service))
    normalized = 0.0 if mu_full <= 0.0 else total / mu_full
    return {
        "service": service,
        "input": [float(v) for v in x_clamped],
        "sorted_indices": [int(i) for i in order],
        "sorted_values": [float(v) for v in x_sorted],
        "poi_types": list(poi_types),
        "steps": steps,
        "mu_full": float(mu_full),
        "total": float(total),
        "normalized": float(max(0.0, min(1.0, normalized))),
    }


if __name__ == "__main__":
    import pprint

    pprint.pprint(get_service_poi_types(), sort_dicts=True)
