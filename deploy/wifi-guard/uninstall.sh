#!/bin/sh
# Undo deploy/wifi-guard/install.sh:  sudo sh deploy/wifi-guard/uninstall.sh
set -u
[ "$(id -u)" = 0 ] || { echo "Run with sudo: sudo sh $0"; exit 1; }
systemctl disable --now aura-wifi-guard.timer 2>/dev/null
rm -f /etc/systemd/system/aura-wifi-guard.service /etc/systemd/system/aura-wifi-guard.timer \
      /usr/local/sbin/aura-wifi-guard /run/aura-wifi-guard.misses \
      /etc/NetworkManager/conf.d/50-aura-wifi-powersave.conf \
      /etc/systemd/journald.conf.d/50-aura-persistent.conf
systemctl daemon-reload
nmcli general reload conf 2>/dev/null
systemctl restart systemd-journald
echo "Removed the Wi-Fi guard, the power-save setting and the kept-logs setting."
