#!/usr/bin/env bash
# Run the capability pipeline inside a systemd memory cgroup so that an
# out-of-memory blow-up kills *this pipeline only* — never the desktop.
#
# Background: the Paris run can exhaust all RAM. When that happens with no cap,
# the kernel can't fork new processes, so you can't even open a terminal and the
# whole machine appears frozen (the mouse still moves because the compositor
# needs no new process). Capping the run in a cgroup with swap disabled makes
# systemd-oomd kill the pipeline instead, leaving the konsole/desktop alive.
#
# This script also auto-retries crashes that look like the known CPU-degradation
# issue on this machine (random SIGILL/SIGSEGV/SIGBUS in code that cannot
# legitimately fault). Retries are bounded and discriminating:
#   - only signals 4 (ILL), 7 (BUS), 11 (SEGV) are retried — never the OOM
#     SIGKILL (a real resource ceiling), never plain nonzero exits (real
#     errors), never a user interrupt;
#   - at most MAX_RETRIES retries (default 3), with a cooldown in between;
#   - if the same signal hits the same file:line twice in a row, that is a
#     deterministic crash — i.e. a software bug, not random hardware corruption
#     — and the script stops retrying so the bug stays visible.
# The pipeline's own stage-level resume logic makes a restart cheap: completed
# artifacts are skipped, so a retry resumes roughly where the crash happened.
#
# Usage:
#   ./run_safe.sh                 # runs `python main.py` capped at 45G, no swap
#   MEM_MAX=40G ./run_safe.sh     # override the cap
#   MAX_RETRIES=0 ./run_safe.sh   # disable crash auto-retry
#   ./run_safe.sh other_script.py # run a different entrypoint under the same cap
#
# Tune MEM_MAX below your total RAM minus what the desktop + VS Code need
# (≈15G here, so 45G of 62G is a safe default).

set -uo pipefail

cd "$(dirname "$0")"

MEM_MAX="${MEM_MAX:-45G}"
PYTHON="${PYTHON:-.venv/bin/python}"
MAX_RETRIES="${MAX_RETRIES:-3}"
RETRY_COOLDOWN="${RETRY_COOLDOWN:-15}"
ENTRY="${1:-main.py}"
shift || true

if ! command -v systemd-run >/dev/null 2>&1; then
    echo "ERROR: systemd-run not found. Run this from a host terminal (konsole)," >&2
    echo "       not from inside the Flatpak sandbox." >&2
    exit 1
fi

echo "[run_safe] Launching '$PYTHON $ENTRY $*' under MemoryMax=$MEM_MAX, swap disabled."
echo "[run_safe] If it exceeds the cap it will be killed; your desktop stays alive."
echo "[run_safe] Hardware-suspect crashes (SIGILL/SIGSEGV/SIGBUS) auto-retry up to $MAX_RETRIES time(s)."

# Tell the pipeline the real memory budget so it derives the worker count from the
# cgroup cap, not the machine's full RAM (context.py reads CAP_MEM_BUDGET_GB).
mem_max_gb="$(printf '%s' "$MEM_MAX" | sed -E 's/[Gg][Bb]?$//; s/[Mm][Bb]?$//')"
case "$MEM_MAX" in
    *[Mm]*) mem_max_gb="$(awk "BEGIN{printf \"%.1f\", $mem_max_gb/1024}")" ;;
esac
export CAP_MEM_BUDGET_GB="$mem_max_gb"

# MemoryHigh is a soft ceiling below MemoryMax: the kernel throttles/reclaims this
# cgroup once it's crossed, instead of the sudden hard SIGKILL at MemoryMax. Without
# it, a fast allocator can go from "fine" to "at the hard cap" in well under one
# 0.5s poll tick, spiking system-wide memory pressure (and I/O from reclaim) right
# up to the moment of the kill — which is the kind of thrashing that can make the
# whole desktop feel frozen even though the cgroup cap is doing its job. Set to 90%
# of MemoryMax so there's real headroom to throttle before the hard kill fires.
MEM_HIGH="${MEM_HIGH:-$(awk "BEGIN{printf \"%.1fG\", $mem_max_gb * 0.9}")}"
echo "[run_safe] Soft throttle at MemoryHigh=$MEM_HIGH, hard kill at MemoryMax=$MEM_MAX, low I/O priority (IOWeight=10)."

MEM_LOG="$(mktemp)"
CRASH_LOG="$(mktemp)"
trap 'rm -f "$MEM_LOG" "$CRASH_LOG"' EXIT

