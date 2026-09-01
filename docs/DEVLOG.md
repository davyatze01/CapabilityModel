# Dev log

Running log of code changes in this repo, one block per day, one bullet per semantically
distinct edit (location, problem detected, fix). See CLAUDE.md's "Dev log" section for how
this is maintained.

## 2026-08-10

## 2026-08-14

- `analysis/sensitivity_upstream.py`: sensitivity dashboard was missing from the debug
  folder because the upstream sweep's per-config worker subprocess (`subprocess.run([sys.executable,
  __file__], ...)`) crashed with `ModuleNotFoundError: No module named 'core'` before writing
  anything — running a script directly puts its own directory (`analysis/`) on `sys.path[0]`,
  not the repo root, so `from core.config import PipelineConfig` in `_work_root()` failed
  immediately. That raised in `run_configs()` and propagated out of `main.py` entirely, so
  `build_sensitivity_report()` never ran. Fixed by adding the same
  `sys.path.insert(0, _PROJECT_ROOT); os.chdir(_PROJECT_ROOT)` bootstrap already used in
  `ops/*.py` and `analysis/scenarios.py`. Verified by running the `baseline` worker standalone;
  it now completes and writes `service_scores.csv`.
- `analysis/sensitivity_analysis.py` (`run_oat_sweeps`): dashboard build then crashed with
  `IndexError: single positional indexer is out-of-bounds` in `sensitivity_report.py`'s
  `_oat_baseline("lambda")`. Cause: `core/config.py`'s `ELECTRE_LAMBDA_CUT` was bumped from
  0.65 to 0.75 in a previous change, but the lambda OAT sweep still hardcoded
  `[0.60, 0.65, 0.70]`, so no swept value matched the real baseline and every row's
  `is_baseline` came out `False`. `q`/`p`'s hardcoded sweeps still happened to match their
  baselines (0.02, 0.06) so they didn't break, but were equally fragile. Fixed by deriving all
  three sweeps from the live `ELECTRE_Q`/`ELECTRE_P`/`LAMBDA_BASELINE` constants (baseline ±
  the same step sizes as before: 0.01 / 0.02 / 0.05) instead of hardcoding the values, so a
  future baseline retune can't silently desync the sweep again.
- `analysis/sensitivity_upstream.py` (`run_worker` `__main__`): worker's return code was
  discarded (`run_worker(worker)` instead of `sys.exit(run_worker(worker))`), so a failed
  worker still exited 0 — `run_configs()` believed it succeeded, and `build_report()` silently
  dropped that config from the summary instead of erroring, per CLAUDE.md's fail-fast rule.
  Fixed to propagate the real exit code. Doing so surfaced the actual underlying failure:
  `decay_x0.8`/`decay_x1.2` errored with `ERROR: impedance bundle failed to load in
  workspace` / an `origins_sig` mismatch. Root cause: `_resolve_mode_graph_path()`
  (`utils/graphml.py:134`) bakes `get_global_radius_m(cfg)` into the walk-graph filename, and
  that radius is derived from `poi_types.csv`'s max `decay_coefficient` — the exact value the
  decay axis mutates. So decay_x0.8/x1.2 were silently routing against a different OSM graph
  extract than baseline (measured: 225574 vs 184195 candidate nodes), which made the symlinked
  baseline `impedances.npz` genuinely invalid for them (not spurious noise) — the `origins_sig`
  check was correctly rejecting a real mismatch. `contribution`/`capacity`/`interactions`/`rra`
  never touch `decay_coefficient`, so they were unaffected — which is also why their
  reachable-node tornado charts are correctly all-zero (those axes only reweight already-
  reachable POIs, they can't cause dropout). Fixed by freezing the sweep's candidate universe:
  `run_configs()` now computes `get_global_radius_m()` once from the pristine (pre-mutation)
  config and passes it to every worker via `CAP_UPSTREAM_BASELINE_RADIUS_M`; `run_worker()`
  passes it as `PipelineConfig(poi_radius_m=...)` (a constructor kwarg, since the radius-
  bucketed paths are derived once in `__post_init__` and won't update from a post-construction
  attribute set) so every config in the sweep routes against the same baseline graph — only the
  aggregation math reacts to the mutated `decay_coefficient`, matching the module's documented
  Option-A design ("re-run accessibility + service stages from the cached travel times").
  Verified end-to-end: `decay_x0.8` now reaches the same 225574/45447-node graph as a
  from-scratch baseline rerun and completes, writing `service_scores.csv`.
- `core/config.py` (`poi_radius_m`) / `analysis/sensitivity_upstream.py`: the radius-freeze
  fix above still wasn't enough — `decay_x1.2` triggered a live multi-minute OSM POI download
  (`[POI] Query area expanded by 18.0 km buffer`, i.e. 15000×1.2). Cause: `utils/graphml.py`'s
  `get_poi()` (line 861) builds its own `cfg = PipelineConfig()` with no arguments, completely
  disconnected from the `cfg` object `run_worker()` constructs and passes to `build_context()`
  — so the frozen radius from the previous fix never reached it, and it kept re-deriving the
  radius from whatever `poi_types.csv` currently says on disk (the mutated one, for the
  duration of that worker). Fixed at the root instead of patching each disconnected call site:
  `poi_radius_m` now has a `CAP_POI_RADIUS_M` env-var default factory, following the same
  pattern already used for `CAP_STUDY_CITY`/`CAP_SAFE_MODE`/etc. — any `PipelineConfig()` built
  anywhere in a process that has this env var set picks up the override automatically, with no
  need to thread `cfg` through every call site. `sensitivity_upstream.py`'s `run_configs()` now
  sets `CAP_POI_RADIUS_M` (renamed from the bespoke `CAP_UPSTREAM_BASELINE_RADIUS_M`) on the
  worker env instead of passing a constructor kwarg. Verified: re-ran `decay_x1.2` with the env
  var set — same 225574/45447-node graph as baseline, no OSM download, completes cleanly.

## 2026-08-18

- `housing/housing_capability.py` (`electre_tri_classify_housing`): refactored into a new
  `electre_tri_housing_details()` that returns the full per-boundary ELECTRE TRI breakdown
  (concordance terms, credibility, outranking decision per boundary) instead of only the final
  Q1..Q5 category — mirrors the shape `utils/capabilities.py`'s `electre_tri_details()` already
  returns for the mobility/accessibility capabilities, minus the veto/discordance section (the
  housing model has no veto by design). `electre_tri_classify_housing()` is now a thin wrapper
  over it, so existing callers are unaffected.
- `tools/debug_pipeline.py`: added a new "Housing Affordability" section (panel 6) to the debug
  dashboard, mirroring Step 4's (Capability Scores) ELECTRE TRI presentation. New
  `_load_housing_context()` loads `housing/cagliari_nodes_omi.csv` once per run (Cagliari-only;
  `None` elsewhere), `_build_step5_housing()` looks up each sampled hex's node and runs it
  through `electre_tri_housing_details()`, and the result is threaded into `_build_chain()` as
  `chain["housing"]`. Renders per-boundary concordance tables and the assigned category as a
  chip colored via `housing_category_colors()` (the same white→green ramp used on the map
  layer), with an explicit "no data" message for nodes in a zone with no OMI prices at all
  (e.g. E5). Verified end-to-end via `run_debug_pipeline("cagliari")`: all 48 sampled hexagons
  resolved housing data correctly and the panel renders in
  `outputs/debug/Cagliari/debug_pipeline.html`.
- `housing/housing_capability.py` (`generate_housing_capability_grid`): fixed a Pylance type
  error — `row.hex_id`/`row.housing_category` from `itertuples()` are typed as the broad
  `Scalar` union (pandas-stubs), not `str`, so passing them into `_hex_geometry()` and indexing
  `category_colors[...]` (both `str`-typed) failed static checking, even though both columns
  are always strings at runtime. Fixed with explicit `str(...)` casts, matching the same
  defensive-cast pattern already used for node IDs in `tools/debug_pipeline.py`.
- `analysis/sensitivity_report.py`: removed the "reachable-node changes" (dropout) tornado
  chart section entirely — `chart_tornado_dropout()`, its call site, the HTML paragraph/table,
  and the stale `axis_notes["decay"]` prose that referenced it. This chart was structurally
  guaranteed to be all-zero for every axis that has it (decay/RRA/contribution/capacity all
  reweight already-reachable POIs, none can add/drop a node — confirmed by re-reading the
  dropout math and each axis's actual formula), so an empty chart there was never a bug; the
  2026-08-14 `CAP_POI_RADIUS_M` freeze fix is what makes it reliably zero, and a *non-zero*
  dropout would have signaled a graph-desync regression instead. `R["up_dropout"]` still loads
  (it separately feeds `n_nodes` in the report header) — only the dead chart/table was cut.
- `tools/debug_pipeline.py`: fixed the Housing Affordability panel (added earlier today)
  rendering *before* the Per-POI Export Check panel despite being numbered "6" vs "5" — the JS
  block had been inserted between Step 4 and Step 5 in the template instead of after Step 5.
  Moved the whole block (declaration, per-boundary rendering, `appendChild`) to after
  `el.appendChild(p5.panel);` so panels now render in numeric order.
- `core/config.py` (`PipelineConfig.__post_init__`): root-caused why the new Housing
  Affordability panel showed "not available" even after regenerating — `main.py:302` calls
  `run_debug_pipeline(cfg.city_slug)`, and `city_slug` is `"Cagliari"` (capitalized, derived
  from `city_name` for folder-naming), not the lowercase `study_city` config key. Inside
  `run_debug_pipeline`, that capitalized string gets threaded straight into a new
  `PipelineConfig(study_city="Cagliari")`, and the field was never canonicalized — so
  `cfg.study_city` stayed `"Cagliari"`, and `_load_housing_context()`'s
  `if cfg.study_city != "cagliari"` guard (case-sensitive) silently returned `None` for every
  hex. `apply_study_city()` already normalizes case internally for its own preset lookup, which
  is why every *other* panel worked fine — only the raw `study_city` field itself was left
  uncanonicalized. Fixed at the source instead of patching the one comparison: `__post_init__`
  now runs `self.study_city = normalize_study_city(self.study_city)` before anything reads it,
  so every current and future `cfg.study_city` comparison anywhere in the codebase is safe
  regardless of input casing. `city_slug`/`artifact_slug` (and therefore all existing on-disk
  artifact/output folder names) are untouched, since they derive from `city_name`, not
  `study_city`. Verified: regenerating via the exact `run_debug_pipeline(cfg.city_slug)` path
  now yields `cfg.study_city == "cagliari"` and all 48 sampled hexagons resolve housing data.
- `exports/poi_exports.py` (`_collect_poi_records`): fixed a lat/lon axis-swap bug affecting
  every city's `pois_used.gpkg` (found while investigating why Cagliari's coast wasn't showing
  elevated `nature_contact` — turned out unrelated, see below, but this bug was real and
  independently confirmed by reading the code). For the cached-GeoJSON/no-GDAL path (what both
  Cagliari and Paris use), `__snap_coord` tuples are documented and verified as `(lat, lon)`
  (`utils/graphml.py`'s `_geojson_vertices`), but were fed into `shapely.points(x=..., y=...)`
  unswapped, so every exported POI's geometry and `lat`/`lon` columns had latitude and longitude
  reversed (e.g. Cagliari plotting at ~39°E, 9°N instead of ~9°E, 39°N). Fixed by swapping which
  tuple index each list comprehension pulls; the downstream `lat = point.y; lon = point.x` read
  already assumed correct construction, so it needed no change. Checked for the same bug
  elsewhere (`analysis/scenarios.py`'s `_is_mirrionis_representative_point`) — that one already
  builds points correctly, so this was an isolated occurrence. The on-disk `pois_used.gpkg`
  files predate this fix and will self-correct on the next `generate_poi_exports` run.
  - Investigation note (no code change): the actual "coast should have higher nature_contact"
    question resolved as *not a bug*. Checked the real service-score matrix directly (bypassing
    the broken export entirely): `nature_contact` genuinely is elevated near Poetto beach
    (~0.75-0.81 vs ~0.74 city-wide). What's shown on the capability-grid map is `restorativeness`
    (`config/capability.csv`), which equally weights `nature_contact` with 4 other services
    including `quietness` — and coastal nodes are held to "High" (never "Very High") specifically
    because `quietness` is comparatively weak there (busy beachfront promenade), not because of
    any nature_contact deficiency. Confirmed via `utils.capabilities.electre_tri_details` on real
    node data: near-coast quietness ~0.43-0.46 vs scenic_views/nature_contact ~0.79-0.93.
- `core/config.py` / `exports/poi_exports.py` / `analysis/score_report.py`: Paris runs were
  crashing on memory during the post-routing export stage — the per-hexagon POI "list of
  belonging" and its score computation both scan the full `poi_radius_m`-derived cache
  (~15km for Paris), which is far more data than the interface export actually needs. Added a
  new export-only knob, `PipelineConfig.export_hex_radius_m` (`None` everywhere except Paris,
  where it's `5000.0`, via the existing `CITY_PRESETS`/`apply_study_city` per-city-override
  pattern) — explicitly independent of `poi_radius_m`, which the real accessibility/capability
  computation still uses unchanged. Applied as a post-hoc haversine-distance filter in two
  places: `exports/poi_exports.py`'s `_stream_node_worker` (using `payload["origin"]` and each
  candidate's `source_coords`, both already-correct coordinates straight from the non-bus
  routing cache, verified against `non_bus_routing_stage.py`'s `origin = (data["y"], data["x"])`
  construction) trims the membership list itself; `analysis/score_report.py`'s
  `_score_node_worker` (using origin coords from `ctx.nodes_with_coords` and POI coords newly
  added to `_read_poi_table`'s query, now reliable thanks to the lat/lon-swap fix above) trims
  the score computation to match. Both stages apply the filter independently rather than
  threading a shared allowlist between them, since each already had (or could cheaply obtain)
  the coordinates it needed.

## 2026-08-19

- `analysis/score_report.py` (`_score_node_worker` / node-scoring `mp.Pool`): a Paris run OOM'd
  during the post-routing export/scoring stage even with `export_hex_radius_m=5000` already in
  place (2026-08-18 above) — the 5 km radius cut per-node work, not the pool's memory hygiene.
  Kernel OOM dump showed all 8 sibling worker processes (matching `score_report_max_workers=8`)
  sitting at 4.4-8.5 GB RSS each (~47 GB combined) by the time of the crash. Root cause: unlike
  `non_bus_routing_stage.py`'s and `poi_exports.py`'s worker pools (both hardened after their own
  prior OOM incidents with `maxtasksperchild` + periodic `gc.collect()`/`malloc_trim(0)`), this
  pool — added 2026-08-04, flagged in `core/config.py`'s `score_report_max_workers` comment as
  "less battle-tested" — had neither: it never recycled workers and never trimmed. Its
  `_WORKER_POI_WEIGHTS`/`_WORKER_POI_COORDS` dicts are shared copy-on-write from the fork, but
  per-node refcounting touches (per `config.py`'s own note) tend to fragment that into private
  per-page copies over time, and with no worker recycling and no trim, that fragmentation only
  ever grows across the pool's entire lifetime. Fixed by mirroring the existing pattern exactly:
  added `_trim_score_memory()` (gc.collect + malloc_trim(0), same as `poi_exports._trim_export_memory`),
  called every 20 nodes via a per-worker counter, and added `maxtasksperchild=200` to the pool
  (mirrors `poi_exports.py`'s export pool).
  - Follow-up: the user reported the memory throttle firing again on the next run while the
    scoring progress bar was still at 0 nodes — before any worker could have done enough
    per-node work to explain it, ruling out the trim/recycle fix above as the whole story.
    `generate_score_report(ctx=ctx)` runs in-process at the tail of the same long-lived
    `main.py` process that already ran routing/accessibility (`main.py:261-262`,
    `pipeline_runner.py:892`), and `mp.Pool` defaults to the `fork` start method on Linux --
    so every worker inherits a copy-on-write snapshot of whatever's still resident in that
    parent heap the instant the pool is created, independent of `poi_weights`/`poi_coords`
    or any per-node work. CPython's refcounting touches pages on nearly every operation, so
    that inherited heap starts splitting into private per-worker copies almost immediately
    post-fork. Fixed by switching this pool to `mp.get_context("spawn").Pool(...)`
    (`analysis/score_report.py`, the node-scoring pool): a spawned worker starts a fresh
    interpreter and holds only what `_init_score_worker`'s `initargs` explicitly pass it, so
    worker memory no longer depends on what earlier pipeline stages left resident. Not yet
    verified against a real run.
  - Follow-up 2 (architectural, not yet run): discussing the throttle-at-0-progress incident
    raised a separate question -- why does `score_report.py` fully recompute per-hexagon POI
    membership from scratch when `exports/poi_exports.py`'s post-routing pass (`pipeline_runner.py:889`,
    right before `generate_score_report` at line 892) already computed almost the same thing?
    Traced the two: `poi_exports.py`'s stream reads the raw `non_bus_cache_dir` payloads (every
    POI within `poi_radius_m` that was queried per origin -- a candidate superset, pre-fusion)
    while `score_report.py` read `accessibility_poi_by_node_dir` (the post-RRA-fusion set with
    nonzero accessibility -- necessarily a subset of the candidates). Not a bug -- genuinely
    different data -- but `score_report.py` had no reason to re-derive membership via its own
    haversine radius filter (`_WORKER_POI_COORDS`/`_WORKER_EXPORT_HEX_RADIUS_M`) when it could
    reuse `poi_exports.py`'s already-filtered candidate lists instead. Refactored across three
    files:
    - `exports/hex_shard_writer.py`: added `read_shard_poi_ids(out_dir, shard_name)`, decoding
      an already-finalized shard `.js` file back to `{hex_id: [poi_id, ...]}` -- the Python
      counterpart of `hex_shard_loader.js`'s client-side decode.
    - `exports/poi_exports.py`'s `_stream_hexagon_export` call site: added `resume=True` +
      `mark_scoring_complete()` to its `HexShardWriter`, mirroring `score_report.py`'s existing
      resume pattern (crash during the stream loop or during `finalize()`'s shard-packing pool
      no longer means redoing the whole stream from node 0).
    - `analysis/score_report.py`: replaced `_score_node_worker` (per-node, haversine-filtered
      against a full-city `poi_coords` dict held in every worker) with `_score_shard_worker`
      (per-shard: reads that one `poi_exports.py` shard's candidate list once, then scores
      every node in it against that list -- no haversine filtering, no `poi_coords` dict in
      worker memory at all). Nodes are grouped by `shard_name_for_hex(hex_id)` before dispatch
      so each pool task is "one shard's nodes," guaranteeing a worker only ever reads the shard
      files it's actually assigned (the user's explicit requirement) rather than an LRU cache
      of whatever it happened to touch recently. Since `poi_exports.py` and `score_report.py`
      write to the same `cfg.hex_pois_dir` (the latter's richer output supersedes the former's
      id-only one), and `HexShardWriter.__init__` wipes `hex_pois_dir` on construction unless
      resuming its own prior pass, `poi_exports.py`'s finished shards are moved aside (`shutil.move`,
      not copied) to a sibling `_poi_export_src` directory before `score_report.py`'s own writer
      is constructed -- `_parts_tmp/` is left in place so `score_report.py`'s own self-resume
      marker survives the move. The moved-aside directory is deleted once scoring completes
      (fully superseded by the scored output by then). Also considered and rejected: preloading
      all of `poi_exports.py`'s candidate data into one dict passed to every worker via
      `initargs` -- unlike the per-POI `poi_weights` dict, a candidate map has one entry per
      (hex, POI) pair within radius, so at Paris's 5 km export radius it could plausibly be
      larger in total than the flat structures this whole investigation has been trying to
      bound. Not yet verified against a real run.

## 2026-08-20

- `core/pipeline_runner.py` (`_stop_color_expr`): service-grid hexagons on the exported
  GeoPackage all rendered as one flat color when a colleague opened the file in QGIS on macOS,
  while the same file looked correct on Linux/Windows. Root cause: the per-feature fill-color
  data-defined override used `color_mix(color1, color2, ratio)`, a QGIS expression function only
  available from QGIS 3.24 onward; on an older QGIS build the expression fails to evaluate and
  QGIS silently falls back to the symbol's flat base color for every feature. Fixed by dropping
  `color_mix()` in favor of a hand-rolled linear RGB interpolation built from `color_rgb()` (a
  QGIS 2.x-era core function) fed by literal per-stop R/G/B ints computed in Python at generation
  time -- same CASE/WHEN structure, no version-dependent expression function.
- `analysis/sensitivity_upstream.py` / `analysis/sensitivity_report.py`: the contribution-coefficient
  sensitivity sweep (`contribution_min`/`contribution_max`) and its report prose hardcoded the tier
  set as `{1, 2, 3, 5, 8}` ("Fibonacci-like"), but `config/services.csv`'s real tier set is now
  `{5, 10, 30, 50, 80}` -- the sweep was pinning every POI type to 1/8, well inside the real range
  rather than at its true extremes, understating how load-bearing the parameter actually is. Fixed
  by replacing the two hardcoded constants with `_contribution_tier_bounds()`, which reads
  `config/services.csv` live and returns `min`/`max` of the actually-configured tiers (now 5/80);
  `sensitivity_report.py`'s prose (`UPSTREAM_CONFIG_DESC`, `axis_notes["contribution"]`) now
  interpolates the same live constants instead of literal 1/8 text. Also fixed two smaller
  instances of the same class of bug in `sensitivity_report.py`: `"11 services"` (stale -- actual
  count is `len(serv.SERVICE_KEYS)` = 12) in two spots, now interpolated live; and `LEVELS`, a
  second hand-typed copy of the five ELECTRE class names, now imports `utils.capabilities._CATEGORIES`
  (the same source `sensitivity_analysis.py`/`robustness_analysis.py` already use) instead of
  re-typing the list.
- `analysis/sensitivity_report.py` / `analysis/sensitivity_upstream.py`: the RRA lambda-taper
  prose hardcoded `λ = (1, ½, ⅓, ¼)`, i.e. 4 modes (walk/bike/drive/bus) -- stale for Cagliari,
  which now has `enable_subway: True` (`core/config.py:69`), giving `utils.decay.calculate_rra`
  m=5 modes in the real run. Fixed by adding `RRA_MODE_COUNT = 5 if _cfg.enable_subway else 4`
  and `_rra_lambda_taper_str(m)` (builds "1, 1/2, ..., 1/m" for whatever m actually is) in
  `sensitivity_report.py`, used by both `rra_lambda_uniform`/`rra_lambda_reversed` prose entries;
  also de-numbered a matching "3 slow backup modes" comment in `sensitivity_upstream.py` that
  can't be made dynamic (it's a plain comment, not interpolated).

## 2026-08-24

- `tools/debug_pipeline.py`: Paris's `debug_pipeline.html` ballooned to ~29MB even with
  `LIGHT_MODE` capped at 40 hexes, because the entire per-hex computation chain (steps 1-4 +
  verify, ~0.8MB/hex) was embedded as one giant `CHAIN_DATA` JS object literal that had to be
  fully parsed before the map could render at all -- that parse, not hex count, was what made
  the map "take an infinity to load". Split into a small eagerly-loaded `MAP_DATA` (hex
  vertices, node coords, a deduped list of POI marker positions/names pulled from step1) plus
  one `<script type="application/json" id="hex-data-{hexId}">` per hex holding the heavy
  step1-4/verify payload, lazily `JSON.parse`d by `getHeavyData()` only when that hex is
  clicked (`_build_html`, `selectHex`). Stays fully self-contained (no `fetch()`/server), so
  the report still opens by double-clicking the file. Map now renders instantly regardless of
  how much data is embedded.

## 2026-08-25

- `tools/debug_pipeline.py` / `tools/inspect_hex_pois.py`: `_select_hexagons` hard-required
  `hex_pois/index.js` (POI export) just to enumerate hex ids, so a `LIGHT_OUTPUT=True` run
  (which skips POI export) crashed `run_debug_pipeline` with `FileNotFoundError` even though
  hex selection itself needs no per-POI data. Added `_list_hex_ids_from_grid_gpkg` (reads hex
  ids straight from the `zz_capability_grid` layer, which `generate_spatial_outputs` always
  writes regardless of `LIGHT_OUTPUT`) as a fallback when the shard directory is missing.
- `tools/debug_pipeline.py`: Paris/mgp_boundary has 806 "quartiere" place-label entries (one
  hex each) vs. Cagliari's handful, mixing true Paris neighbourhoods with suburb neighbourhoods
  OSM tags the same way -- selecting one hex per entry was both far too many hexes and not
  actually "one per Paris quartier". `_select_hexagons` now builds a convex hull from the 20
  arrondissement-named quartiere points as a city-boundary proxy (no authoritative Paris
  boundary exists in config), keeps only quartiere points inside it (103 hexes), and adds a
  bounded, evenly-strided sample of "comune" points from outside it (`METRO_COMMUNE_SAMPLE =
  40`) as a second group covering the wider metropolitan area -- 143 hexes total instead of
  806. Cities without arrondissement-named entries (Cagliari) fall through with every quartiere
  selected, unchanged. `LIGHT_MODE_MAX_HEXAGONS` raised 40 -> 150 so this set isn't
  re-truncated.
- `main.py`: added `DEBUG_REPORT_ONLY` knob -- when true, skips the whole pipeline and just
  calls `run_debug_pipeline(cfg.city_slug)` against whatever artifacts already exist on disk,
  for iterating on `debug_pipeline.py` itself (or regenerating the report after a Paris run)
  without a full rerun.
- `core/profiles.py` / `stages/accessibility_stage.py`: `Profile.affordability` was a flat
  multiplier applied to every POI's accessibility regardless of poi_type, so the elder/student
  personas degraded all 12 services -- including free/public ones like parks, quiet spaces, and
  public healthcare -- by the same fraction, a blanket penalty with no real basis. Replaced with
  `PAID_POI_TYPES`, a curated set of the 8 poi_types (of 36) that are actually market/
  discretionary spending (organised sport, cinema/museums, dining, private wellness/spa),
  sourced from the real OSM tag definitions in `config/poi_types.csv` (e.g.
  `informal_sport_outdoor`, tagged to parks/beaches/trails, stayed free even though its sibling
  `informal_sport_indoor` -- fitness centres, indoor pools -- did not). `Profile.utility_for`
  now takes `poi_type` and only applies `affordability` to `PAID_POI_TYPES`, leaving the other
  28 poi_types at u=1.0 for every persona. `_utility_for_source_key` in `accessibility_stage.py`
  updated to take `poi_type` and check `PAID_POI_TYPES` (lazy import, mirroring the existing
  canteen-lookup pattern); its one call site (inside `_compute_per_poi_accessibility`) already
  had `poi_type` in scope.

## 2026-08-26

- `utils/services.py`: added `CONTRIBUTION_TIER_REMAP`, a module-level dict remapping the
  discrete `contribution_coefficient` saturation tiers read from `services.csv` (e.g.
  `{5:15,10:30,30:90,50:150,80:240}`) without hand-editing every row, applied in
  `_load_service_weights`. Currently `None` (inactive; commented-out example left in place).
- `analysis/calibrate_electre_boundaries.py`: pointed `CITY_SLUG` at `"mgp_boundary"` (Paris)
  instead of `"Cagliari"` to calibrate ELECTRE TRI boundaries from the Paris run's own
  distribution. Its `jenks_natural_breaks` is an exact O(n^2) DP -- fine at Cagliari's ~4,689
  pooled values (1,563 nodes x 3 capabilities) but hung indefinitely at Paris's ~69,138 (23,046
  nodes x 3); killed after a 90s timeout rather than left to run indefinitely, per fail-fast.
  Added `JENKS_SAMPLE_CAP = 4689` (matching Cagliari's own scale) with a random subsample
  before the DP -- ran in seconds afterward, producing `[0.3889, 0.624, 0.7548, 0.8408]`
  (rounded to `[0.39, 0.62, 0.75, 0.84]`).
- `core/config.py`: `ELECTRE_BOUNDARIES` was a single hardcoded list calibrated against
  Cagliari, so any other city (Paris) silently classified against boundaries calibrated on a
  different distribution. Replaced with `ELECTRE_BOUNDARIES_BY_PROFILE` (one calibrated set per
  city) and `ELECTRE_BOUNDARIES_PROFILE`, which auto-follows `CAP_STUDY_CITY`/`study_city`
  (mirroring `PipelineConfig.study_city`'s own default) rather than a separate manual knob, so
  the boundaries always match whichever city is actually configured; falls back to Cagliari's
  set for any city without its own calibration. `"paris"` is currently pointed at Cagliari's
  values `[0.31, 0.44, 0.56, 0.71]` for a deliberate side-by-side comparison run -- revert to
  Paris's own `[0.39, 0.62, 0.75, 0.84]` afterward.
- `analysis/sensitivity_upstream.py`: the upstream sweep (11 full accessibility+service
  subprocess reruns, strictly serial) was flagged as too slow on Paris's full ~23k nodes, even
  though every axis/direction is still needed (no config dropped). Added
  `UPSTREAM_LIGHT_MAX_NODES_BY_CITY` (per-city node cap, `{"paris": 3000}`), passed into each
  worker's `PipelineConfig(debug_max_nodes=...)` -- reuses the existing seeded node-subsampling
  knob (`core/context.py`'s `debug_max_nodes` handling, already used by `main.py`'s debug runs)
  instead of adding new sampling logic. Cagliari stays uncapped since it must match the node
  set its cached impedance bundle was actually routed for.

## 2026-08-31

- `analysis/calibrate_electre_boundaries.py`: `main()` previously pooled only one city
  (`CITY_SLUG = "mgp_boundary"`, i.e. Paris) for the Jenks calibration. Replaced with
  `CITY_SLUGS = ["mgp_boundary", "Cagliari"]`; `main()` now loops over both cities, loading
  each one's experiment CSV and computing their per-node ELECTRE-weighted means. Docstring
  updated to describe both cities.
- Same file: first pooled both cities' values into one list and drew a single uniform
  `rng.choice` subsample against `JENKS_SAMPLE_CAP` — since Paris's pool (~69k) dwarfs
  Cagliari's (~4.7k), a uniform draw let Paris dominate the sample instead of the two cities
  being represented equally. Restructured to keep each city's values separate
  (`pooled_by_city`) and split the cap evenly per city (`per_city_cap`, remainder to the first
  city) before subsampling each city independently and concatenating. Also bumped
  `JENKS_SAMPLE_CAP` from 4689 to 9000 (now split ~4500/4500 across the two cities) per
  explicit instruction.
- Ran `python -m analysis.calibrate_electre_boundaries` (plain `python analysis/...py` fails
  with `ModuleNotFoundError: No module named 'analysis'`, same self-import quirk noted for
  `sensitivity_upstream.py` on 2026-08-14 — the script's own directory lands on `sys.path[0]`,
  not the repo root). Produced pooled Jenks breaks `[0.3485, 0.5111, 0.6601, 0.8012]` from
  Paris's 23,046 nodes + Cagliari's 1,563 nodes (4,500 subsampled from each). `core/config.py`'s
  `ELECTRE_BOUNDARIES_BY_PROFILE` previously had separate calibrated sets per city
  (`"cagliari": [0.31, 0.44, 0.56, 0.71]`, `"paris": [0.39, 0.62, 0.75, 0.84]`); replaced both
  with the same shared, rounded set `[0.35, 0.51, 0.66, 0.80]` so every city now classifies
  against one universal boundary.

