# AURA

An ambient smart-home dashboard for a display by the front door. It shows a large
clock and date, your home city, live weather with a 5-day outlook, advice for
heading out, a weekday commute planner, and a shared grocery list, over a slowly
shifting background. The weather panel animates to match the real conditions. The
grocery list can be managed from both of your phones and updates live on the
entrance display.

It also has a push-to-talk voice assistant that answers from this data, and the
Pi runs Pi-hole for network-wide ad blocking. **Day-to-day operation, recovery
and the router settings are in [HANDOVER.md](HANDOVER.md).**

## Project layout

```
aura/
├── app/                     FastAPI backend
│   ├── __main__.py          launcher (python -m app)
│   ├── config.py            settings from env / .env
│   ├── main.py              routes + app factory
│   ├── location.py          home location: IP detection, city search, persistence
│   ├── weather.py           weather models, mock provider, caching service
│   ├── open_meteo.py        Open-Meteo provider (live weather + air quality)
│   ├── recommendations.py   "Before you go" rules (pure functions)
│   ├── commute.py           weekday commute monitor (TomTom), schedule, history
│   ├── groceries.py         shared grocery list (SQLite), validation, conflicts
│   ├── auth.py              household PIN, signed sessions, rate limiting
│   ├── events.py            Server-Sent Events for live updates
│   ├── network.py           the Pi's current LAN address (for the QR code)
│   ├── netinfo.py           network status + Wi-Fi via NetworkManager (fixed nmcli operations)
│   ├── settings_store.py    Settings Center: schemas, validation, overrides in aura.db
│   ├── sysinfo.py           read-only system information
│   ├── storage.py           crash-safe JSON writes
│   └── security.py          home-network-only middleware + security headers
├── static/                  frontend (plain HTML/CSS/JS, no build step)
│   ├── index.html           entrance dashboard
│   ├── groceries.html       phone grocery page (/groceries)
│   ├── settings.html        Settings Center (/settings)
│   └── js/weather-scene.js  animated weather backdrop (CSS, no libraries)
├── data/                    aura.db (groceries + commute history), location.json, weather_cache.json, session.key
├── logs/                    aura.log (rotating, 1 MB × 3)
├── tests/                   pytest suite (no real network access)
│   └── browser/             real-Chromium tests (layout, sync, animations)
├── backups/                 snapshots taken before big changes (git-ignored)
├── requirements.txt         runtime deps
├── requirements-dev.txt     + test deps
└── .env.example             all settings, documented
```

## Install

Everything goes into the existing virtual environment. Nothing is installed system-wide.

```bash
cd /home/arpit/aura
.venv/bin/python -m pip install -r requirements-dev.txt
cp -n .env.example .env       # -n: never overwrites an existing .env
```

## Run (automatic startup)

AURA starts by itself when the Pi powers on. No login and no manual command are
needed.

| Part | How it starts | Runs as |
|---|---|---|
| Backend (`python -m app`) | systemd **user** service `aura.service` (`~/.config/systemd/user/`). Lingering is enabled for `arpit`, so it starts at boot without a login. | `arpit` (not root) |
| Entrance display | XDG autostart entry `~/.config/autostart/aura-kiosk.desktop`, run by the desktop session that LightDM auto-logs in (labwc/Wayland). It runs `deploy/kiosk/aura-kiosk.sh`. | `arpit` |

**Everyday commands** (over SSH or on the Pi):

```bash
systemctl --user status aura          # running? PID, uptime, last log lines
systemctl --user restart aura         # restart (≈0.2 s; open dashboards reconnect by themselves)
systemctl --user stop aura            # stop
systemctl --user start aura           # start
journalctl --user-unit aura -f        # follow the backend log (this boot)
tail -f ~/aura/logs/aura.log          # same log, kept across reboots
tail -f ~/.local/state/aura-kiosk.log # entrance-display launcher log
```

The journal on this Pi is in-memory (volatile), so `journalctl` shows only the
current boot. `logs/aura.log` keeps older entries (it rotates at 1 MB, keeping 3
files). Use `journalctl --user-unit aura`, not `journalctl --user -u aura`. The
latter needs a persistent journal and shows nothing here.

**Service behaviour:**
- **Crashes:** the service restarts 5 s after a crash. After 5 crashes in 5
  minutes it stops trying, so `systemctl --user status aura` shows the error.
- **Clean stops:** a normal stop doesn't trigger a restart.
- **Network:** user services can't wait for the system's network. AURA doesn't
  need to: it serves the dashboard at once and fetches weather and traffic when
  the network is ready.
- **Hardening:** a private umask, `NoNewPrivileges`, and memory limits
  (`MemoryHigh=600M`, `MemoryMax=900M`).

**Install, update or remove** (no `sudo`; existing files are backed up as
`*.bak-<time>`):

```bash
cd ~/aura && deploy/install.sh               # service + kiosk (use --takeover to replace a hand-started AURA)
cd ~/aura && deploy/install.sh --no-kiosk    # service only
cd ~/aura && deploy/uninstall.sh             # remove autostart (keeps all data)
```

**Running by hand** (for debugging) means stopping the service first:
`systemctl --user stop aura && .venv/bin/python -m app`. If the port is already
in use, a second copy exits with a clear message.

### Entrance display (kiosk)

`deploy/kiosk/aura-kiosk.sh`:
- **Waits for a screen:** it starts only once an HDMI (or DSI) screen is
  connected, so the Pi doesn't spend CPU drawing to nothing.
- **Waits for AURA:** it opens nothing until `http://127.0.0.1:8080/api/health`
  answers.
- **Shows the dashboard full-screen:** Chromium in `--kiosk` mode, with no tabs,
  address bar or error dialogs, using its own profile
  (`~/.config/aura-kiosk/chromium`). Swipe-back and pinch-zoom are disabled.
  Because it uses `127.0.0.1`, the Pi's own screen counts as the trusted local
  display.
- **Recovers:**
  - If Chromium closes or crashes, it's relaunched within about 3 s.
  - If the screen is unplugged, Chromium is closed and comes back when it's
    plugged in again.
  - If the backend restarts, the page reconnects by itself.
