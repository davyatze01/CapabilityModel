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