#!/usr/bin/env bash
# Install AURA's automatic startup for the current user. No sudo needed.
#
#   deploy/install.sh              install backend service + kiosk autostart
#   deploy/install.sh --no-kiosk   backend service only
#   deploy/install.sh --takeover   also stop a manually started AURA on the port
#
# What it does:
#   1. copies deploy/systemd/aura.service   -> ~/.config/systemd/user/aura.service
#   2. copies deploy/kiosk/aura-kiosk.desktop -> ~/.config/autostart/aura-kiosk.desktop
#   3. backs up any existing copy to <file>.bak-<timestamp> first
#   4. reloads systemd (user), enables the service and starts it
# It never touches system files or other services.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_SRC="$ROOT/deploy/systemd/aura.service"
DESKTOP_SRC="$ROOT/deploy/kiosk/aura-kiosk.desktop"
UNIT_DST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/aura.service"
DESKTOP_DST="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/aura-kiosk.desktop"
PORT="$(grep -E '^AURA_PORT=' "$ROOT/.env" 2>/dev/null | cut -d= -f2 || true)"
PORT="${PORT:-8080}"
KIOSK=1
TAKEOVER=0
for arg in "$@"; do
  case "$arg" in
    --no-kiosk) KIOSK=0 ;;
    --takeover) TAKEOVER=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user, not root." >&2
  exit 1
fi

backup_and_copy() {
  local src="$1" dst="$2"
  mkdir -p "$(dirname "$dst")"
  if [ -e "$dst" ] && ! cmp -s "$src" "$dst"; then
    cp -p "$dst" "$dst.bak-$(date +%Y%m%d-%H%M%S)"
    echo "backed up existing $dst"
  fi
  install -m 0644 "$src" "$dst"
  echo "installed $dst"
}

chmod 0755 "$ROOT/deploy/kiosk/aura-kiosk.sh"
backup_and_copy "$UNIT_SRC" "$UNIT_DST"
if [ "$KIOSK" = 1 ]; then
  backup_and_copy "$DESKTOP_SRC" "$DESKTOP_DST"
fi

systemctl --user daemon-reload
systemd-analyze --user verify "$UNIT_DST"

# A manually started AURA would make the service fail with "port in use".
manual_pid="$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 || true)"
service_pid="$(systemctl --user show -p MainPID --value aura.service 2>/dev/null || echo 0)"
if [ -n "$manual_pid" ] && [ "$manual_pid" != "$service_pid" ]; then
  if [ "$TAKEOVER" = 1 ]; then
    echo "stopping manually started AURA (pid $manual_pid)"
    kill "$manual_pid"
    for _ in $(seq 1 50); do kill -0 "$manual_pid" 2>/dev/null || break; sleep 0.1; done
  else
    echo "Port $PORT is used by pid $manual_pid (a manually started AURA?)." >&2
    echo "Stop it first, or rerun with --takeover." >&2
    exit 1
  fi
fi

systemctl --user enable aura.service
systemctl --user restart aura.service
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo
  echo "NOTE: lingering is off, so AURA would only start after you log in."
  echo "      To start it at power-on without login, run once:  sudo loginctl enable-linger $USER"
fi
echo
systemctl --user --no-pager status aura.service | head -5
