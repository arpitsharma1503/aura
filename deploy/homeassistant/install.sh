#!/usr/bin/env bash
# Install (or update) Home Assistant Core for AURA's Smart Home section, as the normal user:
#
#   ~/aura/deploy/homeassistant/install.sh            # installs the version below
#   HA_VERSION=2026.11.0 ~/aura/deploy/homeassistant/install.sh   # a specific version
#
# What it does (nothing system-wide, no sudo, no Docker, no firewall/router/DNS changes):
#   ~/homeassistant/bootstrap   small venv holding `uv`
#   ~/.local/share/uv/python    a private Python 3.14 (Home Assistant needs >= 3.14.2; the
#                               system Python 3.13 is left alone)
#   ~/homeassistant/venv        Home Assistant and the packages it installs for integrations
#   ~/homeassistant/config      its configuration and data (history kept 3 days)
#   ~/.config/systemd/user/homeassistant.service   started at boot, like aura-voice
#
# Web UI: http://<this Pi>:8123 on the home network only (no port forwarding).
# Remove: systemctl --user disable --now homeassistant && rm -rf ~/homeassistant \
#         ~/.config/systemd/user/homeassistant.service
set -euo pipefail

HA_VERSION="${HA_VERSION:-2026.10.3}"
UV_VERSION="0.13.5"
HERE="$(cd "$(dirname "$0")" && pwd)"
HA="$HOME/homeassistant"

if [[ $EUID -eq 0 ]]; then echo "Run this as your normal user, not with sudo." >&2; exit 1; fi
free_mb=$(df -Pm "$HOME" | awk 'NR==2 {print $4}')
if (( free_mb < 3000 )); then echo "Less than 3 GB free on $HOME; not installing." >&2; exit 1; fi

mkdir -p "$HA/config"
[[ -x "$HA/bootstrap/bin/uv" ]] || { python3 -m venv "$HA/bootstrap" && "$HA/bootstrap/bin/pip" install -q "uv==$UV_VERSION"; }
UV="$HA/bootstrap/bin/uv"
"$UV" python install 3.14
[[ -x "$HA/venv/bin/python" ]] || "$UV" venv --python 3.14 "$HA/venv"
# uv inside the venv too: Home Assistant installs integration packages with `python -m uv`.
VIRTUAL_ENV="$HA/venv" "$UV" pip install "homeassistant==$HA_VERSION" "uv==$UV_VERSION"

if [[ ! -e "$HA/config/configuration.yaml" ]]; then
  cat > "$HA/config/configuration.yaml" <<'EOF'
# Home Assistant for AURA (aura-pi). Installed as a user service:
#   systemctl --user status homeassistant
# Reachable on the home network only (http://<pi>:8123); no port forwarding.

default_config:

# Keep little history on the Pi: less data kept, less SD-card wear.
recorder:
  purge_keep_days: 3
EOF
fi

install -D -m 644 "$HERE/homeassistant.service" "$HOME/.config/systemd/user/homeassistant.service"
systemctl --user daemon-reload
systemctl --user enable homeassistant >/dev/null
systemctl --user restart homeassistant
echo "Home Assistant $HA_VERSION is starting (the first start takes a few minutes)."
echo "Then open http://$(hostname -I | awk '{print $1}'):8123, create your account, and run $HERE/connect.sh"
