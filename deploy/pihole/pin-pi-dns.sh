#!/usr/bin/env bash
# Make the Pi itself always use the router (192.168.1.1) for DNS, whatever the
# router's DHCP hands out. Needed BEFORE the router is told to give out the Pi's
# address: otherwise the Pi would depend on its own Pi-hole and could lose DNS
# (weather, updates, apt) whenever Pi-hole is down.
#
#   sudo deploy/pihole/pin-pi-dns.sh          pin the Pi's DNS to the router
#   sudo deploy/pihole/pin-pi-dns.sh --undo   back to "DNS from DHCP" (the original setting)
#
# Changes only the DNS fields of the wired connection and re-applies them in
# place (`nmcli device reapply`): the IP address, the link and SSH stay up.
# If name lookups fail afterwards, the previous settings are restored.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
CON="netplan-eth0"
DEV="eth0"
ROUTER="${ROUTER_DNS:-192.168.1.1}"
OWNER="${SUDO_USER:-arpit}"
BK="$(getent passwd "$OWNER" | cut -d: -f6)/aura-backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BK"

before="$(nmcli -g ipv4.ignore-auto-dns,ipv4.dns,ipv6.ignore-auto-dns,ipv6.dns con show "$CON" | paste -sd'|')"
nmcli con show "$CON" > "$BK/nm-$CON-$STAMP.txt"
cp -a /etc/netplan "$BK/netplan-$STAMP"
cp -a /etc/resolv.conf "$BK/resolv.conf-$STAMP"
chown -R "$OWNER:$OWNER" "$BK"; chmod -R go-rwx "$BK"
echo "backed up to $BK (nm-$CON-$STAMP.txt, netplan-$STAMP, resolv.conf-$STAMP)"

restore() {
  IFS='|' read -r i4 d4 i6 d6 <<<"$before"
  nmcli con mod "$CON" ipv4.ignore-auto-dns "${i4:-no}" ipv4.dns "${d4}" ipv6.ignore-auto-dns "${i6:-no}" ipv6.dns "${d6}"
  nmcli device reapply "$DEV"
}

if [ "${1:-}" = "--undo" ]; then
  nmcli con mod "$CON" ipv4.ignore-auto-dns no ipv4.dns "" ipv6.ignore-auto-dns no ipv6.dns ""
else
  nmcli con mod "$CON" ipv4.ignore-auto-dns yes ipv4.dns "$ROUTER" ipv6.ignore-auto-dns yes ipv6.dns ""
fi
nmcli device reapply "$DEV"
sleep 2

ok=0
for _ in 1 2 3; do getent hosts deb.debian.org >/dev/null && { ok=1; break; }; sleep 2; done
if [ "$ok" != 1 ]; then
  echo "Name lookups failed after the change - restoring the previous settings." >&2
  restore
  exit 1
fi
echo
echo "eth0 address:  $(ip -4 -o addr show dev "$DEV" | awk '{print $4}')"
echo "Pi uses DNS:   $(grep '^nameserver' /etc/resolv.conf | awk '{print $2}' | paste -sd' ')"
echo "DHCP offers:   $(nmcli -t -g DHCP4.OPTION dev show "$DEV" | tr '|' '\n' | grep domain_name_servers | cut -d= -f2 | xargs)"
echo "lookup test:   OK"
