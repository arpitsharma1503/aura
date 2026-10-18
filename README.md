# AURA

An ambient smart-home dashboard for a display by the front door. It shows a large
clock and date, your home city, live weather with a 5-day outlook, advice for
heading out, and a shared grocery list, over a slowly shifting background.
The grocery list can be managed from both of your phones and updates live on the
entrance display.

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
│   ├── groceries.py         shared grocery list (SQLite), validation, conflicts
│   ├── auth.py              household PIN, signed sessions, rate limiting
│   ├── events.py            Server-Sent Events for live updates
│   ├── network.py           the Pi's current LAN address (for the QR code)
│   ├── storage.py           crash-safe JSON writes
│   └── security.py          home-network-only middleware + security headers
├── static/                  frontend (plain HTML/CSS/JS, no build step)
│   ├── index.html           entrance dashboard
│   └── groceries.html       phone grocery page (/groceries)
├── data/                    aura.db (groceries), location.json, weather_cache.json, session.key
├── logs/                    aura.log (rotating, 1 MB × 3)
├── tests/                   pytest suite (no real network access)
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

## Run

```bash
cd /home/arpit/aura
.venv/bin/python -m app
```

Then open **http://aura-pi.local:8080** (or **http://192.168.1.11:8080**) from any
device on your home network. Stop the server with `Ctrl+C`. It only runs while that
terminal is open. A system service and kiosk mode are not set up yet.

To keep it running after you close the terminal or SSH session:

```bash
cd /home/arpit/aura && setsid nohup .venv/bin/python -m app > /dev/null 2>&1 < /dev/null &
```

To stop it:

```bash
kill $(ss -ltnp | grep ':8080 ' | grep -o 'pid=[0-9]*' | cut -d= -f2)
```

Logs go to `logs/aura.log`. The background process does not survive a reboot.

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
| Clear purchased | **Purchased** section → **Clear all**, then confirm. |

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
| `AURA_HOST` / `AURA_PORT` | `0.0.0.0` / `8080` | IPv4 only |
| `AURA_ALLOWED_HOSTS` | `localhost,aura-pi,aura-pi.local` | Private IPs are always accepted |
| `AURA_LAN_ONLY` | `true` | |

To reset the home location, stop the server and delete `data/location.json`.

## Tests

```bash
.venv/bin/python -m pytest
```

The suite never touches the real internet: all HTTP goes through
`httpx.MockTransport`. It covers:
- **Location:** detection success, disagreement and fallback, total failure (no
  default city), cooldown, persistence across restarts, no re-detection when a city
  is saved, manual search and choice, confirmation, coordinates never exposed, and the
  token kept server-side.
- **Weather:** Open-Meteo parsing, rounded coordinates, timeouts, 5xx and 429 errors,
  optional air quality, caching, retry back-off, stale fallback, maximum staleness,
  cache across restarts, location changes, and mock data never cached.
- **Recommendations:** the travel window, rain timing, raincoat versus umbrella, UV,
  temperature bands, layers, water, storms, wind, air quality, bike versus car, the
  "no traffic claims" rule, and priority and limit.
- **Groceries:** create, edit, delete, purchase and un-purchase, clear purchased,
  duplicates, invalid input (empty, too long, control characters, bad category, wrong
  types, bad JSON), version conflicts, concurrent adds and edits from several threads,
  persistence across restarts, one-time legacy JSON import, suggestions and revisions.
- **Access control:** view-only without the PIN, PIN sign-in and cookie flags,
  logout, forged cookies, a PIN change signing devices out, sessions surviving a
  restart, lockout, no-PIN mode, the localhost-trust switch, cross-site writes, and
  the PIN never exposed.
- **Live updates and QR:** the event stream (hello, change, keepalive, shutdown), the
  connection limit, publishing on change, the share URL and the QR SVG.
- **API and security:** health, config, LAN-only access and the Host-header check.

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
