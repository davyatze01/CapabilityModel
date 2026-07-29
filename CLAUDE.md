# Project preferences

## Fail fast, don't hang

When implementing anything long-running (routing, batch jobs, subprocess calls, GC/memory-bound
work), prioritize a fail-fast design over one that tries indefinitely to recover or muddle through:

- Prefer explicit, bounded resources (fixed memory caps, timeouts, retry limits) over letting a
  process try to reclaim/recover on its own for an unbounded amount of time. If a limit is hit,
  the process should fail/exit/get killed promptly and visibly, not stall while it silently tries
  to work around the limit (e.g. a JVM grinding through expensive GC cycles instead of throwing
  `OutOfMemoryError`).
- A clean crash with a clear error is always preferable to a silent hang. A hang with no signal
  is much harder to diagnose than a crash, and easy to mistake for "still working."
- When something can legitimately take a long time, always pair it with concrete, periodic log
  output showing real progress or real resource usage (not just a spinner) — enough that it's
  unambiguous whether the process is actively working or actually stuck. Prefer non-invasive
  progress signals (reading counters/state) over ones that themselves perturb the system being
  measured (e.g. don't force extra GC cycles just to report memory usage).
- Bounded/capped resource usage (e.g. a memory cgroup that kills the process if exceeded) is
  preferred over unconstrained resource usage, even if the latter would let a run "succeed" by
  simply using more memory. Getting killed at a known ceiling is a feature, not a problem to work
  around.
- Avoid doing background runs because they might hang and you wouldn't know how they went. Instruct instead the user on what to run and what to report

## Always show where the pipeline is

The terminal must always make it clear what stage/operation is currently running — no
silent gaps between log lines that leave the user unable to tell whether the process
is working or stuck (this bit us for real: a CSV-parsing loop inside bus routing ran
completely silently — one print before, one print after — and for Paris that file is
17.4 GB, so the silent stretch could run for many minutes with zero visible signal).

- Any operation expected to take longer than ~1 minute needs a real progress bar
  (`tqdm`, matching the style already used in `non_bus_routing_stage.py` and
  `public_transport_routing_stage.py`'s CSV-reading loop), not just an occasional
  print statement.
- Drive the progress signal from something cheap and already known (bytes read vs.
  total file size, rows processed vs. a precomputed total, etc.) rather than adding a
  separate counting pass just to feed the bar.
- Don't let the progress signal itself perturb the hot loop: update the bar every N
  iterations (e.g. every 100k-200k rows) instead of on every single one.
- When wrapping a text-mode file for byte-accurate progress, don't rely on `f.tell()`
  once you've started iterating it — Python raises `OSError: telling position
  disabled by next() call`. Track bytes consumed yourself (wrap the iterator, sum
  `len(line.encode("utf-8"))` per line) instead.

## In-script knobs, not CLI flags

Runnable entry points are configured by editing module-level knobs at the top of the file
(the way `main.py` exposes `study_city`, `SAFE_MODE`, `WORKER_COUNT`, `LIGHT_OUTPUT`,
`NOTIFY_CRASH`) — not by passing command-line arguments. New behavior toggles go there as
commented constants, and `__main__` just reads them. Keep `python <script>.py` argument-free.

## Individual-profile scenario comparison (personas)

`profiles.py` + `scenarios.py` run the capability model for different *individuals* (personas)
and compare them. A `Profile` reconfigures a run in two places: before routing
(`Profile.config_overrides` → walking speed, the routed/fused non-bus mode set via
`cfg.enabled_non_bus_modes`, an accessible-stops-only GTFS feed, and an `artifact_slug` suffix
so each persona lands in its own `artifacts/Cagliari_<key>/` + `outputs/.../Cagliari_<key>`
namespace), and during the accessibility stage (`ctx.profile` → the per-POI utility multiplier
`u(y)`, i.e. affordability, with a per-instance override for canteen POIs).

- **Run it** by editing the knobs at the top of `scenarios.py`: `STUDY_CITY` (which city these
  scenarios run against — independent of `main.py`'s `study_city`, so main can stay on `"paris"`
  while scenarios run `"cagliari"`) and `PROFILE_SCENARIO` (a key in `profiles.SCENARIOS`, e.g.
  `"elder-student"`; `None` = legacy routing-disruption scenarios), then `python scenarios.py`.
  Each persona does a **full pipeline pass from scratch** — no
  rescaling of a shared impedance bundle — so a comparison is several full routing runs; launch
  it yourself rather than in the background (see "Fail fast").
- **Baseline** (`profiles.BASELINE`) is the universal traveler (all modes, 5 km/h, `u=1`, full
  GTFS) and runs in its own `Cagliari_baseline` namespace so it never overwrites `main.py`'s
  outputs. With no profile attached, every knob defaults to baseline, so ordinary runs are
  byte-identical to before.
- **Mode gating** is exact via the RRA fusion: a disabled mode contributes an empty (⇒ 0.0)
  decay, which ranks last and is a no-op in `1 − Π(1 − w)`, leaving the enabled modes the
  correct redundancy weights — equivalent to a smaller mode count `m` (no `decay.py` change).
- **Accessible-stops** feed is built by `gtfs/make_accessible_gtfs.py` (keep
  `wheelchair_boarding == 1`; `--keep-unknown` also keeps `0`/empty). Canteen instances live in
  `config/canteens_cagliari.csv` (OSM ids), not as a POI type/category.
- **Outputs** land in `scenarios/<key>/`: per-scenario capability hex grids (3 capabilities ×
  baseline/persona…), `differences.gpkg` with one pairwise level-difference grid per pair
  (`d_<capability> = level(second) − level(first)`, ELECTRE levels 1–5), and
  `profile_comparison.qgz`. The difference grids use a red/yellow/green ramp: yellow = same
  level, red = second scenario lower (by 1–4 levels), green = second scenario higher.