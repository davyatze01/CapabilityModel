"""Build an interactive HTML dashboard of how per-POI sp/cp powers were rescaled.

Reads the `power_scaling.json` that the score-report stage writes next to the
hex-POI store (see utils/power_scaling.py + hex_shard_writer.py) and renders a
self-contained, offline dashboard: for each service (sp) and capability (cp) it
shows the raw value distribution as adjustable ranges that can be split
(expanded) or removed, alongside where each range lands on the scaled [0, 1]
axis after the per-key quantile rescaling.

Usage:
    python generate_power_scaling_dashboard.py                 # city from CAP_STUDY_CITY (default cagliari)
    python generate_power_scaling_dashboard.py --city cagliari
    python generate_power_scaling_dashboard.py --input path/to/power_scaling.json --output out.html
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.config import PipelineConfig
from utils import capabilities as cap_mod
from utils import services as serv


def _key_metadata() -> dict:
    """Build display label, capability group, and color for every sp/cp key."""
    colors = cap_mod.CAPABILITY_COLORS
    # service -> owning capability (some services may feed none -> "other")
    svc_to_cap: dict[str, str] = {}
    for capability, services in cap_mod.CAPABILITY_SERVICES.items():
        for s in services:
            svc_to_cap[s] = capability

    labels: dict[str, str] = {}
    labels_path = Path("config") / "service_labels.json"
    try:
        labels = dict(json.loads(labels_path.read_text(encoding="utf-8")))
    except (FileNotFoundError, ValueError):
        labels = {}

    def _pretty(key: str) -> str:
        return labels.get(key, key.replace("_", " ").title())

    meta: dict[str, dict] = {}
    for svc in serv.SERVICE_KEYS:
        cap = svc_to_cap.get(svc)
        meta[svc] = {
            "kind": "sp",
            "label": _pretty(svc),
            "group": cap or "other",
            "color": colors.get(cap, "#8a8a8a"),
        }
    for cap in cap_mod.CAPABILITY_SERVICES:
        meta[cap] = {
            "kind": "cp",
            "label": cap.title(),
            "group": cap,
            "color": colors.get(cap, "#8a8a8a"),
        }
    return meta


def _resolve_input(city: str | None, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    cfg = PipelineConfig(study_city=(city or "cagliari"))
    return Path(cfg.poi_export_dir) / "power_scaling.json"


def build_dashboard(
    input_path: str | Path,
    output_path: str | Path,
    city_label: str,
) -> Path:
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.exists():
        raise FileNotFoundError(
            f"power_scaling.json not found at {input_path}. Run the score-report "
            "stage first (python score_report.py --city <city>) so it is written."
        )

    report = json.loads(input_path.read_text(encoding="utf-8"))
    meta = _key_metadata()
    # Keep only keys that actually have data, in a stable, grouped order.
    present = report.get("keys", {})
    ordered_keys = [k for k in list(serv.SERVICE_KEYS) + list(cap_mod.CAPABILITY_SERVICES)
                    if k in present and present[k].get("total", 0) > 0]

    payload = {
        "city": city_label,
        "scale": report.get("scale", 10000),
        "keys": {k: present[k] for k in ordered_keys},
        "order": ordered_keys,
        "meta": {k: meta.get(k, {"kind": "sp", "label": k, "group": "other", "color": "#8a8a8a"})
                 for k in ordered_keys},
    }

    html = _HTML_TEMPLATE.replace("__CITY__", _escape(city_label))
    html = html.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# The dashboard is a single self-contained page: no external libraries, all
# charts drawn as inline SVG, all state in vanilla JS. Data is injected in place
# of the `/*__DATA__*/null` token below.
_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Power scaling — __CITY__</title>
<style>
:root{
  --bg:#f7f7f8; --surface:#ffffff; --ink:#1a1a1a; --muted:#6b6b6b; --line:#e3e3e6;
  --grid:#ececef; --accent:#3355dd; --shadow:0 1px 2px rgba(0,0,0,.06),0 4px 12px rgba(0,0,0,.05);
}
@media (prefers-color-scheme:dark){
  :root{ --bg:#131316; --surface:#1c1c20; --ink:#ececef; --muted:#9a9aa2; --line:#2c2c33;
    --grid:#26262c; --accent:#7f9bff; --shadow:0 1px 2px rgba(0,0,0,.4); }
}
:root[data-theme="dark"]{ --bg:#131316; --surface:#1c1c20; --ink:#ececef; --muted:#9a9aa2;
  --line:#2c2c33; --grid:#26262c; --accent:#7f9bff; --shadow:0 1px 2px rgba(0,0,0,.4); }
:root[data-theme="light"]{ --bg:#f7f7f8; --surface:#ffffff; --ink:#1a1a1a; --muted:#6b6b6b;
  --line:#e3e3e6; --grid:#ececef; --accent:#3355dd; --shadow:0 1px 2px rgba(0,0,0,.06),0 4px 12px rgba(0,0,0,.05); }
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.5 "Open Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}
header{padding:20px 24px 8px;}
h1{font-size:18px;margin:0 0 2px;font-weight:700;}
.sub{color:var(--muted);font-size:13px;}
.wrap{display:grid;grid-template-columns:250px 1fr;gap:18px;padding:12px 24px 40px;align-items:start;}
@media (max-width:820px){.wrap{grid-template-columns:1fr;}}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);}
.side{padding:10px;position:sticky;top:12px;max-height:calc(100vh - 24px);overflow:auto;}
.grp{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:12px 8px 4px;}
.kbtn{display:flex;align-items:center;gap:8px;width:100%;text-align:left;border:0;background:transparent;
  color:var(--ink);padding:7px 8px;border-radius:8px;cursor:pointer;font-size:13px;}
.kbtn:hover{background:var(--grid);}
.kbtn.active{background:var(--grid);font-weight:700;}
.dot{width:10px;height:10px;border-radius:3px;flex:0 0 auto;}
.kbtn .kk{color:var(--muted);font-size:11px;margin-left:auto;}
.main{padding:16px 18px;}
.hd{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:2px;}
.hd h2{font-size:16px;margin:0;font-weight:700;}
.chip{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:1px 8px;}
.stats{color:var(--muted);font-size:12px;margin:2px 0 14px;}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 14px;}
.tbtn{border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:8px;
  padding:5px 10px;font-size:12px;cursor:pointer;}
.tbtn:hover{border-color:var(--accent);}
.charttitle{font-size:12px;color:var(--muted);margin:16px 0 4px;font-weight:600;}
svg{display:block;width:100%;max-width:520px;height:auto;overflow:visible;}
.axis text{fill:var(--muted);font-size:10px;}
.axis line,.axis path{stroke:var(--grid);}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:12px;}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap;}
th:first-child,td:first-child{text-align:left;}
th{color:var(--muted);font-weight:600;}
tr.removed td{opacity:.4;text-decoration:line-through;}
.rowbtn{border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:6px;
  padding:2px 7px;font-size:11px;cursor:pointer;margin-left:4px;}
.rowbtn:hover{border-color:var(--accent);}
.tip{position:fixed;pointer-events:none;background:var(--surface);border:1px solid var(--line);
  border-radius:8px;box-shadow:var(--shadow);padding:6px 9px;font-size:12px;opacity:0;transition:opacity .08s;z-index:9;}
.themebtn{position:absolute;top:18px;right:22px;border:1px solid var(--line);background:var(--surface);
  color:var(--ink);border-radius:8px;padding:4px 9px;font-size:12px;cursor:pointer;}
.legend{display:flex;gap:14px;font-size:11px;color:var(--muted);margin-top:6px;flex-wrap:wrap;}
.legend span{display:inline-flex;align-items:center;gap:5px;}
.swatch{width:10px;height:10px;border-radius:3px;}
</style>
</head>
<body>
<button class="themebtn" id="themebtn">Theme</button>
<header>
  <h1>Power scaling &mdash; __CITY__</h1>
  <div class="sub">How per-POI service (sp) and capability (cp) powers were spread across [0,&nbsp;1] by per-key quantile rescaling. Split or remove ranges to inspect the distribution.</div>
</header>
<div class="wrap">
  <nav class="panel side" id="side"></nav>
  <section class="panel main" id="main"></section>
</div>
<div class="tip" id="tip"></div>
<script>
const DATA = /*__DATA__*/null;
const $ = (s,r=document)=>r.querySelector(s);
const fmt = (v,d=3)=>Number(v).toFixed(d);
const pct = (n,t)=> t? (100*n/t):0;

let CURRENT = DATA.order[0];
const STATE = {}; // key -> {ranges:[{i0,i1,removed}], }

function atomicBins(key){ return DATA.keys[key].bins; }

function initState(key, nRanges=8){
  const bins = atomicBins(key);
  const n = bins.length;
  const groups = Math.min(nRanges, n);
  const ranges = [];
  for(let g=0; g<groups; g++){
    const i0 = Math.floor(g*n/groups);
    const i1 = Math.floor((g+1)*n/groups)-1;
    if(i1>=i0) ranges.push({i0,i1,removed:false});
  }
  STATE[key] = {ranges};
}

// Aggregate a range of atomic bins into one displayed range.
function agg(key, r){
  const bins = atomicBins(key);
  let count=0, slo=Infinity, shi=-Infinity;
  for(let i=r.i0;i<=r.i1;i++){
    const b=bins[i];
    count += b.count;
    if(b.scaled_lo!=null){ slo=Math.min(slo,b.scaled_lo); shi=Math.max(shi,b.scaled_hi); }
  }
  const lo=isFinite(slo)?slo:null, hi=isFinite(shi)?shi:null;
  return {lo:bins[r.i0].lo, hi:bins[r.i1].hi, count,
          scaled_lo:lo, scaled_hi:hi,
          // single value = where this range begins, so the bottom range anchors at ~0
          scaled_val: lo};
}

function totalShown(key){
  return STATE[key].ranges.filter(r=>!r.removed)
    .reduce((s,r)=>s+agg(key,r).count,0);
}

function splitRange(key,idx){
  const r = STATE[key].ranges[idx];
  if(r.i1<=r.i0) return; // atomic already
  const mid = Math.floor((r.i0+r.i1)/2);
  STATE[key].ranges.splice(idx,1,
    {i0:r.i0,i1:mid,removed:r.removed},
    {i0:mid+1,i1:r.i1,removed:r.removed});
  render();
}
function mergeNext(key,idx){
  const rs=STATE[key].ranges;
  if(idx>=rs.length-1) return;
  const a=rs[idx], b=rs[idx+1];
  rs.splice(idx,2,{i0:a.i0,i1:b.i1,removed:a.removed&&b.removed});
  render();
}
function toggleRemove(key,idx){ STATE[key].ranges[idx].removed=!STATE[key].ranges[idx].removed; render(); }
function resetKey(key){ initState(key); render(); }

// ---- rendering ----
function buildSidebar(){
  const side=$("#side"); side.innerHTML="";
  const groupsOrder=["nutrition","care","restorativeness","other"];
  const byGroup={};
  DATA.order.forEach(k=>{ const g=DATA.meta[k].group; (byGroup[g]=byGroup[g]||[]).push(k); });
  const kinds=[["sp","Services (sp)"],["cp","Capabilities (cp)"]];
  kinds.forEach(([kind,title])=>{
    const keys=DATA.order.filter(k=>DATA.meta[k].kind===kind);
    if(!keys.length) return;
    const h=document.createElement("div"); h.className="grp"; h.textContent=title; side.appendChild(h);
    keys.forEach(k=>{
      const m=DATA.meta[k];
      const b=document.createElement("button");
      b.className="kbtn"+(k===CURRENT?" active":"");
      b.innerHTML=`<span class="dot" style="background:${m.color}"></span>`+
        `<span>${m.label}</span><span class="kk">${DATA.keys[k].total.toLocaleString()}</span>`;
      b.onclick=()=>{CURRENT=k; render();};
      side.appendChild(b);
    });
  });
}

function barChart(key){
  const m=DATA.meta[key];
  const rs=STATE[key].ranges;
  const W=680,H=200,padL=40,padR=10,padT=10,padB=34;
  const iw=W-padL-padR, ih=H-padT-padB;
  const shown=rs.map(r=>({r,a:agg(key,r)}));
  const maxC=Math.max(1,...shown.map(x=>x.a.count));
  const loMin=Math.min(...shown.map(x=>x.a.lo)), hiMax=Math.max(...shown.map(x=>x.a.hi));
  const span=(hiMax-loMin)||1;
  const X=v=>padL+iw*(v-loMin)/span;
  const Y=c=>padT+ih*(1-c/maxC);
  let s=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Raw distribution histogram">`;
  // y grid
  for(let t=0;t<=4;t++){const y=padT+ih*t/4;const val=Math.round(maxC*(1-t/4));
    s+=`<line x1="${padL}" x2="${W-padR}" y1="${y}" y2="${y}" stroke="var(--grid)"/>`+
       `<text x="${padL-6}" y="${y+3}" text-anchor="end" class="ax">${val.toLocaleString()}</text>`;}
  shown.forEach((x,idx)=>{
    if(x.r.removed) return;
    const x0=X(x.a.lo)+1, x1=X(x.a.hi)-1, y=Y(x.a.count);
    const w=Math.max(1,x1-x0);
    s+=`<rect x="${x0}" y="${y}" width="${w}" height="${padT+ih-y}" rx="3" fill="${m.color}" `+
       `data-idx="${idx}" class="bar" style="cursor:pointer"/>`;
  });
  // x labels (ends + middle)
  [loMin,(loMin+hiMax)/2,hiMax].forEach(v=>{
    s+=`<text x="${X(v)}" y="${H-16}" text-anchor="middle" class="ax">${fmt(v)}</text>`;});
  s+=`<text x="${padL+iw/2}" y="${H-2}" text-anchor="middle" class="ax">raw value</text>`;
  s+=`</svg>`;
  return s.replace(/class="ax"/g,'class="ax" fill="var(--muted)" font-size="10"');
}

function scaleMap(key){
  const m=DATA.meta[key];
  const rs=STATE[key].ranges;
  const W=680,H=70,padL=40,padR=10;
  const iw=W-padL-padR;
  const X=v=>padL+iw*Math.max(0,Math.min(1,v));
  let s=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Scaled [0,1] mapping">`;
  s+=`<line x1="${padL}" x2="${W-padR}" y1="34" y2="34" stroke="var(--grid)"/>`;
  for(let t=0;t<=4;t++){const x=padL+iw*t/4;
    s+=`<line x1="${x}" x2="${x}" y1="30" y2="38" stroke="var(--grid)"/>`+
       `<text x="${x}" y="52" text-anchor="middle" fill="var(--muted)" font-size="10">${(t/4).toFixed(2)}</text>`;}
  rs.forEach((r,idx)=>{
    if(r.removed) return;
    const a=agg(key,r); if(a.scaled_lo==null) return;
    const x0=X(a.scaled_lo), x1=X(a.scaled_hi);
    const w=Math.max(2,x1-x0);
    s+=`<rect x="${x0}" y="26" width="${w}" height="16" rx="4" fill="${m.color}" opacity="0.35" `+
       `data-idx="${idx}" class="seg" style="cursor:pointer"/>`;
    // explicit single representative scaled value: marker + label
    if(a.scaled_val!=null){
      const xv=X(a.scaled_val);
      s+=`<line x1="${xv}" x2="${xv}" y1="22" y2="46" stroke="${m.color}" stroke-width="2"/>`+
         `<text x="${xv}" y="18" text-anchor="middle" fill="var(--ink)" font-size="10" font-weight="600">${fmt(a.scaled_val,2)}</text>`;
    }
  });
  s+=`<text x="${padL+iw/2}" y="66" text-anchor="middle" fill="var(--muted)" font-size="10">scaled value (quantile) &mdash; number = where each range begins</text>`;
  s+=`</svg>`;
  return s;
}

function rangeTable(key){
  const rs=STATE[key].ranges, tot=totalShown(key);
  let s=`<table><thead><tr><th>Raw range</th><th>Count</th><th>%</th><th>Scaled start</th><th>Scaled range</th><th>Actions</th></tr></thead><tbody>`;
  rs.forEach((r,idx)=>{
    const a=agg(key,r);
    const scaled = a.scaled_lo==null? "&mdash;" : `${fmt(a.scaled_lo)} &ndash; ${fmt(a.scaled_hi)}`;
    const scaledVal = a.scaled_val==null? "&mdash;" : `<b>${fmt(a.scaled_val)}</b>`;
    s+=`<tr class="${r.removed?'removed':''}">`+
       `<td>${fmt(a.lo)} &ndash; ${fmt(a.hi)}</td>`+
       `<td>${a.count.toLocaleString()}</td>`+
       `<td>${r.removed?'&mdash;':fmt(pct(a.count,tot),1)+'%'}</td>`+
       `<td>${scaledVal}</td>`+
       `<td>${scaled}</td>`+
       `<td>`+
         `<button class="rowbtn" data-act="split" data-idx="${idx}" ${r.i1<=r.i0?'disabled':''}>Split</button>`+
         `<button class="rowbtn" data-act="merge" data-idx="${idx}" ${idx>=rs.length-1?'disabled':''}>Merge&rarr;</button>`+
         `<button class="rowbtn" data-act="remove" data-idx="${idx}">${r.removed?'Restore':'Remove'}</button>`+
       `</td></tr>`;
  });
  s+=`</tbody></table>`;
  return s;
}

function render(){
  buildSidebar();
  const key=CURRENT;
  if(!STATE[key]) initState(key);
  const m=DATA.meta[key], d=DATA.keys[key];
  const main=$("#main");
  main.innerHTML=
    `<div class="hd"><span class="dot" style="width:12px;height:12px;background:${m.color}"></span>`+
      `<h2>${m.label}</h2><span class="chip">${m.kind.toUpperCase()}</span>`+
      `<span class="chip">${m.group}</span></div>`+
    `<div class="stats">${d.total.toLocaleString()} values &middot; raw range ${fmt(d.raw_min)} &ndash; ${fmt(d.raw_max)} &middot; ${STATE[key].ranges.length} ranges</div>`+
    `<div class="toolbar">`+
      `<button class="tbtn" id="splitall">Expand all</button>`+
      `<button class="tbtn" id="collapse">Collapse to 4</button>`+
      `<button class="tbtn" id="reset">Reset</button>`+
    `</div>`+
    `<div class="charttitle">Raw distribution (counts per range)</div>`+ barChart(key)+
    `<div class="charttitle">Where each range lands after scaling &mdash; spread across [0,&nbsp;1]</div>`+ scaleMap(key)+
    rangeTable(key);

  // events
  $("#reset").onclick=()=>resetKey(key);
  $("#splitall").onclick=()=>{ STATE[key].ranges = atomicBins(key).map((_,i)=>({i0:i,i1:i,removed:false})); render(); };
  $("#collapse").onclick=()=>{ initState(key,4); render(); };
  main.querySelectorAll(".rowbtn").forEach(b=>{
    b.onclick=()=>{const i=+b.dataset.idx, act=b.dataset.act;
      if(act==="split")splitRange(key,i); else if(act==="merge")mergeNext(key,i); else toggleRemove(key,i);};
  });
  hookTips(main,key);
}

function hookTips(root,key){
  const tip=$("#tip");
  const show=(html,e)=>{tip.innerHTML=html;tip.style.opacity=1;
    tip.style.left=(e.clientX+12)+"px";tip.style.top=(e.clientY+12)+"px";};
  const hide=()=>tip.style.opacity=0;
  root.querySelectorAll(".bar,.seg").forEach(el=>{
    el.onmousemove=(e)=>{const a=agg(key,STATE[key].ranges[+el.dataset.idx]);
      const t=totalShown(key);
      show(`<b>raw</b> ${fmt(a.lo)} &ndash; ${fmt(a.hi)}<br><b>count</b> ${a.count.toLocaleString()} (${fmt(pct(a.count,t),1)}%)`+
           (a.scaled_val!=null?`<br><b>scaled start</b> ${fmt(a.scaled_val)}`:"")+
           (a.scaled_lo!=null?`<br><b>scaled range</b> ${fmt(a.scaled_lo)} &ndash; ${fmt(a.scaled_hi)}`:""),e);};
    el.onmouseleave=hide;
  });
}

// theme toggle
$("#themebtn").onclick=()=>{
  const root=document.documentElement;
  const cur=root.getAttribute("data-theme")|| (matchMedia("(prefers-color-scheme:dark)").matches?"dark":"light");
  root.setAttribute("data-theme", cur==="dark"?"light":"dark");
};

render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the sp/cp power-scaling dashboard.")
    parser.add_argument("--city", default=None, help="City slug (default: cagliari).")
    parser.add_argument("--input", default=None, help="Explicit path to power_scaling.json.")
    parser.add_argument("--output", default=None, help="Output HTML path.")
    args = parser.parse_args()

    city = args.city or "cagliari"
    in_path = _resolve_input(city, args.input)
    cfg = PipelineConfig(study_city=city)
    out_path = Path(args.output) if args.output else Path("outputs") / f"power_scaling_dashboard_{cfg.artifact_slug}.html"
    written = build_dashboard(in_path, out_path, city_label=cfg.artifact_slug)
    print(f"[PowerScaling] Dashboard written: {written}")
