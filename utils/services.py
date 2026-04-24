import csv
import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_CSV_PATH = Path(__file__).resolve().parents[1] / "config" / "poi_types.csv"


@dataclass(frozen=True)
class PoiQuery:
    service: str
    poi_type: str
    tags: dict | None = None
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


def _validate_required_columns(fieldnames: list[str] | None) -> None:
    """Ensure required POI configuration columns are present.

    Inputs:
    - fieldnames: header field list from CSV reader.

    Outputs:
    - None. Raises ValueError if required columns are missing.
    """
    required = ["poi_type", "decay_constant", "choquet_capacity", "contribution_constant", "tags", "services"]
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

            tags = _parse_json_cell(row.get("tags") or "", idx, "tags")
            if not isinstance(tags, dict):
                raise _config_error(idx, "tags", f"expected JSON object, got {type(tags).__name__}")

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

            choquet_capacity_raw = _parse_json_cell(row.get("choquet_capacity") or "", idx, "choquet_capacity")
            if isinstance(choquet_capacity_raw, list):
                if not choquet_capacity_raw:
                    raise _config_error(idx, "choquet_capacity", "expected non-empty JSON array of numbers")
                if len(choquet_capacity_raw) != len(services):
                    raise _config_error(
                        idx,
                        "choquet_capacity",
                        f"length mismatch: len(services)={len(services)} len(choquet_capacity)={len(choquet_capacity_raw)}",
                    )
                choquet_capacity_floats: list[float] = []
                for j, value in enumerate(choquet_capacity_raw):
                    try:
                        choquet_capacity_floats.append(float(value))
                    except Exception as exc:
                        raise _config_error(
                            idx,
                            "choquet_capacity",
                            f"entry {j} expected number, got {value!r}",
                        ) from exc
            else:
                try:
                    choquet_capacity_floats = [float(choquet_capacity_raw)] * len(services)
                except Exception as exc:
                    raise _config_error(
                        idx,
                        "choquet_capacity",
                        f"expected number or JSON array of numbers, got {choquet_capacity_raw!r}",
                    ) from exc

            contribution_raw = _parse_json_cell(row.get("contribution_constant") or "", idx, "contribution_constant")
            if isinstance(contribution_raw, list):
                if not contribution_raw:
                    raise _config_error(idx, "contribution_constant", "expected non-empty JSON array of numbers")
                if len(contribution_raw) != len(services):
                    raise _config_error(
                        idx,
                        "contribution_constant",
                        f"length mismatch: len(services)={len(services)} len(contribution_constant)={len(contribution_raw)}",
                    )
                contribution_constant_floats: list[float] = []
                for j, value in enumerate(contribution_raw):
                    try:
                        contribution_constant_floats.append(float(value))
                    except Exception as exc:
                        raise _config_error(
                            idx,
                            "contribution_constant",
                            f"entry {j} expected number, got {value!r}",
                        ) from exc
            else:
                try:
                    contribution_constant_floats = [float(contribution_raw)] * len(services)
                except Exception as exc:
                    raise _config_error(
                        idx,
                        "contribution_constant",
                        f"expected number or JSON array of numbers, got {contribution_raw!r}",
                    ) from exc

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
                    "choquet_capacity": choquet_capacity_floats,
                    "contribution_constant": contribution_constant_floats,
                    "choquet_interactions": choquet_interactions,
                }
            )

    return parsed_rows


def _build_runtime_structures(rows: list[dict[str, Any]]):
    """Build runtime lookup structures used by service/capability stages.

    Inputs:
    - rows: validated row dictionaries from `_load_rows`.

    Outputs:
    - tuple containing service queries, singleton measures, decay constants, and contribution constants.
    """
    service_poi_queries: "OrderedDict[str, list[PoiQuery]]" = OrderedDict()
    service_singleton_m: "OrderedDict[str, dict[str, float]]" = OrderedDict()
    decay_constants: dict[str, float] = {}
    contribution_constants: dict[str, dict[str, float]] = {}
    interaction_rows: "OrderedDict[str, dict[str, list[Any] | None]]" = OrderedDict()
    seen_pairs: set[tuple[str, str]] = set()

    for row in rows:
        poi_type = row["poi_type"]
        decay_constants[poi_type] = float(row["decay_constant"])
        tags = dict(row["tags"])
        labels = tuple(row.get("labels", (poi_type,)))
        services = row["services"]
        choquet_caps = row["choquet_capacity"]
        contribs = row["contribution_constant"]
        interactions_row = row.get("choquet_interactions")

        for service, choquet_weight, contrib_weight in zip(services, choquet_caps, contribs):
            pair = (service, poi_type)
            if pair in seen_pairs:
                raise _config_error(None, None, f"duplicate (service, poi_type) pair generated: {pair}")
            seen_pairs.add(pair)
            service_poi_queries.setdefault(service, []).append(
                PoiQuery(service=service, poi_type=poi_type, tags=tags, labels=labels)
            )
            service_singleton_m.setdefault(service, {})[poi_type] = float(choquet_weight)
            contribution_constants.setdefault(service, {})[poi_type] = float(contrib_weight)
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


def get_service_queries(service: str) -> list[PoiQuery]:
    """Get ordered POI queries that contribute to one service.

    Inputs:
    - service: service key.

    Outputs:
    - list of `PoiQuery` definitions.
    """
    return SERVICE_POI_QUERIES[service]


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


def query_key(q: PoiQuery) -> tuple[str, tuple | None]:
    """Build hashable key for a POI query.

    Inputs:
    - q: POI query object.

    Outputs:
    - tuple key using poi_type and normalized tags.
    """
    tags_key = None
    if q.tags:
        tags_key = tuple(sorted(q.tags.items()))
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
    order = sorted(range(n), key=lambda i: x[i])
    x_sorted = [x[i] for i in order]
    poi_types = [q.poi_type for q in SERVICE_POI_QUERIES[service]]

    total = 0.0
    prev = 0.0
    for j in range(n):
        tail = [poi_types[i] for i in order[j:]]
        total += (x_sorted[j] - prev) * cap(tail, service)
        prev = x_sorted[j]
    return total


if __name__ == "__main__":
    import pprint

    pprint.pprint(get_service_poi_types(), sort_dicts=True)
