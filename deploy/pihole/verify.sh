#!/usr/bin/env bash
# Check Pi-hole on the Pi itself. No sudo needed, changes nothing.
#   deploy/pihole/verify.sh
set -uo pipefail
PI_IP="$(ip -4 -o addr show dev eth0 | awk '{print $4}' | cut -d/ -f1)"
pass=0; fail=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }
info() { printf '  ---- %s\n' "$1"; }
q() { dig +time=3 +tries=1 +short "$@" 2>/dev/null | tail -1; }

echo "Pi-hole checks (Pi at $PI_IP)"
systemctl is-active --quiet pihole-FTL && ok "pihole-FTL service is running" || bad "pihole-FTL service is not running"
ss -lnuH 'sport = :53' | grep -q . && ok "listening for DNS on port 53" || bad "nothing listening on port 53"
command -v dig >/dev/null || { bad "dig missing (sudo apt install bind9-dnsutils)"; exit 1; }

a="$(q example.com @127.0.0.1)"
[[ "$a" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && ok "normal domain resolves via Pi-hole (example.com -> $a)" || bad "example.com did not resolve via Pi-hole ('$a')"
a="$(q example.com @"$PI_IP")"
[[ "$a" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && ok "resolves on the LAN address $PI_IP (what other devices will use)" || bad "no answer on $PI_IP"

# Known ad/tracker test domains; Pi-hole answers 0.0.0.0 (or nothing) for blocked names.
blocked=0; tested=0
for d in doubleclick.net googleadservices.com ads.yahoo.com adservice.google.com; do
  tested=$((tested+1))
  a="$(q "$d" @127.0.0.1)"
  if [ "$a" = "0.0.0.0" ] || [ "$a" = "::" ]; then blocked=$((blocked+1)); info "$d -> blocked ($a)"; else info "$d -> ${a:-no answer} (not on the list)"; fi
done
[ "$blocked" -gt 0 ] && ok "$blocked of $tested ad/tracker test domains are blocked" || bad "none of the ad test domains were blocked (blocklist not loaded?)"
a="$(q doubleclick.net @192.168.1.1)"
info "for comparison, the router answers doubleclick.net -> ${a:-no answer}"

grep -q '^nameserver 192.168.1.1' /etc/resolv.conf && ok "the Pi itself still uses the router for DNS (recovery path intact)" \
  || bad "the Pi's own DNS changed: $(grep '^nameserver' /etc/resolv.conf | paste -sd' ')"
curl -s -m3 -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/health | grep -q 200 && ok "AURA dashboard still answers on :8080" || bad "AURA dashboard not answering on :8080"
code="$(curl -s -m3 -o /dev/null -w '%{http_code}' http://127.0.0.1/admin/)"
[[ "$code" =~ ^(200|301|302|308)$ ]] && ok "Pi-hole admin page answers on port 80 (http://$PI_IP/admin)" || bad "Pi-hole admin page not on port 80 (HTTP $code)"
ss -ltnpH 'sport = :8080' 2>/dev/null | grep -q pihole && bad "pihole-FTL took port 8080!" || ok "port 8080 is still AURA's"
echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
