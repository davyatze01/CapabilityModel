"""Verification dashboard for per-service POI de-duplication (OSM mode).

By design the poi_types of a single service are meant to be mutually exclusive: one
physical POI should contribute to at most one poi_type per service. Because poi_types
are matched from overlapping OSM tag clauses, a single OSM element can be matched by
several poi_types of the same service and counted multiple times.

`utils.poi_dedup` resolves, per service, which poi_type *owns* each shared POI (most
specific matched tag clause wins; ties → service config order). This dashboard renders
that resolution so the constraint can be inspected: per service it shows every shared
("conflict") POI, which poi_types matched it and how specifically, the chosen owner,
and a PASS/FAIL self-check that after dedup each POI maps to exactly one poi_type.

This applies only when POIs are downloaded from OSM. In shapefile mode the dashboard
renders a notice and stops (there is no tag-overlap conflict to resolve).

Usage:
    # Pick the city by editing STUDY_CITY below, then just run:
    python debug_poi_dedup.py
    # (--study-city / --output remain as optional overrides.)
"""

from __future__ import annotations

# ── Edit here to switch the dashboard to another city ─────────────────────────────────
# Mirrors main.py's `study_city` knob. Must match a key in config.CITY_PRESETS
# (e.g. "cagliari", "paris"). Dedup only applies in OSM mode; shapefile cities (Paris)
# render a notice instead.
STUDY_CITY = "cagliari"

import argparse
import html
import json
import os
from pathlib import Path

from config import PipelineConfig
from utils import poi_dedup
from utils import services as serv


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _short_source_key(source_key: str) -> str:
    """Render a source_key JSON blob as a compact human label."""
    try:
        obj = json.loads(source_key)
    except Exception:
        return source_key[:40]
    kind = obj.get("kind")
    if kind == "osmid":
        et = obj.get("element_type")
        et = f"{et}/" if et else ""
        return f"osm {et}{obj.get('value')}"
    if kind == "fallback":
        return f"fallback:{obj.get('name') or '(unnamed)'}"
    return f"{kind}:{obj.get('value')}"


def _clause_str(clause: dict) -> str:
    """Render one tag clause as 'k=v AND k2=v2' (value True shown as '*')."""
    parts = []
    for k, v in clause.items():
        if v is True:
            parts.append(f"{k}=*")
        elif isinstance(v, list):
            parts.append(f"{k}∈{{{', '.join(str(x) for x in v)}}}")
        else:
            parts.append(f"{k}={v}")
    return " AND ".join(parts)


def _esc(v) -> str:
    return html.escape(str(v))


# ---------------------------------------------------------------------------
# Config-level clause overlap (why conflicts are possible for a service)
# ---------------------------------------------------------------------------

