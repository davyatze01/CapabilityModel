import ast
import csv
import json
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
    tags: dict | None = None


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


def _service_config_error(row_num: int | None, column: str | None, message: str) -> ValueError:
    where = f"path={SERVICES_CSV_PATH}"
    if row_num is not None:
        where += f" row={row_num}"
    if column is not None:
        where += f" column={column}"
    return ValueError(f"Invalid services CSV ({where}): {message}")


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
    required = ["poi_type", "decay_constant", "tags", "services"]
    if fieldnames is None:
        raise _config_error(None, None, f"missing header row, expected columns {required}")
    missing = [c for c in required if c not in fieldnames]
    if missing:
        raise _config_error(None, None, f"missing required columns {missing}; found {fieldnames}")


def _parse_list_cell(raw: str, row_num: int, column: str) -> list[Any]:
    try:
        value = ast.literal_eval(raw)
    except Exception as exc:
        raise _service_config_error(row_num, column, f"expected list, got {raw!r}") from exc
    if not isinstance(value, list):
        raise _service_config_error(row_num, column, f"expected list, got {type(value).__name__}")
    return value


def _load_service_weights() -> dict[str, dict[str, list[Any]]]:
    if not SERVICES_CSV_PATH.is_file():
        raise _service_config_error(None, None, f"file not found: {SERVICES_CSV_PATH}")

    service_map: dict[str, dict[str, list[Any]]] = {}
    with SERVICES_CSV_PATH.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise _service_config_error(None, None, "missing header row")
        for idx, row in enumerate(reader, start=2):
            service = (row.get("service") or "").strip()
            if not service:
                raise _service_config_error(idx, "service", "expected non-empty string")

            poi_types = _parse_list_cell(row.get("poi_types") or "", idx, "poi_types")
            choquet_capacity = _parse_list_cell(row.get("choquet_capacity") or "", idx, "choquet_capacity")
            contribution_constant = _parse_list_cell(row.get("contribution_constant") or "", idx, "contribution_constant")

            if len(poi_types) != len(choquet_capacity):
                raise _service_config_error(
                    idx,
                    "choquet_capacity",
                    f"length mismatch: len(poi_types)={len(poi_types)} len(choquet_capacity)={len(choquet_capacity)}",
                )
            if len(poi_types) != len(contribution_constant):
                raise _service_config_error(
                    idx,
                    "contribution_constant",
                    f"length mismatch: len(poi_types)={len(poi_types)} len(contribution_constant)={len(contribution_constant)}",
                )

            service_map[service] = {
                "poi_types": poi_types,
                "choquet_capacity": [float(v) for v in choquet_capacity],
                "contribution_constant": [float(v) for v in contribution_constant],
            }

    return service_map


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
    service_weights = _load_service_weights()

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

            choquet_capacity_floats: list[float] = []
            contribution_constant_floats: list[float] = []
            for service in services:
                weights = service_weights.get(service)
                if weights is None:
                    raise _config_error(idx, "services", f"service {service!r} not found in services.csv")
                poi_types = weights["poi_types"]
                if poi_type not in poi_types:
                    raise _config_error(
                        idx,
                        "services",
                        f"poi_type {poi_type!r} not listed for service {service!r} in services.csv",
                    )
                index = poi_types.index(poi_type)
                choquet_capacity_floats.append(float(weights["choquet_capacity"][index]))
                contribution_constant_floats.append(float(weights["contribution_constant"][index]))

            parsed_rows.append(
                {
                    "poi_type": poi_type,
                    "decay_constant": decay_constant,
                    "tags": tags,
                    "services": services,
                    "choquet_capacity": choquet_capacity_floats,
                    "contribution_constant": contribution_constant_floats,
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
    seen_pairs: set[tuple[str, str]] = set()

    for row in rows:
        poi_type = row["poi_type"]
        decay_constants[poi_type] = float(row["decay_constant"])
        tags = dict(row["tags"])
        services = row["services"]
        choquet_caps = row["choquet_capacity"]
        contribs = row["contribution_constant"]

        for service, choquet_weight, contrib_weight in zip(services, choquet_caps, contribs):
            pair = (service, poi_type)
            if pair in seen_pairs:
                raise _config_error(None, None, f"duplicate (service, poi_type) pair generated: {pair}")
            seen_pairs.add(pair)
            service_poi_queries.setdefault(service, []).append(PoiQuery(service=service, poi_type=poi_type, tags=tags))
            service_singleton_m.setdefault(service, {})[poi_type] = float(choquet_weight)
            contribution_constants.setdefault(service, {})[poi_type] = float(contrib_weight)

    return dict(service_poi_queries), dict(service_singleton_m), decay_constants, dict(contribution_constants)


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
SERVICE_POI_QUERIES, SERVICE_SINGLETON_M, POI_DECAY_CONSTANTS, SERVICE_CONTRIBUTION_CONSTANTS = _build_runtime_structures(_ROWS)
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


SPORT_AND_MOVEMENT_IDX = _service_idx_map("sport_and_movement")
SCENIC_VIEWS_IDX = _service_idx_map("scenic_views")
QUIETNESS_IDX = _service_idx_map("quietness")
CULTURAL_ACTIVITIES_IDX = _service_idx_map("cultural_activities")
NATURE_CONTACT_IDX = _service_idx_map("nature_contact")
EATING_OUT_IDX = _service_idx_map("eating_out")
FRESH_FOOD_ACCESS_IDX = _service_idx_map("fresh_food_access")
READY_FOOD_ACCESS_IDX = _service_idx_map("ready_food_access")
MEDICINES_AND_SUPPLIES_IDX = _service_idx_map("medicines_and_supplies")
IMPATIENT_AND_CARE_IDX = _service_idx_map("impatient_and_care")
REHABILITATION_SERVICES_IDX = _service_idx_map("rehabilitation_services")
DIAGNOSIS_AND_PREVENTION_IDX = _service_idx_map("diagnosis_and_prevention")
EMERGENCY_SERVICES_IDX = _service_idx_map("emergency_services")
CARE_SERVICES_IDX = _service_idx_map("care_services")


def cap(S, service):
    """Compute fuzzy measure for a subset of POI types in one service.

    Inputs:
    - S: subset/list of POI types.
    - service: service key.

    Outputs:
    - float fuzzy measure used by Choquet aggregation.
    """
    if len(S) == 0:
        return 0
    if len(S) == 1:
        return SERVICE_SINGLETON_M[service][S[0]]
    singletons = [SERVICE_SINGLETON_M[service][k] for k in S]
    m = max(singletons)
    return min(1, m + 0.2 * (1 - m))


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
