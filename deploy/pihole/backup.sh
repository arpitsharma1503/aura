#!/usr/bin/env bash
# Back up the Pi's network configuration before installing Pi-hole.
#   sudo deploy/pihole/backup.sh
# Writes ~/aura-backups/network-<timestamp>.tar.gz (mode 600: it contains Wi-Fi
# connection files with passwords). Changes nothing else.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
OWNER="${SUDO_USER:-arpit}"
HOME_DIR="$(getent passwd "$OWNER" | cut -d: -f6)"
DEST="$HOME_DIR/aura-backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$DEST"
# Current state, for comparison after the install.
{ ip addr; echo; ip route; echo; ip -6 route; } > "$WORK/ip.txt"
nmcli -t con show > "$WORK/nmcli-connections.txt" 2>&1 || true
nmcli dev show > "$WORK/nmcli-devices.txt" 2>&1 || true
ss -lntup > "$WORK/listening-ports.txt" 2>&1 || true
nft list ruleset > "$WORK/nftables-ruleset.txt" 2>&1 || true
systemctl list-units --type=service --state=running --no-legend > "$WORK/services.txt" 2>&1 || true
cp -a /etc/resolv.conf "$WORK/resolv.conf" 2>/dev/null || true
FILES=(/etc/hosts /etc/hostname /etc/NetworkManager /etc/nftables.conf /etc/dnsmasq.conf /etc/dnsmasq.d /etc/systemd/resolved.conf)
EXIST=()
for f in "${FILES[@]}"; do [ -e "$f" ] && EXIST+=("$f"); done
umask 077
tar -czf "$DEST/network-$STAMP.tar.gz" -C / "${EXIST[@]#/}" -C "$WORK" .
chown -R "$OWNER:$OWNER" "$DEST"
chmod 700 "$DEST"; chmod 600 "$DEST/network-$STAMP.tar.gz"
echo "Backup written: $DEST/network-$STAMP.tar.gz"
tar -tzf "$DEST/network-$STAMP.tar.gz" | sed 's/^/  /' | head -40
