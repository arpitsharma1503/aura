#!/usr/bin/env bash
# Settings applied right after the official Pi-hole installer finishes.
#   sudo deploy/pihole/configure.sh
# - admin page pinned to ports 80/443, so it can never fall back to 8080 (AURA's port)
# - answer DNS only for devices on the local network (not an open resolver)
# - keep the query log for 7 days (enough to diagnose a broken site)
# It does not change the Pi's own DNS, network interfaces, DHCP or the router.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
command -v pihole-FTL >/dev/null || { echo "Pi-hole is not installed yet." >&2; exit 1; }
cp -a /etc/pihole/pihole.toml "/etc/pihole/pihole.toml.bak-$(date +%Y%m%d-%H%M%S)"
pihole-FTL --config webserver.port '80,443s'
pihole-FTL --config dns.listeningMode LOCAL
pihole-FTL --config database.maxDBdays 7
pihole-FTL --config dhcp.active false
systemctl restart pihole-FTL
sleep 2
echo
echo "pihole-FTL: $(systemctl is-active pihole-FTL)"
echo "upstream DNS: $(pihole-FTL --config dns.upstreams)"
echo "listening:    $(pihole-FTL --config dns.listeningMode)"
echo "web ports:    $(pihole-FTL --config webserver.port)"
echo "DHCP server:  $(pihole-FTL --config dhcp.active)   (must stay false: the router keeps doing DHCP)"
