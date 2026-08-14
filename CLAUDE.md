# Project preferences

## Pair-programming mode: propose one block at a time

Write code, but never in bulk and never unannounced. The user must be able to follow every change
as it happens and explain it afterwards to a colleague, so the unit of work is **one block** —
one function, or one contiguous logical unit.

The user has ADHD and finds long reading hard. Long diffs and "go read the file" both break the
thread. **Keep it conversational: the code comes to them in the chat message, not as a file they
have to go open.**

Per block, always in this order:

1. Say what is changing and why, in a sentence or two.
2. Show *that block only*, inline in the message.
3. Wait for yes / adjust / no.
4. Apply it, then stop.

- **Always ask before applying.** No exceptions for "obvious" changes.
- **Group by module, the way the user would work.** All changes needed *inside* one function are
  proposed together in one message. Don't jump files mid-thread — finish the function, then ask
  separately about the caller in the other file.
- **Length is the signal.** If a proposal is turning into a wall of text, it was grouped too
  greedily — split it. Never send a multi-file diff dump.
- **Batch exception, one-sentence rule.** When a change touches several functions but each
  individual edit is explainable in *one sentence* (updating a call site for a new parameter,
  a rename, propagating a signature), offer the batch: list the one-liners and let the user
  approve them all at once. If any edit needs more than one sentence to explain, it is not
  simple — take it through the normal per-block flow instead. The user can also ask for a batch
  explicitly ("batch this"); that is a per-moment call, not a standing exception.
- **Design forks still come first.** Before writing anything that settles a real design question
  (algorithm, data model, where a stage plugs into the pipeline), propose exactly 2 approaches as
  numbered steps and explain why approach 1 is preferable to approach 2. The user decides.
- When asked to explain a step, frame it as "we're doing this instead of that" — contrast the
  chosen step against the alternative it displaced.

## Dev log

Maintain `docs/DEVLOG.md` as a running log of the code edits made in this repo — whether the user
wrote them or I did, since every change is proposed and approved block by block (see above).

- One `## YYYY-MM-DD` block per day.
- Within a block, one bullet per semantically distinct edit: where the edit is (file/function),
  what problem was detected, and how it was fixed. Keep adding detail to the same bullet while
  we're still working the same thread; start a new bullet once the conversation moves to a
  semantically different edit.
- Check `git diff`/`git status` to ground entries in what actually changed, rather than relying
  only on the conversation's account of it.
- Update the log at natural checkpoints — when a discussed change looks finished, or when asked
  directly — not continuously mid-edit.

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
  baseline/persona…), `differences.gpkg` with one `diff_<A>_to_<B>__<capability>` layer per
  pair *per capability* (`d_<capability> = level(second) − level(first)`, ELECTRE levels 1–5),
  and `profile_comparison.qgz`. The difference grids use a red/yellow/green ramp: yellow = same
  level, red = second scenario lower (by 1–4 levels), green = second scenario higher. Each
  layer's style is embedded into the gpkg itself (`saveStyleToDatabase`, one layer = one table
  = one unambiguous default style), so opening `differences.gpkg` directly in QGIS — outside
  `profile_comparison.qgz` — still shows the correct colors.