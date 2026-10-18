#!/usr/bin/env bash
# Remove AURA's automatic startup (service + kiosk autostart). Data is kept.
set -euo pipefail
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/aura.service"
DESKTOP="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/aura-kiosk.desktop"
systemctl --user disable --now aura.service 2>/dev/null || true
pkill -u "$USER" -f 'deploy/kiosk/aura-kiosk.sh' 2>/dev/null || true
pkill -u "$USER" -f 'aura-kiosk/chromium' 2>/dev/null || true
for f in "$UNIT" "$DESKTOP"; do
  if [ -e "$f" ]; then mv "$f" "$f.removed-$(date +%Y%m%d-%H%M%S)"; echo "moved aside $f"; fi
done
systemctl --user daemon-reload
echo "AURA autostart removed. Start it by hand with: cd $(cd "$(dirname "$0")/.." && pwd) && .venv/bin/python -m app"