def _service_clause_overlaps(service: str) -> list[dict]:
    """For each pair of poi_types in a service, the tag clauses they share verbatim."""
    queries = serv.get_service_queries(service)
    clauses_by_type: dict[str, list[dict]] = {
        str(q.poi_type): poi_dedup._clauses_for_query(q) for q in queries
    }

    def _norm(clause: dict) -> str:
        return json.dumps(clause, sort_keys=True, ensure_ascii=True)

    types = list(clauses_by_type)
    out = []
    for i in range(len(types)):
        for j in range(i + 1, len(types)):
            a, b = types[i], types[j]
            set_a = {_norm(c): c for c in clauses_by_type[a]}
            set_b = {_norm(c): c for c in clauses_by_type[b]}
            shared = [set_a[k] for k in set_a.keys() & set_b.keys()]
            if shared:
                out.append({"a": a, "b": b, "shared": shared})
    return out


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_CSS = """
*{box-sizing:border-box}
body{margin:0;font-family:Arial,sans-serif;background:#f3f1ea;color:#1f2933;font-size:13px;padding:18px 22px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:16px;margin:0}
.muted{color:#6b7280;font-size:12px}
.banner{padding:12px 16px;border-radius:10px;font-weight:700;font-size:15px;margin:14px 0}
.pass{background:#d1fae5;color:#065f46;border:1px solid #6ee7b7}
.fail{background:#fee2e2;color:#991b1b;border:1px solid #fca5a5}
.notice{background:#fef3c7;color:#92400e;border:1px solid #fcd34d}
.panel{background:#fffdf8;border:1px solid #d9d3c7;border-radius:10px;margin-bottom:16px;overflow:hidden}
.panel-header{padding:10px 14px;background:#f1ede4;display:flex;justify-content:space-between;align-items:center}
.panel-body{padding:12px 16px}
.stat{display:inline-block;margin-right:18px}
.stat b{font-size:16px}
.section-label{font-size:11px;font-weight:700;text-transform:uppercase;color:#52606d;margin:14px 0 6px}
table{width:100%;border-collapse:collapse;font-size:12px;margin:4px 0 10px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid #e8e3d8;vertical-align:top}
th{background:#f1ede4;font-weight:600}
.mono{font-family:monospace;font-size:11px}
.card{border:1px solid #ddd;border-radius:8px;margin:10px 0;overflow:hidden}
.card-head{background:#f4f0e6;padding:7px 12px;font-weight:700;font-size:12px}
.card-body{padding:8px 12px}
.tags{font-family:monospace;font-size:11px;color:#52606d;margin:2px 0 8px;word-break:break-word}
.cand{display:flex;align-items:baseline;gap:8px;padding:4px 0;border-bottom:1px solid #f0ece2}
.spec-badge{display:inline-flex;align-items:center;justify-content:center;min-width:20px;height:20px;border-radius:50%;background:#0f766e;color:#fff;font-size:11px;font-weight:700;padding:0 5px}
.owner{background:#d1fae5;border-radius:6px;padding:2px 6px}
.loser{color:#9ca3af;text-decoration:line-through}
.owner-name{font-weight:700;color:#065f46}
.decision{margin-top:6px;font-size:12px;background:#eef4ff;border:1px solid #c3d9f5;border-radius:6px;padding:6px 10px}
.tie{color:#92400e;font-weight:700}
.pill{display:inline-block;background:#e5e7eb;border-radius:10px;padding:1px 8px;font-size:11px;margin-left:6px}
.panel-header{cursor:pointer;user-select:none}
.chevron{display:inline-block;transition:transform .15s ease;font-size:11px;color:#52606d}
.panel.collapsed .chevron{transform:rotate(-90deg)}
.panel.collapsed .panel-body{display:none}
.clause-row{padding:2px 0}
.maps-link{margin-left:8px;font-size:11px;color:#1d4ed8;text-decoration:none;font-weight:700}
.maps-link:hover{text-decoration:underline}
"""

_JS = """
function toggleService(service){
  var panel = document.getElementById('panel-body-' + service).closest('.panel');
  panel.classList.toggle('collapsed');
}
function expandAll(collapse){
  document.querySelectorAll('.panel').forEach(function(p){
    if(collapse){ p.classList.add('collapsed'); } else { p.classList.remove('collapsed'); }
  });
}
"""


def _render_candidate(cand: dict, owner: str) -> str:
    pt = cand["poi_type"]
    is_owner = pt == owner
    clauses = " ; ".join(_clause_str(c) for c in cand.get("matched_clauses", [])) or "(no clause)"
    name_cls = "owner-name" if is_owner else "loser"
    wrap_open, wrap_close = ('<span class="owner">', "</span>") if is_owner else ("", "")
    tag = " ← OWNER" if is_owner else " — dropped (counted under owner)"
    return (
        f'<div class="cand">{wrap_open}'
        f'<span class="spec-badge">{cand["best_specificity"]}</span>'
        f'<span class="{name_cls}">{_esc(pt)}</span>'
        f'<span class="mono">&larr; {_esc(clauses)}</span>'
        f'<span class="muted">{tag}</span>'
        f'{wrap_close}</div>'
    )


def _maps_link(lat, lon) -> str:
    if lat is None or lon is None:
        return ""
    url = f"https://www.google.com/maps?q={lat:.6f},{lon:.6f}"
    return f'<a class="maps-link" href="{_esc(url)}" target="_blank" rel="noopener">map ↗</a>'


