#!/usr/bin/env bash
# Installs the systemd unit that keeps faulty core 24 (logical CPUs 12,13) offline
# across reboots and resume-from-suspend. Run with: sudo ./install-disable-bad-cpus.sh
set -euo pipefail

UNIT_NAME="disable-bad-cpus.service"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SRC_DIR/$UNIT_NAME"
DEST="/etc/systemd/system/$UNIT_NAME"

if [[ $EUID -ne 0 ]]; then
  echo "This installer must run as root. Re-run: sudo $0" >&2
  exit 1
fi

if [[ ! -f "$SRC" ]]; then
  echo "Unit file not found at $SRC" >&2
  exit 1
fi

echo "Installing $UNIT_NAME -> $DEST"
install -m 0644 "$SRC" "$DEST"

systemctl daemon-reload
systemctl enable --now "$UNIT_NAME"

echo
echo "Done. Current state:"
systemctl --no-pager --full status "$UNIT_NAME" || true
echo
echo "CPU 12/13 online status (expect 0):"
for c in 12 13; do
  printf "  cpu%s online = %s\n" "$c" "$(cat /sys/devices/system/cpu/cpu$c/online 2>/dev/null || echo '?')"
done
