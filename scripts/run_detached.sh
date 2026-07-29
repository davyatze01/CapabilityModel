#!/usr/bin/env bash
# Launch the capability pipeline FULLY DETACHED from this terminal and the editor.
#
# Why this exists: running a long routing job in the VS Code integrated terminal
# couples the run's lifetime to the editor. VS Code here is a Flatpak, so its
# terminal lives *inside the sandbox* -- if the editor (or the terminal) dies, the
# whole process tree gets SIGHUP and the run dies with it (you saw the JVM print
# its orderly "Database closed" shutdown when this happened). The reverse is a
# risk too: a runaway run could drag the desktop down.
#
# This wrapper runs run_safe.sh in a brand-new session (setsid) with no
# controlling terminal and output redirected to a timestamped log, so:
#   * closing or crashing this terminal / the editor does NOT stop the run, and
#   * the run still gets run_safe.sh's memory cap + crash auto-retry.
#
# Run this from a HOST terminal (konsole) -- NOT the Flatpak/VS Code terminal --
# so systemd-run is reachable and the job is truly independent of the editor.
#
# Usage:
#   ./run_detached.sh                  # python main.py, capped, detached
#   MEM_MAX=40G ./run_detached.sh      # override the cap (forwarded to run_safe.sh)
#   ./run_detached.sh other.py --flag  # different entrypoint + args
#
# After launch it prints the log path and PID. To watch or stop:
#   tail -f <log>            # follow progress
#   kill <PID>              # stop the run (or: pkill -f run_safe.sh)

set -uo pipefail
cd "$(dirname "$0")/.."

if [ -f /.flatpak-info ]; then
    echo "ERROR: You're inside the Flatpak sandbox (the VS Code integrated terminal)." >&2
    echo "       Run this from a HOST terminal (konsole) so the job is independent of" >&2
    echo "       the editor and systemd-run is reachable." >&2
    exit 1
fi
if ! command -v systemd-run >/dev/null 2>&1; then
    echo "ERROR: systemd-run not found. Run this from a host terminal (konsole)." >&2
    exit 1
fi

mkdir -p logs
ts="$(date +%Y%m%d_%H%M%S)"
LOG="logs/run_${ts}.log"
PIDFILE="logs/run_${ts}.pid"

# setsid --fork: run in a NEW session with no controlling terminal, immune to the
# SIGHUP fired when this terminal / the editor closes. The inner shell records its
# own PID, then exec's run_safe.sh (PID is preserved across exec), so the PID we
# report is the one that actually controls the run. stdin from /dev/null so the
# detached job never blocks waiting on a terminal that is gone.
setsid --fork bash -c 'echo $$ > "$0"; exec nohup ./scripts/run_safe.sh "$@"' \
    "$PIDFILE" "$@" >"$LOG" 2>&1 </dev/null

# Let the child write its PID before we read it back.
sleep 1
PID="$(cat "$PIDFILE" 2>/dev/null || echo '?')"

echo "[run_detached] Started, fully detached from this terminal (PID $PID)."
echo "[run_detached]   Log:    $LOG"
echo "[run_detached]   Follow: tail -f $LOG"
echo "[run_detached]   Recent: grep -E '\[mem\]|chunk|ERROR' $LOG | tail"
echo "[run_detached]   Stop:   kill $PID    (fallback: pkill -f run_safe.sh)"
echo "[run_detached] You can now close this terminal or the editor; the run keeps going."