def _render_conflict_card(source_key: str, entry: dict) -> str:
    candidates = sorted(
        entry["candidates"], key=lambda c: (-c["best_specificity"], c["config_index"])
    )
    owner = entry["owner"]
    name = entry.get("name") or "(unnamed)"
    tags = ", ".join(f"{k}={v}" for k, v in (entry.get("osm_tags") or {}).items())
    maps_html = _maps_link(entry.get("lat"), entry.get("lon"))

    best_spec = candidates[0]["best_specificity"]
    if entry.get("tie_broken_by_config_order"):
        decision = (
            f'<span class="tie">tie at specificity {best_spec}</span> '
            f"→ resolved by config order → winner = <b>{_esc(owner)}</b>"
        )
    else:
        decision = f"max specificity = {best_spec} → winner = <b>{_esc(owner)}</b>"

    cand_html = "".join(_render_candidate(c, owner) for c in candidates)
    return (
        f'<div class="card"><div class="card-head">{_esc(name)} '
        f'<span class="pill mono">{_esc(_short_source_key(source_key))}</span>'
        f'{maps_html}</div>'
        f'<div class="card-body">'
        f'<div class="tags">OSM tags: {_esc(tags) if tags else "(none)"}</div>'
        f"{cand_html}"
        f'<div class="decision">{decision}</div>'
        f"</div></div>"
    )


def _render_full_query_table(service: str) -> str:
    """Full tag-clause query for every poi_type in this service, not just the shared
    subset — so the query itself can be inspected/refined, not just what overlaps."""
    queries = serv.get_service_queries(service)
    rows = []
    for q in queries:
        poi_type = str(q.poi_type)
        clauses = poi_dedup._clauses_for_query(q)
        clause_html = "".join(
            f'<div class="clause-row">{_esc(_clause_str(c))}</div>' for c in clauses
        ) or '<div class="muted">(no clauses)</div>'
        rows.append(
            f"<tr><td><b>{_esc(poi_type)}</b></td>"
            f'<td class="mono">{clause_html}</td></tr>'
        )
    return (
        "<table><thead><tr><th>poi_type</th><th>full query (OR of these clauses)</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_overlap_table(service: str) -> str:
    overlaps = _service_clause_overlaps(service)
    if not overlaps:
        return '<div class="muted">No poi_type pair in this service shares a tag clause.</div>'
    rows = "".join(
        f"<tr><td>{_esc(o['a'])}</td><td>{_esc(o['b'])}</td>"
        f'<td class="mono">{_esc("; ".join(_clause_str(c) for c in o["shared"]))}</td></tr>'
        for o in overlaps
    )
    return (
        "<table><thead><tr><th>poi_type</th><th>poi_type</th>"
        "<th>shared tag clause(s)</th></tr></thead><tbody>"
        f"{rows}</tbody></table>"
    )


def _verify(ownership: dict) -> tuple[bool, list[str]]:
    """Self-check: every POI resolves to exactly one owner present in its candidates."""
    problems: list[str] = []
    for service, info in ownership.items():
        for sk, entry in info["pois"].items():
            owner = entry.get("owner")
            cand_types = {c["poi_type"] for c in entry["candidates"]}
            if owner not in cand_types:
                problems.append(f"{service}: owner {owner!r} not among candidates for {sk}")
    return (len(problems) == 0, problems)


def build_html(cfg: PipelineConfig, ownership: dict) -> str:
    ok, problems = _verify(ownership)

    total_conflicts = 0
    total_dupes = 0
    panels = []
    for service in serv.SERVICE_KEYS:
        info = ownership.get(service)
        if info is None:
            continue
        pois = info["pois"]
        conflicts = {sk: e for sk, e in pois.items() if e.get("is_conflict")}
        dupes = sum(len(e["candidates"]) - 1 for e in conflicts.values())
        total_conflicts += len(conflicts)
        total_dupes += dupes

        cards = "".join(
            _render_conflict_card(sk, e)
            for sk, e in sorted(conflicts.items(), key=lambda kv: (kv[1].get("name") or "~"))
        ) or '<div class="muted">No shared POIs in this service — nothing to de-duplicate.</div>'

        body_id = f"panel-body-{_esc(service)}"
        panels.append(
            f'<div class="panel"><div class="panel-header" onclick="toggleService(\'{_esc(service)}\')">'
            f'<div><span class="chevron" id="chevron-{_esc(service)}">&#9662;</span>'
            f'<h2 style="display:inline-block;margin-left:6px">{_esc(service)}</h2></div>'
            f'<span class="muted">{_esc(", ".join(info["poi_types"]))}</span></div>'
            f'<div class="panel-body" id="{body_id}">'
            f'<div><span class="stat">unique POIs <b>{len(pois)}</b></span>'
            f'<span class="stat">conflicts <b>{len(conflicts)}</b></span>'
            f'<span class="stat">duplicate contributions eliminated <b>{dupes}</b></span></div>'
            f'<div class="section-label">Full query per poi_type contributing to this service</div>'
            f"{_render_full_query_table(service)}"
            f'<div class="section-label">Why conflicts are possible (shared tag clauses between poi_types)</div>'
            f"{_render_overlap_table(service)}"
            f'<div class="section-label">Conflict resolution per shared POI</div>'
            f"{cards}"
            f"</div></div>"
        )

    banner = (
        f'<div class="banner pass">✓ PASS — after dedup every physical POI maps to '
        f"exactly one poi_type per service.</div>"
        if ok
        else f'<div class="banner fail">✗ FAIL — {len(problems)} issue(s): '
        f"{_esc('; '.join(problems[:5]))}</div>"
    )

    header = (
        f"<h1>POI de-duplication — {_esc(cfg.city_name)}</h1>"
        f'<div class="muted">slug={_esc(cfg.artifact_slug)} &nbsp;|&nbsp; '
        f"owner rule: most specific matched tag clause, ties → service config order &nbsp;|&nbsp; "
        f"<b>{total_conflicts}</b> shared POIs, <b>{total_dupes}</b> duplicate contributions removed "
        f'&nbsp;|&nbsp; <a href="#" onclick="expandAll(false);return false;">expand all</a> '
        f'&nbsp;<a href="#" onclick="expandAll(true);return false;">collapse all</a></div>'
    )

    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<title>POI dedup</title>"
        f"<style>{_CSS}</style></head><body>"
        f"{header}{banner}{''.join(panels)}"
        f"<script>{_JS}</script>"
        "</body></html>"
    )


