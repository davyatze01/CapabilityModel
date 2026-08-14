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

