/* AURA illustrations: weather art (layered SVG) and small line icons.
   Pure string builders. Uses SVG presentation attributes only, so the strict
   CSP (no inline styles) is respected. */
"use strict";

window.AuraIcons = (() => {
  let uid = 0;
  const id = (p) => `${p}${++uid}`;

  // ---------- weather art (viewBox 0 0 100 100) ----------

  function sun(cx, cy, r, withRays = true) {
    const g = id("sun"), h = id("halo");
    const rays = withRays
      ? `<g class="wx-sun-rays" stroke="#ffd27a" stroke-width="2.4" stroke-linecap="round" opacity="0.75">${
          Array.from({ length: 8 }, (_, i) => {
            const a = (i * Math.PI) / 4;
            const x1 = cx + Math.cos(a) * (r + 6), y1 = cy + Math.sin(a) * (r + 6);
            const x2 = cx + Math.cos(a) * (r + 12), y2 = cy + Math.sin(a) * (r + 12);
            return `<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}"/>`;
          }).join("")
        }</g>`
      : "";
    return `
      <defs>
        <radialGradient id="${g}" cx="38%" cy="35%" r="70%">
          <stop offset="0" stop-color="#fff6d6"/>
          <stop offset="0.45" stop-color="#ffd16b"/>
          <stop offset="1" stop-color="#ff9442"/>
        </radialGradient>
        <radialGradient id="${h}">
          <stop offset="0" stop-color="#ffb84d" stop-opacity="0.45"/>
          <stop offset="1" stop-color="#ffb84d" stop-opacity="0"/>
        </radialGradient>
      </defs>
      <circle cx="${cx}" cy="${cy}" r="${r * 2.1}" fill="url(#${h})"/>
      ${rays}
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="url(#${g})"/>`;
  }

  function moon(cx, cy, r, stars = true) {
    const g = id("moon"), m = id("mask"), h = id("mhalo");
    const starDots = stars
      ? `<g fill="#e8ecff">
           <circle cx="${cx - r * 1.9}" cy="${cy - r * 0.9}" r="1.1" opacity="0.8"/>
           <circle cx="${cx - r * 1.2}" cy="${cy + r * 1.5}" r="0.8" opacity="0.6"/>
           <circle cx="${cx + r * 1.6}" cy="${cy - r * 1.3}" r="0.9" opacity="0.7"/>
         </g>`
      : "";
    return `
      <defs>
        <linearGradient id="${g}" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="#fbfcff"/>
          <stop offset="1" stop-color="#b9c4e8"/>
        </linearGradient>
        <radialGradient id="${h}">
          <stop offset="0" stop-color="#9fb2ff" stop-opacity="0.35"/>
          <stop offset="1" stop-color="#9fb2ff" stop-opacity="0"/>
        </radialGradient>
        <mask id="${m}">
          <rect width="100" height="100" fill="#fff"/>
          <circle cx="${cx + r * 0.55}" cy="${cy - r * 0.4}" r="${r * 0.9}" fill="#000"/>
        </mask>
      </defs>
      <circle cx="${cx}" cy="${cy}" r="${r * 2}" fill="url(#${h})"/>
      ${starDots}
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="url(#${g})" mask="url(#${m})"/>`;
  }

  const CLOUD_TONES = {
    light: ["#ffffff", "#cfd8e8"],
    dim:   ["#c5cedd", "#7f8ba3"],
    dark:  ["#8a94a8", "#434b5e"],
  };

  // Union of shapes with a userSpace gradient, so there are no seams.
  function cloud(dx, dy, s, tone = "light", cls = "wx-cloud") {
    const g = id("cloud");
    const [top, bottom] = CLOUD_TONES[tone];
    const t = (x) => `${(dx + x * s).toFixed(1)}`;
    const ty = (y) => `${(dy + y * s).toFixed(1)}`;
    return `
      <defs>
        <linearGradient id="${g}" gradientUnits="userSpaceOnUse" x1="0" y1="${ty(30)}" x2="0" y2="${ty(74)}">
          <stop offset="0" stop-color="${top}"/>
          <stop offset="1" stop-color="${bottom}"/>
        </linearGradient>
      </defs>
      <g class="${cls}" fill="url(#${g})">
        <circle cx="${t(36)}" cy="${ty(56)}" r="${(13 * s).toFixed(1)}"/>
        <circle cx="${t(54)}" cy="${ty(47)}" r="${(18 * s).toFixed(1)}"/>
        <circle cx="${t(70)}" cy="${ty(57)}" r="${(11 * s).toFixed(1)}"/>
        <rect x="${t(23)}" y="${ty(55)}" width="${(58 * s).toFixed(1)}" height="${(16 * s).toFixed(1)}" rx="${(8 * s).toFixed(1)}"/>
      </g>`;
  }

  function drops(count, color = "#8cc8ff", xs = null) {
    xs = xs || (count === 3 ? [40, 52, 64] : [34, 44, 54, 64, 74].slice(0, count));
    return `<g stroke="${color}" stroke-width="2.6" stroke-linecap="round">${
      xs.map((x, i) =>
        `<line class="wx-drop wx-d${i}" x1="${x}" y1="67" x2="${x - 2.5}" y2="75"/>`
      ).join("")
    }</g>`;
  }

  function flakes() {
    return `<g fill="#f3f7ff">${
      [[38, 69], [50, 73], [62, 69], [44, 79], [56, 80]].map(([x, y], i) =>
        `<circle class="wx-flake wx-d${i}" cx="${x}" cy="${y}" r="2"/>`).join("")
    }</g>`;
  }

  function bolt() {
    return `<path class="wx-bolt" d="M55 57 L46 72 H53 L48 86 L62 67 H55 L60 57 Z" fill="#ffd45c" stroke="#ffb02e" stroke-width="0.8" stroke-linejoin="round"/>`;
  }

  function fogLines() {
    return `<g class="wx-fog" stroke="#c6cfdd" stroke-width="3" stroke-linecap="round" opacity="0.75">
      <line x1="24" y1="68" x2="70" y2="68"/><line x1="32" y1="76" x2="78" y2="76"/></g>`;
  }

  // `celestial(cx, cy, r)`, when given, draws the Sun or Moon itself (the weather
  // card passes the phase-accurate Sun & Moon art). Only clear and cloudy skies
  // show it; rain, storms, fog and snow keep their own pictures.
  function weather(condition, isDay = true, celestial = null) {
    const sky = (cx, cy, r, rays) => (celestial ? celestial(cx, cy, r)
      : isDay ? sun(cx, cy, r, rays) : moon(cx, cy, r, false));
    let body;
    switch (condition) {
      case "clear":
        body = celestial ? celestial(50, 50, isDay ? 19 : 31) : isDay ? sun(50, 50, 20) : moon(50, 50, 22);
        break;
      case "partly_cloudy":
        body = sky(62, 36, celestial ? 18 : 15, true) + cloud(-6, 6, 0.95, isDay ? "light" : "dim");
        break;
      case "cloudy":
        body = (celestial ? celestial(68, 30, 14) : "")
          + cloud(10, -8, 0.8, "dim", "wx-cloud wx-cloud--back") + cloud(-4, 6, 0.95, isDay ? "light" : "dim");
        break;
      case "fog":
        body = cloud(0, -6, 0.9, "dim") + fogLines();
        break;
      case "drizzle":
        body = cloud(0, -6, 0.95, isDay ? "light" : "dim") + drops(3);
        break;
      case "rain":
        body = cloud(10, -14, 0.75, "dark", "wx-cloud wx-cloud--back") + cloud(0, -6, 0.95, "dim") + drops(5);
        break;
      case "thunderstorm":
        body = cloud(10, -14, 0.75, "dark", "wx-cloud wx-cloud--back") + cloud(0, -6, 0.95, "dark") + bolt() + drops(2, "#7fb2e6", [36, 72]);
        break;
      case "snow":
        body = cloud(0, -6, 0.95, "light") + flakes();
        break;
      default:
        body = cloud(0, 0, 0.95, "dim");
    }
    return `<svg viewBox="0 0 100 100" role="img" aria-hidden="true">${body}</svg>`;
  }

  // ---------- line icons (viewBox 0 0 24 24) ----------

  const P = {
    umbrella: '<path d="M3 12a9 9 0 0 1 18 0H3Z"/><path d="M12 12v6.5a2.5 2.5 0 0 1-5 0"/><path d="M12 3v0"/>',
    water: '<path d="M12 3.5s6 6.4 6 10.5a6 6 0 0 1-12 0c0-4.1 6-10.5 6-10.5Z"/><path d="M9 14.5a3 3 0 0 0 3 3"/>',
    droplet: '<path d="M12 3.5s6 6.4 6 10.5a6 6 0 0 1-12 0c0-4.1 6-10.5 6-10.5Z"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
    wind: '<path d="M3 9h11a3 3 0 1 0-3-3"/><path d="M3 15h15a3 3 0 1 1-3 3"/><path d="M3 12h7"/>',
    jacket: '<path d="M8 3l4 3 4-3 4 3v14h-5v-7h-6v7H4V6l4-3Z"/><path d="M12 6v14"/>',
    coat: '<path d="M8 3l4 3 4-3 4 3v15H4V6l4-3Z"/><path d="M12 6v15M9 13h.01M9 17h.01"/>',
    shirt: '<path d="M8 4 4 6.5l2 3.5 2-1V20h8V9l2 1 2-3.5L16 4c0 1.7-1.8 3-4 3s-4-1.3-4-3Z"/>',
    storm: '<path d="M7 16a5 5 0 1 1 1.2-9.9A6 6 0 0 1 19 9a3.5 3.5 0 0 1-1 7"/><path d="m13 12-3 5h4l-3 5"/>',
    snow: '<path d="M12 3v18M4.2 7.5l15.6 9M4.2 16.5l15.6-9"/>',
    fog: '<path d="M4 9h16M3 13h18M6 17h12"/>',
    check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    cloudOff: '<path d="M7 18a5 5 0 0 1-.9-9.9M10.5 6.2A6 6 0 0 1 19 10a3.5 3.5 0 0 1 1.7 6.5"/><path d="M3 3l18 18"/>',
    alert: '<path d="M12 4 2.5 20h19L12 4Z"/><path d="M12 10v4.5M12 17.5v.01"/>',
    cart: '<path d="M3 4h2.5l2.2 11h10.6L21 7.5H6.4"/><circle cx="9" cy="19.5" r="1.3"/><circle cx="17" cy="19.5" r="1.3"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.5v.01"/>',
    raincoat: '<path d="M9 3.5h6l1 2.5 3 2v12h-4v-6h-6v6H5V8l3-2 1-2.5Z"/><path d="M9 3.5c0 2 1.3 3 3 3s3-1 3-3"/><path d="M12 6.5v7"/>',
    sunglasses: '<path d="M2.5 10h19"/><path d="M4 10l.6 4a2.5 2.5 0 0 0 2.5 2h1.3a2.5 2.5 0 0 0 2.4-1.8L11.5 12h1l.7 2.2a2.5 2.5 0 0 0 2.4 1.8h1.3a2.5 2.5 0 0 0 2.5-2l.6-4"/>',
    air: '<path d="M3 8h10a2.5 2.5 0 1 0-2.5-2.5"/><path d="M3 12h15a3 3 0 1 1-3 3"/><path d="M3 16h6"/><circle cx="19" cy="6" r="0.6"/><circle cx="21" cy="10" r="0.6"/>',
    bike: '<circle cx="6" cy="16" r="3.5"/><circle cx="18" cy="16" r="3.5"/><path d="M6 16l4-7h6l2 7M10 9l-1.5-3H6M16 9l-4 7H6"/>',
    car: '<path d="M4 16v-4l2-5h12l2 5v4"/><path d="M3 16h18v2H3z"/><circle cx="7.5" cy="13" r="1"/><circle cx="16.5" cy="13" r="1"/><path d="M6 18v2M18 18v2"/>',
    sunrise: '<path d="M12 3v5M8.5 6.5 12 3l3.5 3.5"/><path d="M5 17a7 7 0 0 1 14 0"/><path d="M2.5 20.5h19M3 14l1.5.8M21 14l-1.5.8"/>',
    sunset: '<path d="M12 8V3M8.5 4.5 12 8l3.5-3.5"/><path d="M5 17a7 7 0 0 1 14 0"/><path d="M2.5 20.5h19M3 14l1.5.8M21 14l-1.5.8"/>',
    pin: '<path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0C18.5 15.4 12 21 12 21Z"/><circle cx="12" cy="10" r="2.3"/>',
    cake: '<path d="M4 21h16M5 21v-7a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v7"/><path d="M5 16c1.2 1 2.3 1 3.5 0s2.3-1 3.5 0 2.3 1 3.5 0 2.3-1 3.5 0"/><path d="M12 12V9M12 6.5c-.8-.8-.8-1.8 0-3 .8 1.2.8 2.2 0 3Z"/>',
    calendar: '<rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7"/>',
    micOff: '<path d="M3 3l18 18M9 9v2a3 3 0 0 0 5.1 2.1M15 9.3V6a3 3 0 0 0-5.9-.8"/><path d="M5.5 11a6.5 6.5 0 0 0 10.9 4.8M18.4 12.6a6.5 6.5 0 0 0 .1-1.6M12 17.5V21M8.5 21h7"/>',
    stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/>',
    send: '<path d="M4 12l16-8-6 16-2.5-6.5L4 12Z"/>',
    speaker: '<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>',
    retry: '<path d="M4 12a8 8 0 1 0 2.4-5.7L4 8.5"/><path d="M4 4v4.5h4.5"/>',
    shield: '<path d="M12 3 5 6v5.5c0 4.3 2.9 8 7 9.5 4.1-1.5 7-5.2 7-9.5V6l-7-3Z"/><path d="m9 12 2.2 2.2L15.5 10"/>',
    shieldOff: '<path d="M12 3 5 6v5.5c0 4.3 2.9 8 7 9.5 4.1-1.5 7-5.2 7-9.5V6l-7-3Z"/><path d="M12 8v4.5M12 15.6v.1"/>',
    chip: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/><path d="M9.5 3.5v3M14.5 3.5v3M9.5 17.5v3M14.5 17.5v3M3.5 9.5h3M3.5 14.5h3M17.5 9.5h3M17.5 14.5h3"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/>',
    home: '<path d="M4 11.2 12 4.5l8 6.7"/><path d="M6 9.6V19.5h12V9.6"/><path d="M10 19.5v-5h4v5"/>',
    compass: '<circle cx="12" cy="12" r="8.5"/><path d="m15.3 8.7-2.1 4.5-4.5 2.1 2.1-4.5 4.5-2.1Z"/>',
    wifi: '<path d="M3.5 9.5a12 12 0 0 1 17 0M6.5 12.6a7.5 7.5 0 0 1 11 0M9.5 15.6a3 3 0 0 1 5 0"/><circle cx="12" cy="18.6" r="0.9" fill="currentColor"/>',
    cloud: '<path d="M7.5 18.5a4 4 0 0 1-.6-7.96A5.5 5.5 0 0 1 17.6 9.2a4.7 4.7 0 0 1-.6 9.3H7.5Z"/>',
    bowl: '<path d="M3.5 11.5h17a8.5 8.5 0 0 1-17 0Z"/><path d="M8 20.5h8M9 8c0-1.5 1-2 1-3.5M12.5 8c0-1.5 1-2 1-3.5"/>',
    star: '<path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8L3.5 9.7l5.9-.9L12 3.5Z"/>',
    dot: '<circle cx="12" cy="12" r="3.2"/>',
    bulb: '<path d="M9 17.5h6M10 21h4"/><path d="M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2V16.5h5v-.6c0-.8.4-1.5 1-2A6 6 0 0 0 12 3Z"/>',
    plug: '<path d="M9 3v5M15 3v5"/><path d="M6.5 8h11v3a5.5 5.5 0 0 1-11 0V8Z"/><path d="M12 16.5V21"/>',
    fan: '<circle cx="12" cy="12" r="1.6"/><path d="M12 10.4c-1-3.4.2-6.4 2.6-6.4 2.2 0 2.6 3.6-1.3 6.9M13.6 12.4c3.4.9 5.4 3.5 4.2 5.5-1.1 1.9-4.4.4-5.6-4.4M10.6 13c-2.5 2.5-5.7 2.9-6.8.8-1.1-1.9 1.8-4.1 6.6-2.7"/>',
    thermo: '<path d="M10 4a2 2 0 0 1 4 0v9.5a4 4 0 1 1-4 0V4Z"/><path d="M12 9v7.5"/>',
    blinds: '<path d="M4 3.5h16M5 3.5v13h14v-13M5 7.5h14M5 11.5h14M12 16.5V20"/><circle cx="12" cy="20.5" r="0.8"/>',
    lock: '<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5M12 14.5v2.5"/>',
    tv: '<rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8 21h8M12 17v4"/>',
    motion: '<circle cx="13" cy="4.5" r="1.6"/><path d="m9 21 2.5-6.5L14 17v4M7 12l3-4.5 4 1 2.5 3.5M11.5 14.5 10 9"/>',
    door: '<path d="M6 21V4.5A1.5 1.5 0 0 1 7.5 3h9A1.5 1.5 0 0 1 18 4.5V21M3.5 21h17"/><circle cx="15" cy="12.5" r="0.9"/>',
    camera: '<rect x="3" y="7" width="13" height="10" rx="2"/><path d="m16 11 5-3v8l-5-3"/>',
    coins: '<ellipse cx="9" cy="7" rx="5.5" ry="2.5"/><path d="M3.5 7v4c0 1.4 2.5 2.5 5.5 2.5s5.5-1.1 5.5-2.5V7"/><path d="M9.5 16.4c.9 1.2 3.1 2.1 5.5 2.1 3 0 5.5-1.1 5.5-2.5v-4c0-1.4-2.5-2.5-5.5-2.5"/><path d="M20.5 12c0 1.4-2.5 2.5-5.5 2.5"/>',
    crescent: '<path d="M15.5 3.5a8.5 8.5 0 1 0 5 15.4A7 7 0 0 1 15.5 3.5Z"/>',
    sliders: '<path d="M4 6.5h9M17 6.5h3M4 12h3M11 12h9M4 17.5h11M19 17.5h1"/><circle cx="15" cy="6.5" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="17.5" r="2"/>',
    bolt: '<path d="M13 3 5 13.5h6L10 21l8-10.5h-6L13 3Z"/>',
    gauge: '<path d="M4.5 17a8.5 8.5 0 1 1 15 0"/><path d="m12 13.5 3.5-4"/><circle cx="12" cy="13.5" r="1.2" fill="currentColor"/>',
  };

  function icon(name, strokeWidth = 1.7) {
    const d = P[name] || P.info;
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${strokeWidth}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
  }

  return { weather, icon };
})();
