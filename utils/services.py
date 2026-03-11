import csv
import json
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


def _config_error(row_num: int | None, column: str | None, message: str) -> ValueError:
    where = f"path={CONFIG_CSV_PATH}"
    if row_num is not None:
        where += f" row={row_num}"
    if column is not None:
        where += f" column={column}"
    return ValueError(f"Invalid POI config CSV ({where}): {message}")


def _parse_json_cell(raw: str, row_num: int, column: str) -> Any:
    try:
        return json.loads(raw)
    except Exception as exc:
        raise _config_error(row_num, column, f"expected valid JSON, got {raw!r}") from exc


def _validate_required_columns(fieldnames: list[str] | None) -> None:
    required = ["poi_type", "decay_constant", "choquet_capacity", "contribution_constant", "tags", "services"]
    if fieldnames is None:
        raise _config_error(None, None, f"missing header row, expected columns {required}")
    missing = [c for c in required if c not in fieldnames]
    if missing:
        raise _config_error(None, None, f"missing required columns {missing}; found {fieldnames}")


def _load_rows() -> list[dict[str, Any]]:
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

            choquet_capacity = _parse_json_cell(row.get("choquet_capacity") or "", idx, "choquet_capacity")
            if not isinstance(choquet_capacity, list) or not choquet_capacity:
                raise _config_error(idx, "choquet_capacity", "expected non-empty JSON array of numbers")
            if len(choquet_capacity) != len(services):
                raise _config_error(
                    idx,
                    "choquet_capacity",
                    f"length mismatch: len(services)={len(services)} len(choquet_capacity)={len(choquet_capacity)}",
                )
            choquet_capacity_floats: list[float] = []
            for j, value in enumerate(choquet_capacity):
                try:
                    choquet_capacity_floats.append(float(value))
                except Exception as exc:
                    raise _config_error(
                        idx,
                        "choquet_capacity",
                        f"entry {j} expected number, got {value!r}",
                    ) from exc

            contribution_constant = _parse_json_cell(row.get("contribution_constant") or "", idx, "contribution_constant")
            if not isinstance(contribution_constant, list) or not contribution_constant:
                raise _config_error(idx, "contribution_constant", "expected non-empty JSON array of numbers")
            if len(contribution_constant) != len(services):
                raise _config_error(
                    idx,
                    "contribution_constant",
                    f"length mismatch: len(services)={len(services)} len(contribution_constant)={len(contribution_constant)}",
                )
            contribution_constant_floats: list[float] = []
            for j, value in enumerate(contribution_constant):
                try:
                    contribution_constant_floats.append(float(value))
                except Exception as exc:
                    raise _config_error(
                        idx,
                        "contribution_constant",
                        f"entry {j} expected number, got {value!r}",
                    ) from exc

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
    return POI_DECAY_CONSTANTS[poi_type]


def get_contribution_constant(poi_type: str, service: str | None = None) -> float:
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
    return SERVICE_POI_QUERIES[service]


def get_service_poi_types() -> dict[str, list[str]]:
    return {
        service: [q.poi_type for q in queries]
        for service, queries in SERVICE_POI_QUERIES.items()
    }


def all_queries() -> list[PoiQuery]:
    return [q for group in SERVICE_POI_QUERIES.values() for q in group]


def unique_query_keys() -> list[PoiQuery]:
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
    tags_key = None
    if q.tags:
        tags_key = tuple(sorted(q.tags.items()))
    return (q.poi_type, tags_key)


def _service_idx_map(service: str) -> dict[str, int]:
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
    if len(S) == 0:
        return 0
    if len(S) == 1:
        return SERVICE_SINGLETON_M[service][S[0]]
    singletons = [SERVICE_SINGLETON_M[service][k] for k in S]
    m = max(singletons)
    return min(1, m + 0.2 * (1 - m))


def choquet_integral(x, service):
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
