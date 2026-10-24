/* Sun & Moon drawings for the weather card (inline SVG, no data invented).
   - moon(): the disc is lit from the side the server reports (`lit_side`).
     The lit limb is a semicircle; the terminator is half an ellipse whose
     x-radius is r·|cos i| = r·|2k − 1| (i = phase angle, k = lit fraction).
   - sunArc(): the Sun's place on a sunrise→sunset arc from the server's
     `progress` (0 at sunrise, 1 at sunset). */
"use strict";

window.AuraSky = (() => {
  let uid = 0;
  const R = 46, C = 50;
  // Subtle maria/craters: [x, y, r, opacity]
  const CRATERS = [
    [36, 34, 9, 0.10], [58, 28, 6, 0.08], [64, 52, 11, 0.09], [40, 62, 7, 0.08],
    [52, 76, 5, 0.07], [28, 50, 4, 0.07], [72, 70, 4, 0.06], [46, 46, 3, 0.06],
  ];

  /** The SVG path of the lit part of the disc, or "" (new) / "full". */
  function litPath(k, side) {
    if (!(k > 0.002)) return "";
    if (k >= 0.998) return "full";
    const rx = (R * Math.abs(2 * k - 1)).toFixed(3);
    const top = `${C} ${C - R}`, bottom = `${C} ${C + R}`;
    const gibbous = k > 0.5;
    if (side === "left") {
      return `M${top} A${R} ${R} 0 0 0 ${bottom} A${rx} ${R} 0 0 ${gibbous ? 0 : 1} ${top} Z`;
    }
    return `M${top} A${R} ${R} 0 0 1 ${bottom} A${rx} ${R} 0 0 ${gibbous ? 1 : 0} ${top} Z`;
  }

  function moon(m, { label = true } = {}) {
    const id = `mn${++uid}`;
    const k = Math.max(0, Math.min(1, Number(m.illumination) || 0));
    const path = litPath(k, m.lit_side);
    const lit = path === "full"
      ? `<circle cx="${C}" cy="${C}" r="${R}"/>`
      : path ? `<path d="${path}"/>` : "";
    const craters = (fill, scale) => CRATERS.map(([x, y, r, o]) =>
      `<circle cx="${x}" cy="${y}" r="${r}" fill="${fill}" fill-opacity="${(o * scale).toFixed(3)}"/>`).join("");
    const aria = label ? `role="img" aria-label="${m.phase_name}, ${m.illumination_pct}% lit"` : 'aria-hidden="true"';
    return `<svg class="moon" viewBox="0 0 100 100" ${aria} data-lit-side="${m.lit_side}" data-illumination="${k}">
      <defs>
        <radialGradient id="${id}-lit" cx="${m.lit_side === "left" ? 38 : 62}%" cy="36%" r="70%">
          <stop offset="0" stop-color="#fffaf0"/><stop offset="0.55" stop-color="#ebe4d2"/><stop offset="1" stop-color="#bdb5a2"/>
        </radialGradient>
        <radialGradient id="${id}-dark" cx="50%" cy="40%" r="65%">
          <stop offset="0" stop-color="#2a2f3c"/><stop offset="1" stop-color="#161921"/>
        </radialGradient>
        <radialGradient id="${id}-halo"><stop offset="0.82" stop-color="#fff6dc" stop-opacity="0.22"/><stop offset="1" stop-color="#fff6dc" stop-opacity="0"/></radialGradient>
        <clipPath id="${id}-clip">${lit}</clipPath>
      </defs>
      ${k > 0.05 ? `<circle cx="${C}" cy="${C}" r="${R + 4}" fill="url(#${id}-halo)" opacity="${(0.3 + 0.7 * k).toFixed(2)}"/>` : ""}
      <circle cx="${C}" cy="${C}" r="${R}" fill="url(#${id}-dark)"/>
      <g>${craters("#ffffff", 0.25)}</g>
      ${lit ? `<g clip-path="url(#${id}-clip)"><circle class="moon__lit" cx="${C}" cy="${C}" r="${R}" fill="url(#${id}-lit)"/>${craters("#7d735e", 1)}</g>` : ""}
      <circle cx="${C}" cy="${C}" r="${R}" fill="none" stroke="#ffffff" stroke-opacity="0.10" stroke-width="0.8"/>
    </svg>`;
  }

  function sunArc(sun) {
    const id = `sn${++uid}`;
    const p = Math.max(0, Math.min(1, Number(sun.progress) || 0));
    const x0 = 14, x1 = 186, base = 78, rx = 86, ry = 62;
    const x = 100 - rx * Math.cos(Math.PI * p), y = base - ry * Math.sin(Math.PI * p);
    const day = sun.status === "day";
    const done = p > 0 ? `<path d="M${x0} ${base} A${rx} ${ry} 0 0 1 ${x.toFixed(2)} ${y.toFixed(2)}" fill="none" stroke="url(#${id}-trail)" stroke-width="2.2" stroke-linecap="round"/>` : "";
    return `<svg class="sunarc" viewBox="0 0 200 92" role="img" aria-label="Sun position" data-status="${sun.status}" data-progress="${p.toFixed(4)}">
      <defs>
        <radialGradient id="${id}-glow"><stop offset="0" stop-color="#ffe7a3" stop-opacity="0.9"/><stop offset="0.35" stop-color="#ffcf7a" stop-opacity="0.45"/><stop offset="1" stop-color="#ffb35c" stop-opacity="0"/></radialGradient>
        <radialGradient id="${id}-core" cx="40%" cy="38%"><stop offset="0" stop-color="#fffbe8"/><stop offset="0.6" stop-color="#ffd978"/><stop offset="1" stop-color="#ffb347"/></radialGradient>
        <linearGradient id="${id}-trail" x1="0" x2="1"><stop offset="0" stop-color="#ffb35c" stop-opacity="0.35"/><stop offset="1" stop-color="#ffcf7a" stop-opacity="0.95"/></linearGradient>
      </defs>
      <line x1="4" y1="${base}" x2="196" y2="${base}" stroke="#ffffff" stroke-opacity="0.18" stroke-width="1"/>
      <path d="M${x0} ${base} A${rx} ${ry} 0 0 1 ${x1} ${base}" fill="none" stroke="#ffffff" stroke-opacity="0.22" stroke-width="1.2" stroke-dasharray="2 4"/>
      ${done}
      <g class="sunarc__sun" opacity="${day ? 1 : 0.4}">
        <circle cx="${x.toFixed(2)}" cy="${y.toFixed(2)}" r="${day ? 22 : 14}" fill="url(#${id}-glow)"/>
        <circle cx="${x.toFixed(2)}" cy="${y.toFixed(2)}" r="7" fill="url(#${id}-core)"/>
      </g>
    </svg>`;
  }

  return { moon, sunArc, litPath };
})();
