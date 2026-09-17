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


## 2026-09-07

- Investigated why `artifacts/mgp_boundary/impedances.npz` had grown to 107 GB (345 GB for the
  whole artifacts dir). Measured its actual composition: only ~32% of the bytes are impedance
  values. Of the 114 GB uncompressed, `non_bus_blobs/` is 88.8 GB and the bus + subway matrices
  are 12.6 GB each; within a blob, `poi_coords` is exactly 50% of the array bytes, the three
  `imp_*` arrays 12.5% each, `kept_idx` 12.5%. The two matrices are 86.7%/88.4% exact zeros
  (subway's median row has *zero* nonzeros). The bundle also ships the working non-bus cache
  verbatim — `exports/artifact_bundle.py` `zf.write(cache_path, ...)` zips the scratch `.pkl`
  files in as-is, pickle framing included — which is what `poi_coords` was doing there at all.
- Established that the blob's `poi_coords` is never user-facing geometry: exported POI
  coordinates come from the shared catalog (`exports/poi_exports.py` reads
  `catalog["source_coords"]`), and all three consumers of the blob copy used it purely as an
  addressing key — `accessibility_stage` and `debug_pipeline` both hashed the rounded lat/lon
  into a bus/subway matrix column, and the only true geometric use was the radius haversine.
  Replaced the geometry with the addressing it was used to compute.
- `utils/delta_g.py`: added `_dest_col_map`, a per-worker-cached `{rounded (lat, lon) ->
  matrix column}` map built with the same 6-decimal rounding `accessibility_stage` used, so a
  POI resolves to the identical column on the write and read sides. Verified the resulting map
  is equal to the map the old read path built from the same files. Bus and subway were found to
  share one destination set (`bus == subway` as dicts; `dest_col == subway_col` on 181380/181380
  POIs), since both routing runs use the same snap output — so one map addresses both matrices
  and the function raises if a city ever diverges. Also moved `_haversine_m_np` here from
  `accessibility_stage` (it was documented as the array mirror of this file's `_haversine_m`)
  and gave it the file-local `import numpy as np` the other functions use.
- Same file, `accessibility_non_bus_from_snap_map`: `poi_coords` is now a per-origin transient
  only. The return dict drops it in favour of `dest_col` (-1 when the POI reaches no stop) and
  `in_radius` (the same radius test, but against the *snapped* coord — `kept_idx` upstream
  filters on the *source* coord, so the two differ for POIs that snap across the boundary).
  This moves the per-POI coord-hash loop off the accessibility hot path and onto routing, where
  it runs once instead of on every read.
- `routing/non_bus_routing_stage.py`, `stages/accessibility_stage.py`,
  `tools/debug_pipeline.py`: propagated the new entry shape. The accessibility stage now reads
  `entry["dest_col"]` / `entry["in_radius"]` instead of re-deriving them, which deleted the
  coord-hashing loop, the radius haversine, `_haversine_m_np`, `origin_coord`, the
  `_BUS_/_SUBWAY_DEST_COORD_TO_COL` globals and the `_global_radius_m` machinery — each worker
  had been parsing two ~140k-row destination CSVs into dicts it now never reads. `_bus_time` in
  the debug tool takes a column instead of a coord; its reported `src_lat`/`src_lon` came from
  the catalog and are unchanged.
- `core/config.py`: bumped `non_bus_cache_schema_version` 10 -> 11 (existing blobs carry
  `poi_coords` and no `dest_col`, so without the bump `_is_valid_non_bus_cache` would accept a
  stale blob and the accessibility stage would die on the missing key).
  `exports/artifact_bundle.py`: bumped `ARTIFACT_SCHEMA_VERSION` 3 -> 4.
- First wrote `scripts/convert_non_bus_cache.py` to migrate the on-disk v10 blobs in place
  (verified on three real Paris blobs: 34.3% smaller, all `imp_*`/`kept_idx` bit-identical,
  100% of columns resolved), then deleted it: converting the *cache* only helps the artifact
  indirectly, since `write_impedance_bundle` builds the artifact from the cache. Replaced by a
  direct bundle-to-bundle conversion (below), after which loading a v4 bundle regenerates the
  cache anyway, so there is one migration route rather than two.
- Rejected a vectorised `np.searchsorted` version of that lookup after measuring it: Python's
  `round()` rounds the decimal value half-to-even while `np.round` does multiply-rint-divide in
  binary, and the two disagree on exact ties — 5% of these snapped latitudes are ties, giving a
  9.7% mismatch rate. A mismatched POI would resolve to -1 and silently lose its transit
  impedance, so both the converter and `delta_g` keep the scalar Python dict lookup (the same
  reasoning as the existing scalar-haversine comment in the `kept_idx` radius filter).
- `exports/artifact_bundle.py`: added the schema-4 codec, shared by the writer, the reader and
  the converter so all three agree by construction. `_shuffle_bytes`/`_unshuffle_bytes` group a
  float32 array's byte planes before compression (the exponent bytes are nearly constant across
  travel times, so planing them makes runs a compressor can use; measured 1.94x with lzma vs
  1.48x on interleaved bytes, and it is a permutation, not a quantization).
  `_pack_kept_idx`/`_unpack_kept_idx` replace the int32 index list with a presence bitmask over
  the shared catalog (~14x smaller at the measured ~47% coverage). `encode_blob`/`decode_blob`
  serialize one node as a JSON header plus ONE lzma stream over all its arrays -- compressing
  per-array instead measured 1.40x vs 1.73x joined, since lzma needs a wide window to find the
  cross-array redundancy. `encode_matrix_csr`/`decode_matrix_csr` store the two matrices as CSR
  (they are 86-88% exact zeros; zero means "no service" and already decodes to zero decay via
  `_matrix_decay`'s `vals > 0`, and CSR reconstructs it as exactly 0.0). `_write_shuffled_lzma`/
  `_read_shuffled_lzma` stream that in 64 MB chunks so peak memory is one chunk rather than the
  ~1.7 GB a dense city's CSR index array reaches. `_memmap_zip_member` maps an uncompressed .npy
  member in place, so a 12.6 GB matrix is read straight out of the .npz at an offset instead of
  being extracted to a temp file; `_restore_matrix_fast` became unused and was removed.
- Same file: rewrote `load_impedance_bundle` and `write_impedance_bundle` for schema 4. The
  bundle is now described by a `manifest.json` member (schema versions, node ids, signatures,
  matrix headers, and `poi_radius_m` -- recorded so the artifact is self-describing rather than
  silently depending on the recipient's config matching the writer's). Restore still produces
  exactly the on-disk layout the live stages write (catalog pickle, row/column JSON indexes,
  destinations CSV via the new `_write_mode_sidecars`, dense `.dat` memmaps), so nothing
  downstream can tell whether a run routed or restored. Both directions keep a thread pool:
  measured that lzma releases the GIL (5.83x on 8 threads, 11.2 min -> 1.9 min to decode 23k
  blobs), so the pool now tracks core count rather than the old 4x I/O oversubscription, and
  the writer encodes in parallel but writes serially in bounded windows since `ZipFile` is not
  thread-safe.
- Replaced `scripts/convert_paris_impedance_bundle.py` (was a one-time schema 2 -> 3 migration)
  with a schema 3 -> 4 one. It streams `.npz` -> `.npz` directly -- no scratch cache, no
  pipeline run, no re-routing -- and is self-contained: the column map is rebuilt from the
  bundle's own `bus_dest_coords` (verified equal to the map built from the on-disk CSV), so the
  conversion needs nothing but the input file. Verified end-to-end on a miniature bundle built
  from real Paris data (3 real node blobs, the real 136,744-destination catalog, real matrix
  rows): every `imp_*`/`kept_idx` array bit-identical, both matrices bit-identical after the CSR
  round-trip, `dest_col` matching a direct hash of the original `poi_coords` on 503,000/503,000
  POIs, `in_radius` matching the original haversine on 503,000/503,000, and zero unresolved
  columns. Then round-tripped the whole chain (v3 -> convert -> v4 -> load -> write -> load)
  with everything still bit-identical.
- Measured projections for the real Paris artifact: blobs 88.8 GB -> ~24.5 GB (3.6x per blob,
  measured), matrices 25.2 GB -> ~2.5 GB (10.15x, measured on 2000 real rows), so ~107 GB ->
  ~27 GB, entirely lossless. The 13 GB target is below the lossless floor: after removing all
  redundancy the remaining bulk is ~17 GB of genuine float32 travel times across three modes,
  and reaching 13 GB would require quantizing them (uint16 at 0.01-min resolution) or shipping
  fewer modes, neither of which is in this change.
- Added progress reporting to the conversion path, since the long stretches were invisible.
  In `exports/artifact_bundle.py`, `encode_matrix_csr`/`decode_matrix_csr` now carry a bar
  across their row passes (a dense city reads ~25 GB there) and `_write_shuffled_lzma`/
  `_read_shuffled_lzma` take an optional `desc` for a byte-scaled bar (lzma at preset=1 moves
  tens of MB/s, so a 1.7 GB CSR index array is minutes of silence). All are driven by counters
  already known -- bytes consumed, rows processed -- rather than a separate counting pass, and
  the inner bars use `leave=False` so they clear rather than pile up.
  `scripts/convert_paris_impedance_bundle.py` got a `_log` helper that stamps every line with
  elapsed mm:ss, numbered phase banners (matrices / metadata / blobs), a blob bar whose postfix
  reports running GB in->out and the live compression ratio (refreshed every 200 nodes to keep
  formatting off the hot path) with `smoothing=0.05` so the ETA is stable across 23k
  variable-sized items instead of tracking the last few, and a closing breakdown that splits
  matrices from blobs so a disappointing total says which part underperformed. Verified the
  rewritten loop (now `src.read` rather than `src.open`, so bytes can be measured) emits
  byte-identical blobs to the pre-logging version.
- `tools/debug_pipeline.py` `_build_chain`: added the non-bus cache schema-version check that
  this reader was missing. `accessibility_stage` guards its reads with
  `_is_valid_non_bus_cache`, but the debug tool unpickled and trusted the shape, so running
  `main.py` with `DEBUG_REPORT_ONLY` against a pre-schema-4 cache surfaced as
  `KeyError: 'dest_col'` several frames deep rather than saying the cache was stale. Raises
  rather than skipping the node: every blob is stale at once, and skipping would emit a report
  full of silently empty chains.
- Ran the real migration on Paris: `impedances.npz` 107 GB -> 23.9 GB (4.5x), all 23,046 node
  blobs and both matrices present (bus nnz 431,383,809 = 13.7% of dense; subway 371,223,606 =
  11.8%), and loading it rewrote the whole non-bus cache to v11 (83 GB -> 55 GB, all 23,046
  sidecars at v11). Nothing quantized: the reduction is entirely removed duplication plus
  lossless compression.
- Checked dependency portability after the change: it adds no third-party package (the imports
  are numpy/tqdm, both already pinned) and drops one, since the rewritten converter no longer
  needs geopandas -- so `pip install -r requirements.txt` on a fresh Windows or Linux clone is
  still sufficient. `lzma` is new to the repo but is stdlib and ships prebuilt in every Windows
  CPython; only source-built Linux Pythons need xz-devel present at build time, which this
  machine's pyenv build has. Benchmarked zstandard as an alternative (zstd-19: 2.18x vs lzma's
  2.24x on blobs, but 20.7x vs 13.8x on CSR indices and ~23x faster decompression) and did not
  switch -- roughly +0.5 GB overall, and it would have invalidated the freshly converted
  bundle for no requirement that actually needed satisfying.
- Same script: Pylance flagged `enumerate(subway_dest_coords)` as possibly-None, since it
  cannot correlate the `has_subway` bool with the optional array assigned beside it. Narrowed
  on the array itself (`if subway_dest_coords is not None`) at both use sites rather than
  casting, and extracted the coord->column dict comprehension (duplicated verbatim for bus and
  subway) into `_coord_col_map(dest_coords: np.ndarray)`, whose non-optional parameter fixes
  the complaint at its source. Verified the refactor emits a byte-identical bundle.
- Same file, two more Pylance optional-narrowing complaints of the same shape: `node["lat"]`
  in `_pick_hex_for` (the non-None guarantee lived in a separate `nid` symbol, so the checker
  could not follow it) and `imp_*_arr[i]` (guarded by `_reachable`, which does the None test
  inside the function and returns only a bool). Fixed both by making the guarantee visible
  rather than casting: `_pick_hex_for` now narrows on `node` itself with an early `continue`
  (which also flattens a nesting level, and keeps the empty-node_id case the old `if nid and
  ...` covered), and the three `imp_*` reads use `entry[...]` instead of `entry.get(...)`,
  since delta_g writes all three unconditionally in both its normal and empty returns -- the
  `.get()` was claiming an optionality that does not exist.
- `_load_housing_context`: Pylance flagged `float(row.buy_price_eur_sqm_month)` and the rent
  field (`Argument of type "Scalar" cannot be assigned ... "complex" is not assignable to
  "ConvertibleToFloat"`) -- a pandas-stubs quirk where `itertuples()` types every field as
  `Scalar`, a union that includes `complex`, which `float()`'s stub rejects. Switched to
  `df.to_dict("records")` (dict access types as `Any`, sidestepping the union) instead of
  casting. Verified against the real 1,462-row `housing/cagliari_nodes_omi.csv`: identical
  output to the old itertuples version.

## 2026-09-08

- `config/poi_types.csv` (`high_nature_immersion` row): `nature_contact` was scoring low
  around Poetto (Cagliari's beach). Traced the service (`config/services.csv`) down to its
  three POI types and found `natural=coastline` fed `scenic_views` (via `natural_aesthetic`)
  but nothing in `nature_contact` -- only `natural=beach` (in `accessible_nature`) connected
  the service to the sea at all, and that row's decay coefficient (12) is short, so proximity
  to the coastline itself wasn't credited. Added `{"natural": "coastline"}` to the `tags`
  column and `"natural_coastline"` to the `labels` column of `high_nature_immersion` (decay
  30), so coastline proximity now also feeds `nature_contact`.

## 2026-09-08

- Diagnosed a "differences.gpkg shows unexplainable high scores" report: verified numerically
  that the aggregation math is monotonic under POI removal (accessibility_from_rra's rank-based
  Choquet weights are a fixed decreasing sequence, so zeroing a POI's value can never increase
  the sum -- swept coefficients 0.5-10 and set sizes to 200, delta never positive; the
  service->capability ELECTRE-TRI step uses fixed structural weights, not renormalized by which
  services are "active", so it's monotonic too) and that the on-disk differences.gpkg files
  showed no positive deltas at all. Traced the actual issue to staleness instead: the boundary
  recalibration commit (b9ae0172, 2026-09-01) landed after every scenario output on disk
  (differences.gpkg/profile_comparison.qgz from 2026-08-31, the merged
  all_scenarios_differences.gpkg from 2026-08-26). Confirmed capability_score_mode="discrete"
  (production default) bakes the ELECTRE classification into the stored values at compute time,
  not just the legend, so this needs a regeneration, not a re-style. Since underservice-is-
  mirrionis is a bundle-reuse (not persona) scenario, this is fast to fix: RUN_ALL_SCENARIOS ->
  False, SCENARIO -> "underservice-is-mirrionis" in analysis/scenarios.py, then
  `python analysis/scenarios.py` -- reuses the already-routed impedance bundle, no re-routing.
- Replaced the service/capability map color scheme with a user-specified palette. Added
  `utils/capabilities.py`'s SERVICE_COLOR_STOPS (10 hex stops, one per 0.1-wide service-score
  bucket: d7191c red -> 1a9641 green) and `service_step_color()`, plus CAPABILITY_COLOR_STOPS
  (5 stops: the service scale's two endpoints plus fdae61/ffffc0/a6d96a) for the capability
  grid's ELECTRE classes. Verified both interval boundaries with the user before implementing --
  '#d791c' was a truncated '#d7191c', and '#ffffc0' (not ColorBrewer's ffffbf) was confirmed as
  typed. Propagated into all 5 consumers: core/pipeline_runner.py (live gpkg/qgz styling --
  service grids moved from a continuous 5-stop white->hue gradient to a genuine 10-bucket CASE
  step expression, capability grids from a per-capability computed shade to the fixed 5-color
  list), exports/generate_capability_legend.py, exports/generate_heatmap_legends.py (collapsed
  from 3 per-capability bars to 1 shared service_legend.png, since every service now shares one
  scale -- reuses service_step_color directly rather than re-deriving the bucketing a third
  time), and analysis/scenarios.py's comparison-grid styling. Deliberately left
  generate_power_scaling_dashboard.py on the old per-capability hues (#006BFF/#FFA200/#EB4CCC):
  it uses CAPABILITY_COLORS to give 3 chart GROUPS distinct categorical identity, not to encode
  a magnitude scale, so swapping it for the new shared gradient would have made two of three
  groups look confusingly similar rather than fixed anything -- confirmed with the user before
  touching it. capability_shade_hexes/CAPABILITY_SHADE_FRACTIONS/CAPABILITY_COLORS/
  get_capability_colors were kept (not deleted) since the user framed this as "for now" and the
  dashboard still needs CAPABILITY_COLORS. Verified end to end: all 5 files import clean,
  the generated QGIS CASE expression buckets identically to service_step_color at every 0.1
  boundary, and both a rendered service-score bar (10 bands) and a capability legend (5 bands,
  boundaries correctly reading 0.0-0.3/0.3-0.5/0.5-0.7/0.7-0.8/0.8-1.0 from the same
  already-fixed ELECTRE_BOUNDARIES) look correct.

## 2026-09-09

- Diagnosed a real portability problem: pasting an existing impedances.npz into a fresh
  clone printed "Bundle origins_sig doesn't match the current node set". Traced the root
  cause -- `utils/graphml.py`'s Cagliari/boundary-based graph path (`ox.geocode_to_gdf` +
  `ox.graph_from_polygon`, lines ~472-491) downloads live from Nominatim/Overpass with no
  pinned OSM snapshot or date, so two machines (or the same machine at different times)
  building "the same" city graph aren't guaranteed to get the same node set. Confirmed
  `get_mode_csr` doesn't need the graph -- if the .graphml is absent it loads the small
  cached `_csr_v2.npz` directly rather than rebuilding, so a bundle can be made portable by
  shipping the matching CSR file (15-30 MB) instead of the .graphml (100-200 MB) alongside
  the impedance artifact.
- Re-examined whether the origins_sig check itself was doing the right thing, prompted by
  pushback that requiring an exact match "doesn't make sense" for something as stable as
  road topology. Confirmed two things by reading the actual code rather than assuming:
  `_coords_signature` (routing/public_transport_routing_stage.py) hashes the node list IN
  ORDER with no sort, so even the identical node SET in a different order (which OSM
  re-downloads produce routinely, since Overpass element order isn't guaranteed stable) fails
  the old check; but downstream, `accessibility_stage.py`'s actual lookups are by node ID
  (`source_id_to_row.get(str(node_id))`), not by list position. So the real risk was never
  misattribution -- it's a coverage gap (a current node absent from the bundle silently gets
  zero transit/non-bus accessibility) -- and the old check was answering a stricter question
  than the one that actually matters.
- Replaced the ordered-hash equality check in `exports/artifact_bundle.py`'s
  `load_impedance_bundle` with an ID-set coverage check: computes
  `current_node_ids & bundle_node_ids` and accepts the bundle when coverage is >=
  `_MIN_ORIGIN_COVERAGE` (0.90, a new module constant next to ARTIFACT_SCHEMA_VERSION),
  printing which current nodes have no bundle entry (and will get zero accessibility this
  run) instead of silently either accepting or hard-rejecting. Verified on the real Paris
  bundle's 23,046-node set: an identical node set still gives 100% coverage/accept (so
  nothing that worked before breaks), a simulated realistic OSM-style drift (50 dropped, 30
  added) gives 99.9%/accept (the case that used to hard-reject and shouldn't have), and a
  simulated unrelated node set gives 0%/reject (the safety net the original check existed
  for is still intact). This is a pure load-time acceptance-logic change -- the bundle file
  format and ARTIFACT_SCHEMA_VERSION (still 4) are untouched, `man["origins_sig"]` is still
  read and passed through into BusRoutingStageResult for other consumers, so no existing
  bundle needs reconverting and nothing that loaded before stops loading.
- Fixed the WinError 32 masking bug diagnosed for `_merge_gpkg_table` in
  exports/generate_experiment_shapefiles.py: `with sqlite3.connect(src_gpkg) as conn:`
  (line 592) only manages the SQL transaction on exit (commit/rollback) -- it never calls
  conn.close(), which is documented sqlite3 behavior, not a misuse. On a mid-transaction
  failure (the "no such table" flake the function's own docstring already documented), the
  exception's traceback kept conn alive with an open OS handle on src_gpkg straight through
  the caller's `finally: staging_grid.unlink()`, which raised WinError 32 "file in use" on
  Windows (a silent no-op on Linux) and masked the real underlying SQL error. Replaced the
  `with` with an explicit try/finally calling conn.close(), so the handle is released before
  the caller's unlink runs regardless of whether the SQL inside succeeded. Verified two ways
  on real sqlite files built to the same minimal gpkg_contents/gpkg_geometry_columns shape:
  the success path still merges and unlinks cleanly (unchanged), and on the actual failure
  path (a genuinely missing table) the real `OperationalError: no such table` now surfaces
  correctly and a spied-on reference to the live Connection object proves it -- not merely
  infers it -- is closed afterward (`conn.execute` raises "Cannot operate on a closed
  database"), which is OS-independent proof and not something a Linux-only unlink() success
  could have shown by itself.
- Extended the sqlite connection-leak fix to every other `with sqlite3.connect(...) as conn:`
  site in the repo (13 total, list audited against current line numbers before touching
  anything): exports/generate_experiment_shapefiles.py (`_hide_gpkg_layer`,
  `_create_service_grid_views` -- the latter has an early `return` inside the old `with`
  block, which still runs the new `finally: conn.close()` correctly since a `return` inside
  `try` triggers `finally` first), analysis/merge_scenario_differences.py (6 sites across
  `_merge_gpkg_table`, `_merge_gpkg_table_renamed`, `_merge_layer_styles` -- same copy-pasted
  ATTACH/DETACH idiom), analysis/robustness_report.py and housing/housing_capability.py
  (`_embed_gpkg_style`, one each), and tools/inspect_hex_pois.py (3 read-only SELECT sites).
  None of the 12 beyond the already-fixed `_merge_gpkg_table` are followed by an unlink of
  the same file, so none are confirmed *live* WinError-32 bugs -- same latent fragility
  (relying on refcounting rather than a guaranteed close), lower urgency.
- Caught a real bug while converting rather than templating blindly: `with conn:` commits
  the transaction on normal exit (that's what makes the pattern look safe), but two sites
  (`robustness_report.py` and `housing_capability.py`'s `_embed_gpkg_style`) had no explicit
  `conn.commit()` at all -- they relied entirely on that implicit commit. `conn.close()`
  alone discards uncommitted changes, so a mechanical swap to `try/finally: conn.close()`
  without adding a commit would have made those two functions silently stop writing their
  QGIS style rows. Added `conn.commit()` before close in both. Verified every other
  conversion already had an explicit `conn.commit()` in the original code (preserved
  as-is), and that the 3 inspect_hex_pois.py sites are genuinely read-only (nothing to
  commit). Confirmed on disk, not just by reading the diff: wrote a style row through each
  of the two commit-added functions, then reopened the file in a *fresh* connection and
  read the row back -- both come back present, proving the commit is real and not silently
  lost on close.
- Investigated a user-reported "why is contact-with-nature lower here, it's the beach" question
  for Cagliari's Poetto peninsula (screenshot-driven). Traced it to a specific origin node
  (11190449900, at the base of the peninsula) whose walk AND bike impedance to every candidate
  POI was NaN (0/1449 reachable) while drive worked fine (1373/1449, ~10 min) -- confirmed via
  the walk CSR's own adjacency that this node's connected component has size 2 (a lone 34 m
  dangling stub to node 8297117039, which connects to nothing else): a genuine OSM digitization
  gap (an unlinked footway/cycleway near what's likely a car-only causeway/junction), not a
  pipeline bug in the routing math itself.
- First proposed excluding small-component nodes from the snap-candidate KD-tree entirely, then
  found and walked back two real flaws in that design before writing any code: (1) the KD-tree
  in `get_mode_csr` is built from `node_xy` (ALL real nodes) while `snap_indices` (meant to
  restrict it) is currently `np.arange(real_node_count)` -- a no-op identity -- so filtering
  `snap_indices` alone without also filtering the tree's own input would have been either
  inert or an index-alignment bug, not a fix; (2) more fundamentally, a POI's true nearest node
  and an origin's true position can legitimately sit in the SAME small component, where the
  real internal path is short and correct -- relocating the POI's snap to the main graph would
  have broken that already-correct case to "fix" a different (possibly already-correct, if the
  isolation is real) case for other origins. Concluded a single fixed snap per POI can't be
  simultaneously right for same-component and cross-component origins.
- Landed on two independent, additive fixes instead, both implemented and verified against
  real Cagliari data:
  1. **Last-mile snap-distance** (utils/delta_g.py): `accessibility_non_bus_from_snap_map`'s
     dist_km was purely snapped-node-to-snapped-node graph distance, silently dropping the gap
     between a true coordinate and its snapped node at both ends, unconditionally (not specific
     to disconnected components). Generalized `_haversine_m_np` from scalar-origin-only to
     elementwise array-vs-array (verified bit-identical on the existing scalar-vs-array call
     site before relying on it), then added `origin_gap_km` (one haversine per mode) and
     `poi_gap_km` (vectorized, per POI) into walk/bike/drive's impedance. Drive's gap portion
     uses speed_walk_kmh, not speed_drive_kmh (per explicit direction -- the last mile to/from
     a car is walked, not driven), added separately from the graph-distance portion which still
     uses speed_drive_kmh. Verified on a real origin: origin gap was 0 m (this particular node
     sits on its own snap point), POI gaps ranged 0-441 m (median 6.6 m), and every resulting
     delta was positive and proportional to its gap (e.g. the 441 m outlier POI got exactly
     +5.3 min, matching 441 m at ~5 km/h) -- confirms the formula, not just that it runs.
     Bumped non_bus_cache_schema_version 11 -> 12 (every impedance value changes, however
     slightly, so old caches must not be reused).
  2. **Graph-level component bridging** (utils/graphml.py): rather than filtering snap
     candidates, `_build_mode_csr_streaming` now computes undirected connected components
     right after building the adjacency matrix and, for every non-main component, adds ONE
     synthetic bidirectional edge to its closest node in the main component (found via a
     KD-tree query against main-component-only coordinates), weighted by real haversine
     distance x `non_bus_dijkstra_detour_factor` (1.6) -- not straight-line, per explicit
     direction, to better estimate real walking distance across an undocumented gap. Needed a
     local `_haversine_m` copy in graphml.py (can't import utils.delta_g -- delta_g already
     imports graphml, so it would be circular). Bumped the CSR cache filename `_csr_v2.npz` ->
     `_csr_v3.npz` to force every city to rebuild (the new edges are baked into the cached
     adjacency itself). Verified end-to-end on Cagliari's real walk graph: rebuild found and
     bridged exactly 169 components (matching the previously-measured 170 total, i.e. every
     non-main component got exactly one bridge) in 8 seconds; the graph goes from 170 components
     to 1; the previously-dead-end origin (11190449900) now gets a finite distance (2959 m /
     ~35.5 min) to a nearby real destination instead of inf; and -- the property that actually
     matters, since it's what the abandoned exclude-based approach would have broken -- the
     TRUE internal 34 m edge between 11190449900 and its real neighbor is still returned as
     exactly 34 m, confirming Dijkstra prefers the genuine short path and only engages the
     bridge when nothing else connects the two sides.

## 2026-09-10

- Diagnosed a Windows-only crash reported by the user running the Paris pipeline:
  `ValueError: concurrent send_bytes() calls are not supported`. Traced it to
  `maxtasksperchild` worker recycling on `multiprocessing.Pool` -- a documented Windows
  bug where a recycled worker's respawn races the Pool's result-handler thread on the same
  overlapped-I/O pipe. The recycling itself exists purely to work around glibc/pymalloc not
  returning freed memory mid-process (see `_trim_worker_memory`'s `malloc_trim`, which is
  already a no-op on Windows since `_LIBC` only resolves off `libc.so.6`), so it buys nothing
  there anyway. Fixed by making `maxtasksperchild` `None` on Windows (`os.name == "nt"`),
  keeping the existing value elsewhere, in all four pools that set it:
  `routing/non_bus_routing_stage.py` (20), `stages/accessibility_stage.py` (200),
  `exports/poi_exports.py` (200), `analysis/score_report.py` (200).

## 2026-09-11

- `main.py` (`main()`) / `core/notify.py`: `NOTIFY_CRASH = True` used to fail fast in
  `install_crash_notifier()` whenever `notify_config.json` was missing, even though the knob
  defaults to `False` and colleagues without Telegram set up shouldn't hit that. Added
  `notify.notify_config_exists()` (checks `_CONFIG_PATH.exists()` without validating contents)
  and, in `main()`, check it before arming: if `NOTIFY_CRASH` is set but the config file isn't
  there, log a line and flip the global back to `False` instead of crashing. Covers both
  `NOTIFY_CRASH` checks in `main()` (arm at start, `mark_success` at the end) since they read
  the same corrected global.
- `tools/nature_immersion/` (new): standalone Leaflet page + `exports/generate_nature_immersion_export.py`
  to browse `high_nature_immersion` POIs on a map, filterable by the specific raw OSM tag that
  matched (joins `pois_used.gpkg` against the cached `poi/Cagliari/all_tags_*.geojson` raw-tag
  universe via `element_type`+`osmid`, since `pois_used.gpkg` only keeps the derived `poi_types`,
  not the raw tag). Clicking a POI opens Google Street View for that point in a new tab
  (`maps/@?api=1&map_action=pano&viewpoint=...`) — the no-key iframe-embed trick
  (`layer=c&cbll=...&output=embed`) no longer works, Google has locked it down.
- `config/poi_types.csv`: moved `natural=grassland` and `landuse=meadow` (plus their matching
  text labels) from `high_nature_immersion`'s clause list to `perceived_nature`'s, per request.
  Left the row's non-OSM `GI*`/`BI*` codes untouched — no confirmed mapping from those codes to
  which specific tag they represent, so touching them risked silently misclassifying
  shapefile-sourced POIs.
- `stages/snapping_stage.py` / `utils/line_merge.py` (new) / `utils/graphml.py`: line-like POIs
  (coastline, rivers, streams, hiking/running routes, etc.) get snap-candidate access points
  every 500m along their merged connected components, instead of the raw OSM way vertices
  `_extract_geom_vertices` used to return. Went through several design iterations in
  conversation before landing here — first built coastline-only independent POIs sampled every
  500m in `exports/poi_exports.py` (fully reverted, see below), then moved to per-connected-
  component snap candidates, then generalized from coastline-only to every line-like poi_type.
  `utils/line_merge.py` holds the reusable bits (`merge_connected_lines`: union-find on shared
  endpoints + `shapely.ops.linemerge`, in a UTM CRS; `sample_line_every`: fixed-spacing
  `line.interpolate()` walk) — kept out of `stages/snapping_stage.py` and `exports/poi_exports.py`
  proper since the latter already imports from the former, so it can't import back.
  `_build_poi_snap_map` now runs a pre-pass per query: pulls out every line-like item via the
  new `_line_geometry_from_item`, merges/samples once, and only those items get their candidate
  list replaced (points and polygons untouched). Along the way, fixed a real bug this surfaced:
  the no-GDAL cached-geometry path (`_read_geojson_without_gdal`, used for Cagliari) flattened
  a `MultiLineString`'s separate parts into one combined vertex list with no boundary between
  them (`_geojson_vertices`'s `Polygon`/`MultiLineString` branch) — naively rebuilding a single
  `LineString` from that would silently draw a bogus straight segment connecting two disjoint
  parts. Fixed by adding `_geojson_line_parts()` to preserve per-part vertex lists (new
  `__snap_line_parts` cached column, threaded through `get_poi_geometries` as `"line_parts"`),
  so `_line_geometry_from_item` now rebuilds one `LineString` per actual part. Also added
  `__geometry_type` (new cached column) since the dict/no-GDAL geometry shape previously carried
  no type info at all — needed to safely tell a line from a flattened polygon ring using only
  vertices. `exports/poi_exports.py` itself ended up back at exactly its pre-session state (one
  centroid point per raw OSM feature, uniformly, no coastline special-casing) — confirmed via
  `git diff --stat` showing no changes once the coastline-specific code was removed.
- `scripts/setup_r.py` (`_install_r`) / `routing/public_transport_routing_stage.py` (new
  `resolve_rscript_path`): `main.py` crashed with `RscriptNotFoundError` even though R was
  genuinely installed (`winget list --id RProject.R -e` confirmed R 4.6.1 present). Two bugs:
  (1) `_install_r()` gated success on the installer subprocess's exit code, but winget returns
  nonzero when the package is already installed and there's nothing to update ("Non sono
  disponibili versioni più recenti") — a false negative that made the script exit before ever
  rechecking whether `Rscript` was actually reachable. Fixed by always running the install
  command and letting the caller decide success based on whether `Rscript` is now findable,
  not the subprocess's return code. (2) Even after that fix, `Rscript` genuinely wasn't
  reachable: confirmed via `Get-ChildItem`/`[Environment]::GetEnvironmentVariable` that R's
  Windows installer (via winget) put `Rscript.exe` at `C:\Program Files\R\R-4.6.1\bin\` but
  never added that folder to either the Machine or User `PATH` — so a bare `shutil.which
  ("Rscript")` (used both in `scripts/setup_r.py` and, more importantly, in the actual pipeline
  call at `routing/public_transport_routing_stage.py`'s `_run_r5r_script`) would keep failing
  on every run, PATH-restart or not. Added `resolve_rscript_path()`: tries `shutil.which` first,
  then falls back to globbing R's known Windows install roots
  (`%ProgramFiles%\R\R-*\bin\Rscript.exe`, `%LOCALAPPDATA%\Programs\R\R-*\bin\Rscript.exe`,
  highest version first). Wired into both the actual `_run_r5r_script` call site and
  `setup_r.py`'s two detection points, so setup and the real pipeline run agree on what
  "Rscript is installed" means and neither depends on PATH being correctly configured.
- `routing/public_transport_routing_stage.py` (`_autobuild_pbf_from_place`): next blocker in
  the same run — `osmium-tool` missing, and the error message hardcoded a Fedora-only
  `sudo dnf install osmium-tool` hint with no Windows guidance at all. Added
  `_osmium_install_hint()` (platform-aware: Windows gets conda-forge/OSGeo4W/WSL options, macOS
  gets brew, Linux detects apt/dnf/pacman). Actually installed it on this machine via Miniconda
  (`winget install --id Anaconda.Miniconda3`, then `conda install -c conda-forge osmium-tool`,
  after accepting the three default-channel ToS prompts conda now requires non-interactively).
  Hit the exact same PATH gap as the Rscript fix above — conda doesn't add an env's
  `Library\bin` to PATH unless activated — so added `resolve_osmium_path()` mirroring
  `resolve_rscript_path()`'s fallback pattern (checks `%USERPROFILE%\miniconda3`,
  `...\anaconda3`, `%LOCALAPPDATA%\miniconda3`, `C:\ProgramData\miniconda3`), wired into
  `_autobuild_pbf_from_place` in place of the bare `shutil.which("osmium")`.

## 2026-09-14

- `utils/graphml.py`: OSM/shapefile can split one physical polygon POI into several
  fragments (e.g. a school cut by an internal road, a park split by a bridge), which
  were counted/routed as distinct POIs. Added a merge step, grouped by the raw
  matched OSM tag or Paris `TYPEQU` code — not the config `poi_type`, since a
  poi_type's tag clause can OR together several distinct raw tags that must NOT be
  merged into each other. Added in three pieces: `_stamp_poi_raw_tag()` +
  `_row_raw_tags()` persist a new `poi_raw_tag` column (`"key=value&key2=value2"`,
  `&` only ever joining distinct keys — a row whose actual value is itself
  semicolon multi-valued, e.g. `access=private;customers`, is duplicated once per
  matched value so each copy carries one unambiguous identity) so the tag that
  matched is legible later, e.g. for a planned tag-relabeling interface, instead of
  being re-derived on demand; `_merge_polygon_cluster()` does the actual geometric
  merge within one raw-tag group (buffer by `distance_m/2` in the group's UTM zone,
  `unary_union` overlapping buffers to find clusters, union the original — unbuffered
  — geometries per cluster); `merge_nearby_polygon_pois()` (default `distance_m=10`)
  dispatches per `poi_raw_tag` group. Wired into `get_poi()` at all three points
  where a fresh, single-query POI set is finalized (city-universe filtered result,
  shapefile load, fresh OSM download) — before caching to GeoJSON, so the merge is
  persisted and future cache reads get pre-merged data for free.
- `config/osm_raw_tag_codes.csv` (new) + `utils/graphml.py` (`_load_osm_raw_tag_codes()`):
  added a raw_tag -> short-code lookup (21 rows, green-infrastructure `GI01..GI12` and
  blue-infrastructure `BI01..BI09`, from a table the user pasted) so OSM-sourced POIs
  get the same kind of stable short identity Paris already has via `TYPEQU`. Wired into
  `_stamp_poi_raw_tag()`'s OSM branch: a computed `"key=value&..."` string is replaced
  by its code when the table has an entry, left as-is otherwise.
- `utils/graphml.py` (`_merge_polygon_cluster`, `_cluster_by_adjacency`, `_split_cluster_by_name`):
  tested the polygon-merge feature above against real cached Cagliari OSM data
  (`poi/Cagliari/all_tags_*.geojson`) instead of synthetic cases, per the user's
  request, and found two real bugs. (1) The original clustering built one big
  `unary_union` blob from every buffered polygon in a raw-tag group and assigned
  membership via `blob.intersects(polygon)`; on a large/complex union (hundreds of
  polygons across the whole group) that test numerically misfired, lumping polygons
  7,977m apart into the same "cluster" (verified: two differently-named parks,
  `Parco 22 ottobre 2008` and `Parco Dell'Acqua`, wrongly merged into one 974m-wide
  record). Fixed by replacing the blob-membership test with `_cluster_by_adjacency()`:
  a spatial-index-backed exact pairwise buffer-intersects graph, clustered via
  `scipy.sparse.csgraph.connected_components` — the same ground-truth method used to
  catch the bug, made into the actual implementation. (2) Added `_split_cluster_by_name()`
  so a cluster containing 2+ distinct non-null names gets split by nearest-named-anchor
  instead of unioned wholesale (a genuine single facility split by a road shares one
  name or has it on only one fragment; two distinct names means the buffer bridged
  unrelated facilities) — first version had a tie-breaking bug where a named anchor
  touching another anchor at distance 0 could be reassigned away from its own name;
  fixed by never reassigning anchors, only unnamed fragments. Verified with a full
  correctness pass across all 173 distinct tag clauses in `config/poi_types.csv`
  against the real Cagliari dataset (918 multi-fragment merge groups): worst chain
  link found anywhere is 9.989m, zero groups exceed the 10m threshold, zero
  name-conflict violations remain.

## 2026-09-15

- Renamed `tools/nature_immersion/` -> `tools/gi_bi_relabeling/` and
  `exports/generate_nature_immersion_export.py` ->
  `exports/generate_gi_bi_relabeling_export.py` (title updated to "Blue/Green
  Infrastructure Relabeling"): the tool was previously hardcoded to one poi_type
  (`high_nature_immersion`) and only showed centroid points, so its old name/scope no
  longer matched what it does now that it covers the whole `nature_contact` service.
- `exports/generate_gi_bi_relabeling_export.py`: rewritten to (1) pull every poi_type
  contributing to the `nature_contact` service (`utils.services.get_service_queries`)
  instead of one hardcoded poi_type, so all 3 poi_types' OSM queries feed the
  interface; (2) show real polygon/line shapes instead of centroid points — needed to
  visually inspect/fix the merge feature above, so `pois_used.gpkg`'s POI identity
  (`source_key`, built the same way `exports/poi_exports.py` builds it) is used only
  to filter down to POIs actually used by the pipeline, then the real geometry is
  pulled fresh from `graphml.get_poi()` per poi_type and joined back by `source_key`;
  Point geometries are dropped (only Polygon/MultiPolygon/LineString/MultiLineString
  kept, per the user — this tool is for relabeling shape data, not point POIs).
  Deliberately left `pois_used.gpkg` itself untouched (a plain-points interface
  elsewhere depends on it); the new tool writes its own `tools/gi_bi_relabeling/
  data.geojson` instead of reusing/extending the old `data.json`.
- `tools/gi_bi_relabeling/index.html`: switched from manually-built `L.circleMarker`
  points to `L.geoJSON` rendering real shapes from `data.geojson`, grouped/filterable
  by `poi_type — tag` (checkboxes, one layer group per combination, with a POI count
  per group). Clicking a shape now opens its Street View link automatically in a new
  tab (`window.open`, previously required a manual click on a separate link) at the
  clicked point on the shape (`e.latlng`) rather than a fixed centroid — more useful
  for inspecting one particular fragment of a merged/split polygon.
- `utils/graphml.py` (`_download_poi_for_place`, new `_flatten_osm_index()`): found and
  fixed a real (pre-existing) identity bug while testing the interface above end to
  end against a fresh `main.py` run. OSMnx returns downloaded POIs with
  `osmid`/`element_type` as an index, not real columns, so `build_poi_source_key()`
  couldn't see them on a freshly-downloaded (not-yet-cached) GeoDataFrame and fell
  back to a geometry-derived signature — which differs depending on whether the
  geometry is later read as a real shapely object or as a cached token, so
  `pois_used.gpkg` and the accessibility stage's per-node writer silently disagreed on
  every POI's identity. Symptom: `accessibility_stage` wrote zero `.npz` files into
  `artifacts/<city>/accessibility/poi_by_node` with no error (the per-node writer
  drops unmatched POIs silently), and the pipeline only crashed 3 stages later in
  `generate_score_report` (`FileNotFoundError`, poi_by_node empty). Fixed at the root:
  `_flatten_osm_index()` calls `.reset_index()` right after every OSMnx download call
  in `_download_poi_for_place()`, so `osmid` is a real column from the very first
  moment — uses the stable id already in the data instead of adding a synthetic one,
  per explicit direction. Rebuilding a synthetic persisted `source_key` column was
  considered and deliberately rejected (adds a second identity scheme that itself
  needs to stay in sync — the same class of problem, not a fix). This only fixes
  *future* downloads; existing `poi/<city>/*.geojson` cache, `non_bus_poi_catalog.pkl`,
  `artifacts/<city>/non_bus/`, `artifacts/<city>/accessibility/`,
  `artifacts/<city>/snapping/`, and `outputs/poi_exports/<city>/` all still need
  clearing/regenerating to actually pick up the corrected identities (bus/subway
  routing caches confirmed unaffected — their destination ids are synthetic sequential
  labels tied to sampled graph nodes, not POI source_keys).
- `exports/generate_gi_bi_relabeling_export.py`: after the fix above, `graphml.get_poi()`
  was still returning a stripped point-token form for already-cached queries (correct
  for the main pipeline's hot loop, useless here). Added `_cached_geojson_path()` +
  switched to reading that same cache file directly via `geopandas.read_file()` for
  real geometry, calling `get_poi()` only to guarantee the cache file exists.
- `tools/gi_bi_relabeling/`: added `avvia_interfaccia.sh` (Linux/macOS) and
  `avvia_interfaccia.bat` (Windows) double-click launchers — start a local HTTP server
  and open the map in the default browser, for a non-technical colleague — and vendored
  Leaflet 1.9.4 locally (`vendor/leaflet/`, ~184KB: JS, CSS, marker icons) so the page
  no longer depends on the `unpkg.com` CDN; `index.html` now points at
  `vendor/leaflet/...`. The OpenStreetMap basemap tiles still require internet (too
  large to bundle), so the page isn't fully offline — only the Leaflet library itself
  is now local.
- `exports/generate_gi_bi_relabeling_export.py` (`_load_code_to_raw_tag()`): the "tag"
  field was showing the GI0x/BI0x short code when `config/osm_raw_tag_codes.csv` had
  one, instead of the plain OSM query — confusing for a colleague relabeling raw OSM
  data who needs to see e.g. `leisure=park`, not `GI01`. Added a reverse lookup
  (code -> plain "key=value" text) applied only to the exported "tag" property; the
  underlying merge/grouping logic (which uses the codes) is untouched.
- `tools/gi_bi_relabeling/index.html`: UI overhaul per user feedback after first trying
  the interface — (1) nothing is selected/rendered on load, with "Select all"/
  "Deselect all" buttons added above the filter list, instead of every group starting
  checked; (2) each poi_type/tag group gets a distinct color (golden-angle hue
  rotation, `colorForIndex()`) shown as a swatch next to its checkbox, instead of one
  fixed green for every shape, so overlapping groups are distinguishable on the map;
  (3) hovering any shape restyles it (and, since a merged POI is already one Leaflet
  feature — possibly multi-part — the whole merged shape) to yellow and reverts to its
  group color on mouseout, to visually show what the merge feature considers "the same
  POI"; (4) removed the unused "Open Street View" link/button (clicking already
  auto-opens it) and repurposed that panel space to show POI details (name, poi_type,
  tag, source_key) on click instead.
- `tools/gi_bi_relabeling/index.html`: added hover-to-locate on the filter-list labels
  themselves — hovering a category row highlights every shape in that category in
  yellow across the whole map, temporarily adding it to the map first if its checkbox
  is unchecked (and removing it again on mouseout, without touching the checkbox
  state), so a category's spatial extent can be previewed without selecting it.
  Verified visually end to end with a headless Playwright browser (initial empty
  state, Select all rendering distinct per-group colors, shape hover, click ->
  detail panel + Street View, and this label-hover reveal/unreveal).
- `tools/gi_bi_relabeling/index.html`: fixed a real bug the user hit — checking a
  category's checkbox *while* hovering its label left the checkbox checked but the
  shape missing from the map. Cause: `mouseleave` decided whether to remove the layer
  from a `wasVisible` flag captured once on `mouseenter`, which went stale the moment
  the checkbox was toggled mid-hover. Fixed by reading the checkbox's live `.checked`
  state on `mouseleave` instead of a captured flag. Reproduced the exact sequence
  (hover -> click checkbox -> move mouse away) with Playwright before and after the
  fix to confirm it.
- `tools/gi_bi_relabeling/`: added POI relabeling, persisted to a file. New
  `server.py` (standard-library only, no dependencies — a `http.server.SimpleHTTPRequestHandler`
  subclass) replaces the plain `python -m http.server` in both launcher scripts;
  it still serves the same static files but adds one endpoint, `POST /save_relabel`,
  which merges the posted `{source_key, poi_type, name, original_tag, new_tag}` into
  `relabels.json` in the same folder (atomic write via a `.tmp` + `os.replace`, same
  pattern as `poi_dedup.py`'s `write_drop_map`). `index.html`: the detail panel's
  static "tag" text became a `<select>` populated with every distinct plain-text tag
  seen in `data.geojson`; per explicit direction there is no Save button — choosing a
  different option in the dropdown (`change` event) immediately POSTs the correction
  and updates the shape's style to a dashed border so already-corrected POIs are
  visually distinguishable on the map. Also switched the port from 8765 to 8766 after
  discovering a stale process (outside this session's reach) still holding 8765 from
  before this change. Verified the full flow with Playwright: dropdown lists all 21
  known tags, changing it POSTs successfully, `relabels.json` is written correctly on
  disk, and the "Saved." status appears in the panel.
- `tools/gi_bi_relabeling/index.html`: fixed a real bug the user hit — relabeling a
  POI never actually moved it into its new category. `groups`/counts were computed
  once from `data.geojson`'s original `tag` at load time, and `relabels.json` was
  only ever used for the dropdown default and the dashed-border style, never fed back
  into grouping — so a corrected POI stayed under its old category forever, counts
  never changed, and this persisted across refreshes (there was nothing to refresh
  into). Fixed by refactoring the render logic into `loadAndRender()`: it now
  overwrites each feature's effective `tag` with its saved correction (if any) via
  `effectiveTag()` *before* computing groups, so a relabeled POI is grouped/counted
  under its corrected tag from that point on. `loadAndRender()` is called both on
  initial page load and again after every successful save (instead of just patching
  one shape's style in place), preserving the currently-checked categories and map
  view across the rebuild so the colleague's place isn't lost. Also added
  zoom-to-nearest: checking a category's checkbox now pans/zooms the map to whichever
  POI in that category is closest to the current view center (`zoomToNearest()`),
  making rare categories easy to actually find. Verified with Playwright: relabeling
  a "perceived_nature — natural=water&water=pond" POI to "amenity=fountain" moved it
  out of the old group entirely and into the new one (66 -> 67), confirmed on a
  completely fresh page load (not just in the live session); checking a category
  jumped the map from the default view straight to a zoom-15 view centered on one of
  its POIs.
- `exports/generate_gi_bi_relabeling_export.py`: broadened scope per the user — pulls
  every configured poi_type across all services now (`utils.services.unique_query_keys()`)
  instead of just `nature_contact`'s 3, still filtered to POIs actually used by the
  pipeline (`_used_source_keys()` simplified accordingly, no poi_type intersection
  needed since we want all of them). Added a `seen_keys` guard since a POI can be
  matched by more than one poi_type's query — each now appears exactly once, under
  whichever poi_type's query processes it first, instead of being duplicated once per
  matching poi_type. Regenerated `data.geojson`: 5,598 shapes from 7,482 used POIs
  (up from 3,881/3,964 when scoped to nature_contact alone), 13s to build since
  everything was already cached from the earlier full pipeline run.
- `tools/gi_bi_relabeling/index.html`: added the ability to remove a POI from the
  analysis. Per explicit direction, removing is literally picking a special
  `"removed"` value from the same tag dropdown used for every other correction (no
  separate button/action) — always present as an option even when no POI is currently
  removed (`allTags` gets it force-added). `groupKey()` special-cases it so every
  removed POI collapses into one unified `"removed"` category regardless of its
  original poi_type (rather than splitting into `"poi_type — removed"` per type), and
  that group is forced to pure black instead of a `colorForIndex()` hue. Added a small
  centroid icon marker on every edited shape (✎, or ✕ specifically for removed) so
  modified shapes are recognizable at a glance without opening the panel. Added a
  persistent "currently selected" highlight (solid blue, `SELECTED_STYLE`) distinct
  from the transient yellow hover — tracked via `selectedSourceKey`, survives
  `loadAndRender()` rebuilds (re-applied after each one), and correctly hands off
  between shapes as a new one is clicked. Verified with Playwright: the "removed"
  group doesn't exist until first used, then appears as "removed (1)"; the dropdown
  always offers "removed"; the removed shape renders black with a dashed border and
  ✕ icon; clicking a second shape reverts the first from blue back to its own style
  while the new one turns blue.
- `exports/generate_gi_bi_relabeling_export.py` (`_poi_type_order()`) + `tools/gi_bi_relabeling/index.html`:
  reorganized the sidebar into collapsible per-poi_type sections instead of one flat
  "poi_type — tag (count)" list. `_poi_type_order()` reads `config/poi_types.csv`
  top-to-bottom (first-occurrence order, since one poi_type spans several clause rows)
  and writes it to a new `tools/gi_bi_relabeling/poi_type_order.json`, so the sidebar
  section order matches the CSV exactly rather than alphabetical or discovery order.
  `index.html` fetches that file alongside the other two, builds one native
  `<details>/<summary>` per poi_type (free expand/collapse, no extra JS needed) in
  that order, with "removed" as its own section first (a poi_type of its own, per the
  user) — labels underneath now show just `tag (count)`, not the poi_type name
  repeated on every row. Per-group color/layer/checkbox logic is untouched; only
  where each label gets appended (into its poi_type's `<details>` instead of flatly
  into `#filter-list`) and its text changed. Sections start collapsed on first load
  and each one's open/closed state is preserved across `loadAndRender()` rebuilds
  (captured before the rebuild, re-applied after), the same way checked categories
  already were. Also bumped the edit/removed icon size (14px/16px box -> 22px bold
  text in a 24px box) since the user found the original too small. Verified with
  Playwright: collapsed sidebar shows one row per poi_type in exact CSV order;
  expanding one shows its tags indented underneath with per-tag color swatches;
  marking a POI removed correctly promotes "removed" to the first section; the icon
  is now clearly legible when zoomed to the shape.
- `tools/gi_bi_relabeling/index.html`: fixed a gap the user caught — relabeling only
  ever overrode `tag`, never `poi_type`, so a POI relabeled to a tag belonging to a
  completely different poi_type (e.g. a hospital corrected to `amenity=ice_cream`)
  stayed grouped under its old poi_type section (`residential_healthcare`) instead of
  the one the new tag actually belongs to (`takeaway_consumption`). Fixed in
  `loadAndRender()`: build a `tag -> poi_type` map from the natural, pre-relabel data
  first (every tag in the dropdown occurs on at least one real POI, so its "home"
  poi_type is always discoverable from `data.geojson` itself — no new export file
  needed), then when applying each POI's effective tag, also override its effective
  `poi_type` from that map (falling back to the original poi_type only if the new tag
  isn't found in it, e.g. "removed", which stays in its own unified section).
  Verified with Playwright: relabeling a real `residential_healthcare` POI to
  `amenity=ice_cream` moved it into the `takeaway_consumption` section's
  `amenity=ice_cream (2)` row, and re-inspecting that same POI's live feature data
  directly confirmed its effective `poi_type` is now `takeaway_consumption`.
- `tools/gi_bi_relabeling/index.html`: stopped auto-opening a new tab on every click
  (too noisy once you're clicking through hundreds of shapes) in favor of a button.
  Initially swapped the target to OpenStreetMap (misread of the request — briefly
  added `openStreetMapUrl()`, linking straight to the real OSM feature page via
  `source_key`), then corrected back to Google Street View per the user's
  clarification: kept `streetViewUrl()` as before, restored `#detail-link`'s original
  green-button styling and "Open Street View ↗" label, just no longer auto-fired on
  click. Also dropped the stale "(also opens Street View)" placeholder text, since the
  button, not the click itself, is now what opens it. Verified with Playwright:
  clicking a shape opens zero new browser tabs (checked via context.pages() count
  before/after), and the button's href resolves to the correct Street View viewpoint
  at the clicked coordinates.
- `tools/gi_bi_relabeling/index.html`: added a red "Remove POI" button next to the
  green "Open Street View" one, as a one-click shortcut for the most common
  correction. Refactored the dropdown's save logic out into a shared `saveRelabel(props,
  newTag)` so the button and the dropdown's `change` handler both call the exact same
  path instead of duplicating the POST/loadAndRender logic — the button just calls
  `saveRelabel(props, REMOVED_TAG)`. Caught via testing (not just assumed correct):
  clicking the button correctly moved the POI into the "removed" section, but the
  still-open detail panel's own dropdown kept showing the POI's old tag, since
  `loadAndRender()` rebuilds the sidebar/map but never touches the already-rendered
  panel. Fixed by setting `#tag-select`'s value to `REMOVED_TAG` immediately in the
  button's click handler, mirroring what picking it from the dropdown does natively.
- `tools/gi_bi_relabeling/index.html`: added an "original tag" row above the editable
  tag dropdown, so a colleague can see what a POI was tagged before any correction and
  restore it if needed. Deliberately sourced from `data.geojson` itself
  (`f.properties.originalTag`, captured once per load before any relabel override is
  applied), not from `relabels.json`'s own `original_tag` field — the latter only
  records the value at the time of the *most recent* save, so it would drift to an
  intermediate value across a second or third relabel of the same POI; `originalTag`
  always stays the true, first OSM-sourced tag regardless of how many times it's been
  corrected since. Verified with Playwright: relabeling a POI from
  `leisure=nature_reserve` to `amenity=ice_cream` left "original tag:
  leisure=nature_reserve" unchanged in the panel both before and after the edit.

Discussed but not yet started: a "Split/Merge" tool to manually override the
automatic polygon-merge decisions (`utils/graphml.py`'s `merge_nearby_polygon_pois`)
from inside this interface — select/deselect nearby pre-merge OSM fragments to define
what counts as one POI, persisted live (survives reload, same as relabels already do)
to a new file that should actually feed back into real POI formation on the next
pipeline run, not just annotate the browsing tool. Also noted: relabels/removals made
here should eventually flow back into the actual pipeline data (`graphml.get_poi()`
excluding "removed" POIs and honoring corrected tags for real), not stay confined to
this tool's own `relabels.json`. Planned as a multi-step sequence: raw-fragment
export, new persisted format, new UI mode, then the `graphml.py` integration.

- Step 1 done: `exports/generate_gi_bi_relabeling_export.py` (`_load_raw_fragments()`)
  exports every pre-merge OSM fragment across all configured poi_types to a new
  `tools/gi_bi_relabeling/raw_fragments.geojson`, real osmid-based identity per
  fragment, deliberately NOT filtered to "used" POIs (a fragment the automatic merge
  excluded is exactly what Split/Merge needs to be able to pull back in). Reuses
  `graphml`'s own fetch/filter/stamp internals directly (`_filter_by_tags`,
  `_stamp_poi_raw_tag`) rather than touching `graphml.py` itself — purely additive,
  read-only reuse, same pattern already used for `_cached_geojson_path`. Hit and fixed
  a real bug immediately: `graphml._get_city_poi_universe()` returned the fast,
  GEOS-avoiding cache form once the universe was already cached on disk (only a
  `__snap_coord` token per row, no real geometry — fine for the main pipeline's hot
  loop, useless here), so every query was silently skipped and the file came out
  empty. Fixed by reading the same universe cache file directly with
  `geopandas.read_file()` instead, which always yields real geometry (and moved the
  universe load outside the per-query loop while at it, since it only needs loading
  once). Verified: 6,906 real fragments, 4.7MB, genuine `osmid`-based source_keys and
  plain-text tags, real Polygon/LineString/MultiPolygon geometry (not tokens).
- Step 2 done: `tools/gi_bi_relabeling/server.py` refactored to a shared
  `_load_json_dict`/`_save_json_dict` pair plus an `ENDPOINTS` map, and gained a
  second endpoint, `POST /save_manual_merge`, persisting a manually-defined fragment
  grouping (`{group_id, poi_type, tag, fragment_keys: [...]}`) to a new
  `manual_merges.json`, anchored the same way `relabels.json` already is: by the
  `source_key` of the merged POI you were viewing when you started editing its
  composition. `/save_relabel` behaves identically to before (regression-tested).
  Hit the same recurring port-8766 conflict as earlier sessions (a process outside
  this session's reach still holding it) — rather than fight it again, verified
  against a throwaway copy on a scratch port instead of the real file/port.
- Step 3 (data side) done: `exports/generate_gi_bi_relabeling_export.py` now computes
  `member_keys` per merged POI — which raw fragments (from the new
  `_raw_fragment_rows()`, shared between `_load_raw_fragments()` and
  `_load_all_shapes()` so the expensive universe filter/stamp only happens once) the
  automatic merge actually combined into it, via real shapely containment
  (`geoms[i].buffer(1e-7).contains(fragment.geometry.centroid)`) — what Split/Merge
  will pre-check when opened on a POI. Caught via a sanity check (not assumed
  correct): a merged POI's own representative fragment was missing from its own
  `member_keys` in ~1,031/5,598 cases. Cause: `_raw_fragment_rows()` and the merged
  loop each run an independent first-match dedup across `unique_query_keys()`, so the
  same physical element could get recorded under a *different* `poi_type` in
  `raw_rows` than the one its merged row was produced under, and candidates were
  grouped by `(poi_type, tag)` — a mismatch on poi_type alone hid the fragment
  entirely. Fixed by grouping by `tag` alone instead, matching what the real
  automatic merge (`merge_nearby_polygon_pois` in `graphml.py`) actually groups by
  (poi_raw_tag only, never poi_type) — plus a defensive fallback that always includes
  a merged POI's own key in its `member_keys`. Verified: 5,598/5,598 (100%, was
  4,567/5,598) now correctly self-contained, and multi-fragment merges went up
  slightly (647 -> 664) since some previously-mismatched genuine members are now
  correctly found too.
- Step 3 (UI) done: `tools/gi_bi_relabeling/index.html` gained the Split/Merge tool
  itself. New "Split/Merge" button in the detail panel opens `enterSplitMerge(props)`:
  finds every raw fragment (`raw_fragments.geojson`, fetched alongside the other three
  files now) sharing the POI's `originalTag` within 150m (`CANDIDATE_RADIUS_M`) of its
  bounds center, renders each as its own clickable shape colored green (included) or
  grey (excluded), pre-checked from a saved `manual_merges.json` entry if one exists
  for this POI's `source_key` (the group_id), else from `member_keys` (what the
  automatic merge actually included, computed server-side in step 3's data pass).
  Clicking a fragment toggles it and immediately POSTs the full current set to
  `/save_manual_merge` — no save button, same live-autosave pattern as tag relabeling.
  A persistent `#split-merge-panel` (sibling of `#detail-body`, not wiped by
  `showDetail()`) shows the running count and an "Exit Split/Merge" control. This
  first version is deliberately a checklist over visible fragment shapes, not a live
  re-union of geometry in the browser — the actual re-formed POI shape is meant to
  happen later when `manual_merges.json` feeds into a real pipeline run (step 4,
  the `graphml.py` integration, not built yet). Verified via direct state inspection
  (not just visual screenshots, after a pixel-click test gave a confusing result that
  turned out to be stale test state + a test-script bug, not an app bug): starting
  from 2 pre-checked members, toggling one via `toggleFragment()` leaves exactly the
  other selected, and the save round-trips correctly through the server.
- `tools/gi_bi_relabeling/index.html`: user reported the server seemed to not accept
  multiple fragment selections and asked for selected fragments to visually count as
  one multipolygon POI. Checked the server was up and serving the latest code (it
  was). Tested multi-select extensively (direct `toggleFragment()` calls, real
  `page.mouse.click()`, simulated Leaflet `.fire('click')`) and found no reproducible
  bug in the underlying selection logic — every method correctly added/removed
  fragments from the `splitMergeIncluded` Set one at a time. Concluded the real gap
  was visual: selected fragments rendered as separate individually-colored shapes
  with nothing tying them together, so multi-selection may have been working but not
  look like it was. Added `updateUnionLayer()`: bundles every currently-included
  fragment's real geometry into one `GeometryCollection` Feature (no geometric union
  math needed, just wrapping — `GeometryCollection` rather than `MultiPolygon` since
  candidates can mix Polygon and LineString) and draws a thick dashed purple outline
  around the whole group, `interactive: false` so it doesn't block clicks on the
  individual shapes underneath. Called after both `enterSplitMerge()` and
  `toggleFragment()`, cleared in `exitSplitMerge()`, and skipped entirely for 0-1
  fragments (nothing to visually group). Verified: a 2-fragment group's union layer
  has exactly 2 geometries and renders a continuous purple outline around them.
- `tools/gi_bi_relabeling/index.html`: found and fixed the real bug behind the user's
  next report ("clicking the other POI opens its own detail panel instead, and
  Split/Merge doesn't exit") — `showDetail()` never called `exitSplitMerge()`, so a
  stray click during Split/Merge mode silently swapped the whole panel to an unrelated
  POI while leaving the old `splitMergeLayer`/`splitMergeIncluded` state dangling on
  the map. Traced why the stray click happens at all: candidates are filtered to
  same-`originalTag` fragments within 150m (`CANDIDATE_RADIUS_M`), and for a
  single-fragment POI that radius is very often genuinely empty — checked one example
  directly and its nearest same-tag fragment was 1.8km away, not a bug, just sparse
  data (anything genuinely close with the same tag would likely already have been
  auto-merged). So clicking a visually-nearby but differently-tagged POI hits no
  candidate overlay at all and falls through to the regular POI layer beneath. Fixed
  the immediate inconsistency by guarding `showDetail()`: while `splitMergeGroupId` is
  set, a POI click is ignored entirely rather than treated as a new selection — the
  colleague must explicitly "Exit Split/Merge" first. Verified: triggering `showDetail()`
  for an unrelated POI while Split/Merge is active no longer changes
  `splitMergeGroupId` or hides the panel. Resolved the open scope question: keep the
  same-tag restriction (cross-category merging is a separate, bigger decision), but
  raised `CANDIDATE_RADIUS_M` from 150 to 500 so sparser categories have a realistic
  chance of finding a genuinely-nearby same-tag candidate. Checked the worst case
  before settling on 500 rather than guessing: even the single densest raw tag
  city-wide (`waterway=stream`, 1,111 fragments) only produced 1 candidate within
  500m at a real test location, and rendering stayed fast — no clutter or performance
  concern from the wider radius.
- `tools/gi_bi_relabeling/avvia_interfaccia.sh` / `.bat`: found the real, root cause
  of the recurring "phantom stale server outside this session's reach" problem that
  had been hit repeatedly all session (had to work around it with throwaway ports
  each time rather than actually fix it) — confirmed directly when the user hit
  "could not save" on a genuinely-running server: `curl` showed `/save_relabel`
  working (200) but `/save_manual_merge` 404ing, meaning the live instance predated
  that endpoint and nobody had a way to restart it. Root cause: the `.sh` launcher
  backgrounded `python3 server.py &` and `wait`ed on it — if the terminal/file-manager
  window that launched it ever closed (or was never a real interactive terminal to
  begin with, common for double-clicked scripts), the backgrounded server became
  orphaned, still running but detached from anything that could Ctrl+C it. Fixed by
  running `exec python3 server.py` as the script's last line instead — `exec`
  replaces the shell process with python entirely (same PID), so any signal sent to
  the script (Ctrl+C, closing the terminal, a plain `kill`) reaches the server
  directly. Verified the fix matters: without `exec`, sending SIGINT to the script's
  PID left the child python process running and the port still serving; with `exec`,
  the same SIGINT killed it immediately (port stopped responding). Also un-minimized
  `avvia_interfaccia.bat`'s server window (previously `/min`) so it isn't as easy to
  lose track of on Windows, where `start` already creates a genuinely separate,
  properly-closable window (a different, already-correct mechanism from the `.sh`
  backgrounding bug).
- `tools/gi_bi_relabeling/index.html`: fixed the "merge said Saved but exiting still
  shows the old polygon" gap the user hit — a real, correctly-diagnosed problem, not
  just the known "no live re-union" limitation: `manual_merges.json` was being saved
  correctly, but nothing ever read it back to change what got *displayed*, so exiting
  Split/Merge silently reverted to the stale automatic shape with no trace the merge
  had happened. Fixed in `loadAndRender()`: for any POI with a saved manual-merge
  entry, its displayed geometry is now replaced with the union of its
  manually-selected fragments (a `GeometryCollection`, same bundling approach as the
  Split/Merge highlight — built from a new `rawFragmentsBySourceKey` lookup), tracked
  in a new `manuallyMergedKeys` Set. `styleFor()` now dashes the border for manual
  merges too (previously only tag relabels), and the centroid-icon logic gained a
  third symbol, ⛓, taking priority over ✎/✕ when both apply. Per the user's explicit
  condition before applying ("if the answer is the moment I merged, you can apply"):
  made `exitSplitMerge()` trigger a `loadAndRender()` refresh so this takes effect the
  moment you leave Split/Merge, not just on the next page load — but only on the
  user-facing "Exit Split/Merge" button, not `enterSplitMerge()`'s internal reset call
  to itself (added a `reload` parameter, defaulting true, called as `exitSplitMerge(false)`
  internally — an unconditional reload there would race the new session being set up,
  since `loadAndRender()` tears down and rebuilds `layersBySourceKey` while
  `enterSplitMerge()` is simultaneously reading from it). Verified live, no manual
  reload: merged a second fragment into a real single-fragment POI, clicked "Exit
  Split/Merge", and immediately confirmed both `manuallyMergedKeys` contains it and
  its displayed geometry is now a `GeometryCollection` — screenshot also shows the ⛓
  icon on the now-larger merged shape.

Step 4 (pipeline integration) started: `relabels.json`/`manual_merges.json` are meant
to be artifacts a separate loading step feeds back into real POI formation, not just
annotate this browsing tool. Confirmed with the user this needs full tag-switching,
not just filtering: a POI relabeled from `healthcare=hospital` to `amenity=ice_cream`
must disappear from `residential_healthcare`'s results and appear in
`takeaway_consumption`'s (`get_poi()` is called once per poi_type/tags query, so
nothing currently maps "a corrected tag string" back to which query owns it).

- Block 1 done: `utils/graphml.py` gained `_tag_to_poi_type_map()` — reverse of
  `config/poi_types.csv`'s clauses, `{tag_string: poi_type}` built by enumerating
  every concrete key=value combination each clause can produce
  (`_enumerate_clause_tag_strings()`, cartesian product over list-valued keys), first
  poi_type in CSV row order wins a given string (same convention used elsewhere, e.g.
  `poi_dedup.py`'s ownership resolution). A `craft=True`-style wildcard clause can't
  be enumerated without a real row, so those are collected separately into a
  `{key: poi_type}` fallback map for key-only matching (handled in a later block, not
  this one). Verified against real, previously-confirmed cases: `amenity=ice_cream`
  -> `takeaway_consumption`, `healthcare=hospital` -> `residential_healthcare`,
  `access=private&leisure=pitch` -> `organized_sport_outdoor` (a multi-key clause);
  193 tag strings mapped total, 1 wildcard key (`craft` -> `cultural_production`).
- Block 2 done: `_universe_source_key_index()` — city-wide `source_key -> {row,
  geometry}`, needed to fetch a relabeled-in POI's real shape regardless of which
  query originally found it. Process-level in-memory cache, same convention as
  `_get_city_poi_universe`'s own cache (keyed by `cache_slug|buffer_m`). Reads the
  universe cache file directly with `geopandas.read_file()` rather than going through
  `_get_city_poi_universe()`, which can return the fast GEOS-avoiding form (only a
  `__snap_coord` token, no real geometry) once the universe is already cached on disk
  — the same class of bug hit and fixed earlier in the raw-fragment export. Verified
  against real data: 8,969 entries built in 8.6s, correctly retrieves the real
  "AcquaSport" POI (Polygon geometry, correct name) by its known `source_key`, and a
  second call hits the in-memory cache (instant, same object returned).
- Block 3 done: `apply_relabels(poi, poi_type, ...)` — drops any POI relabeled away
  from `poi_type` (via `_resolve_tag_poi_type`) or marked `"removed"`, pulls in any
  POI relabeled into `poi_type` from elsewhere (fetched from
  `_universe_source_key_index()`, `poi_raw_tag` overridden to the correction), then
  re-runs `merge_nearby_polygon_pois()` so pulled-in POIs group correctly with
  whatever's already there. Reads `tools/gi_bi_relabeling/relabels.json` (cached
  in-memory, `_load_relabels()`). Not yet wired into `get_poi()` (that's block 4).
  Testing this surfaced the same "fast cache-hit path lacks real geometry" issue hit
  earlier in the raw-fragment export — confirms `apply_relabels()` must be called
  from `get_poi()`'s *fresh-build* branches only (right after merge, before caching),
  so the correction gets baked into the cache file itself; it won't retroactively fix
  already-cached files, which will need clearing to pick it up — consistent with
  every other correction made to `get_poi()` this session. Verified end to end
  against real data with a real hospital -> ice_cream relabel: "Policlinico
  Universitario Duilio Casula" correctly dropped from `residential_healthcare`
  (30 -> 29 rows) and appeared in `takeaway_consumption` (163 -> 164 rows) with
  `poi_raw_tag` correctly overridden to `amenity=ice_cream`.
- Real, pre-existing bug found and fixed while testing block 3/4, unrelated to the
  relabeling feature itself: `_stamp_poi_raw_tag`'s `if tags:` branch (graphml.py)
  unconditionally rebuilt `poi` as a `GeoDataFrame` with `crs=poi.crs` at the end,
  without ever checking whether the input actually had real geometry first — crashing
  with `AttributeError: 'DataFrame' object has no attribute 'crs'` whenever the
  universe was loaded via the fast, GEOS-avoiding token-only path (`__snap_coord`, no
  `.crs`), silently caught by `get_poi()`'s outer exception handler and returning
  **zero POIs** for that poi_type with no visible error. Reproduced with zero
  relabeling code involved (deleted a per-query cache, called `get_poi()` fresh) to
  confirm it predates this session's relabeling work entirely. Fixed by adding the
  same `"geometry" not in poi.columns` pass-through guard `merge_nearby_polygon_pois()`
  already had, matching the function's own docstring promise.
- Deeper investigation this crash led to: confirmed (via an Explore agent, checking
  every call site and the multiprocessing start method) that `get_poi()` is only ever
  called from the single main process before any worker pool spawns — not a
  multiprocessing gap. But there's a real, narrower one: the universe's fast
  token-only path only gets bypassed on a city's *very first* download; any later
  rebuild of a *specific* per-query cache (new poi_type added to config, a cache file
  cleared, etc.) while the universe is already cached on disk silently skips
  real-geometry-dependent stamping/merging/relabeling in that process — exactly what
  the crash above was masking. Discussed with the user (clarified this is "re-read
  the already-downloaded file with a real-geometry parser," not "re-download from
  OSM") and agreed on a size-gated fix: added
  `_read_universe_with_real_geometry_if_safe()` + `_REAL_GEOMETRY_UNIVERSE_SIZE_LIMIT_BYTES`
  (150MB; Cagliari's universe is ~71MB and re-reads safely in ~8.6s, verified against
  real data) to `get_poi()`'s universe branch — upgrades a token-only universe to real
  geometry in place and caches the upgrade (`_CITY_POI_UNIVERSE_CACHE`) so the file
  isn't re-read per query within the same process. **Caveat requested explicitly by
  the user, for future debugging**: this size threshold is a guess with a safety
  margin over Cagliari's real file size, not validated against Paris's actual
  universe file size (which is not available in this environment). Paris's universe
  is known to be dramatically larger than Cagliari's, and `_read_geojson_without_gdal`'s
  own docstring already documents that this exact kind of full-file real-geometry
  read has caused hard GEOS/Shapely crashes there before. **If a crash, hang, or
  memory blowup ever occurs in or around `get_poi()`/`merge_nearby_polygon_pois()`
  on a Paris (or other large-city) run, this threshold is a likely suspect — check
  whether Paris's universe file size sits under 150MB (if so, this path is being
  taken and may be the cause) before assuming it's unrelated.** Full end-to-end
  verification (real `get_poi()`, fresh process, universe pre-cached on disk — the
  exact scenario that used to crash) confirmed: `residential_healthcare` 30 -> 29,
  `takeaway_consumption` 163 -> 164, target POI correctly moved between them with its
  tag overridden.
- Generated a Paris counterpart of the Blue/Green Infrastructure Relabeling tool
  (`tools/gi_bi_relabeling_paris/`, a standalone sibling of `tools/gi_bi_relabeling/`
  with its own `data.geojson`/`raw_fragments.geojson`/`poi_type_order.json` and a copy
  of `index.html`/`server.py`/launchers/`vendor/`). Required a `CITY` in-script knob
  in `exports/generate_gi_bi_relabeling_export.py` (`"cagliari"` or `"paris"`), since
  the script previously hardcoded Cagliari's `pois_used.gpkg` path and output folder;
  now both are derived from `PipelineConfig(study_city=CITY).artifact_slug`.
  - Found and fixed a real design gap while building this: `_raw_fragment_rows()`
    (raw pre-merge fragments, used for Split/Merge candidate discovery) only knew how
    to build an OSM-tag "city universe" via Overpass — meaningless for Paris, which is
    `cfg.use_shapefile=True` and never touches Overpass in the real pipeline
    (`get_poi()`'s shapefile branch reads local MGP shapefiles via
    `feature_from_shapefile`/`poi_from_shp`). Added `_raw_fragment_rows_shapefile()`,
    mirroring `get_poi()`'s own shapefile branch (per-`poi_type` `feature_from_shapefile`
    call + `_stamp_poi_raw_tag(poi, None)`, where `poi_raw_tag` is the raw TYPEQU code)
    instead of downloading anything. First attempt at running this against live
    Overpass (before this fix existed) timed out after OSMnx's default 180s on the
    buffered Paris polygon — a red herring the user correctly called out: the real fix
    was to not hit Overpass at all for a shapefile-mode city, not to raise the timeout.
  - Second, deeper gap the user flagged: `apply_relabels()` resolves a relabel's
    target poi_type via `_tag_to_poi_type_map()`, which only parsed `poi_types.csv`'s
    `"tags"` column (OSM clauses) — so for Paris, relabeling a POI to a new TYPEQU
    code could never resolve a target poi_type, and the relabel would silently no-op
    (kept in its original poi_type regardless of the correction). Fixed by also
    folding `poi_types.csv`'s `"labels"` column (the same TYPEQU-code list
    `poi_from_shp()` itself already uses to filter by poi_type) into the same
    `tag_to_poi_type` dict built by `_tag_to_poi_type_map()` — no collision risk with
    OSM `"key=value"` strings since TYPEQU codes never contain `=`, so
    `_resolve_tag_poi_type()`'s exact-match lookup needed no other change. Verified
    against real data: `F110`/`F114` -> `organised_sport_indoor`, `F120` ->
    `informal_sport_indoor`, and an unconfigured code (`C107`, absent from
    `poi_types.csv`) correctly resolves to `None` rather than mismapping.
  - Also found that the export script's direct call into
    `utils.load_shapefile.feature_from_shapefile()` fell back to Cagliari's
    OSM/Overpass branch even with the `CITY` knob set to `"paris"`: that function (and
    others like it) builds its own bare `PipelineConfig()` internally instead of
    taking the caller's, and that bare config's `study_city` default reads
    `os.environ["CAP_STUDY_CITY"]` — which only `main.py` was setting
    (`main.py:137`). Fixed by setting the same env var at the top of the export
    script, matching `main.py`'s own convention, rather than changing the shared
    `load_shapefile.py`/`graphml.py` code paths. Re-verified end to end after both
    fixes: `raw_fragments.geojson` went from 0 fragments (Overpass branch, wrong city)
    to 5441 real fragments sourced from the local shapefile.
  - User caught a third issue by inspecting the output: all 699 shapes in that
    "working" run showed `tag=unknown`. Root cause: `poi/mgp_boundary/` held 27
    per-query cache files pre-dating the switch to shapefile-sourced Paris POIs
    (leftover from when Paris was still OSM/Overpass-sourced) — they lack
    `poi_raw_tag` and have OSM-style columns (`natural`/`landuse`/`leisure`/`water`/
    `waterway`/`fid`) instead. `get_poi()`'s cache-hit path doesn't validate schema,
    so it silently served these stale files instead of rebuilding via the (now
    correct) shapefile branch. Deleted all 27 (verified first, by checking every
    `unique_query_keys()` cache path for a missing `poi_raw_tag` column) and
    re-ran; `get_poi()` rebuilt them correctly from the shapefile.
  - That rebuild then surfaced a deeper, structural issue: with the used-filter back
    on, `data.geojson` came back with 0 shapes. `outputs/poi_exports/mgp_boundary/
    pois_used.gpkg` (the production "POIs actually used by the pipeline" artifact,
    dated 2026-08-26) turned out to itself predate the shapefile switch — every one
    of its `source_key`s is `{"kind": "fid", ...}`, an identity that only ever came
    from that same now-deleted OSM-era cache (confirmed: `Paris/POI_polygon2.shp`
    has no id column at all, so reading it today yields a plain RangeIndex and
    `build_poi_source_key()` falls back to a geometry-hash signature instead —
    structurally unable to match the old `fid`-based keys). Regenerating
    `pois_used.gpkg` needs a full Paris `main.py` run, which weighs
    [[paris-oom-memory]]'s known risk, so asked the user rather than doing it
    unilaterally. Per their choice, `_load_all_shapes()` in the export script now
    accepts `used_keys=None` to skip the used-filter entirely for shapefile-mode
    cities (`main()` branches on `cfg.use_shapefile`) — shows every shape-typed POI
    the configured queries match, not just ones cross-referenced against the stale
    artifact. Cagliari's OSM branch is unaffected (still filters by `pois_used.gpkg`
    as before). Final verified result: 45255 real shapes across the 9 nature/
    aesthetic/quietness poi_types (the only ones with polygon/line geometry in
    Paris's shapefiles — sport/food/health poi_types are point-only there), with
    real readable tags (`landuse=grass`, `waterway=stream`, etc.) — Paris's green/
    blue infrastructure shapefile layers turn out to themselves be OSM-derived using
    the same GI/BI short-code scheme as Cagliari, so `config/osm_raw_tag_codes.csv`
    correctly reverses them for display.
  - User asked whether `natural=scrub` (GI10) was simply absent from Paris data —
    investigation found it's actively configured (`high_nature_immersion`, part of
    the `nature_contact` service), yet `high_nature_immersion` was completely absent
    from `data.geojson` despite `get_poi()` having cached 20268 real polygon rows for
    it (7132 of them GI10). Root cause was in `utils/poi_identity.py`'s
    `build_poi_source_key()` — a real, shared-code bug, not specific to this export
    tool. Paris's `poi_from_shp()` concatenates 3 shapefiles (point/line/polygon)
    with different native columns; only the line layer has a real `osmid` column, so
    polygon rows get `NaN` for it after the concat. `build_poi_source_key()` checked
    `if osmid is not None`, but `float('nan') is not None` is `True` in Python, so
    every polygon row with no real osmid took the "osmid" branch anyway and
    normalized to one single collapsed key (`{"kind": "osmid", "value": null,
    "element_type": null}`) — e.g. 15258 of `high_nature_immersion`'s 20268 cached
    rows all shared that one key, so only the first one encountered (globally, across
    the whole poi_type sweep) survived the export's `seen_keys` dedup. Fixed by
    adding `_is_missing()` (treats both `None` and `pd.isna()` as missing) and using
    it in both the `osmid` check and the `id`/`fid`/`objectid`/`OBJECTID`/`osm_id`
    loop, so NaN-osmid rows correctly fall through to the geometry-hash fallback
    signature (unique per polygon) instead of collapsing. Affects the real pipeline
    too (`apply_relabels`, any other `build_poi_source_key` caller), not just this
    export script. Re-verified against real data: Paris `data.geojson` went from
    45255 to 50594 shapes, `natural=scrub` now present (3749, under
    `high_nature_immersion`), `natural_aesthetic` 5011 -> 14298, `accessible_nature`
    430 -> 2021 (previously-collapsed distinct polygons now correctly separated).
  - User also reported switching from the Paris interface to the Cagliari one still
    showed Paris POIs — both `tools/gi_bi_relabeling/server.py` and
    `tools/gi_bi_relabeling_paris/server.py` were hardcoded to the same port (8766),
    so a still-running Paris server process (even an orphaned background one) could
    keep answering requests meant for the newly-started Cagliari one. Changed the
    Paris copy's `PORT` to 8767 (server.py + both launcher scripts' browser-open
    URL), so the two tools can no longer collide regardless of what's still running.
  - User reported Split/Merge finding no candidates on Paris. Root cause:
    `_raw_fragment_rows_shapefile()`'s `"tag"` field was left as the bare raw code
    (`"GI10"`), while `_load_all_shapes()`'s `"tag"` field (in `data.geojson`) is the
    human-readable reversal (`"natural=scrub"`) — Split/Merge's candidate search
    (`index.html`) matches fragments by exact `tag` equality, so for Paris the two
    vocabularies never matched and candidate lists were always empty. Fixed by
    reversing through the same `code_to_raw_tag` lookup in
    `_raw_fragment_rows_shapefile()` too, so both files agree.
  - Re-running to verify surfaced a real multi-minute hang (killed after 13+ minutes
    with zero incremental output — the exact "silent long loop" pattern flagged in
    this file's own conventions). Timed profiling (per-query `get_poi()` calls: all
    cache hits, ~6s total combined) ruled out the POI loading itself, isolating the
    actual cost to `_load_all_shapes()`'s `member_keys` computation: for every POI
    row it did a real Shapely `.buffer()` then linearly scanned *every* raw fragment
    sharing that tag, checking `.contains()` one by one — O(rows_in_poi_type x
    candidates_for_tag). Before the `poi_identity.py` NaN fix above, most fragments
    collapsed into one bogus duplicate, keeping this loop accidentally cheap; fixing
    that identity bug correctly made fragment lists real and large (e.g. ~45000 for
    `landuse=grass`), which is what turned this into a ~2-billion-check hang for
    `perceived_nature` alone. Fixed by building one `shapely.strtree.STRtree` per tag
    group (indexing fragment centroids) instead of a linear scan — verified: full
    Paris export now completes in 1m23s (previously killed after 13+ minutes,
    unfinished), same 50594-shape/95383-fragment result, `data.geojson`/
    `raw_fragments.geojson` tag vocabularies now 19/19 matching, and a spot-checked
    POI's `member_keys` now correctly lists multiple real fragments instead of just
    itself.
