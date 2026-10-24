#!/usr/bin/env bash
# Connect AURA to Home Assistant on this Pi (run once, on the Pi, as the normal user):
#
#   ~/aura/deploy/homeassistant/connect.sh
#
# 1. In Home Assistant (http://<pi>:8123) open your profile → Security →
#    "Long-lived access tokens" → Create token, name it "AURA", copy it.
# 2. Run this script and paste the token when asked (it isn't shown or logged).
#
# The token is checked against Home Assistant first, then stored only in
# ~/aura/.env (mode 600, git-ignored) as AURA_HOMEASSISTANT_TOKEN. A backup of
# .env is kept as .env.<date>.bak. Finally the aura service is restarted.
# Undo: remove the AURA_HOMEASSISTANT_TOKEN line from ~/aura/.env and
# `systemctl --user restart aura` (and delete the token in Home Assistant).
set -euo pipefail

AURA_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="$AURA_DIR/.env"
HA_URL="${AURA_HOMEASSISTANT_URL:-http://127.0.0.1:8123}"

if [[ $EUID -eq 0 ]]; then echo "Run this as your normal user, not with sudo." >&2; exit 1; fi

read -r -s -p "Paste the Home Assistant long-lived access token: " TOKEN
echo
TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
if [[ ! "$TOKEN" =~ ^[A-Za-z0-9._-]{100,400}$ ]]; then
  echo "That doesn't look like a long-lived access token (a long text of letters, digits and dots)." >&2
  exit 1
fi

# Check it against Home Assistant before saving (loopback only; the token isn't put on the command line).
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -K - <<<"header = \"Authorization: Bearer $TOKEN\"
url = \"$HA_URL/api/\"" || true)"
if [[ "$code" != "200" ]]; then
  echo "Home Assistant didn't accept the token (HTTP $code from $HA_URL). Nothing was changed." >&2
  exit 1
fi

touch "$ENV_FILE"
chmod 600 "$ENV_FILE"
backup="$ENV_FILE.$(date +%Y%m%d-%H%M%S).bak"
cp -p "$ENV_FILE" "$backup"
tmp="$(mktemp "$AURA_DIR/.env.XXXXXX")"
chmod 600 "$tmp"
grep -v '^AURA_HOMEASSISTANT_TOKEN=' "$ENV_FILE" > "$tmp" || true
printf 'AURA_HOMEASSISTANT_TOKEN=%s\n' "$TOKEN" >> "$tmp"
mv "$tmp" "$ENV_FILE"
unset TOKEN

echo "Saved. Backup of the previous .env: $backup"
systemctl --user restart aura
echo "AURA restarted. Open the Smart Home tile on the dashboard; it should say Connected within a few seconds."