# `systemd-run --scope` hands the actual pipeline process off to the systemd --user
# manager as a new scope unit — it is NOT a signal-reachable child of this script or
# its process group. A plain terminal Ctrl+C sent to the whole foreground process
# group happens to also reach it, but anything that signals only this script's PID
# (VS Code's Stop button, `kill <pid>`, some job-control setups) leaves the scoped
# pipeline process running, orphaned, in the background. So on INT/TERM we stop the
# scope explicitly rather than relying on signal propagation.
#
# `systemctl --user stop` talks to the systemd --user manager over D-Bus. If the
# machine is already under heavy memory/IO pressure (the exact situation this script
# exists to prevent, but the box can still get there before the cgroup cap bites),
# that manager can be too starved to answer, and the stop call hangs forever with no
# feedback — repeated Ctrl+C just re-prints the message and does nothing, and the
# only way out becomes a hard reset. Every step below is wrapped in `timeout` and
# there is always a next, more forceful fallback that does not depend on D-Bus or any
# other daemon being responsive, so an interrupt is bounded no matter what state the
# system is in: (1) ask systemd to stop the scope, (2) if that doesn't land within a
# few seconds, tell systemd to SIGKILL it, (3) if even that call hangs or fails,
# SIGKILL the local process directly — RUN_PID is a real child of this shell
# (confirmed by the fact that `wait "$RUN_PID"` below works), so this is reachable
# even with systemd/D-Bus completely wedged.
_interrupted=0
_interrupt_count=0
UNIT=""
RUN_PID=""
STOP_GRACE="${STOP_GRACE:-5}"
_force_kill() {
    echo "[run_safe] Force-killing $UNIT.scope via systemd (bounded, non-blocking)." >&2
    timeout 3 systemctl --user kill --signal=KILL "$UNIT.scope" 2>/dev/null
    # Whether or not the above worked or hung, make sure the local process is dead —
    # this line does not depend on systemd/D-Bus at all.
    [ -n "$RUN_PID" ] && kill -KILL "$RUN_PID" 2>/dev/null
}
_on_interrupt() {
    _interrupted=1
    _interrupt_count=$((_interrupt_count + 1))
    if [ "$_interrupt_count" -gt 1 ]; then
        # User already asked once and is asking again — they've decided the graceful
        # path is taking too long. Don't make them wait out the grace period twice.
        echo "[run_safe] Second interrupt — forcing it now." >&2
        _force_kill
        return
    fi
    echo "[run_safe] Caught interrupt — stopping $UNIT.scope (grace period ${STOP_GRACE}s before a forced kill)." >&2
    [ -n "$UNIT" ] && timeout 3 systemctl --user stop "$UNIT.scope" 2>/dev/null &
    # Watchdog: force-kill after STOP_GRACE regardless of further Ctrl+C presses, so
    # the interrupt is bounded even if the user can't tell the first one landed.
    (
        sleep "$STOP_GRACE"
        if [ -n "$RUN_PID" ] && kill -0 "$RUN_PID" 2>/dev/null; then
            _force_kill
        fi
    ) &
}
trap _on_interrupt INT TERM

# Run one attempt of the pipeline. Sets the global STATUS to the exit status.
# stderr of the pipeline is tee'd into CRASH_LOG so that after a signal death we
# can read the faulthandler traceback and identify where the crash happened.
run_once() {
    local attempt="$1"
    shift
    UNIT="cap-pipeline-$$-a$attempt"
    : > "$MEM_LOG"
    : > "$CRASH_LOG"

    # A process killed by SIGKILL (which is exactly what happens when the cgroup's
    # MemoryMax is exceeded) cannot run any code of its own to explain why — SIGKILL
    # isn't catchable. So the "why was it killed" report has to come from something
    # that survives the kill and was watching from outside: this script. Run it in
    # the background, poll the cgroup's own memory.current via systemctl in parallel
    # (so we have a real last-known-reading close to the kill, not just a guess),
    # then report clearly once it exits.
    # PYTHONFAULTHANDLER guarantees a Python-level traceback even when the fault
    # happens inside native extension code — that traceback is what the crash
    # signature (below) is extracted from.
    systemd-run --user --scope \
        --unit "$UNIT" \
        -p MemoryMax="$MEM_MAX" \
        -p MemoryHigh="$MEM_HIGH" \
        -p MemorySwapMax=0 \
        -p IOWeight=10 \
        -E CAP_MEM_BUDGET_GB="$mem_max_gb" \
        -E OPENBLAS_CORETYPE=HASWELL \
        -E PYTHONFAULTHANDLER=1 \
        "$PYTHON" "$ENTRY" "$@" 2> >(tee -a "$CRASH_LOG" >&2) &
    RUN_PID=$!

    (
        while kill -0 "$RUN_PID" 2>/dev/null; do
            current="$(systemctl --user show "$UNIT.scope" --property=MemoryCurrent --value 2>/dev/null)"
            if [ -n "$current" ] && [ "$current" != "[not set]" ]; then
                gb="$(awk "BEGIN{printf \"%.2f\", $current/1024/1024/1024}")"
                echo "$(date '+%H:%M:%S') ${gb}GB / ${mem_max_gb}GB" > "$MEM_LOG"
            fi
            sleep 0.5
        done
    ) &
    POLL_PID=$!

    wait "$RUN_PID"
    STATUS=$?

    if [ "$_interrupted" -eq 1 ]; then
        # Bash returns from `wait` as soon as the trapped signal is handled, which
        # can be before systemd-run has actually finished stopping the scope.
        # RUN_PID hasn't been reaped yet in that case, so wait on it again to get
        # the real exit status instead of reporting success while it's still
        # shutting down.
        wait "$RUN_PID" 2>/dev/null
        STATUS=$?
    fi

    kill "$POLL_PID" 2>/dev/null
    wait "$POLL_PID" 2>/dev/null
}

