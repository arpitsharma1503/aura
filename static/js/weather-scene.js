/* AURA weather scene: a lightweight animated backdrop inside the weather card.
   - The scene comes from the server (`visual.scene`), which maps the provider's
     weather code; this file never guesses weather.
   - Only transform/opacity animations (GPU-friendly), small element counts,
     everything clipped to the card. No libraries.
   - motion "static" (sample/stale data) and prefers-reduced-motion render a
     still scene; motion "none" renders a neutral backdrop. */
"use strict";

window.AuraWeatherScene = (() => {
  const COUNTS = { drizzle: 14, rain: 30, "heavy-rain": 54, thunderstorm: 44, snow: 26, stars: 22 };
  const reduceQuery = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

  // Deterministic pseudo-random so a scene looks the same each render (no flicker on refresh).
  function rng(seed) {
    let s = seed >>> 0;
    return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296);
  }

  function el(cls, vars) {
    const d = document.createElement("span");
    d.className = cls;
    if (vars) for (const [k, v] of Object.entries(vars)) d.style.setProperty(k, v);
    return d;
  }

  function particles(layer, cls, n, seed, make) {
    const r = rng(seed);
    for (let i = 0; i < n; i++) layer.append(el(cls, make(r, i)));
  }

  function clouds(layer, n, tone) {
    const r = rng(7);
    for (let i = 0; i < n; i++) {
      layer.append(el(`wxs-cloud wxs-cloud--${tone}`, {
        "--top": `${4 + r() * 38}%`,
        "--size": `${38 + r() * 30}%`,
        "--dur": `${70 + r() * 60}s`,
        "--delay": `${-r() * 120}s`,
        "--alpha": `${0.35 + r() * 0.3}`,
      }));
    }
  }

  function rain(layer, n, heavy) {
    particles(layer, `wxs-drop${heavy ? " wxs-drop--heavy" : ""}`, n, 11, (r) => ({
      "--x": `${r() * 100}%`,
      "--len": `${(heavy ? 26 : 16) + r() * 14}px`,
      "--dur": `${(heavy ? 0.55 : 0.85) + r() * 0.45}s`,
      "--delay": `${-r() * 2}s`,
      "--alpha": `${0.25 + r() * 0.35}`,
    }));
  }

  function build(scene) {
    const frag = document.createDocumentFragment();
    const layer = document.createElement("div");
    layer.className = "wxs-layer";
    frag.append(layer);

    switch (scene) {
      case "clear-day":
        layer.append(el("wxs-sun"), el("wxs-rays"));
        break;
      case "clear-night":
        layer.append(el("wxs-moonglow"));
        particles(layer, "wxs-star", COUNTS.stars, 3, (r) => ({
          "--x": `${r() * 100}%`, "--y": `${r() * 70}%`,
          "--s": `${1 + r() * 1.6}px`, "--dur": `${3 + r() * 4}s`, "--delay": `${-r() * 6}s`,
        }));
        break;
      case "partly-cloudy-day":
        layer.append(el("wxs-sun wxs-sun--soft"));
        clouds(layer, 3, "light");
        break;
      case "partly-cloudy-night":
        layer.append(el("wxs-moonglow"));
        particles(layer, "wxs-star", 10, 5, (r) => ({
          "--x": `${r() * 100}%`, "--y": `${r() * 55}%`,
          "--s": `${1 + r()}px`, "--dur": `${3 + r() * 4}s`, "--delay": `${-r() * 6}s`,
        }));
        clouds(layer, 3, "dim");
        break;
      case "overcast":
        clouds(layer, 5, "dim");
        break;
      case "fog":
        for (let i = 0; i < 4; i++) {
          layer.append(el("wxs-mist", { "--top": `${18 + i * 18}%`, "--dur": `${40 + i * 12}s`, "--delay": `${-i * 9}s` }));
        }
        break;
      case "drizzle":
        clouds(layer, 3, "dim");
        rain(layer, COUNTS.drizzle, false);
        break;
      case "rain":
        clouds(layer, 4, "dark");
        rain(layer, COUNTS.rain, false);
        break;
      case "heavy-rain":
        clouds(layer, 4, "dark");
        rain(layer, COUNTS["heavy-rain"], true);
        break;
      case "thunderstorm":
        clouds(layer, 4, "dark");
        rain(layer, COUNTS.thunderstorm, true);
        layer.append(el("wxs-flash"));
        break;
      case "snow":
        clouds(layer, 3, "light");
        particles(layer, "wxs-flake", COUNTS.snow, 13, (r) => ({
          "--x": `${r() * 100}%`, "--s": `${2 + r() * 3}px`,
          "--dur": `${7 + r() * 6}s`, "--delay": `${-r() * 12}s`, "--sway": `${10 + r() * 24}px`,
        }));
        break;
      default:
        // unknown / unavailable: neutral, still backdrop
        break;
    }
    return frag;
  }

  /** Render a scene into `root`. Re-renders only when something changed. */
  function render(root, visual) {
    const scene = visual?.scene || "unknown";
    const reduced = !!reduceQuery?.matches || document.body.classList.contains("force-reduce");
    const motion = !visual || visual.motion === "none" ? "none" : (reduced ? "reduced" : visual.motion);
    const key = `${scene}|${motion}`;
    if (root.dataset.key === key) return;
    root.dataset.key = key;
    root.dataset.scene = scene;
    root.dataset.motion = motion;            // live | static | reduced | none
    root.replaceChildren(motion === "none" ? document.createDocumentFragment() : build(scene));
  }

  // Follow OS-level changes to the reduced-motion preference live.
  function watch(root, getVisual) {
    reduceQuery?.addEventListener?.("change", () => { root.dataset.key = ""; render(root, getVisual()); });
  }

  return { render, watch };
})();