- **Only one launcher ever runs.** To turn the kiosk off without uninstalling:
  `mkdir -p ~/.config/aura-kiosk && touch ~/.config/aura-kiosk/disabled`.
  Delete that file to turn it back on.

**Screen blanking:** none is configured. The desktop session starts no `swayidle`
(the tool Raspberry Pi OS uses for *Screen Blanking*), so the screen stays on. If
the setting is later enabled (Raspberry Pi Configuration → Display, or
`raspi-config`), the display will sleep after the idle time. Some monitors also
have their own power-saving setting, so check the monitor's menu.

**SSH and remote access stay unchanged.** `sshd` and `rpi-connect` are untouched.

### Testing a power cycle safely

The service and kiosk have been checked in the running system: start, stop,
restart, crash recovery, and the kiosk launching on the desktop session and
loading the dashboard. **A real power-on hasn't been tested yet, and neither has
a physical screen.** To test:

1. Connect the screen to the Pi's HDMI port.
2. Save your work, then restart cleanly: `sudo reboot`. Pulling the power is
   safe-ish (SQLite and AURA's files survive), but a clean reboot is kinder to
   the SD card.
3. Within about a minute, the dashboard should fill the screen with no login
   prompt and no browser bars.
4. From the Mac, check:
   ```bash
   ssh arpit@aura-pi.local 'systemctl --user status aura --no-pager | head -5; tail -3 ~/.local/state/aura-kiosk.log'
   ```
   You should see `active (running)` and a `starting chromium (platform=wayland)`
   line.
5. If the screen stays on the desktop, check the kiosk log. Check that
   `~/.config/autostart/aura-kiosk.desktop` exists, and that LightDM still
   auto-logs in (`autologin-user=arpit` in `/etc/lightdm/lightdm.conf`).

### Network and security notes

- **Binding:** unchanged. AURA listens on all IPv4 addresses on port 8080 and is
  reachable from your home Wi-Fi at http://192.168.1.11:8080. It's not reachable
  from the internet, since there's no port forwarding.
- **Firewall:** none is active on this Pi (`nftables` is installed but unused).
  The protection is AURA's own home-network-only check (see *Security*). Don't
  forward port 8080 on the router.
- **Access:** `AURA_EDIT_ACCESS=lan` means any device on the Wi-Fi can edit
  groceries and settings. The kiosk browser is local and doesn't change this.

## Settings

Open **Settings** with the gear icon in the top-right corner of the dashboard, or go
to `http://<pi>:8080/settings`. **Dashboard** at the top returns you to the
dashboard. Each section has its own **Save**, **Cancel** and **Reset to defaults**.
Nothing changes until you press Save. While a section has unsaved changes, its
buttons stay pinned to the bottom of the screen, and leaving the page asks first.

| Section | What you can change | What it really affects |
|---|---|---|
| **Network & Wi-Fi** | View status: internet, Wi-Fi network and signal, Ethernet, hostname, address. Scan for and join a Wi-Fi network. | NetworkManager on the Pi (see below) |
| **Home & office** | Name and latitude/longitude for each (or search a place to fill them in) | The commute route; also weather, if *Use exact Home coordinates* is on |
| **Weather** | Use the exact Home coordinates or the city under the clock; refresh interval; how far ahead *Before you go* looks. Shows the location source, detection status and last update. | Weather fetching and recommendations, from the next update |
| **Commute** | On/off, days, start and end times, timezone, comparison interval, optional "arrive by" time, request schedule and daily limit. Shows provider and key status, last update, and requests used. | The running scheduler: new slots are fetched at the next tick; the recommendation prefers departures that arrive on time |
| **Display** | 12/24-hour clock, °C/°F, animated weather on/off, motion (follow each device / always minimal). Shows theme, layout and screen-sleep status. | Every open dashboard, within seconds, with no reload |
| **Groceries** | Quick-add suggestions, which tab phones open on, automatic removal of purchased items | Suggestions, the phone page, and an hourly clean-up |
| **System** | Read-only: backend health, version and start time, device, OS, Python, uptime, storage, memory, CPU temperature, how AURA is running | — |

**How settings are stored:**
- **Defaults** come from `.env`. Your home (13.0087307, 77.7381464) and office
  (12.9927608, 77.6995722) are the defaults there.
- **Your changes** are stored separately in `data/aura.db` (table `settings`).
  *Reset to defaults* removes them for that section only.
- **Secrets** (TomTom key, PIN) stay in `.env` and never appear in Settings.

**Validation and logging:**
- Every value is checked on the server: coordinate ranges, time formats, the end
  after the start, real timezone names, allowed intervals, at least one weekday.
  Errors appear next to the field.
- Every change is logged (`grep settings: logs/aura.log`). Coordinates are logged
  only as "changed".

**Who can change settings:**
- **Settings:** the same people who can edit groceries (`AURA_EDIT_ACCESS`; today,
  anyone on your Wi-Fi).
- **Wi-Fi changes are stricter:** only on the **entrance display itself**
  (`127.0.0.1`), or from a phone unlocked with the household PIN if you set one.
  Phones on the Wi-Fi can view the network status but not change it.

### Wi-Fi management

This Pi uses **NetworkManager**. AURA can join a Wi-Fi network for you without
`sudo`, because Raspberry Pi OS lets a user in the `netdev`/`sudo` group with an
active local desktop session (the automatic login) manage networks. If that
session isn't active, Settings says so and the scan button is disabled.

How it works, and what it never does:
- **Fixed operations only:** status, an on-demand scan, and connecting to a network
  from that scan. Nothing typed in the browser is ever run as a command.
- **Confirmation:** joining needs a confirmation. If Wi-Fi is the Pi's *only*
  connection, a second warning appears, because a failed switch could cut remote
  access until the Pi reconnects.
- **The password is never put on a command line, in a log or in a response.**
  It's written to a private, temporary file (mode 600) that `nmcli` reads, and the
  file is deleted immediately. NetworkManager then stores it in its own root-only
  connection file, as for any Wi-Fi network.
- **Failures:** a wrong password, a timeout or a lost network gives a clear message.
  The half-created profile is removed, so the Pi returns to its previous network.
- **Ethernet** (how this Pi is connected today, and how SSH reaches it) is never
  touched.

**To change Wi-Fi another way:**
- **On the Pi's screen:** use the network icon on the Raspberry Pi desktop
  (Alt+F4 leaves the kiosk).
- **Over SSH:**
  ```bash
  nmcli device wifi list
  nmcli --ask device wifi connect "Network name"
  ```
- **Text menu:** `sudo nmtui`.

**If you lose access:** plug in Ethernet (the Pi keeps that link), or use a
keyboard and screen. Saved networks reconnect automatically.

**Not verified on this Pi:** actually joining a network through Settings. The
scan, status and permission checks work on this Pi. Joining needs your Wi-Fi
password, and it changes the Pi's network, so it has only been tested against a
simulated `nmcli`. To try it:
1. Open Settings on the entrance display (only that screen may change Wi-Fi).
2. Tap **Find Wi-Fi networks**, pick yours, enter the password, and confirm.
3. Check with `nmcli -t -f DEVICE,STATE,CONNECTION device`.

## Interactive tiles

Every information tile on the dashboard opens a detail view. Tap or click
anywhere on the tile. The small **→** in its bottom-right corner marks it as
clickable, and with a keyboard you can Tab to that arrow and press Enter. Close with **Back**, Esc, a click outside, or the browser's Back button.
Each view has its own address, so `/#/commute` opens straight to it.

| Tile | Opens |
|---|---|
| Clock and date | Live time with seconds, full date, timezone and UTC offset, the device's time if it differs, sunrise, sunset and daylight. *Events and birthdays: no calendar is connected yet, so it says so.* |
| Weather (also the hourly row, 5-day row and metrics) | Current conditions with the animated scene; feels-like, humidity, wind and gusts, UV (now and today's maximum), rain chance, precipitation, air quality (US AQI, PM2.5), sunrise and sunset; 24-hour table; 7-day forecast; every *Before you go* tip; provider, update time, stale or sample labels |
| Before you go | Every applicable tip with its explanation and priority, the forecast window used, and the data source |
| Commute | Best departure, journey and arrival, traffic and delay, free-flow time, distance; leave-now (live); a table of every departure slot (journey, arrival, traffic, source, when checked); schedule, provider, requests used, last problem; what *Live*, *Forecast*, *Your history* and *No data* mean |
| Groceries | The full grocery page (add, edit, delete, purchase, undo, filters), with a **Dashboard** button to return |

- **Controls inside tiles keep their own action:** the Location button, ticking a
  grocery item, and the QR code don't open the tile.
- **Not built yet:** news and daily-quote tiles. They'd need a data source first.
- **Reduced motion:** the panels respect it.

## Weekday commute

The **Commute** card helps you decide when to leave home for the office.

- **When:** Monday to Friday, 7:00–11:00 AM IST (`Asia/Kolkata`), with departure
  options every 15 minutes (17 slots). Holidays in `AURA_COMMUTE_HOLIDAYS` are
  skipped.
- **What it shows:**
  - the **best time to leave** (shortest journey; ties go to the earlier slot)
  - the journey time, arrival time and traffic level (light / moderate / heavy /
    severe), computed from the provider's delay compared with free-flowing traffic
  - a bar chart comparing every slot, with the best one outlined and past slots dimmed
  - "Leaving now" based on live traffic
  - the provider and the last successful update
- **Where the numbers come from** is always labelled:
  - **Live traffic:** TomTom's current conditions, used for "leave now".
  - **Forecast · TomTom historical traffic patterns:** TomTom's prediction for a
    future departure time.
  - **Typical for Mondays · from N of your past trips:** the median of AURA's own
    stored live checks for that weekday and time. It's shown only when at least 3
    exist, for example before the window opens or when TomTom is unreachable.
  - AURA never invents durations. With no data, it says so.
- **Outside the window** (after 11:00 AM, weekends, holidays) the card says that
  monitoring is off and when the next check is. Earlier results are shown as
  "Earlier today", never as a current recommendation.

**Schedule and requests:**
- **6:45 AM** (15 minutes before the window): a forecast for all 17 slots.
- **During the window:**
  - a live "leave now" check every 15 minutes
  - a refresh of the remaining slots every 30 minutes
- **Outside the window:** nothing is requested.
- **Daily volume:** about 100 requests per weekday, with a hard cap of
  `AURA_COMMUTE_MAX_REQUESTS_PER_DAY` (200).
- **History:** every result is kept in `data/aura.db` (table
  `commute_observations`, 180 days) for later pattern analysis.

**Reliability:**
- **Start-up:** if AURA starts mid-window, it catches up immediately. A restart
  doesn't repeat fetches that are still fresh.
- **Duplicates:** only one monitor ever runs. A lock file stops a second AURA
  process from starting another, and runs never overlap.
- **Rate limits:** AURA backs off for 30 minutes, or for TomTom's `Retry-After`
  time.
- **Timeouts:** it waits 5 minutes before trying again.
- **Rejected API key:** it stops for the day and shows the error.

### Turning on live traffic (needs a free TomTom key)

Traffic-aware routing can't be done without a provider account. TomTom's free plan
fits AURA: 20,000 routing requests a month, **no credit card**, and
departure-time forecasts.

1. Create a free account at https://developer.tomtom.com and copy the API key it
   gives you (*Dashboard → Keys*).
2. On the Pi, put it in `.env` (never in source code):
   ```
   AURA_TOMTOM_API_KEY=your-key-here
   ```
   Home and office coordinates are already set in `.env`
   (`AURA_COMMUTE_HOME`, `AURA_COMMUTE_OFFICE`).
3. Restart AURA (see *Run*).

Without a key, the card says **"Traffic isn't connected yet"** and shows no numbers.

Google Routes needs a billing account, and HERE now asks for a credit card, so
neither is used without your approval.

### Is the commute monitor running?

- **Log:** at startup, `logs/aura.log` says `Commute monitor started: mon,tue,wed,thu,fri
  07:00-11:00 Asia/Kolkata, every 15 min, max 200 requests/day`. If it can't start,
  the log says why. During the window, every check is logged:
  ```bash
  grep -i commute /home/arpit/aura/logs/aura.log | tail
  ```
- **Status:** `curl -s http://192.168.1.11:8080/api/commute` shows `monitor_running`,
  `state` (`prefetch` / `active` / `after_window` / `off_day` / `unconfigured`),
  `requests_today`, `last_success_at` and `last_error`.
- **Dashboard:** the card footer shows "TomTom · updated 8:00 AM" during the window.

## Weather animations

The weather card has a subtle animated backdrop that matches the **reported**
conditions:

| Scene | When |
|---|---|
| Sun with slow rays and glow / moon with stars | clear, by day / by night |
| Sun or moon with drifting clouds | partly cloudy |
| Layered slow clouds | overcast |
| Light or steady rain, darker clouds | drizzle / rain |
| Heavier rain | heavy rain and violent showers |
| Rain with a rare, soft lightning flash | thunderstorm |
| Gently falling flakes | snow |
| Drifting mist bands | fog |
| Neutral, still | unknown code or no weather |

- **How the scene is chosen:** the server picks it from Open-Meteo's own weather
  code (never from temperature). Day or night comes from the real sunrise and sunset
  at your home location.
- **Out-of-date or sample weather:** the scene is shown **still and muted**, next to
  the "showing data from…" or "Sample data" label, so it never looks like live
  conditions.
- **Reduced motion:** if a device asks for it (iOS/macOS *Reduce Motion*, Android
  *Remove animations*), nothing moves.
- **Lightweight:**
  - Animations are CSS only, at most about 60 small elements, all contained in the
    card behind the text.
  - Only the main weather illustration animates; the hourly and 5-day icons are still.
  - The large background glow moves in one-second steps.
  - Measured on this Pi in headless Chromium without GPU, the whole dashboard during
    heavy rain uses about 45% of one CPU core (about 11% of the Pi's four cores).
    A browser using the GPU should use less.

## Shared grocery list

**On the entrance display:** the Groceries card shows what's left to buy, plus a
**QR code**. Scan it with a phone camera to open the grocery page. On the Pi's own
screen you can tap an item to mark it purchased.

**On your phones:** open **http://192.168.1.11:8080/groceries**. Scanning the QR
code takes you to the same place. On iPhone, Safari → Share → *Add to Home Screen*
turns it into an app-like icon.

| Action | How |
|---|---|
| Add an item | Type the name at the bottom and tap **+**. Use **Qty** (−/+) and the category picker if you like. Categories are guessed when left on *Auto*. |
| Quick add | Tap a suggestion chip (*+ Milk*, *+ Eggs*, …). Suggestions learn from what you add most. |
| Mark purchased | Tap the circle next to the item (one tap). **Undo** appears for 5 seconds. |
| Edit | Tap the item name to change its name, quantity, category or notes. |
| Delete | Tap the item, then **Delete**, then confirm. |
| Filter | **To buy** / **Purchased** / **All** tabs at the top. Each phone remembers its choice. |
| Clear purchased | **Purchased** tab → **Clear all**, then confirm. |

**Live sync:** every change appears on the other phone and the entrance display
within about a second, without reloading. This uses Server-Sent Events: each open
page keeps one lightweight connection to the Pi. If the connection drops (Wi-Fi
blip, server restart), the phone shows **Reconnecting**, reconnects by itself and
re-syncs. Pages also re-sync when you return to the browser tab.

**Simultaneous edits** are handled safely:
- **Two people adding the same item:** "Milk" can't be added twice. The second
  person is told it's already on the list.
- **Two people editing the same item:** every item has a version. If one person
  saves first, the other's edit is rejected and the latest values are shown, so
  nothing is silently overwritten.
- **Two people marking the same item purchased:** this is harmless and needs no
  check.

**Storage:** the list lives in SQLite (`data/aura.db`, WAL mode). It survives
restarts and power cuts. A `data/groceries.json` from Phase 1, if present, is
imported once on startup and kept as `groceries.json.migrated`.

### Who can make changes

By default (`AURA_EDIT_ACCESS=lan`) **any phone or computer on your home Wi-Fi can
view and change** the grocery list and location settings, with no PIN. Devices
outside the home network can't connect at all, and other websites can't make
changes on your behalf (Origin check).

If you later want a PIN (for example, if guests use your Wi-Fi), set
`AURA_EDIT_ACCESS=pin` and `AURA_HOUSEHOLD_PIN=` (at least 6 characters) in `.env`,
then restart. In PIN mode:
- **Viewing** stays open to the home network.
- **Changes** need either the Pi's own screen (unless `AURA_TRUST_LOCALHOST=false`)
  or a phone that entered the PIN once. Phones stay signed in for
  `AURA_SESSION_DAYS`.
- **Wrong PINs:** 5 wrong tries from one device, or 20 household-wide, lock sign-in
  for 10 minutes.
- **Changing the PIN** signs every phone out.

## Phone access and the Pi's address

The server listens on all of the Pi's **IPv4** addresses (`AURA_HOST=0.0.0.0`,
port `8080`), so any phone on the **same home Wi-Fi** can reach it. Requests from
outside the home network are refused, and nothing is forwarded from the internet.

**Finding the Pi's current IP address:**
- **On the Pi:** run `hostname -I`. The first address (e.g. `192.168.1.11`) is
  the one to use.
- **On the dashboard:** the QR code always encodes the current address. AURA looks
  it up live and refreshes the code every 10 minutes.
- **In your router's admin page** (usually http://192.168.1.1): look for
  **aura-pi** in the list of connected devices.
- **By name:** `http://aura-pi.local:8080` works on Macs and iPhones. Many Android
  phones don't support `.local` names, so use the IP there.

**The IP can change.** The Pi currently gets its address automatically (DHCP) from
the router, so after a router restart it might get a different one. To make it
stable, do one of the following. Nothing has been changed yet.

1. **Recommended: a DHCP reservation in your router.** Find the Pi's MAC address
   with `cat /sys/class/net/eth0/address`. Then, in the router's admin page (LAN or
   DHCP settings), reserve `192.168.1.11` for that MAC. Nothing on the Pi changes.
2. **Static IP on the Pi.** Use this only if the router can't do reservations, and
   pick an address outside the router's DHCP range:

   ```bash
   sudo nmcli con mod netplan-eth0 ipv4.method manual ipv4.addresses 192.168.1.11/24 \
        ipv4.gateway 192.168.1.1 ipv4.dns 192.168.1.1
   sudo nmcli con up netplan-eth0
   ```

   This will briefly drop your SSH session.

Once the address is stable, you can pin the QR code to it (or to a local DNS name)
with `AURA_PUBLIC_URL=http://192.168.1.11:8080` in `.env`.

## Home location

The city shown under the clock is your **home**, which is wherever the Pi is.
It is never the location of the phone or laptop viewing the dashboard.

**First launch.** If no location has been saved, AURA asks two IP-geolocation
services (ipinfo.io, then ipapi.co as a fallback) where the Pi's internet
connection is. The result is saved to `data/location.json` and shown with an
**APPROXIMATE** badge. If the two services name different cities, the dashboard
says so.

**It never changes on its own.** Once saved, a location is never re-detected
automatically, across page reloads or server restarts. It only changes when you act
in the settings dialog.

**Settings dialog.** Use the **Location** button under the clock, or open
`http://aura-pi.local:8080/#location`. From there you can:

- **Yes, this is home**: confirm the detected city. The badge changes to *Home · confirmed*.
- **Choose your city**: search by name (via Open-Meteo geocoding) and pick from the
  results. The badge changes to *Set manually*.
- **Detect from internet connection**: re-run IP detection. Requests are rate-limited
  to one per minute.

If detection fails, the dashboard says **Location not configured**, offers
**Set location**, and shows no weather. It never falls back to a guessed city.

### Why IP geolocation is only approximate

IP geolocation finds where your internet provider registers or routes your
connection, not where your house is. With mobile-network-style ISPs in particular,
that can be a regional hub hundreds of kilometres away. Your IPv4 and IPv6 addresses
can even resolve to different cities. On this Pi, ipinfo.io says *Bengaluru* and
ipapi.co says *Delhi*. Treat the detected city as a suggestion and confirm or
correct it once.

### Privacy

- **Coordinates:** home coordinates are rounded to 2 decimals (about 1 km) and stored
  only in `data/location.json` on the Pi. The dashboard API never returns them.
  Search results are matched by ID on the server.
- **Outbound requests:** the Pi contacts these services and nothing else:
  - ipinfo.io and ipapi.co: only on first launch or when you press *Detect*
  - geocoding-api.open-meteo.com: only when you search for a city
  - api.open-meteo.com and air-quality-api.open-meteo.com: one request each per
    refresh interval, sending the rounded coordinates
- **Tokens:** the optional `AURA_IPINFO_TOKEN` stays on the server and is never
  sent to the browser.
- **Commute:** the exact home and office coordinates and the TomTom key live only in
  `.env`. They're sent to TomTom for routing and are never in the dashboard, the API
  or the logs.

## Weather

Live weather comes from **[Open-Meteo](https://open-meteo.com)**. It is free for
non-commercial use, which explicitly includes personal home automation, and needs
no API key. Data is licensed CC BY 4.0, and the dashboard shows the attribution.
Its limits (10,000 calls/day) are far above AURA's usage: with the default 15-minute
refresh, that's about 200 calls a day.

The dashboard shows:
- current temperature and feels-like, conditions, humidity, wind, UV and US AQI
- sunrise and sunset
- the next 6 hours, with rain probability
- the next 5 days, with highs, lows and rain probability

It uses the home location's timezone for all times.

### Caching and failures

- **Caching:** data is fetched at most once per `AURA_WEATHER_REFRESH_SECONDS`,
  however many screens are open. It is also cached in `data/weather_cache.json`,
  so a restart doesn't need the network.
- **Failures:** if Open-Meteo fails or times out (`AURA_HTTP_TIMEOUT_SECONDS`),
  the last good data stays on screen. It is labelled *Weather service unavailable ·
  showing data from 6:40 PM*. AURA retries after `AURA_WEATHER_RETRY_SECONDS`.
- **Too old:** data older than `AURA_WEATHER_MAX_STALE_HOURS` is never shown. The
  card shows an error instead.
- **Air quality:** it comes from a separate request. If that request fails, the
  weather still shows.

### Development preview

`AURA_WEATHER_PROVIDER=mock` shows fixed sample data, labelled **DEVELOPMENT PREVIEW
· SAMPLE DATA** in the header and **Sample data · not live weather** on the card.
Sample data is never written to the weather cache.

## Before you go

The advice looks at the **next `AURA_TRAVEL_WINDOW_HOURS` hours** (default 6), not
just the current moment. Rain at 4 PM still means "take an umbrella" at 1 PM.
Each tip says why.

| Advice | Based on |
|---|---|
| Umbrella / raincoat | Rain chance and amount over the window; a raincoat when it's heavy or too windy for an umbrella |
| Compact umbrella, just in case | 30–49% chance of rain |
| Sunscreen and sunglasses / sunglasses | Daytime UV peak in the window (≥ 6 / ≥ 3) |
| Warm coat / jacket / layers / dress light | Lowest and highest feels-like temperature in the window |
| Water bottle | Feels-like ≥ 30°, or warm and humid |
| Thunderstorms / strong winds / snow / low visibility | Weather codes and wind gusts in the window |
| Air quality | US AQI ≥ 101 (take care) or ≥ 151 (poor) |
| Bike or car/transit | Next 3 hours: rain, gusts, heat, cold, air quality |

The bike/car advice is **weather-only** and says so. AURA does not know about traffic.
Up to 4 tips are shown, most important first.

## Calendars

Events, birthdays and holidays come from iCalendar (ICS) feeds — for example Google
Calendar's *Secret address in iCal format* (Settings → your calendar → Integrate
calendar) or a public holiday calendar. Add them to `.env` on the Pi, as
comma-separated `Label|URL` entries, then `systemctl --user restart aura`:

```
AURA_CALENDAR_FEEDS=Personal|https://calendar.google.com/calendar/ical/.../basic.ics,Holidays|https://calendar.google.com/calendar/ical/en.indian%23holiday/public/basic.ics
AURA_CALENDAR_REFRESH_MINUTES=30
AURA_CALENDAR_DAYS_AHEAD=30
```

- **Dashboard:** under the location, today's items (or the next day that has any) —
  at most three rows, with "+N more". Tap it for the full list in *Time & date*.
- **No repeats:** an item with the same title on the same day is shown once, even if
  it is in several calendars or entered twice (its calendars are listed together).
  A multi-day event is shown once, on its first day, with "until …".
- Repeating events (RRULE, including skipped and moved occurrences) are expanded and
  shown in the home time zone. Cancelled events are hidden.
- **Kinds:** items from a holiday calendar are *Public holiday* or *Observance* (from
  Google's description). Other items are *Birthday* when the title contains
  "birthday", "bday" or "anniversary", otherwise *Event*.
- **Privacy:** secret addresses are treated like passwords. They stay in `.env`
  (mode 600, git-ignored); the API, Settings page, logs and the cache
  (`data/calendar_cache.json`) only ever show the label, host and a short fingerprint.
  Anyone with a secret address can read that calendar — if one was shared, use
  *Reset* next to it in Google Calendar and update `.env`.
- **Failures:** if a feed can't be fetched, its last saved copy keeps being shown and
  Settings → Calendars says so. Nothing is invented when no feed is reachable.

## Voice assistant

Press the **microphone** in the top-right corner of the entrance screen (or the
**M** key / the TV remote's **red** button, if the TV passes remote keys to the Pi
over HDMI-CEC) and ask a question. AURA listens until you pause (at most 10 s by
default), shows **Listening** on every open screen while the microphone is on,
then answers on screen and, if the TV is on, out loud.

You can ask about:

| Ask | Answer comes from |
|---|---|
| "What's the weather today?", "Is it hot outside?" | Weather (Open-Meteo) |
| "Will it rain before I leave?", "Do I need an umbrella?" | Hourly forecast up to today's recommended departure (or the travel window) |
| "How is my commute to the office?", "When should I leave?" | Commute monitor (TomTom) |
| "What's on my calendar today / tomorrow?", "Any birthdays this week?" | Calendars |
| "What groceries do I need?" | Shared grocery list |
| "Summarize my day." | All of the above |
| "What time is it?", "What can you do?" | — |

Anything else gets "I can't help with that yet". If data is missing (no TomTom key,
no calendar, weather offline) the answer says so; nothing is made up. Requests to
*change* something ("delete the groceries", "turn off Wi-Fi") are refused: the
assistant is read-only. Phones and laptops can open the same view and **type** a
question. Only the entrance screen can switch the microphone on, unless you turn on
**Settings → Assistant → Start the microphone from phones and laptops** (off by
default): then any device on your home network can start it. It is still the Pi's
USB microphone, so speak near the Pi; starts from other devices are logged with
their address (never the audio or the words).

**How it works** (all on the Pi, no cloud, works offline):

```
dashboard (aura.service :8080)  ──loopback──►  voice worker (aura-voice.service, 127.0.0.1:8091)
  intent → allow-listed tool → AURA data         arecord (USB mic) → faster-whisper base.en → text
                                                 Piper en_US-lessac-medium → WAV → played by the screen's browser
```

- **Speech recognition:** faster-whisper `small.en` (int8, beam 5) for questions,
  about 5.5 s per question on the Pi 5. It was the clear winner in a benchmark on
  distant, noisy speech (no errors at normal distance vs 26% for `base.en`, and
  32% vs 55% far away); the small `base.en` model is kept only for the quick
  "did they say Aura?" wake-word check (about 1.9 s). **Spoken replies:** Piper,
  about 0.2 s per sentence. The worker uses about 1 GB of RAM. Its unit sets `MemoryMax=1200M`, but
  Raspberry Pi OS boots with `cgroup_disable=memory`, so systemd memory limits
  (here and in `aura.service`) are **not enforced** unless you add
  `cgroup_enable=memory` to `/boot/firmware/cmdline.txt` and reboot. The two
  services are still separate processes, so one crashing doesn't stop the other.
- **Understanding** is a fixed list of question types matched by keywords, not a
  language model. New abilities are added as tools in `app/assistant.py`.
- **Models** live in `data/models/` (about 200 MB, git-ignored). Re-download with
  `.venv/bin/python -m app.voice_worker --download`.
- **Sound** plays on whatever screen asked: the TV's speakers through HDMI when
  the TV is on. The USB microphone has no speaker. With the TV off, answers are
  on screen only. Turn spoken replies off in Settings → Assistant.

**Wake word "Hey Aura"** (Settings → Assistant, off by default): the voice service
keeps the Pi's microphone open and listens only for the phrase. Stage 1 is Vosk
keyword spotting (`data/models/vosk/small-en-us`, 68 MB) with a grammar that knows
nothing but "hey aura", so conversation is never transcribed; stage 2 has Whisper
re-check the candidate, which drops lookalikes such as "Hey, are you ready?". Say
"Hey Aura, what's the weather?" in one go, or "Hey Aura", wait for Listening, then
ask. Answers are spoken on the entrance screen (and on any screen with Ask AURA
open). About 3% of one CPU core while listening. Audio stays in a 12-second
in-memory buffer and is never saved or sent; rejected candidates are logged only
as "rejected", never with what was said. Known limits: occasional misses from far
away, and rarely a similar-sounding phrase (e.g. "Hey Laura") may trigger it.

**Privacy:** the microphone records only after a button press, for at most the
configured seconds; the audio stays in the worker's memory, is transcribed and
then dropped, never written to disk or sent anywhere. The question/answer
transcript is kept in memory only (last 20) until you press *Clear* or AURA restarts.

**Settings → Assistant:** turn the assistant off, turn spoken replies off, change
the listening limit, and see whether the speech service, models and microphone
are ready.

```bash
systemctl --user status aura-voice        # running? models loaded?
journalctl --user-unit aura-voice -f      # its log (no audio or transcripts are logged)
systemctl --user restart aura-voice
curl -s http://127.0.0.1:8091/health      # from the Pi itself only
```

If the microphone is unplugged or a different one is used, find its name with
`arecord -L` and set `AURA_VOICE_MIC_DEVICE` in `.env`, then restart `aura-voice`.

## Network protection (Pi-hole)

Pi-hole is a DNS server that answers "0.0.0.0" for known ad and tracker domains.
It runs natively on the Pi (service `pihole-FTL`; admin page on port 80), next
to AURA (port 8080). Scripts are in `deploy/pihole/`:

| Script | sudo | What it does |
|---|---|---|
| `backup.sh` | yes | saves network config to `~/aura-backups/network-<time>.tar.gz` (mode 600) |
| *(official installer)* | yes | `basic-install.sh` from install.pi-hole.net |
| `configure.sh` | yes | admin page pinned to 80/443, local-network-only DNS, 7-day query log, DHCP off |
| `verify.sh` | no | resolving, blocking, the Pi's own DNS, AURA still up, port 8080 untouched |
| `rollback.sh` | yes | stop + disable Pi-hole (`--start` to undo) |

**What does not change:** the router's DHCP and DNS, the Pi's network settings,
and the Pi's own DNS (`/etc/resolv.conf` keeps pointing at the router, so the Pi
can always reach the internet and be repaired even if Pi-hole fails).

**Only devices that use the Pi for DNS are filtered.** A device is protected only
after its DNS (or the router's DHCP DNS setting) points to 192.168.1.11. DNS
blocking cannot remove ads served from the same domain as the content (most
YouTube ads, many app and social-media ads), and devices using encrypted DNS
(DNS-over-HTTPS, iCloud Private Relay, Android "Private DNS") or hard-coded DNS
bypass it.

**AURA Settings → Network protection** shows (read-only): whether `pihole-FTL`
is running, a real test lookup through it (example.com must resolve,
doubleclick.net must be blocked), the last healthy check, and which DNS server
the router hands out, so it never claims devices are protected when they are not.
Query statistics (last 24 h, % blocked, blocklist size) need a Pi-hole *app
password*: in the Pi-hole admin page → Settings → Web interface / API → switch to
Expert → **Configure app password**, copy it once, then on the Pi add to `.env`

```
AURA_PIHOLE_APP_PASSWORD=<the app password>
```

and `systemctl --user restart aura`. It stays on the Pi, is only sent to Pi-hole
over loopback, and never reaches a browser. AURA has no Pi-hole controls;
changes are made in Pi-hole's own admin page.

**If a site or app breaks:** open the admin page → Query Log, filter by the device,
find the blocked domain that the app needs, and allow just that domain
(Domains → Allow). Prefer allowing a single domain over removing a blocklist.

**Emergency:** set the device (or router) DNS back to automatic / 192.168.1.1;
then, if needed, `sudo deploy/pihole/rollback.sh`.

### Turning it on for the whole home (router change)

Do these in order, and only after one test device has worked through Pi-hole.

1. **Pin the Pi's own DNS to the router** so the Pi never depends on itself:
   `sudo deploy/pihole/pin-pi-dns.sh` (keeps the IP and SSH up; reverts itself if
   lookups fail; `--undo` to go back).
2. **Reserve the Pi's address** on the router: LAN / DHCP → static lease /
   address reservation → MAC `88:a2:9e:b1:64:da` → `192.168.1.11`.
3. **Give out the Pi as DNS** on the router: LAN / DHCP server → DNS →
   *Primary DNS* `192.168.1.11`. Leave *Secondary* empty, or put `192.168.1.11`
   again if it can't be empty. **Do not** put a public DNS (8.8.8.8, 1.1.1.1) or
   the router's own address there: devices use the secondary at random and
   would skip filtering. Do **not** change the router's WAN/Internet DNS and do
   **not** turn off its DHCP server.
4. **IPv6:** if devices get an IPv6 DNS server from the router (on a Mac:
   `scutil --dns | grep nameserver`), they can bypass Pi-hole. On the router's
   LAN IPv6 page, turn off DNS in router advertisements / DHCPv6 DNS, or set
   it to "use IPv4 DNS". If the ISP gives no IPv6 internet (the Pi has no
   global IPv6 address today), disabling LAN IPv6 is also safe.
5. **Apply:** devices pick up the new DNS when they reconnect or renew their
   lease (up to 24 h). Toggle Wi-Fi off/on to switch a device immediately.
6. **Check:** AURA Settings → Network protection should say the router hands
   out `192.168.1.11`; Pi-hole's dashboard should list your devices as clients.

**Rollback (if anything misbehaves):** on the router set the LAN DNS back to
automatic / `192.168.1.1` (or the value you wrote down), save, then toggle Wi-Fi
on the affected devices. Pi-hole can stay installed. If the router UI is
unreachable, any device can still browse by setting its own DNS to `192.168.1.1`.
The Pi itself is unaffected throughout (step 1).

**Limitations:** devices with hard-coded or encrypted DNS (some smart TVs,
Chromecast/Android "Private DNS", iCloud Private Relay, browsers set to a DoH
provider) bypass Pi-hole. Guests' devices are filtered too while on your Wi-Fi.
If the Pi is off, devices that follow the router have no DNS until it is back
or the router is rolled back.

## Configuration

All settings use the `AURA_` prefix and go in `.env`. Restart the server after
changing them. The full list with explanations is in `.env.example`.

| Setting | Default | Notes |
|---|---|---|
| `AURA_CLOCK_FORMAT` | `12h` | `12h` or `24h` |
| `AURA_TIMEZONE` | Pi's system timezone | Used only until a home location is set |
| `AURA_LOCATION_DETECTION` | `ip` | `off` = never call IP-geolocation services |
| `AURA_IPINFO_TOKEN` | — | Optional, server-side only |
| `AURA_WEATHER_PROVIDER` | `open_meteo` | `open_meteo`, `mock` or `none` |
| `AURA_WEATHER_REFRESH_SECONDS` | `900` | |
| `AURA_WEATHER_RETRY_SECONDS` | `120` | Back-off after a failed fetch |
| `AURA_WEATHER_MAX_STALE_HOURS` | `6` | |
| `AURA_HTTP_TIMEOUT_SECONDS` | `8` | |
| `AURA_TRAVEL_WINDOW_HOURS` | `6` | |
| `AURA_EDIT_ACCESS` | `lan` | `lan` = any home device can edit; `pin` = PIN required |
| `AURA_HOUSEHOLD_PIN` | — | Used only in `pin` mode; at least 6 characters |
| `AURA_SESSION_DAYS` | `365` | How long a phone stays signed in |
| `AURA_TRUST_LOCALHOST` | `true` | The Pi's own browser can edit without the PIN |
| `AURA_PUBLIC_URL` | — | Fixed address for the QR code; empty = the current LAN IP |
| `AURA_TOMTOM_API_KEY` | — | Free TomTom key; enables live commute traffic |
| `AURA_COMMUTE_HOME` / `AURA_COMMUTE_OFFICE` | — | `lat,lon`; server-side only |
| `AURA_COMMUTE_WINDOW_START` / `_END` | `07:00` / `11:00` | In `AURA_COMMUTE_TIMEZONE` (`Asia/Kolkata`) |
| `AURA_COMMUTE_DAYS` | `mon,tue,wed,thu,fri` | |
| `AURA_COMMUTE_HOLIDAYS` | — | `YYYY-MM-DD,YYYY-MM-DD` |
| `AURA_COMMUTE_MAX_REQUESTS_PER_DAY` | `200` | Hard cap on TomTom requests |
| `AURA_HOST` / `AURA_PORT` | `0.0.0.0` / `8080` | IPv4 only |
| `AURA_ALLOWED_HOSTS` | `localhost,aura-pi,aura-pi.local` | Private IPs are always accepted |
| `AURA_LAN_ONLY` | `true` | |

To reset the home location, stop the server and delete `data/location.json`.

## Tests

```bash
cd /home/arpit/aura
.venv/bin/python -m pytest                       # everything (about 4–5 minutes)
.venv/bin/python -m pytest -m "not browser"      # fast: unit/API tests only (about 10 s)
.venv/bin/python -m pytest tests/browser         # real-browser tests only
```

- **Network:** the unit and API tests never touch the real internet; all HTTP goes
  through `httpx.MockTransport`.
- **Browser tests** (`tests/browser/`):
  - **How they run:** they drive the system Chromium headlessly over the Chrome
    DevTools protocol, using a small standard-library client
    (`tests/browser/cdp.py`). There's no Playwright and nothing is downloaded or
    installed system-wide. They're skipped if Chromium is missing.
  - **Isolation:** each test runs a real AURA server on a free localhost port with
    its own temporary data directory, so your list and settings are never touched.
  - **Screenshots** go to `tests/browser/artifacts/` (git-ignored).

Coverage:
- **Weather and location:**
  - Open-Meteo parsing, timeouts, 5xx and 429 errors, caching, stale fallback,
    and the after-midnight refresh of today's values
  - weather code → animation scene for every code, day/night from sunrise and
    sunset, still scenes for stale or sample data
  - location detection, fallback, persistence and manual override
- **Commute:**
  - the Mon–Fri window and its 6:45/7:00/11:00 boundaries
  - holidays and timezone handling, the 17 slots
  - choosing the best slot (ties, past slots, leave-now)
  - TomTom request parameters and the exact home and office coordinates
  - auth, rate-limit, timeout and bad-response handling, the daily cap,
    missing credentials
  - no fabricated numbers on failure, history-based patterns
  - catching up after a restart, no duplicate jobs, no overlapping runs
- **Groceries:** CRUD, purchase and un-purchase, duplicates, invalid input,
  version conflicts, concurrent writers, persistence, migration, suggestions,
  live-update events, PIN and LAN access modes, the QR code.
- **Browser:**
  - **Dashboard:** clock, date and city; the clock keeps running with the server
    stopped; weather card and advice; all 12 weather scenes kept inside the card;
    reduced motion; stale, sample and unavailable states; the commute card in
    every state.
  - **Groceries, in independent sessions:**
    - add, edit (name, quantity, category, notes), quick add
    - purchase and undo, filters, delete with confirmation, invalid input
    - a simultaneous-edit conflict
    - live sync between two "phones" and the display
    - persistence after a backend restart, PIN mode
  - **Layout** at 320×568, 375×812, 390×844, 430×932, 768×1024, 1024×768,
    1280×800, 1440×900 and 1920×1080, for both pages:
    - no horizontal scroll, no overlapping or clipped cards or text
    - touch targets of at least 40 px, dialogs that fit the screen
    - at 1920×1080, the entrance display fits without scrolling
- **Startup:** the server starts and stops cleanly (within about a second, with
  live streams open), data files are private, and a second instance exits with a
  clear message.

## Security (home network only)

- **No public exposure:** there is no port forwarding, tunnel or cloud service. Don't
  forward port 8080 on your router.
- **IPv4 only:** the Pi's global IPv6 address is not served.
- **Allow-lists:** requests from non-private addresses get `403`, and unknown Host
  headers get `400` (which blocks DNS rebinding).
- **Strict headers:** the pages are served with a strict CSP (`'self'` only),
  `X-Frame-Options: DENY`, `nosniff` and `no-referrer`. API docs are turned off.
- **Anyone on your home Wi-Fi can view and edit** (`AURA_EDIT_ACCESS=lan`). Switch
  to PIN mode if that ever stops being acceptable; see *Who can make changes*.
- **In PIN mode the PIN travels over plain HTTP** inside your home network. Wi-Fi
  encryption (WPA2/WPA3) protects it over the air, but don't reuse an important PIN.
- **For the tightest control,** set `AURA_HOST=127.0.0.1` (the QR code then turns
  off) and use an SSH tunnel from the Mac:

  ```bash
  ssh -N -L 8080:127.0.0.1:8080 arpit@aura-pi.local   # then open http://localhost:8080
  ```