def build_shapefile_notice_html(cfg: PipelineConfig) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<title>POI dedup</title>"
        f"<style>{_CSS}</style></head><body>"
        f"<h1>POI de-duplication — {_esc(cfg.city_name)}</h1>"
        '<div class="banner notice">Per-service POI de-duplication is OSM-only and is '
        "<b>not active</b> for this city: POIs come from a shapefile, where each feature "
        "already carries exactly one poi_type label, so there is no tag-overlap conflict to "
        "resolve.</div></body></html>"
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate POI de-duplication verification HTML.")
    parser.add_argument("--study-city", default=None, help=f"Override the script's STUDY_CITY (default: {STUDY_CITY})")
    parser.add_argument("--output", default=None, help="Output HTML path (default: outputs/poi_dedup_{slug}.html)")
    args = parser.parse_args()

    # Construct from the chosen city so city_slug / artifact_slug / POI-cache paths all
    # re-derive (apply_study_city() after construction does not re-derive them).
    study_city = args.study_city or STUDY_CITY
    cfg = PipelineConfig(study_city=study_city)
    print(f"[Dedup] study_city={cfg.study_city} city={cfg.city_name} slug={cfg.artifact_slug}")

    slug = cfg.artifact_slug
    out_path = args.output or os.path.join("outputs", f"poi_dedup_{slug}.html")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    if not poi_dedup.is_osm_mode(cfg):
        print(f"[Dedup] city={cfg.city_name} uses a shapefile POI source; dedup not applicable.")
        Path(out_path).write_text(build_shapefile_notice_html(cfg), encoding="utf-8")
        print(f"[Dedup] Written: {out_path}")
        return

    print(f"[Dedup] city={cfg.city_name} slug={slug} — computing POI ownership from OSM POIs...")
    specificity = poi_dedup.compute_specificity_by_type(cfg)
    ownership = poi_dedup.build_service_ownership(cfg, specificity_by_type=specificity)

    n_conflicts = sum(
        sum(1 for e in info["pois"].values() if e.get("is_conflict")) for info in ownership.values()
    )
    print(f"[Dedup] Resolved {n_conflicts} shared POIs across {len(ownership)} services.")

    html_str = build_html(cfg, ownership)
    Path(out_path).write_text(html_str, encoding="utf-8")
    print(f"[Dedup] Written: {out_path}")


if __name__ == "__main__":
    main()
