#!/bin/sh
# AURA Wi-Fi guard (runs every minute as root, from aura-wifi-guard.timer).
#
# Seen on 14 Nov 2026: twice the Pi stayed up but lost Wi-Fi for hours (16:00-18:50,
# 23:04-00:32) and never reconnected on its own; only a power cycle brought it back.
# This checks the router and, after 3 misses in a row, brings Wi-Fi back step by step:
#   3 misses: Wi-Fi radio off and on again
#   6 misses: restart NetworkManager
#   9 misses: reload the Wi-Fi driver (brcmfmac), then start counting again
# It never reboots the Pi. Every step goes to the system log: journalctl -t aura-wifi-guard

IFACE=wlan0
STATE=/run/aura-wifi-guard.misses

# Connected by Ethernet (a default route not on Wi-Fi): nothing to guard.
ip route show default | grep -v "dev $IFACE" | grep -q "^default" && exit 0

GW=$(ip route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}')
if [ -n "$GW" ] && ping -c 2 -W 3 -I "$IFACE" "$GW" >/dev/null 2>&1; then
    [ -s "$STATE" ] && [ "$(cat "$STATE")" != 0 ] && logger -t aura-wifi-guard "router $GW answers again"
    echo 0 > "$STATE"
    exit 0
fi

MISSES=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$MISSES" > "$STATE"
logger -t aura-wifi-guard "router ${GW:-(no route)} not answering on $IFACE (miss $MISSES)"

iw dev "$IFACE" set power_save off 2>/dev/null   # in case the driver turned it back on

case "$MISSES" in
    3)
        logger -t aura-wifi-guard "restarting the Wi-Fi radio"
        nmcli radio wifi off; sleep 5; nmcli radio wifi on ;;
    6)
        logger -t aura-wifi-guard "restarting NetworkManager"
        systemctl restart NetworkManager ;;
    9)
        logger -t aura-wifi-guard "reloading the Wi-Fi driver (brcmfmac)"
        nmcli radio wifi off
        modprobe -r brcmfmac_wcc 2>/dev/null; modprobe -r brcmfmac && sleep 3 && modprobe brcmfmac
        sleep 5; nmcli radio wifi on
        echo 0 > "$STATE" ;;
esac
exit 0
