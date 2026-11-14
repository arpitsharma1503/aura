/* AURA dashboard client. No framework, no build step.
   The clock runs entirely in the browser and never waits on the network. */
"use strict";

(() => {
  const { weather: weatherArt, icon } = window.AuraIcons;
  const Scene = window.AuraWeatherScene;
  const Detail = window.AuraDetail;
  const $ = (id) => document.getElementById(id);

  const REQUEST_TIMEOUT_MS = 12000;
  const CONFIG_POLL_MS = 30_000;
  const DETECTING_POLL_MS = 3_000;
  const WEATHER_POLL_MS = 60_000;   // server caches; this only picks up new data
  const GROCERY_POLL_MS = 60_000;   // fallback when live updates are down
  const BURN_IN_SHIFT_MS = 180_000;

  const state = {
    config: null,
    locationKey: null,
    weather: null,          // last payload with data
    weatherReceivedAt: 0,
    clockTimer: null,
    session: null,          // { can_edit, method, pin_configured }
    weatherPayload: null,   // last /api/weather response of any status (for detail views)
    commute: null,          // last /api/commute response
    calendar: null,         // last /api/calendar response
    groceries: null,        // last /api/groceries response
    protection: null,       // last /api/system/dns-filter response
    system: null,           // last /api/system/info response
    network: null,          // last /api/system/network response
    view: null,             // selected view (tab)
    meals: null,            // last /api/meals/day response (today)
    smarthome: null,        // last /api/smarthome response
    panchang: null,         // last /api/panchang response (today)
    prices: null,           // last /api/prices response
    groceryRevision: null,
    events: null,
    configTimer: null,
  };

  // ---------- networking ----------

  async function fetchJSON(url, options = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
    try {
      const res = await fetch(url, { ...options, signal: ctrl.signal, cache: "no-store" });
      setOnline(true);
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
        const err = new Error(typeof detail === "string" ? detail : `HTTP ${res.status}`);
        err.status = res.status;
        throw err;
      }
      return await res.json();
    } catch (err) {
      if (err.name === "AbortError" || err instanceof TypeError) setOnline(false);
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  function setOnline(online) {
    $("offline-pill").hidden = online;
  }

  // ---------- time formatting ----------

  function tz() { return state.config?.timezone; }
  const twelve = () => state.config?.clock_format !== "24h";

  function zonedParts(date) {
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: tz(), hour: "numeric", minute: "numeric", hourCycle: "h23",
    }).formatToParts(date);
    const get = (t) => Number(parts.find((p) => p.type === t)?.value ?? 0);
    return { hour: get("hour"), minute: get("minute") };
  }

  function fmtClockTime(date) {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: tz(), hour: "numeric", minute: "2-digit", hour12: twelve(),
    }).format(date);
  }

  function fmtHour(iso) {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: tz(), hour: "numeric", hour12: twelve(), ...(twelve() ? {} : { minute: "2-digit" }),
    }).format(new Date(iso));
  }

  // Temperatures arrive in Celsius; Settings → Display can show Fahrenheit.
  const fmtTemp = (c) => `${Math.round(state.config?.display?.temperature_unit === "fahrenheit" ? c * 9 / 5 + 32 : c)}°`;

  // ---------- clock & ambient phase ----------

  function phaseFor(now) {
    const sr = state.weather?.data?.sunrise && new Date(state.weather.data.sunrise);
    const ss = state.weather?.data?.sunset && new Date(state.weather.data.sunset);
    if (sr && ss && sr.toDateString() === ss.toDateString()) {
      // Use real sunrise/sunset when we have them (same-day values).
      const t = now.getTime(), H = 3_600_000;
      const srT = sr.getTime(), ssT = ss.getTime();
      const dayOffset = Math.round((t - srT) / (24 * H)) * 24 * H; // shift to today
      const a = srT + dayOffset, b = ssT + dayOffset;
      if (t >= a - H && t < a + H) return "dawn";
      if (t >= a + H && t < b - H) return "day";
      if (t >= b - H && t < b + 0.75 * H) return "dusk";
      return "night";
    }
    const { hour, minute } = zonedParts(now);
    const h = hour + minute / 60;
    if (h >= 5 && h < 7) return "dawn";
    if (h >= 7 && h < 17) return "day";
    if (h >= 17 && h < 19.5) return "dusk";
    return "night";
  }

  function renderClock() {
    const now = new Date();
    let parts;
    try {
      parts = new Intl.DateTimeFormat("en-US", {
        timeZone: tz(), hour: twelve() ? "numeric" : "2-digit", minute: "2-digit", hour12: twelve(),
      }).formatToParts(now);
    } catch {
      parts = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit" }).formatToParts(now);
    }
    const hm = parts.filter((p) => ["hour", "minute", "literal"].includes(p.type) && p.value.trim() !== "")
      .map((p) => p.value).join("");
    $("clock-hm").textContent = hm;
    const { hour: ah, minute: am } = zonedParts(now), as = now.getSeconds();   // the sidebar's analog clock
    const turn = { h: (ah % 12) * 30 + am * 0.5, m: am * 6 + as * 0.1, s: as * 6 };
    for (const [k, deg] of Object.entries(turn)) {
      for (const hand of document.querySelectorAll(`.aclock__${k}`)) hand.setAttribute("transform", `rotate(${deg} 50 50)`);
    }
    $("clock-ampm").textContent = parts.find((p) => p.type === "dayPeriod")?.value ?? "";
    $("date").textContent = new Intl.DateTimeFormat("en-GB", {
      timeZone: tz(), weekday: "long", day: "numeric", month: "long",
    }).format(now);
    $("ctile-time").textContent = `${hm}${$("clock-ampm").textContent ? ` ${$("clock-ampm").textContent}` : ""}`;
    $("ctile-date").textContent = $("date").textContent;
    $("top-time").textContent = $("ctile-time").textContent;          // the header clock on the other views
    document.body.dataset.phase = phaseFor(now);
    const { hour } = zonedParts(now);
    const greeting = hour < 5 ? "Good night" : hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : hour < 22 ? "Good evening" : "Good night";
    if ($("greeting").textContent !== greeting) $("greeting").textContent = greeting;
    // The meal plan (and "Today" in the calendar) roll over at local midnight, no reload needed.
    const today = new Intl.DateTimeFormat("en-CA", { timeZone: tz() }).format(now);
    if (state.meals && state.meals.today !== today && !state.mealsRolling) {
      state.mealsRolling = true;
      Promise.all([loadMeals(), loadCalendar()]).finally(() => { state.mealsRolling = false; });
    }
  }

  function startClock() {
    clearTimeout(state.clockTimer);
    const tick = () => {
      try { renderClock(); } catch { /* never let the clock die */ }
      state.clockTimer = setTimeout(tick, 1000 - (Date.now() % 1000) + 5);
    };
    tick();
  }

  // ---------- location ----------

  const BADGES = {
    Approximate: ["Approximate", "badge--approx"],
    Confirmed: ["Home · confirmed", "badge--confirmed"],
    "Set manually": ["Set manually", "badge--manual"],
  };

  function renderLocation(loc) {
    const place = $("place");
    const badge = $("location-badge");
    const note = $("location-note");
    place.dataset.status = loc.status;
    $("region").textContent = "";
    badge.hidden = true;
    note.hidden = true;

    if (loc.status === "configured") {
      $("city").textContent = loc.city;
      $("region").textContent = [loc.region, loc.country].filter(Boolean).join(", ");
      const [text, cls] = BADGES[loc.label] || [loc.label, ""];
      badge.textContent = text;
      badge.className = `badge ${cls}`;
      badge.title = loc.source === "ip"
        ? `Detected from the home internet connection via ${loc.provider}. IP location is approximate.`
        : "Chosen manually";
      badge.hidden = false;
      $("location-btn-label").textContent = "Location";
      if (loc.note) { note.textContent = loc.note; note.hidden = false; }
    } else if (loc.status === "detecting") {
      $("city").textContent = "Detecting location…";
      $("location-btn-label").textContent = "Set location";
    } else {
      $("city").textContent = "Location not configured";
      $("location-btn-label").textContent = "Set location";
      if (loc.detection_error) {
        note.textContent = `Automatic detection failed: ${loc.detection_error}.`;
        note.hidden = false;
      }
    }
    renderDialogCurrent(loc);
  }

  // ---------- location dialog ----------

  let searchTimer = null;
  let searchSeq = 0;

  function setDialogStatus(text, isError = false) {
    const el = $("loc-status");
    el.textContent = text || "";
    el.classList.toggle("is-error", isError);
  }

  function renderDialogCurrent(loc) {
    const confirm = $("loc-confirm");
    const warn = $("loc-note");
    warn.hidden = true;
    confirm.hidden = true;
    if (loc.status === "configured") {
      $("loc-current").textContent = [loc.city, loc.region, loc.country].filter(Boolean).join(", ");
      const how = loc.source === "ip"
        ? `Approximate · detected via ${loc.provider}${loc.confirmed ? " · confirmed by you" : ""}`
        : "Chosen manually";
      $("loc-meta").textContent = `${how}${loc.timezone ? ` · ${loc.timezone}` : ""}`;
      if (loc.note) { warn.textContent = loc.note; warn.hidden = false; }
      confirm.hidden = !(loc.source === "ip" && !loc.confirmed);
    } else {
      $("loc-current").textContent = loc.status === "detecting" ? "Detecting…" : "Not configured";
      $("loc-meta").textContent = loc.detection_error ? `Detection failed: ${loc.detection_error}` : "";
    }
  }

  function openDialog() {
    const dlg = $("location-dialog");
    setDialogStatus("");
    $("loc-results").replaceChildren();
    $("loc-search").value = "";
    if (!dlg.open) dlg.showModal();
    const s = state.session;
    if (s && !s.can_edit) {
      if (s.pin_configured) $("loc-pin").focus();
      else setDialogStatus("Only the entrance display can change this until a household PIN is set on the Pi.");
    } else {
      $("loc-search").focus();
    }
  }

  async function locationAction(button, url, method, body, okText) {
    button.disabled = true;
    setDialogStatus("Working…");
    try {
      const loc = await fetchJSON(url, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      setDialogStatus(okText);
      await onLocationChanged(loc);
      return loc;
    } catch (err) {
      if (err.status === 401) await loadSession();
      setDialogStatus(err.message || "Something went wrong", true);
      return null;
    } finally {
      button.disabled = false;
    }
  }

  async function onLocationChanged(loc) {
    if (state.config) state.config.location = loc;
    renderLocation(loc);
    await loadConfig();      // timezone may have changed
    loadWeather();
  }

  function renderResults(results) {
    const list = $("loc-results");
    if (!results.length) {
      setDialogStatus("No cities found. Try a different spelling.");
      list.replaceChildren();
      return;
    }
    setDialogStatus("");
    list.replaceChildren(...results.map((r) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      const name = document.createElement("span");
      name.className = "results__name";
      name.textContent = r.name;
      const sub = document.createElement("span");
      sub.className = "results__sub";
      sub.textContent = [r.district, r.region, r.country].filter(Boolean).join(", ");
      btn.append(name, sub);
      btn.addEventListener("click", async () => {
        const loc = await locationAction(btn, "/api/location", "PUT", { id: r.id }, `Saved ${r.name} as home.`);
        if (loc) {
          list.replaceChildren();
          $("loc-search").value = "";
          setTimeout(() => $("location-dialog").close(), 700);
        }
      });
      li.append(btn);
      return li;
    }));
  }

  function onSearchInput() {
    clearTimeout(searchTimer);
    const q = $("loc-search").value.trim();
    if (q.length < 2) { $("loc-results").replaceChildren(); setDialogStatus(""); return; }
    searchTimer = setTimeout(async () => {
      const seq = ++searchSeq;
      setDialogStatus("Searching…");
      try {
        const results = await fetchJSON(`/api/location/search?q=${encodeURIComponent(q)}`);
        if (seq === searchSeq) renderResults(results);
      } catch (err) {
        if (seq === searchSeq) setDialogStatus(err.message || "Search failed", true);
      }
    }, 350);
  }

  function wireDialog() {
    $("location-btn").addEventListener("click", openDialog);
    $("loc-close").addEventListener("click", () => $("location-dialog").close());
    $("location-dialog").addEventListener("click", (e) => {
      if (e.target === e.currentTarget) e.currentTarget.close();  // backdrop click
    });
    $("loc-search").addEventListener("input", onSearchInput);
    $("loc-unlock").addEventListener("submit", async (e) => {
      e.preventDefault();
      const pin = $("loc-pin").value.trim();
      if (!pin) return;
      try {
        await fetchJSON("/api/session", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pin }),
        });
        $("loc-pin").value = "";
        setDialogStatus("Unlocked. This device can now make changes.");
        await loadSession();
        loadGroceries();
      } catch (err) {
        setDialogStatus(err.message || "Wrong PIN", true);
      }
    });
    $("loc-confirm").addEventListener("click", (e) =>
      locationAction(e.currentTarget, "/api/location/confirm", "POST", null, "Confirmed as home."));
    $("loc-detect").addEventListener("click", (e) =>
      locationAction(e.currentTarget, "/api/location/detect", "POST", null, "Location detected. Check it's right."));
    // http://aura-pi.local:8080/#location opens settings directly.
    const openFromHash = () => { if (location.hash === "#location") openDialog(); };
    window.addEventListener("hashchange", openFromHash);
    openFromHash();
  }

  // ---------- weather ----------

  function skyFor(condition) {
    if (condition === "thunderstorm") return "storm";
    if (["rain", "drizzle", "snow"].includes(condition)) return "wet";
    if (["cloudy", "fog"].includes(condition)) return "cloudy";
    return "clear";
  }

  function setWeatherState(name) { $("weather").dataset.state = name; }

  // Compact moon in the tile header; the full Sun & Moon section is in the detail view.
  function renderMoon(payload) {
    const el = $("weather-moon");
    const m = payload?.astronomy?.moon;
    el.hidden = !m;
    if (!m) { el.replaceChildren(); return; }
    el.innerHTML = window.AuraSky.moon(m, { label: false });
    const name = document.createElement("span");
    name.className = "weather__moon-name";
    name.textContent = m.phase_name;
    const pct = document.createElement("span");
    pct.className = "weather__moon-pct";
    pct.textContent = `${m.illumination_pct}%`;
    el.append(name, pct);
    el.title = `${m.phase_name} · ${m.illumination_pct}% lit · ${m.age_days.toFixed(1)} days old`;
    el.setAttribute("aria-label", `Moon: ${el.title}`);
  }

  function renderScene(visual) {
    const root = $("wx-scene");
    // Settings → Display → "Animated weather" off: show the scene, but still.
    if (visual && state.config?.display && !state.config.display.weather_animations && visual.motion === "live") {
      visual = { ...visual, motion: "static", reason: "off" };
    }
    Scene.render(root, visual);
    $("weather").dataset.motion = root.dataset.motion;
    $("weather").dataset.scene = root.dataset.scene;
  }

  function showWeatherMessage(kind, title, body, iconName) {
    setWeatherState(kind);
    $("weather").dataset.art = "none";
    $("weather-message-icon").innerHTML = icon(iconName || (kind === "error" ? "alert" : "cloudOff"), 1.4);
    $("weather-message-title").textContent = title;
    $("weather-message-body").textContent = body;
    document.body.dataset.sky = "clear";
    $("daily").replaceChildren();
    renderScene(null);
  }

  function stat(label, value) {
    const div = document.createElement("div");
    const dt = document.createElement("dt"); dt.textContent = label;
    const dd = document.createElement("dd"); dd.textContent = value;
    div.append(dt, dd);
    return div;
  }

  function renderWeather(payload) {
    const w = payload.data;
    const c = w.current;
    const stale = payload.status === "stale";
    setWeatherState("ok");

    // One decision (sky.js) for the picture and the scene, shared with the detail view.
    const art = window.AuraSky.picture(payload);
    $("weather-art").innerHTML = art.html;
    $("weather").dataset.art = art.kind;
    // the header glyph follows the sky (a cloud on a clear day looked wrong)
    const glyph = c.condition === "thunderstorm" ? "storm" : c.condition === "snow" ? "snow" : c.condition === "fog" ? "fog"
      : art.kind === "sun" && c.condition === "clear" ? "sun" : "cloud";
    const head = $("weather").querySelector(".tile__icon");
    if (head.dataset.icon !== glyph) { head.dataset.icon = glyph; head.innerHTML = icon(glyph, 1.7); }
    const visual = window.AuraSky.visualFor(payload, art.day);
    renderScene(stale && visual ? { ...visual, motion: "static" } : visual);
    $("weather-temp").textContent = fmtTemp(c.temperature_c);
    $("weather-desc").textContent = c.description;
    $("weather-range").textContent = `H ${fmtTemp(w.high_c)}  ·  L ${fmtTemp(w.low_c)}`;

    const sun = $("weather-sun");
    sun.replaceChildren();
    for (const [key, iconName, label] of [["sunrise", "sunrise", "Sunrise"], ["sunset", "sunset", "Sunset"]]) {
      if (!w[key]) continue;
      const span = document.createElement("span");
      span.innerHTML = icon(iconName);
      span.append(document.createTextNode(fmtClockTime(new Date(w[key]))));
      span.title = label;
      span.setAttribute("aria-label", `${label} ${fmtClockTime(new Date(w[key]))}`);
      sun.append(span);
    }

    const stats = [stat("Feels like", fmtTemp(c.feels_like_c))];
    if (c.precipitation_chance_pct != null) stats.push(stat("Rain", `${c.precipitation_chance_pct}%`));
    stats.push(stat("Humidity", `${c.humidity_pct}%`));
    stats.push(stat("Wind", `${Math.round(c.wind_kph)} km/h`));
    if (c.uv_index != null) stats.push(stat("UV", c.is_day ? String(Math.round(c.uv_index)) : "0"));
    if (w.air_quality) stats.push(stat("AQI", String(w.air_quality.us_aqi)));
    for (const st of stats) st.dataset.k = st.querySelector("dt")?.textContent.toLowerCase().replace(/\s+/g, "-") ?? "";   // home.css picks three
    $("weather-stats").replaceChildren(...stats);
    const aqiEl = stats.at(-1);
    if (w.air_quality) aqiEl.title = `US AQI · ${w.air_quality.category}`;

    const now = Date.now();
    const upcoming = w.hourly.filter((h) => new Date(h.time).getTime() > now).slice(0, 6);
    $("hourly").replaceChildren(...upcoming.map((h) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="hourly__time"></span><span class="hourly__icon">${weatherArt(h.condition, h.is_day)}</span><span class="hourly__temp"></span><span class="hourly__rain"></span>`;
      li.querySelector(".hourly__time").textContent = fmtHour(h.time);
      li.querySelector(".hourly__temp").textContent = fmtTemp(h.temperature_c);
      const p = h.precipitation_chance_pct;
      li.querySelector(".hourly__rain").textContent = p != null && p >= 20 ? `${p}%` : "";
      return li;
    }));

    renderDaily(w.daily || []);

    const meta = $("weather-meta");
    meta.replaceChildren();
    const add = (text, cls) => {
      const s = document.createElement("span");
      s.textContent = text;
      if (cls) s.className = cls;
      meta.append(s);
    };
    if (payload.is_mock) add("Sample data · not live weather", "tag-preview");
    const updated = fmtClockTime(new Date(w.fetched_at));
    if (stale) {
      add(`Weather service unavailable · showing data from ${updated}`, "is-stale");
    } else {
      add(`Updated ${updated}`);
    }
    if (w.attribution) add(w.attribution);

    document.body.dataset.sky = skyFor(c.condition);
  }

  function renderDaily(days) {
    const today = new Intl.DateTimeFormat("en-CA", { timeZone: tz() }).format(new Date());
    $("daily").replaceChildren(...days.slice(0, 5).map((d) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="daily__day"></span><span class="daily__icon">${weatherArt(d.condition, true)}</span><span class="daily__temps"></span><span class="daily__rain"></span>`;
      const label = d.date === today ? "Today" : new Intl.DateTimeFormat("en-GB", {
        weekday: "short", timeZone: "UTC",
      }).format(new Date(`${d.date}T12:00:00Z`));
      li.querySelector(".daily__day").textContent = label;
      const temps = li.querySelector(".daily__temps");
      temps.textContent = fmtTemp(d.high_c);
      const lo = document.createElement("span");
      lo.textContent = fmtTemp(d.low_c);
      temps.append(lo);
      const p = d.precipitation_chance_pct;
      li.querySelector(".daily__rain").textContent = p != null && p >= 30 ? `${p}%` : "";
      li.title = d.description;
      return li;
    }));
  }

  async function loadWeather() {
    try {
      const payload = await fetchJSON("/api/weather");
      state.weatherPayload = payload;
      renderMoon(payload);
      Detail.refresh();
      if ((payload.status === "ok" || payload.status === "stale") && payload.data) {
        state.weather = payload;
        state.weatherReceivedAt = Date.now();
        renderWeather(payload);
        renderAdvice(payload);
        renderStatus();
        return;
      }
      state.weather = null;
      if (payload.reason === "no_location") {
        showWeatherMessage("unavailable", "Set your home location",
          "Weather appears once AURA knows which city your home is in. Use the Location button above.", "pin");
      } else if (payload.reason === "disabled") {
        showWeatherMessage("unavailable", "Weather is turned off",
          "Set AURA_WEATHER_PROVIDER=open_meteo in .env to enable live weather.");
      } else {
        showWeatherMessage("error", "Weather couldn't be loaded",
          `${payload.message || "The weather service returned an error"}. Retrying automatically.`);
      }
      renderAdvice(null, payload.reason === "no_location" ? "no_location" : payload.status);
      renderStatus();
    } catch {
      // AURA server unreachable: keep what we have, marked stale.
      if (state.weather) {
        renderWeather({ ...state.weather, status: "stale" });
      } else {
        showWeatherMessage("error", "Can't reach AURA", "The dashboard server isn't responding. Retrying automatically.");
        renderAdvice(null, "error");
      }
    }
  }

  // ---------- advice ----------

  function stateRow(iconName, text, sub, variant = "") {
    const li = document.createElement("li");
    li.className = `state ${variant}`;
    li.innerHTML = icon(iconName);
    const span = document.createElement("span");
    span.textContent = text;
    if (sub) {
      const small = document.createElement("small");
      small.textContent = sub;
      span.append(small);
    }
    li.append(span);
    return li;
  }

  function renderAdvice(payload, status) {
    const list = $("advice-list");
    if (!payload) {
      list.dataset.state = status || "unavailable";
      const sub = status === "no_location"
        ? "Set your home location to get advice based on its weather."
        : "Suggestions are based on live weather and will appear once it's available.";
      list.replaceChildren(stateRow("info", status === "error" ? "Advice paused" : "No advice yet", sub));
      return;
    }
    list.dataset.state = "ok";
    list.replaceChildren(...payload.recommendations.map((r) => {
      const li = document.createElement("li");
      li.className = "advice__item";
      li.dataset.priority = r.priority;
      li.innerHTML = `<span class="advice__icon">${icon(r.icon)}</span><div><div class="advice__title"></div><div class="advice__detail"></div></div>`;
      li.querySelector(".advice__title").textContent = r.title;
      li.querySelector(".advice__detail").textContent = r.detail;
      return li;
    }));
  }

  // ---------- groceries ----------

  const MAX_GROCERIES = 8;
  const QR_REFRESH_MS = 10 * 60_000;   // the Pi's IP can change; keep the code current

  function renderGroceries(res) {
    state.groceries = res;
    renderStatus();
    const list = $("grocery-list");
    const count = $("grocery-count");
    list.dataset.state = res.status;
    if (res.revision != null) state.groceryRevision = res.revision;

    if (res.status === "error") {
      count.textContent = "";
      list.replaceChildren(stateRow("alert", "Couldn't load the grocery list", res.message, "state--error"));
      return;
    }
    const open = (res.items || []).filter((i) => !i.purchased);
    if (!open.length) {
      count.textContent = res.items?.length ? "All bought" : "";
      list.replaceChildren(stateRow("cart",
        res.items?.length ? "Nothing left to buy" : "Your list is empty",
        "Scan the code to add items from your phone."));
      return;
    }

    count.textContent = `${open.length} to buy`;
    // On the entrance screen a wide tile has room for two columns of three; a large one for more.
    const tv = matchMedia("(min-width: 1200px) and (min-aspect-ratio: 4/3)").matches;
    // a wide tile shows as many rows of two as its height allows (3 on Home's grid, up to 5)
    const share = tv ? $("grocery-tile").getBoundingClientRect().height / innerHeight : 0;
    const max = !tv ? MAX_GROCERIES : sizeOf("groceries") === "l" ? 16 : share > 0.36 ? 10 : share > 0.25 ? 8 : 6;
    const shown = open.slice(0, max);
    const canEdit = state.session?.can_edit;
    list.toggleAttribute("data-readonly", !canEdit);
    const rows = shown.map((item) => {
      const li = document.createElement("li");
      li.className = "grocery__item";
      li.innerHTML = `<button type="button"><span class="grocery__box">${icon("check", 3)}</span><span class="grocery__name"></span><span class="grocery__qty"></span></button>`;
      const btn = li.querySelector("button");
      li.querySelector(".grocery__name").textContent = item.name;
      li.querySelector(".grocery__qty").textContent = item.quantity || "";
      if (canEdit) {
        btn.setAttribute("role", "checkbox");
        btn.setAttribute("aria-checked", "false");
        btn.setAttribute("aria-label", `Mark ${item.name} as purchased`);
        btn.addEventListener("click", () => markPurchased(item, li));
      } else {
        btn.disabled = true;
      }
      return li;
    });
    if (open.length > shown.length) {
      const more = document.createElement("li");
      more.className = "grocery__more";
      more.textContent = `+ ${open.length - shown.length} more`;
      rows.push(more);
    }
    list.replaceChildren(...rows);
  }

  async function markPurchased(item, li) {
    li.classList.add("is-pending", "is-checked");
    try {
      await fetchJSON(`/api/groceries/${encodeURIComponent(item.id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ purchased: true }),
      });
    } catch {
      // revert below by reloading the authoritative list
    }
    loadGroceries();
  }

  async function loadGroceries() {
    try {
      renderGroceries(await fetchJSON("/api/groceries"));
    } catch {
      const list = $("grocery-list");
      if (list.dataset.state === "loading" || list.dataset.state === "error") {
        renderGroceries({ status: "error", message: "Server not responding. Retrying." });
      }
    }
  }

  async function loadSession() {
    try {
      state.session = await fetchJSON("/api/session");
    } catch {
      state.session = state.session || { can_edit: false, pin_configured: false };
    }
    $("loc-unlock").hidden = !!state.session.can_edit || !state.session.pin_configured;
    for (const id of ["loc-confirm", "loc-detect", "loc-search"]) {
      $(id).disabled = !state.session.can_edit;
    }
  }

  function refreshQr() {
    const img = $("grocery-qr-img");
    img.onload = () => { $("grocery-qr").hidden = false; };
    img.onerror = () => { $("grocery-qr").hidden = true; };
    img.src = `/api/share/qr.svg?t=${Date.now()}`;
  }

  // ---------- commute ----------

  const TRAFFIC_TEXT = { light: "Light traffic", moderate: "Moderate traffic", heavy: "Heavy traffic", severe: "Severe traffic" };

  function fmtIn(tzName, date, opts) {
    return new Intl.DateTimeFormat("en-US", { timeZone: tzName, ...opts }).format(date);
  }

  function renderCommute(c) {
    const card = $("commute");
    const body = $("commute-body");
    const meta = $("commute-meta");
    const tzName = c.timezone;
    const t = (iso) => fmtIn(tzName, new Date(iso), { hour: "numeric", minute: "2-digit", hour12: twelve() });
    card.dataset.state = c.state;
    $("commute-route").textContent = `${c.home_label} → ${c.office_label}`;
    meta.replaceChildren();
    const addMeta = (text, cls) => {
      const span = document.createElement("span");
      span.textContent = text;
      if (cls) span.className = cls;
      meta.append(span);
    };

    if (c.state === "unconfigured" || c.state === "disabled") {
      body.replaceChildren(stateRow("car",
        c.state === "disabled" ? "Commute monitoring is off" : "Traffic isn't connected yet",
        c.message));
      return;
    }

    const nodes = [];
    const rec = c.recommendation;
    if (rec) {
      const top = document.createElement("div");
      top.className = "commute__top";
      const box = document.createElement("div");
      box.className = "commute__rec";
      const label = document.createElement("div");
      label.className = "commute__label";
      label.textContent = rec.basis === "historical" ? "Usually best to leave" : "Best time to leave";
      const time = document.createElement("div");
      time.className = "commute__time";
      time.textContent = rec.leave_now ? "Now" : t(rec.departure_at);
      const facts = document.createElement("div");
      facts.className = "commute__facts";
      const dur = document.createElement("span");
      dur.innerHTML = "<b></b> min";
      dur.querySelector("b").textContent = rec.duration_min;
      const arr = document.createElement("span");
      arr.textContent = `arrive ${t(rec.arrival_at)}`;
      facts.append(dur, arr);
      if (rec.traffic_level) {
        const tl = document.createElement("span");
        tl.className = `traffic traffic--${rec.traffic_level}`;
        tl.textContent = TRAFFIC_TEXT[rec.traffic_level] + (rec.delay_min ? ` · +${rec.delay_min} min` : "");
        facts.append(tl);
      }
      const basis = document.createElement("span");
      basis.className = `basis basis--${rec.basis}`;
      basis.textContent = rec.basis_label;
      const head = document.createElement("div");
      head.className = "commute__head";
      head.append(label, basis);
      box.append(head, time, facts);
      top.append(box);
      nodes.push(top);
      if (c.live && !rec.leave_now) {
        const now = document.createElement("div");
        now.className = "commute__now";
        now.innerHTML = "Leaving now: <b></b>";
        now.querySelector("b").textContent = `${c.live.duration_min} min (live traffic), arrive ${t(c.live.arrival_at)}`;
        nodes.push(now);
      }
    } else {
      nodes.push(stateRow(c.state === "off_day" || c.state === "after_window" ? "info" : "car",
        c.state === "off_day" ? "No commute today" : c.state === "after_window" ? "Commute window over" : "Commute",
        c.message));
    }

    const withData = c.slots.filter((x) => x.duration_min != null);
    if (withData.length) {
      if (c.state === "after_window") {
        const cap = document.createElement("div");
        cap.className = "commute__label";
        cap.textContent = "Earlier today";
        nodes.push(cap);
      }
      nodes.push(slotChart(c.slots, t, tzName));
    }
    body.replaceChildren(...nodes);

    if (c.provider) {
      addMeta(c.last_success_at ? `${c.provider} · updated ${t(c.last_success_at)}` : `${c.provider} · no data yet`);
    }
    if (rec && c.state !== "active" && c.state !== "prefetch") addMeta(c.message);
    if (c.last_error && c.state !== "unconfigured") addMeta(c.last_error, "is-error");
  }

  function slotChart(slots, t, tzName) {
    const wrap = document.createElement("div");
    const durations = slots.map((x) => x.duration_min).filter((x) => x != null);
    const max = Math.max(...durations), min = Math.min(...durations);
    const ul = document.createElement("ul");
    ul.className = "slots";
    ul.style.setProperty("--n", String(slots.length));
    ul.setAttribute("aria-label", "Journey time by departure time");
    for (const sl of slots) {
      const li = document.createElement("li");
      li.className = "slot";
      const bar = document.createElement("span");
      bar.className = "slot__bar";
      if (sl.duration_min == null) {
        li.dataset.empty = "";
        li.title = `${t(sl.departure_at)}: no data`;
      } else {
        // Scale between 35% and 100% so differences are visible but not exaggerated.
        const frac = max === min ? 0.7 : 0.35 + 0.65 * (sl.duration_min - min) / (max - min);
        bar.style.setProperty("--h", `${Math.round(frac * 100)}%`);
        li.title = `${t(sl.departure_at)}: ${sl.duration_min} min` + (sl.traffic_level ? ` · ${sl.traffic_level}` : "");
        if (sl.traffic_level) li.dataset.level = sl.traffic_level;
        if (sl.basis) li.dataset.basis = sl.basis;
      }
      if (sl.past) li.dataset.past = "";
      if (sl.best) li.dataset.best = "";
      li.setAttribute("aria-label", li.title);
      li.append(bar);
      ul.append(li);
    }
    const axis = document.createElement("div");
    axis.className = "slots__axis";
    axis.style.setProperty("--n", String(slots.length));
    axis.setAttribute("aria-hidden", "true");
    for (const sl of slots) {
      const span = document.createElement("span");
      const d = new Date(sl.departure_at);
      const mins = Number(fmtIn(tzName, d, { minute: "numeric" }));
      span.textContent = mins === 0 ? fmtIn(tzName, d, { hour: "numeric", hour12: twelve() }).replace(" ", "") : "";
      axis.append(span);
    }
    wrap.append(ul, axis);
    return wrap;
  }

  async function loadCommute() {
    try {
      state.commute = await fetchJSON("/api/commute");
      renderCommute(state.commute);
      renderStatus();
      Detail.refresh("commute");
    } catch {
      if ($("commute").dataset.state === "loading") {
        $("commute-body").replaceChildren(stateRow("alert", "Couldn't load commute", "Retrying automatically.", "state--error"));
        $("commute").dataset.state = "error";
      }
    }
  }

  // ---------- calendar ----------

  const KIND_ICON = { birthday: "cake", event: "calendar", holiday: "star", observance: "dot" };

  function dayLabel(isoDate, today) {
    if (isoDate === today) return "Today";
    const t = new Date(`${today}T12:00:00Z`), d = new Date(`${isoDate}T12:00:00Z`);
    if ((d - t) / 86400000 === 1) return "Tomorrow";
    return new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" }).format(d);
  }

  function calTime(item, tzName) {
    if (item.all_day) return "";
    return new Intl.DateTimeFormat("en-US", { timeZone: tzName, hour: "numeric", minute: "2-digit", hour12: twelve() })
      .format(new Date(item.start));
  }

  function renderAgenda(cal) {
    const list = $("agenda");
    const tile = $("calendar-tile");
    const empty = $("calendar-empty");
    const setEmpty = (title, text, kind = "ok") => {
      list.replaceChildren();
      tile.dataset.state = kind;
      $("calendar-when").textContent = "";
      // With the Tithi line below, a small tile has room for the title only (details are in the Calendar view).
      const short = !$("panchang").hidden && sizeOf("calendar") !== "l";
      empty.replaceChildren(Object.assign(document.createElement("b"), { textContent: title }), document.createTextNode(short ? "" : text));
      empty.hidden = false;
    };
    if (!cal || cal.status === "unconfigured") {
      setEmpty("No calendar connected", "Add a calendar address on the Pi (Settings → Calendars).", "off");
      return;
    }
    const days = cal.days || [];
    // Today's items; if there are none, the next day that has something.
    const day = days.find((d) => d.date === cal.today) || days[0];
    if (!day) {
      if (cal.status === "error" || cal.status === "loading") setEmpty("Calendars unavailable", "They couldn't be reached right now.", "error");
      else setEmpty("Nothing coming up", "No events, birthdays or holidays in the next few weeks.");
      return;
    }
    empty.hidden = true;
    tile.dataset.state = cal.status === "partial" ? "alert" : "ok";
    // Small and wide: at most three rows (three items, or two plus "+N more").
    // Large (2x2): up to eight rows, continuing into the following days with a day label.
    const large = sizeOf("calendar") === "l";
    const withTithi = !$("panchang").hidden;          // the Tithi line takes a row at the bottom
    const cap = large ? (withTithi ? 7 : 8) : (withTithi ? 2 : 3);
    const shown = day.items.length > cap ? day.items.slice(0, cap - 1) : day.items;
    $("calendar-when").textContent = dayLabel(day.date, cal.today);
    const later = [];
    if (large && day.items.length <= cap) {
      let room = cap - shown.length;
      for (const d of days.slice(days.indexOf(day) + 1)) {
        if (room < 2 || !d.items.length) continue;
        const items = d.items.slice(0, room - 1);
        later.push({ date: d.date, items });
        room -= items.length + 1;
      }
    }
    const row = (it) => {
      const li = document.createElement("li");
      li.className = "agenda__item";
      li.dataset.kind = it.kind;
      li.innerHTML = `<span class="agenda__icon">${icon(KIND_ICON[it.kind])}</span><span class="agenda__title"></span><span class="agenda__time"></span>`;
      li.querySelector(".agenda__title").textContent = it.title;
      li.querySelector(".agenda__time").textContent = it.all_day ? "" : calTime(it, cal.timezone);
      return li;
    };
    list.replaceChildren(...shown.map(row), ...later.flatMap((d) => {
      const head = document.createElement("li");
      head.className = "agenda__day";
      head.textContent = dayLabel(d.date, cal.today);
      return [head, ...d.items.map(row)];
    }));
    if (day.items.length > shown.length) {
      const more = document.createElement("li");
      more.className = "agenda__more";
      more.textContent = `+ ${day.items.length - shown.length} more`;
      list.append(more);
    }
  }

  // ---------- Price Tracker (gold and silver; app/prices.py) ----------
  async function loadPrices() {
    try { state.prices = await fetchJSON("/api/prices"); } catch { /* keep the last answer */ }
    window.AuraPrices.renderTile(state.prices, sizeOf("prices"));
    window.AuraPrices.refresh(state.prices);
  }

  // ---------- Panchang (Tithi under the events; details in the Calendar view) ----------
  async function loadPanchang() {
    clearTimeout(state.panchangTimer);
    try {
      const p = await fetchJSON("/api/panchang");
      state.panchang = p.status === "ok" ? p : null;
      state.panchangWhy = p.message || null;
    } catch { state.panchang = null; }          // no location or not available: the Calendar stays as it is
    renderPanchang(state.panchang);
    Detail.refresh("calendar");
    // Recalculate right after the current Tithi ends (and in 30 min at the latest).
    const end = state.panchang?.now?.tithi?.end ? new Date(state.panchang.now.tithi.end) - Date.now() + 5000 : Infinity;
    state.panchangTimer = setTimeout(loadPanchang, Math.max(5000, Math.min(end, 30 * 60_000)));
  }

  function renderPanchang(p) {
    const box = $("panchang");
    const t = p?.now?.tithi;
    if (box.hidden !== !t) {
      box.hidden = !t;
      if (state.calendar) renderAgenda(state.calendar);   // one row fewer (or more) for events
    }
    if (!t) return;
    box.querySelector(".panchang__icon").innerHTML = icon("crescent", 1.6);
    $("panchang-hi").textContent = `${t.paksha_hi} · ${t.name_hi}`;
    $("panchang-en").textContent = `${t.paksha_en.replace(" Paksha", "")} ${t.name_en} · until ${clockTime(new Date(t.end))}`;
    box.setAttribute("aria-label", `Tithi: ${t.paksha_en} ${t.name_en}, until ${clockTime(new Date(t.end))}. Open the Panchang`);
  }

  async function loadCalendar() {
    try {
      state.calendar = await fetchJSON("/api/calendar");
      renderAgenda(state.calendar);
      renderStatus();
      Detail.refresh("calendar");
    } catch { /* keep what's shown */ }
  }

  // ---------- meal plan ----------

  const MEAL_LABEL = { breakfast: "Breakfast", lunch: "Lunch", dinner: "Dinner" };

  async function loadMeals() {
    const tile = $("meals-tile");
    if (state.config?.meals_enabled === false) { state.meals = null; return; }
    try {
      state.meals = await fetchJSON("/api/meals/day");
      renderMeals(state.meals);
    } catch (err) {
      if (!state.meals) {
        tile.dataset.state = "error";
        $("meals-list").replaceChildren();
        $("meals-prep").textContent = err.status === 404 ? "The meal plan is turned off." : "Couldn't load today's meals.";
      }
    }
    window.AuraMeals?.refresh();
  }

  function renderMeals(d) {
    const tile = $("meals-tile");
    tile.dataset.state = "ok";
    const date = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" })
      .format(new Date(`${d.date}T12:00:00Z`));
    $("meals-day").textContent = `Day ${d.plan_day} · ${date}`;
    $("meals-list").replaceChildren(...d.meals.map((m) => {
      const li = document.createElement("li");
      li.className = "meals__item";
      const eaten = d.eaten[m.meal];
      const both = eaten.person1 && eaten.person2, some = eaten.person1 || eaten.person2;
      li.dataset.done = both ? "all" : some ? "some" : "none";
      li.innerHTML = `<span class="meals__when"></span><span class="meals__name"></span><span class="meals__check" aria-hidden="true">${icon("check", 2.6)}</span>`;
      li.querySelector(".meals__when").textContent = MEAL_LABEL[m.meal];
      li.querySelector(".meals__name").textContent = m.label;
      li.title = `${m.name} (${m.code})` + (some ? ` · eaten: ${[eaten.person1 && d.people.person1, eaten.person2 && d.people.person2].filter(Boolean).join(", ")}` : "");
      return li;
    }));
    const tonight = d.tomorrow.tasks.filter((t) => t.when === "tonight");
    const open = tonight.filter((t) => !t.done && t.recipe);
    const prep = $("meals-prep");
    prep.replaceChildren();
    const b = document.createElement("b");
    if (open.length) {
      b.textContent = "Tonight: ";
      prep.append(b, open[0].text + (open.length > 1 ? ` (+${open.length - 1} more)` : ""));
      prep.dataset.kind = "todo";
    } else {
      b.textContent = "Tonight: ";
      prep.append(b, tonight.some((t) => t.recipe) ? "all preparation for tomorrow is done."
        : `nothing to soak or prepare for tomorrow (${d.tomorrow.meals.map((m) => m.code).join(", ")}).`);
      prep.dataset.kind = "ok";
    }
    renderGlance();
  }

  // ---------- smart home (Home Assistant) ----------

  async function loadSmartHome() {
    try {
      state.smarthome = await fetchJSON("/api/smarthome");
    } catch { /* keep the last known state */ }
    renderSmartHome(state.smarthome);
    renderStatus();
    window.AuraSmartHome?.refresh();
  }

  /** Summary only: totals, offline devices, favourites. Every value is from /api/smarthome. */
  function renderSmartHome(d) {
    const tile = $("smarthome-tile");
    const val = $("smarthome-status"), sub = $("smarthome-sub"), favs = $("smarthome-favs");
    $("smarthome-tile").querySelector(".sh-empty-cta")?.remove();      // only while there are no devices
    if (!d) { tile.dataset.state = "loading"; val.textContent = "Checking…"; sub.textContent = ""; favs.replaceChildren(); return; }
    const c = d.connection, n = d.counts;
    $("smarthome-aside").textContent = c.status === "connected" && d.scenes.length ? `${d.scenes.length} scene${d.scenes.length === 1 ? "" : "s"}` : "";
    favs.replaceChildren();
    renderSmartHomeRooms([]);
    if (c.status !== "connected") {
      tile.dataset.state = c.status === "not_configured" ? "off" : "alert";
      val.textContent = c.status === "not_configured" ? "Not connected" : c.status === "connecting" ? "Connecting…" : "Unavailable";
      sub.textContent = c.status === "not_configured" ? "Set up Home Assistant to add devices." : c.message || "";
      return;
    }
    if (!n.total) {
      tile.dataset.state = "ok";
      val.textContent = "No devices yet";
      sub.textContent = "Home Assistant is connected. Open to add devices.";
      // a clear next step on the empty tile (the tile itself opens the same place)
      if (!tile.querySelector(".sh-empty-cta")) {
        const b = document.createElement("button");
        b.type = "button"; b.className = "btn sh-empty-cta"; b.dataset.open = "smarthome"; b.textContent = "Add devices";
        sub.after(b);
      }
      return;
    }
    tile.dataset.state = d.alerts.length || n.offline ? "alert" : "ok";
    val.textContent = d.alerts.length ? d.alerts[0] : `${n.online} of ${n.total} online`;
    sub.textContent = n.offline ? d.warnings.slice(0, 2).join(" · ") + (n.offline > 2 ? ` +${n.offline - 2}` : "")
      : d.rooms.map((r) => `${r.name} ${r.on}/${r.total} on`).join(" · ");
    const large = sizeOf("smarthome") === "l";
    if (large && d.rooms.length) sub.textContent = n.offline ? sub.textContent : "";
    renderSmartHomeRooms(large ? d.rooms : []);
    favs.replaceChildren(...d.favorites.slice(0, large ? 8 : 3).map((f) => {
      const li = document.createElement("li");
      li.className = "shtile__fav";
      li.dataset.on = String(["on", "open", "playing", "unlocked"].includes(f.state));
      li.dataset.availability = f.availability;
      li.textContent = f.name;
      return li;
    }));
  }

  /** Large tile: one line per room (devices on / total, offline). */
  function renderSmartHomeRooms(rooms) {
    const ul = $("smarthome-rooms");
    ul.hidden = !rooms.length;
    ul.replaceChildren(...rooms.map((r) => {
      const li = document.createElement("li");
      li.innerHTML = `<b></b><span></span>`;
      li.querySelector("b").textContent = r.name;
      li.querySelector("span").textContent = `${r.on} of ${r.total} on${r.offline ? ` · ${r.offline} offline` : ""}`;
      return li;
    }));
  }

  // ---------- internet speed (the Pi checks every few minutes; shown as a status capsule) ----------

  const fmtMbps = (v) => (v >= 100 ? Math.round(v) : v.toFixed(1));

  async function loadSpeed() {
    try {
      state.speed = await fetchJSON("/api/system/speed");
    } catch { /* keep the last reading */ }
    renderStatus();
  }

  // ---------- network protection (read-only Pi-hole status) ----------

  async function loadProtection() {
    try {
      state.protection = await fetchJSON("/api/system/dns-filter");
    } catch { /* keep the last known state */ }
    renderProtection(state.protection);
    renderStatus();
    Detail.refresh("protection");
  }

  /** One reading of the Pi-hole status for both the tile and its status capsule:
      state (ok/alert/off), the tile's value and line, and the short capsule text. */
  function protectionSummary(d) {
    if (!d.installed) {
      return { state: "off", value: "Not installed", sub: "Network-wide ad blocking (Pi-hole) isn't set up on this Pi.", capsule: null };
    }
    if (d.service !== "running" || d.dns_ok === false) {
      return { state: "alert", value: d.service !== "running" ? "Stopped" : "Not answering",
        sub: d.problems?.[0] || "Pi-hole needs attention.", capsule: "Needs attention" };
    }
    if (d.blocking_ok === false || d.problems?.length) {
      return { state: "alert", value: "Needs attention", sub: d.problems?.[0] || "A test ad domain was not blocked.", capsule: "Needs attention" };
    }
    const n = (x) => Number(x).toLocaleString("en-IN");
    const stats = d.stats ? ` · ${d.stats.percent_blocked}% of ${n(d.stats.total_queries)} lookups blocked (24 h)` : "";
    if (d.router_uses_pihole) {
      return { state: "ok", value: "Active", capsule: "On for home network",
        sub: `Router gives out this Pi for DNS, so devices that follow it are filtered${stats}.` };
    }
    if (d.router_uses_pihole == null) {
      return { state: "ok", value: "Running", capsule: "Running",
        sub: `Blocking works; couldn't tell which DNS server the router gives out${stats}.` };
    }
    return { state: "ok", value: "Running", capsule: "On (not network-wide)",
      sub: `Not network-wide yet: only devices set to use this Pi for DNS are filtered${stats}.` };
  }

  function renderProtection(d) {
    const tile = $("protection-tile");
    const val = $("protection-status");
    const sub = $("protection-sub");
    if (!d) { tile.dataset.state = "loading"; val.textContent = "Checking…"; sub.textContent = ""; return; }
    const s = protectionSummary(d);
    tile.dataset.state = s.state;
    val.textContent = s.value;
    sub.textContent = s.sub;
  }

  // ---------- system ----------

  async function loadSystem() {
    try {
      const [info, net] = await Promise.all([fetchJSON("/api/system/info"), fetchJSON("/api/system/network").catch(() => null)]);
      state.system = info;
      if (net) state.network = net;
    } catch { /* keep the last known state */ }
    renderSystem();
    Detail.refresh("system");
  }

  function renderSystem() {
    const s = state.system, n = state.network;
    const tile = $("system-tile");
    if (!s) { tile.dataset.state = "loading"; return; }
    const items = [];
    const add = (label, value, warn = false) => {
      const div = document.createElement("div");
      const dt = document.createElement("dt"); dt.textContent = label;
      const dd = document.createElement("dd"); dd.textContent = value;
      if (warn) dd.className = "is-warn";
      div.append(dt, dd); items.push(div);
    };
    const hot = s.cpu_temperature_c != null && s.cpu_temperature_c >= 75;
    const memPct = s.memory_total_mb ? Math.round(100 * (1 - s.memory_available_mb / s.memory_total_mb)) : null;
    const diskPct = s.disk_total_gb ? Math.round(100 * (1 - s.disk_free_gb / s.disk_total_gb)) : null;
    if (s.cpu_temperature_c != null) add("CPU", `${Math.round(s.cpu_temperature_c)}°C`, hot);
    if (memPct != null) add("Memory", `${memPct}% used`, memPct >= 90);
    if (diskPct != null) add("Storage", `${Math.round(s.disk_free_gb)} GB free`, diskPct >= 90);
    if (n) add("Network", n.connectivity === "full" ? (n.wifi_connected ? "Wi-Fi" : "Ethernet") : (n.connectivity || "Unknown"), n.connectivity && n.connectivity !== "full");
    $("system-stats").replaceChildren(...items);
    const problem = hot || memPct >= 90 || diskPct >= 90 || (n && n.connectivity && n.connectivity !== "full");
    tile.dataset.state = problem ? "alert" : "ok";
    $("system-aside").textContent = problem ? "Check" : "Healthy";
  }

  // ---------- at-a-glance status capsules (only from data already loaded) ----------


  function renderStatus() {
    const caps = [];
    const cap = (iconName, label, value, open, tone, kind = "") => caps.push({ iconName, label, value, open, tone, kind });
    const w = state.weather?.data;
    if (w) {
      cap("cloud", w.city || "Weather", `${fmtTemp(w.current.temperature_c)} ${w.current.description}`, "weather", "var(--tone-sky)");
      const alert = state.weather.recommendations?.find((r) => r.priority === "high");
      if (alert) cap(alert.icon, "Before you go", alert.title, "advice", "var(--tone-amber)", "alert");
    } else if (state.weatherPayload && state.weatherPayload.reason !== "no_location") {
      cap("cloudOff", "Weather", "Unavailable", "weather", "var(--tone-sky)", "alert");
    }
    const c = state.commute;
    if (c?.recommendation && ["active", "prefetch", "before_window"].includes(c.state)) {
      const r = c.recommendation;
      const t = new Intl.DateTimeFormat("en-US", { timeZone: c.timezone, hour: "numeric", minute: "2-digit", hour12: twelve() }).format(new Date(r.departure_at));
      cap("car", `To ${c.office_label}`, r.leave_now ? `Leave now · ${r.duration_min} min` : `Leave ${t} · ${r.duration_min} min`, "commute", "var(--tone-green)",
        r.traffic_level === "heavy" || r.traffic_level === "severe" ? "alert" : "");
    }
    const cal = state.calendar;
    const today = cal?.days?.find((d) => d.date === cal.today);
    if (today?.items.length) {
      const first = today.items[0];
      cap(KIND_ICON[first.kind] || "calendar", "Today", today.items.length > 1 ? `${first.title} +${today.items.length - 1}` : first.title, "calendar", "var(--tone-red)");
    }
    const g = state.groceries;
    const open = (g?.items || []).filter((i) => !i.purchased).length;
    if (open) cap("cart", "Groceries", `${open} to buy`, "groceries", "var(--tone-orange)");
    const sh = state.smarthome;
    if (sh?.connection?.status === "connected" && (sh.alerts.length || sh.counts.offline)) {
      cap("bulb", "Smart home", sh.alerts[0] || `${sh.counts.offline} offline`, "smarthome", "var(--tone-amber)", "alert");
    }
    const p = state.protection;
    if (p?.installed) {
      const ps = protectionSummary(p);
      if (ps.state === "alert") cap("shieldOff", "Ad blocking", ps.capsule, "protection", "var(--tone-teal)", "alert");
      else cap("shield", "Ad blocking", ps.capsule, "protection", "var(--tone-teal)");
    }
    const sp = state.speed;     // measured on the Pi every few minutes (app/speedtest.py)
    if (sp?.status === "ok") cap("gauge", "Internet", `↓ ${fmtMbps(sp.down_mbps)} Mbps · ${sp.ping_ms} ms`, "system", "var(--tone-sky)");
    else if (sp?.status === "offline") cap("wifi", "Internet", "No internet", "system", "var(--tone-sky)", "alert");
    const onScreen = (open) => !!document.querySelector(`#tiles .tile[data-open="${open}"]:not([hidden])`);
    // Every view is calm: only alerts (not already on a tile) and the internet speed.
    const shown = caps.filter((x) => x.label === "Internet" || (x.kind === "alert" && !onScreen(x.open)));
    $("status-row").replaceChildren(...shown.slice(0, 7).map((x) => {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.className = "status__btn";
      b.dataset.open = x.open;
      if (x.kind) b.dataset.kind = x.kind;
      b.style.setProperty("--tone", x.tone);
      b.setAttribute("aria-label", `${x.label}: ${x.value}`);
      b.innerHTML = `<span class="status__icon" aria-hidden="true">${icon(x.iconName)}</span><span class="status__text"><span class="status__label"></span><span class="status__value"></span></span>`;
      b.querySelector(".status__label").textContent = x.label;
      b.querySelector(".status__value").textContent = x.value;
      li.append(b);
      return li;
    }));
    renderGlance();
  }

  // ---------- glance: the calm Home view shows one fact per tile (home.css hides the rest) ----------
  const MEAL_END = { breakfast: 10.5, lunch: 15.5, dinner: 24 };   // hour each meal stops being "next"

  function glanceFacts() {
    const facts = {};
    const c = state.commute;
    if (c?.recommendation && ["active", "prefetch", "before_window"].includes(c.state)) {
      const r = c.recommendation;
      const t = new Intl.DateTimeFormat("en-US", { timeZone: c.timezone, hour: "numeric", minute: "2-digit", hour12: twelve() }).format(new Date(r.departure_at));
      facts.commute = [r.leave_now ? "Leave now" : `Leave ${t}`, `${r.duration_min} min to ${c.office_label}`];
    } else if (c) {
      const row = $("commute-body").querySelector(".state span");
      // one short line: "Next check Monday at 7:45 AM" rather than the whole explanation
      const sentences = (row?.querySelector("small")?.textContent || "").split(/(?<=\.)\s+/).filter(Boolean);
      const line = (sentences.find((x) => /^Next\b/.test(x)) || sentences[0] || "").replace(/\.$/, "");
      facts.commute = [row?.firstChild?.textContent || "No commute", line];
    }
    const m = state.meals;
    if (m?.meals?.length) {
      const { hour, minute } = zonedParts(new Date());
      const h = hour + minute / 60;
      const next = m.meals.find((x) => h < (MEAL_END[x.meal] ?? 24)) || m.meals.at(-1);
      facts.meals = [next.label, MEAL_LABEL[next.meal] || ""];
    }
    const cal = state.calendar;
    const day = cal?.days?.find((d) => d.date === cal.today && d.items.length) || cal?.days?.find((d) => d.items.length);
    if (day) {
      const it = day.items[0];
      facts.calendar = [it.title, `${day.date === cal.today ? "Today" : $("calendar-when").textContent || day.date}${day.items.length > 1 ? ` · +${day.items.length - 1} more` : ""}`];
    } else if (cal) {
      facts.calendar = ["Nothing planned", "Calendar"];
    }
    const open = (state.groceries?.items || []).filter((i) => !i.purchased).length;
    if (state.groceries) facts.groceries = [open ? String(open) : "All done", open ? "to buy" : "Nothing to buy"];
    const sh = state.smarthome;
    if (sh) {
      const n = sh.counts || {};
      const line = sh.connection?.status !== "connected" ? "Tap to connect"
        : !n.total ? "Tap to add devices" : n.offline ? `${n.offline} of ${n.total} offline` : `${n.total} device${n.total === 1 ? "" : "s"}`;
      facts.smarthome = [$("smarthome-status").textContent.trim() || "Smart home", line];
    }
    const recs = state.weather?.recommendations;
    if (recs) facts.advice = recs.length ? [recs[0].title, recs.length > 1 ? `+${recs.length - 1} more` : "Before you go"] : ["All clear", "Nothing to plan for"];
    return facts;
  }

  const GLANCE_TILES = ["commute", "meals", "calendar", "groceries", "smarthome"];

  function renderGlance() {
    const facts = glanceFacts();
    for (const id of GLANCE_TILES) facts[id] ??= document.querySelector(`#tiles [data-tile="${id}"] .glance`) ? undefined : ["—", "Loading…"];
    for (const [id, fact] of Object.entries(facts)) {
      if (!fact) continue;
      const [big, line] = fact;
      const tile = document.querySelector(`#tiles [data-tile="${id}"]`);
      if (!tile) continue;
      let g = tile.querySelector(".glance");
      if (!g) {
        g = document.createElement("div");
        g.className = "glance";
        g.innerHTML = `<b class="glance__big"></b><span class="glance__line"></span>`;
        tile.append(g);
      }
      g.querySelector(".glance__big").textContent = big;
      g.querySelector(".glance__line").textContent = line;
    }
  }

  // ---------- views: the icon rail on TVs, pills on tablets, the tab bar on phones (Settings → Views) ----------
  // A view is a named set of widgets with their order, sizes and layout. Switching views only
  // shows other tiles: every tile keeps loading as before (the status capsules use them), so a
  // switch makes no requests and adds no listeners.

  const VIEW_ICON = { home: "home", daily: "calendar", "smart-home": "bulb", prices: "coins", system: "chip" };
  const VIEW_KEY = "aura.view";            // this screen's view, for this browser session
  /** The size a widget has in the current view ("s", "w" or "l"). */
  const sizeOf = (id) => document.querySelector(`#tiles [data-tile="${id}"]`)?.dataset.size || "w";
  const SPAN = { s: 1, w: 2, l: 2 };

  function currentViews() {
    const cfg = state.config;
    if (!cfg?.views) return [];
    const off = new Set([...(cfg.home?.hidden_tiles || []), ...(cfg.meals_enabled === false ? ["meals"] : [])]);
    return cfg.views.views.map((v) => ({ ...v, tiles: v.tiles.filter((t) => !off.has(t.id)) }));
  }

  function applyHome() {
    const home = state.config?.home;
    if (!home) return;
    $("home-name").textContent = home.name;
    document.title = `AURA · ${home.name}`;
    const views = currentViews();
    if (!views.some((v) => v.id === state.view)) state.view = state.config.views.default_view;
    if (!views.some((v) => v.id === state.view)) state.view = views[0]?.id;
    const nav = $("views");
    const box = $("views-tabs");
    const shown = views.length > 1 ? views : [];
    // Rebuild the tabs only when the views changed: settings refreshes happen often, and a
    // rebuild would drop keyboard focus. Otherwise just move the selection.
    const sig = shown.map((v) => `${v.id}|${v.name}|${v.description || ""}`).join("\n");
    if (sig !== state.tabsSig) {
      state.tabsSig = sig;
      const focused = document.activeElement?.closest?.(".view-tab")?.dataset.view;
      const tabs = shown.map((v, i) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "view-tab";
        b.setAttribute("role", "tab");
        b.dataset.view = v.id;
        b.setAttribute("aria-controls", "tiles");
        if (i < 9) b.setAttribute("aria-keyshortcuts", String(i + 1));
        if (v.description) b.title = v.description;
        const ic = document.createElement("span");          // sidebar row / tab bar icon (home.css)
        ic.className = "view-tab__icon";
        ic.setAttribute("aria-hidden", "true");
        ic.innerHTML = icon(VIEW_ICON[v.id] || "home", 1.8);
        b.append(ic, v.name);
        b.addEventListener("click", () => selectView(v.id));
        return b;
      });
      const lens = document.createElement("span");
      lens.className = "view-indicator";
      lens.setAttribute("aria-hidden", "true");
      box.replaceChildren(...(tabs.length ? [lens, ...tabs] : []));
      if (focused) box.querySelector(`[data-view="${CSS.escape(focused)}"]`)?.focus({ preventScroll: true });
      state.lensSnap = true;
    }
    for (const b of box.querySelectorAll(".view-tab")) {
      b.setAttribute("aria-selected", String(b.dataset.view === state.view));
      b.tabIndex = b.dataset.view === state.view ? 0 : -1;
    }
    if (state.lensSnap) { state.lensSnap = false; placeLens(false); }
    if (!nav.querySelector(".views__edit")) {
      const edit = document.createElement("a");
      edit.className = "views__edit";
      edit.href = "/settings#views";
      edit.setAttribute("aria-label", "Edit views");
      edit.title = "Edit views";
      edit.innerHTML = icon("sliders", 1.8);
      nav.append(edit);
    }
    layoutView(views.find((v) => v.id === state.view) || { id: "none", name: "", columns: "auto", tiles: [] });
  }

  function layoutView(view) {
    const tiles = $("tiles");
    const cols = view.columns === "auto" ? 4 : Number(view.columns);
    const pos = new Map(view.tiles.map((t, i) => [t.id, { i, size: t.size }]));
    let cells = 0;
    for (const el of tiles.querySelectorAll("[data-tile]")) {
      const p = pos.get(el.dataset.tile);
      el.hidden = !p;
      if (!p) continue;
      el.dataset.size = p.size;
      el.style.order = String(p.i);
      cells += Math.min(SPAN[p.size], cols) * (p.size === "l" ? 2 : 1);
    }
    document.body.dataset.view = view.id;          // home.css: the calm Home layout
    $("view-title").textContent = view.id === "home" ? "" : view.name;
    document.body.classList.toggle("has-view-title", view.id !== "home");
    if (tiles.dataset.view !== view.id) { tiles.dataset.view = view.id; renderStatus(); }
    tiles.dataset.cols = String(cols);
    tiles.style.setProperty("--cols", String(cols));
    tiles.style.setProperty("--rows", String(Math.max(2, Math.ceil(cells / cols))));
    // Widgets that show more when large: redraw from the data already loaded (no requests),
    // after the grid has its rows (a tile's height decides how much it shows).
    const sizes = view.tiles.map((t) => `${t.id}:${t.size}`).join();
    if (sizes !== state.viewSizes) {
      state.viewSizes = sizes;
      if (state.calendar) renderAgenda(state.calendar);
      if (state.groceries) renderGroceries(state.groceries);
      if (state.smarthome) renderSmartHome(state.smarthome);
      if (state.prices) window.AuraPrices.renderTile(state.prices, sizeOf("prices"));
    }
    tiles.setAttribute("aria-label", view.name ? `${view.name} view` : "Tiles");
    const empty = $("tiles-empty");
    empty.hidden = view.tiles.length > 0;
    empty.textContent = `Nothing in ${view.name || "this view"} yet. Choose its widgets in Settings → Views.`;
  }

  /** Move the glass "lens" under the selected tab (glides between tabs; no glide on first draw). */
  function placeLens(animate = true) {
    const box = $("views-tabs"), lens = box.querySelector(".view-indicator");
    const tab = box.querySelector(".view-tab[aria-selected=true]");
    if (!lens || !tab) return;
    if (!animate) lens.style.transition = "none";
    lens.style.width = `${tab.offsetWidth}px`;
    lens.style.transform = `translateX(${tab.offsetLeft}px)`;
    if (!animate) { void lens.offsetWidth; lens.style.transition = ""; }
  }

  function selectView(id, { focus = false } = {}) {
    if (id === state.view) return;
    state.view = id;
    try { sessionStorage.setItem(VIEW_KEY, id); } catch { /* private mode */ }
    const tiles = $("tiles");
    tiles.classList.remove("is-switching");
    void tiles.offsetWidth;                         // restart the short fade
    tiles.classList.add("is-switching");
    applyHome();
    placeLens(true);                                // the lens glides to the new tab
    if (focus) $("views").querySelector(`[data-view="${CSS.escape(id)}"]`)?.focus({ preventScroll: true });
  }

  function wireViews() {
    try { state.view = sessionStorage.getItem(VIEW_KEY); } catch { /* private mode */ }
    if ("ResizeObserver" in window) new ResizeObserver(() => placeLens(false)).observe($("views-tabs"));
    document.fonts?.ready?.then(() => placeLens(false));
    $("tiles").addEventListener("animationend", (e) => { if (e.target === $("tiles")) $("tiles").classList.remove("is-switching"); });
    $("views").addEventListener("keydown", (e) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
      const tabs = [...$("views").querySelectorAll(".view-tab")];
      if (!tabs.length) return;
      const i = tabs.findIndex((t) => t.dataset.view === state.view);
      const next = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1
        : (i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      e.preventDefault();
      selectView(tabs[next].dataset.view, { focus: true });
    });
    // 1-9 switch views (not while typing, and not with a detail view or dialog open).
    document.addEventListener("keydown", (e) => {
      if (!/^[1-9]$/.test(e.key) || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.target.closest?.("input, textarea, select, [contenteditable]") || document.querySelector("dialog[open]")) return;
      const v = currentViews()[Number(e.key) - 1];
      if (v) { e.preventDefault(); selectView(v.id); }
    });
  }

  // ---------- live updates (Server-Sent Events) ----------

  function connectEvents() {
    if (!("EventSource" in window)) return;
    const es = new EventSource("/api/events");
    let connected = false;
    es.addEventListener("hello", (e) => {
      if (state.config) loadConfig();      // after a reconnect: catch server restarts quickly
      if (connected) { loadMeals(); loadCommute(); loadCalendar(); loadPanchang(); loadSmartHome(); }   // events missed while disconnected
      connected = true;
      // (Re)connected: re-sync everything we might have missed.
      try {
        const { groceries } = JSON.parse(e.data);
        if (groceries !== state.groceryRevision) loadGroceries();
      } catch { loadGroceries(); }
    });
    es.addEventListener("groceries", (e) => {
      try {
        const { revision } = JSON.parse(e.data);
        if (revision !== state.groceryRevision) loadGroceries();
      } catch { loadGroceries(); }
    });
    es.addEventListener("location", () => { loadConfig().then(() => { loadMeals(); loadPanchang(); }); });
    es.addEventListener("commute", () => { loadCommute(); });
    es.addEventListener("calendar", () => { loadCalendar(); });
    es.addEventListener("meals", () => { loadMeals(); });
    es.addEventListener("smarthome", () => { loadSmartHome(); });
    es.addEventListener("prices", () => { loadPrices(); });
    es.addEventListener("speed", () => { loadSpeed(); });
    es.addEventListener("assistant", (e) => {
      try { window.AuraAssistant.onEvent(JSON.parse(e.data)); } catch { /* ignore malformed */ }
    });
    state.events = es;
  }

  // ---------- config ----------

  function locationKey(loc) {
    return loc.status === "configured" ? `${loc.city}|${loc.region}|${loc.updated_at}` : loc.status;
  }

  async function loadConfig() {
    clearTimeout(state.configTimer);
    let next = CONFIG_POLL_MS;
    try {
      const cfg = await fetchJSON("/api/config");
      // The server was restarted (possibly updated): reload once so this screen
      // - especially the always-on entrance display - runs the current version.
      if (state.config?.build && cfg.build && cfg.build !== state.config.build) {
        location.reload();
        return;
      }
      const displayChanged = JSON.stringify(cfg.display) !== JSON.stringify(state.config?.display)
        || cfg.clock_format !== state.config?.clock_format;
      state.config = cfg;
      document.body.classList.toggle("force-reduce", cfg.display?.reduced_motion === "reduce");
      applyWallpaper(cfg.display);
      if (displayChanged && state.weatherPayload) {
        // Units, clock or animation settings changed: redraw with the new ones.
        $("wx-scene").dataset.key = "";
        loadWeather();
        Detail.refresh();
      }
      $("preview-pill").hidden = !cfg.dev_preview;
      applyHome();
      renderLocation(cfg.location);
      renderClock();
      Detail.refresh("clock");
      const key = locationKey(cfg.location);
      if (key !== state.locationKey) {
        const first = state.locationKey === null;
        state.locationKey = key;
        if (!first) loadWeather();
      }
      if (cfg.location.status === "detecting") next = DETECTING_POLL_MS;
    } catch {
      if (!state.config) {
        $("city").textContent = "Connecting…";
      }
    }
    state.configTimer = setTimeout(loadConfig, next);
  }

  // ---------- wallpaper (Settings → Display) ----------
  function applyWallpaper(d) {
    const on = d?.background === "image" && d.background_version;
    document.body.dataset.wallpaper = on ? "image" : "aura";
    const src = on ? `/api/display/background?v=${encodeURIComponent(d.background_version)}` : "";
    const dim = (d?.background_dim ?? 40) / 100;
    if (src !== state.wallSrc) {                      // only when it changes: no re-download every config poll
      state.wallSrc = src;
      document.querySelector(".ambient").style.backgroundImage = src ? `url("${src}")` : "";
      state.wallImg = null;
      delete document.body.dataset.wallReady;
      if (src) {
        const img = new Image();
        img.onload = () => { if (state.wallSrc === src) { state.wallImg = img; blurWallpaper(); } };
        img.src = src;
      }
    }
    document.body.style.setProperty("--wall-dim", String(dim));
    if (dim !== state.wallDim) { state.wallDim = dim; blurWallpaper(); }
  }

  /** One blurred, dimmed copy of the picture for the glass to show (see glass.css): drawn
      small in a canvas, matching the background's cover crop, dim and vignette. */
  function blurWallpaper() {
    const img = state.wallImg;
    if (!img) return;
    const W = 640, H = Math.max(1, Math.round(W * innerHeight / innerWidth));
    const c = document.createElement("canvas");
    c.width = W; c.height = H;
    const ctx = c.getContext("2d");
    const scale = Math.max(W / img.naturalWidth, H / img.naturalHeight) * 1.08;   // cover, with bleed for the blur edge
    const w = img.naturalWidth * scale, h = img.naturalHeight * scale;
    ctx.filter = `blur(${Math.round(18 * W / innerWidth * 1.6)}px) saturate(160%)`;
    ctx.drawImage(img, (W - w) / 2, (H - h) / 2, w, h);
    ctx.filter = "none";
    ctx.fillStyle = `rgba(4, 6, 12, ${state.wallDim ?? 0.4})`;
    ctx.fillRect(0, 0, W, H);
    const v = ctx.createRadialGradient(W / 2, H * 1.2, 0, W / 2, H * 1.2, H * 0.75);
    v.addColorStop(0, "rgba(0, 0, 0, 0.45)"); v.addColorStop(1, "rgba(0, 0, 0, 0)");
    ctx.fillStyle = v; ctx.fillRect(0, 0, W, H);
    document.documentElement.style.setProperty("--wall-blur", `url("${c.toDataURL("image/jpeg", 0.86)}")`);   // on :root, where --wall-layer is defined
    document.body.dataset.wallReady = "";
  }
  let wallResize = null;
  addEventListener("resize", () => { clearTimeout(wallResize); wallResize = setTimeout(blurWallpaper, 250); });

  // ---------- burn-in protection ----------

  function shiftPixels() {
    const dx = Math.round(Math.random() * 6 - 3);
    const dy = Math.round(Math.random() * 6 - 3);
    $("shell").style.transform = `translate(${dx}px, ${dy}px)`;
  }

  // ---------- boot ----------

  /** A time of day in the home time zone and the household's 12/24-hour setting. */
  function clockTime(date, { seconds = false } = {}) {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: tz(), hour: "numeric", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}), hour12: twelve(),
    }).format(date);
  }

  // The Pi's own screen opens /?kiosk=1 (see deploy/kiosk): hide the mouse pointer there.
  if (new URLSearchParams(location.search).has("kiosk")) document.body.classList.add("kiosk");
  for (const el of document.querySelectorAll("[data-icon]")) el.innerHTML = icon(el.dataset.icon, 1.7);
  wireViews();
  startClock();
  wireDialog();
  Detail.init({
    config: () => state.config,
    weather: () => state.weatherPayload,
    commute: () => state.commute,
    calendar: () => state.calendar,
    protection: () => state.protection,
    system: () => state.system,
    network: () => state.network,
    reloadProtection: () => loadProtection(),
    reloadMeals: () => loadMeals(),
    panchang: () => state.panchang,
    fetchPanchang: (isoDate) => fetchJSON(`/api/panchang?date=${encodeURIComponent(isoDate)}`),
    calendarTime: (item, tzName) => calTime(item, tzName),
    calendarDay: (isoDate, today) => dayLabel(isoDate, today),
    icon,
    weatherArt,
    temp: fmtTemp,
    formatHour: fmtHour,
    renderScene: (el, visual) => Scene.render(el,
      visual && state.config?.display && !state.config.display.weather_animations && visual.motion === "live"
        ? { ...visual, motion: "static", reason: "off" } : visual),
    formatTime: clockTime,
    formatTimeIn: (tzName, date) => new Intl.DateTimeFormat("en-US", {
      timeZone: tzName, hour: "numeric", minute: "2-digit", hour12: twelve(),
    }).format(date),
    dayName: (isoDate) => {
      const today = new Intl.DateTimeFormat("en-CA", { timeZone: tz() }).format(new Date());
      if (isoDate === today) return "Today";
      return new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "short", timeZone: "UTC" })
        .format(new Date(`${isoDate}T12:00:00Z`));
    },
  });
  window.AuraMeals.init({ icon });
  window.AuraSmartHome.init({ icon, formatTime: clockTime });
  window.AuraPrices.init({ icon, formatTime: clockTime });
  window.AuraAssistant.init({ icon, formatTime: clockTime, openView: () => { if (!Detail.isOpen() || $("detail").dataset.view !== "assistant") Detail.open("assistant"); } });
  Scene.watch($("wx-scene"), () => state.weather ? window.AuraSky.visualFor(state.weather, window.AuraSky.picture(state.weather).day) : null);
  loadConfig().then(async () => {
    loadWeather();
    await loadSession();
    loadGroceries();
    loadCommute();
    loadCalendar();
    loadPanchang();
    loadPrices();
    loadProtection();
    loadSpeed();
    loadSmartHome();
    loadSystem();
    loadMeals();
    refreshQr();
    connectEvents();
  });
  // Re-evaluate every minute: past slots and window changes depend on the clock.
  setInterval(loadCommute, 60_000);
  setInterval(loadCalendar, 10 * 60_000);    // also rolls "Today" over after midnight
  setInterval(loadWeather, WEATHER_POLL_MS);
  // Fallback only: live updates normally arrive via Server-Sent Events.
  setInterval(() => {
    if (!state.events || state.events.readyState !== EventSource.OPEN) loadGroceries();
  }, GROCERY_POLL_MS);
  setInterval(refreshQr, QR_REFRESH_MS);
  setInterval(loadProtection, 60_000);
  setInterval(loadSpeed, 5 * 60_000);        // fallback only: new readings arrive as "speed" events
  setInterval(loadPrices, 30 * 60_000);      // the Pi itself fetches once a day; this just picks it up (and marks it stale)
  // Fallback only: device changes normally arrive as "smarthome" events.
  setInterval(() => { if (!state.events || state.events.readyState !== EventSource.OPEN) loadSmartHome(); }, 60_000);
  setInterval(loadMeals, 5 * 60_000);
  setInterval(loadSystem, 60_000);
  setInterval(shiftPixels, BURN_IN_SHIFT_MS);
})();
