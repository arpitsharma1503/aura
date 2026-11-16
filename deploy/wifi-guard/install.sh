#!/bin/sh
# Keep the Pi reachable on Wi-Fi (run once, with sudo):  sudo sh deploy/wifi-guard/install.sh
#   1. Wi-Fi power saving off for every Wi-Fi network (the Pi's Wi-Fi chip drops off with it on)
#   2. the Wi-Fi guard: checks the router every minute and brings Wi-Fi back if it's gone
#   3. system logs kept across reboots (Raspberry Pi OS keeps them in memory only), capped at 200 MB
# Undo: sudo sh deploy/wifi-guard/uninstall.sh
set -eu
[ "$(id -u)" = 0 ] || { echo "Run with sudo: sudo sh $0"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)

install -m 0755 "$HERE/aura-wifi-guard.sh" /usr/local/sbin/aura-wifi-guard

cat > /etc/systemd/system/aura-wifi-guard.service <<'EOF'
[Unit]
Description=AURA Wi-Fi guard (bring Wi-Fi back if the router stops answering)
After=NetworkManager.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/aura-wifi-guard
EOF

cat > /etc/systemd/system/aura-wifi-guard.timer <<'EOF'
[Unit]
Description=Run the AURA Wi-Fi guard every minute

[Timer]
OnBootSec=3min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF

mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/50-aura-wifi-powersave.conf <<'EOF'
# AURA: Wi-Fi power saving off (2 = disable) for every Wi-Fi network
[connection]
wifi.powersave = 2
EOF

mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/50-aura-persistent.conf <<'EOF'
# AURA: keep system logs across reboots (Raspberry Pi OS default is memory only)
[Journal]
Storage=persistent
SystemMaxUse=200M
EOF

iw dev wlan0 set power_save off 2>/dev/null || true         # now, without reconnecting
nmcli general reload conf 2>/dev/null || true               # the setting applies on the next connect
systemctl restart systemd-journald
journalctl --flush || true
systemctl daemon-reload
systemctl enable --now aura-wifi-guard.timer

echo
echo "Done:"
echo "  Wi-Fi power save: $(iw dev wlan0 get power_save 2>/dev/null | sed 's/Power save: //')"
echo "  Wi-Fi guard:      $(systemctl is-active aura-wifi-guard.timer)"
echo "  Logs kept:        $(ls -d /var/log/journal/*/ 2>/dev/null | head -1 || echo pending)"
