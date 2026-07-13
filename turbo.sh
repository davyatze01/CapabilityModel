#!/usr/bin/env bash
# Intel Turbo Boost control for this machine (i9-14900KF).
#
# Policy: keep turbo ON in general (better performance). Only turn it OFF if you
# start seeing HARD failures — machine-wide segfaults, "invalid opcode", kernel
# GPFs, random freezes — which on this CPU can be a symptom of instability under
# high boost clocks/voltage. Disabling turbo caps clocks at the base frequency
# and is a cheap way to test whether the crashes are boost-related.
#
# Turbo state does NOT persist across reboots; re-run after a restart if needed.
#
# Usage:
#   sudo ./turbo.sh on       # enable turbo (default state)
#   sudo ./turbo.sh off      # disable turbo (do this only under hard failures)
#   ./turbo.sh status        # show current state (no root needed)

set -euo pipefail

PSTATE_NOTURBO=/sys/devices/system/cpu/intel_pstate/no_turbo
CPUFREQ_BOOST=/sys/devices/system/cpu/cpufreq/boost

set_turbo() {
    # $1 = "on" or "off"
    local want="$1"

    if [[ -w "$PSTATE_NOTURBO" ]]; then
        # intel_pstate: no_turbo = 0 means turbo enabled, 1 means disabled.
        if [[ "$want" == "on" ]]; then
            echo 0 > "$PSTATE_NOTURBO"
        else
            echo 1 > "$PSTATE_NOTURBO"
        fi
        echo "intel_pstate: turbo ${want} (no_turbo=$(cat "$PSTATE_NOTURBO"))"
    elif [[ -w "$CPUFREQ_BOOST" ]]; then
        # generic cpufreq: boost = 1 means enabled, 0 means disabled.
        if [[ "$want" == "on" ]]; then
            echo 1 > "$CPUFREQ_BOOST"
        else
            echo 0 > "$CPUFREQ_BOOST"
        fi
        echo "cpufreq: boost ${want} (boost=$(cat "$CPUFREQ_BOOST"))"
    else
        echo "ERROR: no writable turbo control found." >&2
        echo "  Tried: $PSTATE_NOTURBO and $CPUFREQ_BOOST" >&2
        echo "  Are you running as root? (sudo $0 $want)" >&2
        exit 1
    fi
}

status() {
    if [[ -r "$PSTATE_NOTURBO" ]]; then
        if [[ "$(cat "$PSTATE_NOTURBO")" == "0" ]]; then
            echo "intel_pstate: turbo is ON  (no_turbo=0)"
        else
            echo "intel_pstate: turbo is OFF (no_turbo=1)"
        fi
    elif [[ -r "$CPUFREQ_BOOST" ]]; then
        if [[ "$(cat "$CPUFREQ_BOOST")" == "1" ]]; then
            echo "cpufreq: boost is ON  (boost=1)"
        else
            echo "cpufreq: boost is OFF (boost=0)"
        fi
    else
        echo "No turbo control interface found on this system." >&2
        exit 1
    fi

    # Current per-core clocks, handy for confirming turbo is actually engaging.
    if command -v grep >/dev/null; then
        echo "--- current core MHz ---"
        grep -i '^cpu MHz' /proc/cpuinfo || true
    fi
}

case "${1:-}" in
    on)     set_turbo on ;;
    off)    set_turbo off ;;
    status) status ;;
    *)
        echo "Usage: $0 {on|off|status}" >&2
        echo "  on     enable turbo (default; run with sudo)" >&2
        echo "  off    disable turbo — only when hitting hard failures (run with sudo)" >&2
        echo "  status show current turbo state (no root needed)" >&2
        exit 2
        ;;
esac
