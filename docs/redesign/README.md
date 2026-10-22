# AURA Home redesign

Apple Home-inspired overview for the entrance display, built on the existing
backend and data. Screenshots (not committed, kept on the Pi):
`docs/redesign/before/` and `docs/redesign/after/`.

## Structure

| Area | What it shows | Source |
|---|---|---|
| Header | AURA mark, home name, Listening / Offline / Preview pills, mic, Settings | `/api/config`, assistant SSE |
| Overview | greeting, time and date (moderate size), place + Location button | browser clock, `/api/config` |
| Status capsules | weather, top "Before you go" alert, commute departure, today's calendar item, groceries count, ad-blocking state — each only when real data exists; each opens its detail | already-loaded API data |
| Room tabs | Overview + rooms from Settings → Home & rooms (default: Entrance, Kitchen, Network & system) | `home` settings section |
| Tiles | Weather, Commute, Before you go, Groceries (wide) · Calendar, Ask AURA, Network protection, System | existing endpoints |
| Detail views | Time & date, Weather, Before you go, Commute, Calendar, Ask AURA, Network protection, System | `detail.js` |

Rooms group information tiles; they never imply connected hardware. The model
(`HomeSettings` in `app/settings_store.py`: tile order, hidden tiles, rooms of
tile ids) can take real device tiles later by adding ids to `TileId`.

## Design system (`static/css/style.css` `:root`)

- **Surfaces:** `--bg` near-black; `--surface-1/2/3` translucent layers for tile,
  hover, pressed/selected; `--surface-solid` for sheets. Hairlines `--line`.
- **Text:** `--ink` (primary) … `--ink-4` (fine print); contrast ≥ 4.5:1 on tiles for `--ink`–`--ink-3`.
- **Colour:** one accent (`--accent`, warm amber) for focus and primary actions;
  semantic `--ok / --warn / --danger`; eight tile tones (`--tone-*`) used only for
  icon discs, status capsules and the commute/traffic bars — never large fills.
- **Shape & spacing:** `--r-tile`, `--r-sm`, `--r-pill`; `--gap`, `--pad`; `--icon-tile`.
- **Motion:** `--ease`, `--dur-fast` (120 ms, press), `--dur` (220 ms, hover/fade);
  respects Reduce motion. Focus: `--focus` ring on every control.
- **Tile anatomy:** `.tile` › `.tile__head` (tone icon disc, title, aside, arrow) ›
  content (`.tile__value`, `.tile__sub`, or a feature body). States via
  `data-state`: loading / ok / alert / error / off (and listening / processing for the assistant).
- **Entrance layout:** on landscape screens ≥ 1200 px everything is sized from
  `--u = min(1vw, 1.7778vh)`, so 1280×720, 1920×1080 and the 4K TV share one
  layout: 4 columns × 3 rows of tiles that always fit one screen. No zoom or
  transform scaling. Smaller screens: 2 columns (≥ 720 px) or 1, page scrolls.

## Verified

Browser tests at 320×568 … 3840×2160 (13 viewports): no horizontal scroll; on
entrance sizes no vertical scroll, 8 tiles in 3 rows, no tile clips its content.
Tile/detail/keyboard/touch/history behaviour, rooms, Settings editor, honest
states for protection/system/assistant/calendar are covered in
`tests/browser/test_home_browser.py` and the existing suites.