# Extract "signal:file:line" from the faulthandler output of the last attempt —
# the innermost frame of the current thread, i.e. where the crash actually hit.
crash_signature() {
    local sig="$1"
    local frame
    frame="$(grep -A1 '^Current thread' "$CRASH_LOG" | grep -m1 -oE 'File "[^"]+", line [0-9]+' || true)"
    echo "${sig}:${frame:-unknown}"
}

# Report kernel machine-check / hardware-error events from the last few minutes.
check_mce() {
    local hits
    hits="$(journalctl -k --since '5 min ago' --no-pager 2>/dev/null | grep -icE 'mce|machine check|hardware error' || true)"
    if [ -z "$hits" ]; then
        echo "[run_safe] Could not read the kernel journal to check for machine-check events." >&2
    elif [ "$hits" -gt 0 ]; then
        echo "[run_safe] Kernel journal shows $hits machine-check/hardware-error line(s) in the last 5 min — hardware fault confirmed." >&2
    else
        echo "[run_safe] No machine-check events in the kernel journal (absence doesn't rule out silent CPU corruption)." >&2
    fi
}

attempt=0
prev_signature=""
while :; do
    attempt=$((attempt + 1))
    if [ "$attempt" -gt 1 ]; then
        echo "[run_safe] === Retry $((attempt - 1))/$MAX_RETRIES ($(date '+%H:%M:%S')) — the pipeline resumes from completed artifacts. ===" >&2
    fi

    run_once "$attempt" "$@"
    last_mem="$(cat "$MEM_LOG" 2>/dev/null || true)"

    if [ "$_interrupted" -eq 1 ]; then
        echo "[run_safe] Interrupted by user — not retrying." >&2
        break
    fi

    if [ "$STATUS" -le 128 ]; then
        if [ "$STATUS" -ne 0 ]; then
            echo "[run_safe] Process exited with code $STATUS (not a signal kill) — a real error, not retrying." >&2
            [ -n "$last_mem" ] && echo "[run_safe] Last observed memory: $last_mem" >&2
        else
            echo "[run_safe] Process completed successfully."
        fi
        break
    fi

    sig=$((STATUS - 128))
    sig_name="$(kill -l "$sig" 2>/dev/null || echo "unknown")"

    if [ "$sig" -eq 9 ]; then
        echo "[run_safe] KILLED (SIGKILL) — this is the cgroup OOM killer: the process exceeded MemoryMax=$MEM_MAX. Not retrying (it would just OOM again)." >&2
        [ -n "$last_mem" ] && echo "[run_safe] Last observed memory before exit: $last_mem (polled every 0.5s, so this is close to but not exactly the kill instant)." >&2
        break
    fi

    echo "[run_safe] Process terminated by signal $sig ($sig_name)." >&2
    if [ -n "$last_mem" ]; then
        echo "[run_safe] Last observed memory before exit: $last_mem (polled every 0.5s, so this is close to but not exactly the kill instant)." >&2
    else
        echo "[run_safe] No memory reading was captured before it died (killed too fast after the first poll)." >&2
    fi

    case "$sig" in
        4|7|11) ;;  # SIGILL / SIGBUS / SIGSEGV: hardware-suspect on this machine
        *)
            echo "[run_safe] Signal $sig ($sig_name) is not in the hardware-suspect set (ILL/BUS/SEGV) — not retrying." >&2
            break
            ;;
    esac

    signature="$(crash_signature "$sig")"
    check_mce

    if [ -n "$prev_signature" ] && [ "$signature" = "$prev_signature" ]; then
        echo "[run_safe] Same crash twice in a row ($signature) — deterministic, so this looks like a SOFTWARE bug, not random hardware corruption. Not retrying." >&2
        break
    fi
    prev_signature="$signature"

    if [ "$attempt" -gt "$MAX_RETRIES" ]; then
        echo "[run_safe] Crash at $signature looks hardware-suspect, but the retry budget ($MAX_RETRIES) is exhausted — giving up." >&2
        break
    fi

    echo "[run_safe] Crash at $signature looks hardware-suspect (random fault in code that shouldn't crash)." >&2
    echo "[run_safe] Cooling down ${RETRY_COOLDOWN}s, then retrying." >&2
    sleep "$RETRY_COOLDOWN"
done

exit "$STATUS"
