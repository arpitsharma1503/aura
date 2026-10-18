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
    $("clock-ampm").textContent = parts.find((p) => p.type === "dayPeriod")?.value ?? "";
    $("date").textContent = new Intl.DateTimeFormat("en-GB", {
      timeZone: tz(), weekday: "long", day: "numeric", month: "long",
    }).format(now);
    document.body.dataset.phase = phaseFor(now);
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

    const isDay = payload.visual ? payload.visual.is_day : c.is_day;
    $("weather-art").innerHTML = weatherArt(c.condition, isDay);
    renderScene(stale && payload.visual ? { ...payload.visual, motion: "static" } : payload.visual);
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
      Detail.refresh();
      if ((payload.status === "ok" || payload.status === "stale") && payload.data) {
        state.weather = payload;
        state.weatherReceivedAt = Date.now();
        renderWeather(payload);
        renderAdvice(payload);
        return;
      }
      state.weather = null;
      if (payload.reason === "no_location") {
        showWeatherMessage("unavailable", "Set your home location",
          "Weather appears once AURA knows which city your home is in. Use the Location button under the clock.", "pin");
      } else if (payload.reason === "disabled") {
        showWeatherMessage("unavailable", "Weather is turned off",
          "Set AURA_WEATHER_PROVIDER=open_meteo in .env to enable live weather.");
      } else {
        showWeatherMessage("error", "Weather couldn't be loaded",
          `${payload.message || "The weather service returned an error"}. Retrying automatically.`);
      }
      renderAdvice(null, payload.reason === "no_location" ? "no_location" : payload.status);
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
    // The wide entrance layout has room for a single column of 5.
    const max = matchMedia("(min-width: 1200px) and (min-aspect-ratio: 4/3)").matches ? 5 : MAX_GROCERIES;
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
      Detail.refresh("commute");
    } catch {
      if ($("commute").dataset.state === "loading") {
        $("commute-body").replaceChildren(stateRow("alert", "Couldn't load commute", "Retrying automatically.", "state--error"));
        $("commute").dataset.state = "error";
      }
    }
  }

  // ---------- live updates (Server-Sent Events) ----------

  function connectEvents() {
    if (!("EventSource" in window)) return;
    const es = new EventSource("/api/events");
    es.addEventListener("hello", (e) => {
      if (state.config) loadConfig();      // after a reconnect: catch server restarts quickly
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
    es.addEventListener("location", () => { loadConfig(); });
    es.addEventListener("commute", () => { loadCommute(); });
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
      if (displayChanged && state.weatherPayload) {
        // Units, clock or animation settings changed: redraw with the new ones.
        $("wx-scene").dataset.key = "";
        loadWeather();
        Detail.refresh();
      }
      $("preview-pill").hidden = !cfg.dev_preview;
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

  // ---------- burn-in protection ----------

  function shiftPixels() {
    const dx = Math.round(Math.random() * 6 - 3);
    const dy = Math.round(Math.random() * 6 - 3);
    $("shell").style.transform = `translate(${dx}px, ${dy}px)`;
  }

  // ---------- boot ----------

  // The Pi's own screen opens /?kiosk=1 (see deploy/kiosk): hide the mouse pointer there.
  if (new URLSearchParams(location.search).has("kiosk")) document.body.classList.add("kiosk");
  startClock();
  wireDialog();
  Detail.init({
    config: () => state.config,
    weather: () => state.weatherPayload,
    commute: () => state.commute,
    icon,
    weatherArt,
    temp: fmtTemp,
    formatHour: fmtHour,
    renderScene: (el, visual) => Scene.render(el,
      visual && state.config?.display && !state.config.display.weather_animations && visual.motion === "live"
        ? { ...visual, motion: "static", reason: "off" } : visual),
    formatTime: (date, { seconds = false } = {}) => new Intl.DateTimeFormat("en-US", {
      timeZone: tz(), hour: "numeric", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}), hour12: twelve(),
    }).format(date),
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
  Scene.watch($("wx-scene"), () => state.weather?.visual || null);
  loadConfig().then(async () => {
    loadWeather();
    await loadSession();
    loadGroceries();
    loadCommute();
    refreshQr();
    connectEvents();
  });
  // Re-evaluate every minute: past slots and window changes depend on the clock.
  setInterval(loadCommute, 60_000);
  setInterval(loadWeather, WEATHER_POLL_MS);
  // Fallback only: live updates normally arrive via Server-Sent Events.
  setInterval(() => {
    if (!state.events || state.events.readyState !== EventSource.OPEN) loadGroceries();
  }, GROCERY_POLL_MS);
  setInterval(refreshQr, QR_REFRESH_MS);
  setInterval(shiftPixels, BURN_IN_SHIFT_MS);
})();
