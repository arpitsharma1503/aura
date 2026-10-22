#!/usr/bin/env bash
# Turn Pi-hole off without uninstalling it.
#   sudo deploy/pihole/rollback.sh          stop and disable the DNS service
#   sudo deploy/pihole/rollback.sh --start  start it again
# BEFORE running this, if the router (or any device) was pointed at the Pi for
# DNS, set it back to automatic / 192.168.1.1 first, or those devices lose DNS.
# Full removal: sudo pihole uninstall   (the Pi's own DNS never depended on Pi-hole).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
if [ "${1:-}" = "--start" ]; then
  systemctl enable --now pihole-FTL
else
  systemctl disable --now pihole-FTL
fi
systemctl --no-pager status pihole-FTL | head -3
echo "Pi's own DNS (unchanged): $(grep '^nameserver' /etc/resolv.conf | paste -sd' ')"
