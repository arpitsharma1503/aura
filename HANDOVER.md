# AURA on aura-pi — operations handover

Raspberry Pi 5 (8 GB), Debian 13, Ethernet `eth0` at **192.168.1.11** (reserved on
the Airtel router for MAC `88:a2:9e:b1:64:da`). Administer over SSH:
`ssh arpit@192.168.1.11`. Backup way in if SSH fails: Raspberry Pi Connect
(`rpi-connect` is running), or a keyboard + the TV.

## What runs

| Service | Kind | Port | Starts at boot | What it does |
|---|---|---|---|---|
| `aura` | systemd **user** unit | 8080 (LAN) | yes (linger) | dashboard + API: weather, commute, calendar, groceries, assistant |
| `aura-voice` | systemd **user** unit | 127.0.0.1:8091 only | yes | microphone, speech-to-text (faster-whisper), spoken replies (Piper) |
| `pihole-FTL` | systemd **system** unit | 53 (DNS), 80/443 (admin) | yes | network-wide ad/tracker blocking |
| kiosk | XDG autostart (labwc) | — | when the TV is connected | Chromium full-screen on the TV |

Verified 2026-10-23 01:27: after an unplanned reboot all three services came back
by themselves within 13 s.

Measured use (idle): AURA 76 MB, voice 331 MB (≈520 MB while transcribing),
Pi-hole 15 MB; 6.3 GB RAM free; 19 GB disk free. A spoken question takes about
2 s to recognise on the Pi. Note: Raspberry Pi OS disables the memory cgroup,
so `MemoryMax` in the units is advisory (see README).

## Everyday commands

```bash
# status
systemctl --user status aura aura-voice
systemctl status pihole-FTL
~/aura/deploy/pihole/verify.sh                 # 9 checks: resolve, block, AURA, ports, Pi's own DNS

# logs (no secrets, no audio, no transcripts are logged)
journalctl --user-unit aura -f                 # also ~/aura/logs/aura.log (survives reboots)
journalctl --user-unit aura-voice -f
sudo journalctl -u pihole-FTL -f               # queries: Pi-hole admin page -> Query Log

# restart
systemctl --user restart aura                  # dashboards reload themselves
systemctl --user restart aura-voice
sudo systemctl restart pihole-FTL

# health endpoints (from the Pi)
curl -s http://127.0.0.1:8080/api/health
curl -s http://127.0.0.1:8091/health
curl -s http://127.0.0.1:8080/api/system/dns-filter
```

Web pages: dashboard `http://192.168.1.11:8080`, settings `…:8080/settings`,
groceries `…:8080/groceries`, Pi-hole admin `http://192.168.1.11/admin`
(its own password).

## When something goes wrong

| Symptom | Do this |
|---|---|
| **No internet on phones/laptops** (but Wi-Fi connected) | Pi-hole is down or the Pi is off. Quick fix: `sudo systemctl restart pihole-FTL`. If the Pi is unreachable: router → Network → LAN → clear **Primary DNS** → Save, then toggle Wi-Fi on devices. |
| A site/app is broken | Pi-hole admin → Query Log → find red entries from that device at that moment → allow only that domain. Telemetry like `firebaselogging-pa.googleapis.com` is blocked on purpose and is harmless. |
| Turn ad blocking off for a while | Pi-hole admin → Disable blocking (5 min / 1 h), or `sudo pihole disable 1h`. |
| Remove Pi-hole from the network entirely | Router → LAN → clear Primary DNS → Save; then `sudo ~/aura/deploy/pihole/rollback.sh` (stop) or `sudo pihole uninstall`. |
| Dashboard blank / not loading | `systemctl --user status aura`; `systemctl --user restart aura`; check `~/aura/logs/aura.log`. |
| Microphone button says voice unavailable | `systemctl --user restart aura-voice`; check `curl -s http://127.0.0.1:8091/health` (mic present? models ready?). Different mic: `arecord -L`, set `AURA_VOICE_MIC_DEVICE` in `.env`. |
| Pi's own lookups fail (weather, apt) | The Pi is pinned to the router (`192.168.1.1`), independent of Pi-hole. Check `cat /etc/resolv.conf`; re-pin with `sudo ~/aura/deploy/pihole/pin-pi-dns.sh`. |
| Pi got a new IP | The router reservation (LAN → Static DHCP Entry) should prevent it; re-add `88:a2:9e:b1:64:da → 192.168.1.11`. Until fixed, devices lose DNS: clear Primary DNS on the router. |
| TV shows nothing | Kiosk waits for a connected display; `tail ~/.local/state/aura-kiosk.log`. |

Undo scripts: `deploy/uninstall.sh` (AURA autostart), `deploy/pihole/rollback.sh`,
`deploy/pihole/pin-pi-dns.sh --undo`. Network backups: `~/aura-backups/`.

## Router settings changed (Airtel GPON Home Gateway, 192.168.1.1)

| Page | Setting | Was | Now |
|---|---|---|---|
| Network → LAN | Static DHCP Entry | — | `88:a2:9e:b1:64:da` → `192.168.1.11` |
| Network → LAN | Primary DNS | empty (router itself) | was set to `192.168.1.11`; **the router reverted it to `192.168.1.1` on 23 Oct (02:14)**, so filtering is not network-wide until it is set again (the dashboard tile says "Not network-wide yet") |
| Network → LAN | Secondary DNS | empty | empty (keep it so: a public DNS here bypasses filtering) |
| Network → LAN_IPv6 | (unchanged) | HGWProxy | — no IPv6 internet; MacBook confirmed no IPv6 DNS leak |

DHCP, Wi-Fi, WAN and port forwarding were not touched. Nothing is exposed to the internet.

## Secrets and privacy

- Secrets live only in `~/aura/.env` (mode 600, git-ignored): TomTom key, calendar
  addresses, optional `AURA_PIHOLE_APP_PASSWORD`. Never in the repo, logs or browser.
- Voice: microphone only after a button press on the TV (or on a phone/laptop if
  Settings → Assistant → "Start the microphone from phones and laptops" is on);
  with Settings → Assistant → "Wake word" on, the Pi listens continuously for
  "Hey Aura" (on the Pi only; nothing stored; a "Hey Aura" pill shows on screens); audio stays in memory and
  is dropped after transcription; transcript in memory only (Clear button).
- Pi-hole keeps the query log 7 days (`database.maxDBdays`).

## Not yet verified (needs someone at the Pi / TV)

- Spoken replies through the TV speakers over long use (a real voice through the USB microphone and "Hey Aura" were tried by the household on 23–24 Oct).
- The TV-remote shortcut (HDMI-CEC).
- Pi-hole statistics in AURA Settings (needs the app password in `.env`).

## Tests

```bash
cd ~/aura && .venv/bin/python -m pytest -m "not browser"   # fast, ~20 s
cd ~/aura && .venv/bin/python -m pytest                    # everything incl. headless browser, ~15 min
```
