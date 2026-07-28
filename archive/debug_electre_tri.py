import argparse
import csv
import json
from pathlib import Path

from utils import capabilities as cap


def _resolve_capability_csv(path_arg: str | None) -> Path:
    if path_arg:
        path = Path(path_arg)
        if not path.exists():
            raise FileNotFoundError(f"Capability CSV not found: {path}")
        return path

    experiments_dir = Path("experiments")
    candidates = sorted(experiments_dir.glob("*capability.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(
            "No capability CSV provided and none found under experiments/. "
            "Pass --csv path/to/capability.csv."
        )
    return candidates[0]


def _load_rows(csv_path: Path) -> list[dict[str, str]]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _select_row(rows: list[dict[str, str]], node_id: str | None, row_index: int | None) -> dict[str, str]:
    if node_id is not None:
        for row in rows:
            if str(row["node_id"]) == str(node_id):
                return row
        raise KeyError(f"No row found for node_id={node_id}")

    if row_index is None:
        row_index = 0
    if row_index < 0 or row_index >= len(rows):
        raise IndexError(f"row_index={row_index} is out of range for {len(rows)} rows")
    return rows[row_index]


def _print_human_report(details: dict, row: dict[str, str]) -> None:
    print(f"node_id={row['node_id']} lat={row['lat']} lon={row['lon']}")
    print(f"capability={details['capability']}")
    print(f"service_scores={json.dumps(details['service_scores'], ensure_ascii=False, sort_keys=True)}")
    print(
        f"std={details.get('std', 0.0):.6f} "
        f"q={details.get('q', 0.0):.6f} "
        f"p={details.get('p', 0.0):.6f} "
        f"v={details.get('veto_threshold')} "
        f"lambda={details.get('lambda_cut', 0.65):.2f}"
    )
    print(
        f"assigned_category={details['assigned_category']} "
        f"assigned_index={details['assigned_category_index']} "
        f"score={details['score']:.6f}"
    )

    if details.get("mode") == "constant_scores":
        print(details["rule"])
        return

    for boundary in details["boundaries"]:
        print("")
        print(
            f"boundary={boundary['boundary_value']:.1f} "
            f"concordance={boundary['global_concordance']:.6f} "
            f"credibility={boundary['credibility']:.6f} "
            f"outranks={boundary['outranks_boundary']}"
        )
        for term in boundary["concordance_terms"]:
            print(
                "  "
                f"{term['service']}: score={term['score']:.6f} "
                f"diff={term['difference_vs_boundary']:.6f} "
                f"c_j={term['partial_concordance']:.6f} "
                f"rule={term['rule']}"
            )
        if boundary["discordance_terms"]:
            print("  discordance:")
            for term in boundary["discordance_terms"]:
                print(
                    "    "
                    f"{term['service']}: gap={term['gap']:.6f} "
                    f"d_j={term['discordance']:.6f} "
                    f"cred={term['adjusted_credibility']:.6f} "
                    f"rule={term['rule']}"
                )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect how ELECTRE TRI capability scores are computed from service scores."
    )
    parser.add_argument("--csv", help="Path to a generated capability CSV. Defaults to the latest experiments/*capability.csv.")
    parser.add_argument(
        "--capability",
        choices=list(cap.CAPABILITY_SERVICES.keys()),
        required=True,
        help="Capability to inspect.",
    )
    parser.add_argument("--node-id", help="Node id to inspect.")
    parser.add_argument("--row-index", type=int, help="Fallback row index if node id is not provided.")
    parser.add_argument("--json", action="store_true", help="Emit the explanation as JSON.")
    args = parser.parse_args()

    csv_path = _resolve_capability_csv(args.csv)
    rows = _load_rows(csv_path)
    row = _select_row(rows, args.node_id, args.row_index)

    services = cap.CAPABILITY_SERVICES[args.capability]
    values = [float(row[f"service_{service}"]) for service in services]
    details = cap.electre_tri_details(values, args.capability)
    details["csv_path"] = str(csv_path)
    details["node_id"] = str(row["node_id"])
    details["lat"] = float(row["lat"])
    details["lon"] = float(row["lon"])

    if args.json:
        print(json.dumps(details, ensure_ascii=False, indent=2))
        return

    print(f"csv_path={csv_path}")
    _print_human_report(details, row)


if __name__ == "__main__":
    main()
