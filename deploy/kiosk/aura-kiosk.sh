#!/usr/bin/env bash
# AURA entrance-display kiosk.
#
# Started by the desktop session (XDG autostart: ~/.config/autostart/aura-kiosk.desktop)
# after the automatic login. It:
#   * waits until a screen is connected and AURA answers on /api/health,
#   * opens Chromium full-screen with no browser UI, in its own profile,
#   * relaunches Chromium if it closes or crashes,
#   * closes Chromium if the screen is unplugged (saves CPU on a headless Pi),
#   * never runs twice at once.
#
# Options (environment variables):
#   AURA_KIOSK_URL=http://127.0.0.1:8080/?kiosk=1   page to show (?kiosk=1 hides the pointer)
#   AURA_KIOSK_REQUIRE_DISPLAY=0            launch even with no HDMI screen detected
#   Disable completely: touch ~/.config/aura-kiosk/disabled
#
# Log: ~/.local/state/aura-kiosk.log

set -u

URL="${AURA_KIOSK_URL:-http://127.0.0.1:8080/?kiosk=1}"
HEALTH="${URL%%\?*}"; HEALTH="${HEALTH%/}/api/health"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/aura-kiosk"
PROFILE="$CONF_DIR/chromium"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}"
LOG="$STATE_DIR/aura-kiosk.log"
LOCK="${XDG_RUNTIME_DIR:-/tmp}/aura-kiosk.lock"
REQUIRE_DISPLAY="${AURA_KIOSK_REQUIRE_DISPLAY:-1}"
CHROMIUM="$(command -v chromium || command -v chromium-browser || true)"

mkdir -p "$PROFILE" "$STATE_DIR"
# Keep the log small: start fresh if it grows past ~1 MB.
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 1000000 ]; then : > "$LOG"; fi
exec >>"$LOG" 2>&1

log() { echo "$(date '+%F %T') $*"; }

if [ -e "$CONF_DIR/disabled" ]; then
  log "kiosk disabled ($CONF_DIR/disabled exists); not starting"
  exit 0
fi
if [ -z "$CHROMIUM" ]; then
  log "chromium not found; cannot start kiosk"
  exit 1
fi

# fd 9 holds the single-instance lock. Every child below runs with "9>&-" so a
# leftover sleep/curl/browser can never keep the lock after this script exits.
exec 9>"$LOCK"
if ! flock -n 9; then
  log "another kiosk launcher is already running; exiting"
  exit 0
fi

display_connected() {
  [ "$REQUIRE_DISPLAY" = "0" ] && return 0
  grep -qx connected /sys/class/drm/card*-HDMI-*/status /sys/class/drm/card*-DSI-*/status 2>/dev/null
}

backend_up() { curl -fsS -m 3 -o /dev/null "$HEALTH" 9>&-; }

# Chromium remembers an unclean exit and would show a "Restore pages?" bar.
mark_clean_exit() {
  local prefs="$PROFILE/Default/Preferences"
  [ -f "$prefs" ] && sed -i 's/"exit_type":"[A-Za-z]*"/"exit_type":"Normal"/; s/"exited_cleanly":false/"exited_cleanly":true/' "$prefs"
}

log "kiosk launcher started (url=$URL, require_display=$REQUIRE_DISPLAY)"
while true; do
  until display_connected; do sleep 10 9>&-; done
  waited=0
  until backend_up; do
    [ $((waited % 30)) -eq 0 ] && log "waiting for AURA at $HEALTH"
    sleep 2 9>&-; waited=$((waited + 2))
  done

  # Raspberry Pi OS desktop = labwc (Wayland). Fall back to X11 if that's what we're in.
  if [ -n "${WAYLAND_DISPLAY:-}" ] || [ -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wayland-0" ]; then
    PLATFORM=wayland
    export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
  else
    PLATFORM=x11
  fi

  mark_clean_exit
  log "starting chromium (platform=$PLATFORM)"
  "$CHROMIUM" \
    --kiosk "$URL" \
    --user-data-dir="$PROFILE" \
    --ozone-platform="$PLATFORM" \
    --noerrdialogs \
    --disable-infobars \
    --no-first-run \
    --no-default-browser-check \
    --disable-session-crashed-bubble \
    --disable-features=Translate,TranslateUI,MediaRouter \
    --overscroll-history-navigation=0 \
    --disable-pinch \
    --password-store=basic \
    --disable-background-networking \
    --disable-sync \
    --check-for-update-interval=31536000 9>&- &
  pid=$!

  # Close the browser if the screen is unplugged; the loop restarts it later.
  (
    while kill -0 "$pid" 2>/dev/null; do
      sleep 15
      if ! display_connected; then
        log "display disconnected; closing chromium"
        kill "$pid" 2>/dev/null
      fi
    done
  ) 9>&- &
  watcher=$!

  wait "$pid" 2>/dev/null
  status=$?
  kill "$watcher" 2>/dev/null
  log "chromium exited (status $status); restarting in 3 s"
  sleep 3 9>&-
done
