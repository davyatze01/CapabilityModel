"""One-off cleanup of a manual_merges.json produced before the interface enforced
"a fragment belongs to one POI only".

Overlapping groups are resolved biggest-group-wins; identical/emptied duplicates are
dropped; groups with a partial overlap are flagged as ambiguous and taken off the
approved list so the reviewer re-checks them. Never overwrites an existing output.
"""
import json
import os

# Knobs — edit here, then run `python clean_manual_merges.py`
INPUT_MERGES = os.path.expanduser("~/Scaricati/manual_merges.json")  # colleague's file
OUTPUT_MERGES = "manual_merges.json"     # cleaned result (refuses to overwrite)
APPROVALS_PATH = "approvals.json"        # ambiguous POIs are removed from here


def main():
    if os.path.exists(OUTPUT_MERGES):
        raise SystemExit(f"{OUTPUT_MERGES} already exists, refusing to overwrite")
    with open(INPUT_MERGES, encoding="utf-8") as f:
        merges = json.load(f)

    # Biggest group claims its fragments first; ties broken by group_id.
    claimed, cleaned, duplicates, ambiguous = set(), {}, [], set()
    for gid in sorted(merges, key=lambda g: (-len(merges[g]["fragment_keys"]), g)):
        entry = dict(merges[gid])
        keys = entry["fragment_keys"]
        kept = [k for k in keys if k not in claimed]
        if not kept:  # everything already claimed (or the group was empty)
            duplicates.append(gid)
            continue
        if len(kept) < len(keys):  # lost some, kept some: partial overlap
            ambiguous.add(gid)
            ambiguous.update(g for g in cleaned if set(cleaned[g]["fragment_keys"]) & set(keys))
        entry["fragment_keys"] = kept
        claimed.update(kept)
        cleaned[gid] = entry

    with open(OUTPUT_MERGES, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2, sort_keys=True)

    approvals = {}
    if os.path.exists(APPROVALS_PATH):
        with open(APPROVALS_PATH, encoding="utf-8") as f:
            approvals = json.load(f)
    unapproved = [g for g in ambiguous if approvals.pop(g, None) is not None]
    with open(APPROVALS_PATH, "w", encoding="utf-8") as f:
        json.dump(approvals, f, ensure_ascii=False, indent=2, sort_keys=True)

    short = lambda k: json.loads(k)["value"]
    print(f"{len(merges)} groups in, {len(cleaned)} out")
    print(f"dropped as duplicate/empty ({len(duplicates)}): {sorted(short(g) for g in duplicates)}")
    print(f"ambiguous, for the reviewer ({len(ambiguous)}): {sorted(short(g) for g in ambiguous)}")
    print(f"removed from approved ({len(unapproved)}): {sorted(short(g) for g in unapproved)}")


if __name__ == "__main__":
    main()
