/* AURA tile detail views.
   - Tiles declare `data-open="weather"` (or "weather#hourly" for a section).
   - One delegated click listener opens them, so a tap can never fire twice and
     real controls inside a tile (buttons, links, inputs) keep their own action.
   - Each view is a history entry (#/weather/hourly): browser Back closes it.
   - Views render only data the app already has; missing data gets an honest
     empty/unavailable state. Nothing is invented. */
"use strict";

window.AuraDetail = (() => {
  let ctx = null;              // provided by app.js: data getters + formatters
  let current = null;          // { view, section }
  let pushed = false;          // did we add a history entry for the open view?
  let returnFocus = null;
  let ticker = null;

  const $ = (id) => document.getElementById(id);
  const VIEWS = {
    clock: { title: "Time & date", render: renderClock },
    weather: { title: "Weather", render: renderWeather },
    advice: { title: "Before you go", render: renderAdvice },
    commute: { title: "Commute", render: renderCommute },
    assistant: { title: "Ask AURA", render: () => { const v = window.AuraAssistant.view(); window.AuraAssistant.paintView(); return v; } },
    calendar: { title: "Calendar", render: renderCalendar },
    protection: { title: "Network protection", render: renderProtection },
    system: { title: "System", render: renderSystem },
    meals: { title: "Meal plan", render: () => window.AuraMeals.view() },
  };

  // ---------- tiny DOM helper (text only; never innerHTML with data) ----------
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "html") el.innerHTML = v;       // only for our own SVG icons
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const section = (id, title, ...kids) =>
    h("section", { class: "dsec", id: `dsec-${id}`, "aria-labelledby": `dsec-${id}-t` },
      h("h3", { class: "dsec__title", id: `dsec-${id}-t` }, title), ...kids);
  const note = (text, cls = "") => h("p", { class: `dnote ${cls}` }, text);
  const metric = (label, value, sub) =>
    h("div", { class: "dmetric" }, h("dt", {}, label), h("dd", {}, value ?? "—", sub ? h("small", {}, sub) : null));

  // ---------- open / close with history ----------

  function parseHash(hash) {
    const m = /^#\/(clock|weather|advice|commute|assistant|calendar|protection|system|meals)(?:\/([a-z]+))?$/.exec(hash || "");
    return m ? { view: m[1], section: m[2] || null } : null;
  }

  function open(view, sectionId = null, { fromHistory = false } = {}) {
    if (!VIEWS[view]) return;
    const dlg = $("detail");
    if (!dlg.open) returnFocus = document.activeElement;
    current = { view, section: sectionId };
    const url = `#/${view}${sectionId ? `/${sectionId}` : ""}`;
    if (!fromHistory) {
      if (dlg.open && pushed) history.replaceState({ auraDetail: current }, "", url);
      else { history.pushState({ auraDetail: current }, "", url); pushed = true; }
    }
    render();
    if (!dlg.open) dlg.showModal();
    $("detail-back").focus({ preventScroll: true });
    scrollToSection();
    startTicker();
  }

  function scrollToSection() {
    const body = $("detail-body");
    const target = current?.section ? $(`dsec-${current.section}`) : null;
    if (target) {
      body.scrollTop = target.offsetTop - body.offsetTop - 8;
    } else {
      body.scrollTop = 0;
    }
  }

  /** User asked to go back (Back button, Esc, backdrop). */
  function requestClose() {
    if (pushed) history.back();          // popstate will close the dialog
    else close({ fromHistory: false });
  }

  function close({ fromHistory = true } = {}) {
    const dlg = $("detail");
    stopTicker();
    current = null;
    if (dlg.open) dlg.close();
    if (!fromHistory) {
      // Opened from a deep link (no entry of ours to go back to): clean the URL.
      history.replaceState(null, "", location.pathname + location.search);
    }
    pushed = false;
    if (returnFocus && document.contains(returnFocus)) returnFocus.focus({ preventScroll: true });
    returnFocus = null;
  }

  function onPopState(e) {
    const st = e.state?.auraDetail || parseHash(location.hash);
    if (st && VIEWS[st.view]) open(st.view, st.section, { fromHistory: true });
    else if ($("detail").open) close();
  }

  // ---------- rendering ----------

  function render() {
    if (!current) return;
    const v = VIEWS[current.view];
    $("detail-title").textContent = v.title;
    $("detail-sub").textContent = "";
    const body = $("detail-body");
    const keep = body.scrollTop;
    body.replaceChildren(...[].concat(v.render() || []).filter(Boolean));
    body.scrollTop = keep;
    $("detail").dataset.view = current.view;
  }

  /** Called by app.js whenever data refreshes, so an open view stays current. */
  function refresh(view) {
    if (current && (!view || current.view === view)) render();
  }

  function startTicker() {
    stopTicker();
    if (current?.view === "protection") ctx.reloadProtection?.();
    if (current?.view !== "clock") return;
    ticker = setInterval(() => {
      const el = $("dclock-time");
      if (el) el.textContent = ctx.formatTime(new Date(), { seconds: true });
    }, 1000);
  }
  function stopTicker() { clearInterval(ticker); ticker = null; }

  // ----- clock -----
  function renderClock() {
    const cfg = ctx.config();
    const tz = cfg?.timezone;
    const now = new Date();
    const loc = cfg?.location;
    const deviceTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const offset = tz ? new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "longOffset" })
      .formatToParts(now).find((p) => p.type === "timeZoneName")?.value : null;
    $("detail-sub").textContent = loc?.status === "configured" ? [loc.city, loc.region, loc.country].filter(Boolean).join(", ") : "";

    const out = [
      h("div", { class: "dclock" },
        h("div", { class: "dclock__time", id: "dclock-time" }, ctx.formatTime(now, { seconds: true })),
        h("div", { class: "dclock__date" }, new Intl.DateTimeFormat("en-GB", {
          timeZone: tz, weekday: "long", day: "numeric", month: "long", year: "numeric" }).format(now))),
    ];
    const rows = [metric("Time zone", tz || "—", offset ? offset.replace("GMT", "UTC") : null)];
    if (loc?.status === "configured") rows.push(metric("Home location", loc.city, loc.label));
    if (tz && deviceTz && deviceTz !== tz) {
      rows.push(metric("This device", new Intl.DateTimeFormat("en-US", {
        timeZone: deviceTz, hour: "numeric", minute: "2-digit" }).format(now), deviceTz));
    }
    const w = ctx.weather()?.data;
    if (w?.sunrise && w?.sunset) {
      const len = (new Date(w.sunset) - new Date(w.sunrise)) / 60000;
      rows.push(metric("Sunrise", ctx.formatTime(new Date(w.sunrise))));
      rows.push(metric("Sunset", ctx.formatTime(new Date(w.sunset))));
      rows.push(metric("Daylight", `${Math.floor(len / 60)} h ${Math.round(len % 60)} min`));
    }
    out.push(section("time", "Details", h("dl", { class: "dmetrics" }, rows)));
    return out;
  }

  // ----- calendar (inside the clock view) -----
  const CAL_ICON = { birthday: "cake", event: "calendar", holiday: "star", observance: "dot" };
  const CAL_KIND = { birthday: "Birthday", event: "Event", holiday: "Public holiday", observance: "Observance" };

  function calendarList() {
    const cal = ctx.calendar();
    if (!cal) return note("Loading calendars…");
    if (cal.status === "unconfigured") {
      return h("div", { class: "dempty" }, h("span", { html: ctx.icon("info") }),
        h("div", {}, h("strong", {}, "No calendar connected"),
          h("p", {}, "Add calendar addresses to AURA_CALENDAR_FEEDS in .env on the Pi (see README → Calendars).")));
    }
    const parts = [];
    if (!cal.days.length) {
      parts.push(note(cal.status === "error" ? "Calendars couldn't be reached right now." :
        `Nothing in the next ${cal.days_ahead || 30} days.`));
    }
    const fmtRange = (it) => {
      if (it.first_day === it.last_day) return null;
      const end = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" })
        .format(new Date(`${it.last_day}T12:00:00Z`));
      return `until ${end}`;
    };
    parts.push(h("ol", { class: "dcal" }, cal.days.map((d) => h("li", { class: "dcal__day" },
      h("h4", {}, ctx.calendarDay(d.date, cal.today)),
      h("ul", { class: "dcal__items" }, d.items.map((it) => h("li", { class: "dcal__item", "data-kind": it.kind },
        h("span", { html: ctx.icon(CAL_ICON[it.kind]) }),
        h("span", { class: "dcal__title" }, it.title),
        h("span", { class: "dcal__when" }, it.all_day ? "All day" : ctx.calendarTime(it, cal.timezone)),
        h("span", { class: "dcal__sub" }, [CAL_KIND[it.kind], fmtRange(it), it.calendars.join(" · ")].filter(Boolean).join(" · ")))))))));
    const feeds = (cal.feeds || []).map((f) => `${f.label}: ${f.ok ? "up to date" : (f.error || "not loaded")}`).join(" · ");
    if (cal.message) parts.push(note(cal.message, "dnote--warn"));
    if (feeds) parts.push(note(`Calendars — ${feeds}`, "dnote--muted"));
    return parts;
  }

  function renderCalendar() {
    const cal = ctx.calendar();
    const feeds = cal?.feeds || [];
    $("detail-sub").textContent = feeds.length ? feeds.map((f) => f.label).join(" · ") : "";
    return [section("calendar", "Events, birthdays & holidays", calendarList())];
  }

  // ----- network protection (read-only) -----
  function renderProtection() {
    const d = ctx.protection();
    if (!d) return [note("Checking network protection…")];
    const svc = { running: "Running", stopped: "Stopped", failed: "Failed", not_installed: "Not installed", unknown: "Unknown" }[d.service];
    const yes = (v, a, b) => (v == null ? "—" : v ? a : b);
    $("detail-sub").textContent = d.pi_address ? `Pi-hole on ${d.pi_address}` : "Pi-hole";
    const out = [];
    if (!d.installed) {
      out.push(h("div", { class: "dempty" }, h("span", { html: ctx.icon("info") }),
        h("div", {}, h("strong", {}, "Pi-hole isn't installed"),
          h("p", {}, "Network-wide ad blocking isn't set up on this Pi. See README → Network protection."))));
      return out;
    }
    out.push(section("health", "Service health", h("dl", { class: "dmetrics" },
      metric("Pi-hole service", svc),
      metric("DNS answers", yes(d.dns_ok, "Working", "Not answering"), "test lookup of example.com"),
      metric("Ad blocking", yes(d.blocking_ok, "Working", "Not blocking"), "test domain doubleclick.net"),
      metric("Last healthy check", d.last_ok_at ? ctx.formatTime(new Date(d.last_ok_at)) : "—",
        `checked ${ctx.formatTime(new Date(d.checked_at))}`)),
      d.problems.length ? h("ul", { class: "dproblems" }, d.problems.map((t) => h("li", {}, t))) : null));
    const routerDns = d.router_dns.join(", ") || "unknown";
    out.push(section("coverage", "Which devices are protected", h("dl", { class: "dmetrics" },
      metric("Router gives out DNS", routerDns, d.router_uses_pihole ? "includes this Pi" : "not this Pi")),
      note(d.router_uses_pihole
        ? "Devices that take their settings from the router use Pi-hole. Devices with encrypted or fixed DNS (iCloud Private Relay, Android Private DNS, some TVs) bypass it, and DNS filtering can't remove ads served from the same site (most YouTube ads)."
        : `Not network-wide: only devices you have set to use ${d.pi_address || "this Pi"} for DNS are filtered. Pi-hole running alone doesn't protect a device.`,
        d.router_uses_pihole ? "dnote--muted" : "dnote--warn")));
    if (d.stats) {
      const n = (x) => Number(x).toLocaleString("en-IN");
      out.push(section("stats", "Last 24 hours", h("dl", { class: "dmetrics" },
        metric("DNS lookups", n(d.stats.total_queries), d.stats.active_clients != null ? `${d.stats.active_clients} active devices` : null),
        metric("Blocked", `${n(d.stats.blocked_queries)} (${d.stats.percent_blocked}%)`),
        metric("Domains on blocklists", d.stats.domains_on_blocklists != null ? n(d.stats.domains_on_blocklists) : "—"),
        metric("Blocking", d.stats.blocking_enabled == null ? "—" : d.stats.blocking_enabled ? "Enabled" : "Paused"))));
    } else if (d.stats_note) {
      out.push(section("stats", "Statistics", note(d.stats_note, "dnote--muted")));
    }
    out.push(section("manage", "Manage", note("Blocklists, the query log and allowed domains are managed in Pi-hole's own admin page, which asks for its own password."),
      h("p", {}, h("a", { class: "btn", href: `http://${location.hostname}/admin/`, target: "_blank", rel: "noopener" }, "Open Pi-hole admin"))));
    return out;
  }

  // ----- system -----
  function renderSystem() {
    const s = ctx.system();
    const n = ctx.network();
    if (!s) return [note("Reading system status…")];
    $("detail-sub").textContent = `${s.hostname}${s.model ? ` · ${s.model}` : ""}`;
    const pct = (a, b) => (b ? `${Math.round(100 * (1 - a / b))}% used` : "—");
    const up = s.system_uptime_s != null ? (s.system_uptime_s >= 86400 ? `${Math.floor(s.system_uptime_s / 86400)} d ${Math.floor((s.system_uptime_s % 86400) / 3600)} h`
      : `${Math.floor(s.system_uptime_s / 3600)} h ${Math.floor((s.system_uptime_s % 3600) / 60)} min`) : "—";
    const out = [section("health", "Raspberry Pi", h("dl", { class: "dmetrics" },
      metric("CPU temperature", s.cpu_temperature_c != null ? `${s.cpu_temperature_c.toFixed(1)}°C` : "—", s.cpu_temperature_c >= 75 ? "hot: check ventilation" : null),
      metric("CPU load (1 min)", s.load_1m != null ? s.load_1m.toFixed(2) : "—", "4 cores"),
      metric("Memory", pct(s.memory_available_mb, s.memory_total_mb), s.memory_available_mb != null ? `${(s.memory_available_mb / 1024).toFixed(1)} GB available` : null),
      metric("Storage", s.disk_free_gb != null ? `${s.disk_free_gb.toFixed(1)} GB free` : "—", s.disk_total_gb ? `of ${s.disk_total_gb.toFixed(0)} GB` : null),
      metric("Uptime", up),
      metric("System", s.os, s.kernel)))];
    if (n) {
      out.push(section("network", "Network", h("dl", { class: "dmetrics" },
        metric("Internet", n.connectivity === "full" ? "Connected" : (n.connectivity || "Unknown")),
        metric("Connection", n.wifi_connected ? `Wi-Fi · ${n.wifi_ssid || ""}` : (n.interfaces.some((x) => x.type === "ethernet" && x.state === "connected") ? "Ethernet" : "—")),
        metric("Address", n.lan_ip || "—", `${n.hostname}.local`))));
    }
    out.push(section("services", "AURA", h("dl", { class: "dmetrics" },
      metric("Dashboard", "Running", `version ${s.app_version}`),
      metric("Running since", ctx.formatTime(new Date(s.app_started_at))))),
      note("Wi-Fi, locations and other preferences are in Settings.", "dnote--muted"));
    return out;
  }

  // ----- weather -----
  function weatherStatusLine(p) {
    const w = p.data;
    const parts = [];
    if (p.is_mock) parts.push(h("span", { class: "dtag dtag--preview" }, "Sample data · not live weather"));
    if (p.status === "stale") parts.push(h("span", { class: "dtag dtag--warn" }, `Weather service unavailable · showing data from ${ctx.formatTime(new Date(w.fetched_at))}`));
    else parts.push(h("span", {}, `Updated ${ctx.formatTime(new Date(w.fetched_at))}`));
    parts.push(h("span", {}, w.attribution || w.source));
    return h("p", { class: "dstatus" }, parts);
  }

  // ----- sun & moon -----
  const hm = (min) => { min = Math.max(0, Math.round(min)); const hh = Math.floor(min / 60), mm = min % 60;
    return hh ? `${hh} h${mm ? ` ${mm} min` : ""}` : `${mm} min`; };

  function sunCard(a) {
    const s = a.sun;
    if (!s) {
      return h("div", { class: "dsky__card dsky__sun", id: "dsky-sun" },
        h("h4", { class: "dsky__title" }, "Sun"),
        note("Sunrise and sunset appear when weather data is available.", "dnote--muted"));
    }
    const now = new Date(a.computed_at);
    const rise = new Date(s.sunrise), set = new Date(s.sunset);
    const t = (d) => ctx.formatTime(d);
    const status = s.status === "before_sunrise" ? ["Before sunrise", `Sunrise in ${hm((rise - now) / 60000)}`]
      : s.status === "day" ? ["Daytime", `${hm((set - now) / 60000)} of daylight left`]
      : ["After sunset", s.next_sunrise ? `Sunrise tomorrow ${t(new Date(s.next_sunrise))}` : `Sun set at ${t(set)}`];
    return h("div", { class: "dsky__card dsky__sun", id: "dsky-sun", "data-status": s.status },
      h("h4", { class: "dsky__title" }, "Sun"),
      h("p", { class: "dsky__status" }, h("strong", {}, status[0]), ` · ${status[1]}`),
      h("div", { class: "dsky__arc", html: window.AuraSky.sunArc(s) }),
      h("div", { class: "dsky__ends" }, h("span", {}, "Sunrise ", h("b", {}, t(rise))), h("span", {}, "Sunset ", h("b", {}, t(set)))),
      h("dl", { class: "dmetrics dmetrics--tight" }, metric("Daylight", hm(s.daylight_minutes))));
  }

  function moonCard(a) {
    const m = a.moon;
    const day = (iso) => new Intl.DateTimeFormat("en-GB", { timeZone: a.timezone, weekday: "short", day: "numeric", month: "short" }).format(new Date(iso));
    return h("div", { class: "dsky__card dsky__moon", id: "dsky-moon", "data-phase": m.phase_name },
      h("h4", { class: "dsky__title" }, "Moon"),
      h("div", { class: "dsky__moonrow" },
        h("div", { class: "dsky__disc", html: window.AuraSky.moon(m) }),
        h("div", { class: "dsky__moontext" },
          h("div", { class: "dsky__phase" }, m.phase_name),
          h("dl", { class: "dmetrics dmetrics--tight" },
            metric("Illumination", `${m.illumination_pct}%`),
            metric("Age", `${m.age_days.toFixed(1)} days`, m.waxing ? "waxing" : "waning")))),
      h("ol", { class: "dsky__next" }, m.next_phases.map((e) =>
        h("li", {}, h("span", {}, e.name), h("b", {}, day(e.at))))));
  }

  function skySection(p) {
    const a = p?.astronomy;
    if (!a) return null;
    return section("astro", "Sun & Moon",
      h("div", { class: "dsky" }, sunCard(a), moonCard(a)),
      note(`Moon computed on the Pi (Meeus, Astronomical Algorithms) and drawn as seen from the ${a.moon.hemisphere}ern hemisphere; the real crescent tilts with the hour.`
        + (a.sun ? ` Sunrise and sunset: ${a.sun.source}.` : ""), "dnote--muted"));
  }

  function weatherUnavailable(p) {
    const title = p?.reason === "no_location" ? "Set your home location"
      : p?.reason === "disabled" ? "Weather is turned off"
      : p ? "Weather couldn't be loaded" : "Loading weather…";
    const text = p?.message || (p ? "" : "Fetching the latest data.");
    return [h("div", { class: "dempty" }, h("span", { html: ctx.icon(p ? "cloudOff" : "info") }),
      h("div", {}, h("strong", {}, title), text ? h("p", {}, text) : null)), skySection(p)];
  }

  function renderWeather() {
    const p = ctx.weather();
    if (!p || !p.data) return weatherUnavailable(p);
    const w = p.data, c = w.current;
    const t = (iso) => ctx.formatTime(new Date(iso));
    $("detail-sub").textContent = w.city;

    const scene = h("div", { class: "wx-scene dhero__scene", "aria-hidden": "true" });
    ctx.renderScene(scene, p.status === "stale" && p.visual ? { ...p.visual, motion: "static" } : p.visual);
    const hero = h("div", { class: "dhero" }, scene,
      h("div", { class: "dhero__art", html: ctx.weatherArt(c.condition, p.visual ? p.visual.is_day : c.is_day) }),
      h("div", { class: "dhero__text" },
        h("div", { class: "dhero__temp" }, ctx.temp(c.temperature_c)),
        h("div", { class: "dhero__desc" }, c.description),
        h("div", { class: "dhero__sub" }, `Feels like ${ctx.temp(c.feels_like_c)} · H ${ctx.temp(w.high_c)} · L ${ctx.temp(w.low_c)}`)));

    const aq = w.air_quality;
    const uvMax = w.daily?.[0]?.uv_index_max;
    const metrics = [
      metric("Feels like", ctx.temp(c.feels_like_c)),
      metric("Humidity", `${c.humidity_pct}%`),
      metric("Wind", `${Math.round(c.wind_kph)} km/h`, c.wind_gust_kph != null ? `gusts ${Math.round(c.wind_gust_kph)} km/h` : null),
      metric("UV index", c.uv_index != null ? String(Math.round(c.uv_index)) : null, uvMax != null ? `max today ${Math.round(uvMax)}` : null),
      metric("Rain chance", c.precipitation_chance_pct != null ? `${c.precipitation_chance_pct}%` : null, "this hour"),
      metric("Precipitation", `${c.precipitation_mm.toFixed(1)} mm`, "now"),
      metric("Air quality", aq ? `${aq.us_aqi} US AQI` : "Not available", aq ? `${aq.category}${aq.pm2_5 != null ? ` · PM2.5 ${aq.pm2_5} µg/m³` : ""}` : null),
      metric("Sunrise", w.sunrise ? t(w.sunrise) : null),
      metric("Sunset", w.sunset ? t(w.sunset) : null),
    ];

    const hourly = h("div", { class: "dtable-wrap" }, h("table", { class: "dtable" },
      h("thead", {}, h("tr", {}, ["Time", "", "Temp", "Feels", "Rain", "mm", "Wind", "UV"].map((x) => h("th", { scope: "col" }, x)))),
      h("tbody", {}, w.hourly.slice(0, 24).map((hr) => h("tr", {},
        h("th", { scope: "row" }, ctx.formatHour(hr.time)),
        h("td", { class: "dtable__icon", html: ctx.weatherArt(hr.condition, hr.is_day) }),
        h("td", {}, ctx.temp(hr.temperature_c)),
        h("td", {}, hr.feels_like_c != null ? ctx.temp(hr.feels_like_c) : "—"),
        h("td", {}, hr.precipitation_chance_pct != null ? `${hr.precipitation_chance_pct}%` : "—"),
        h("td", {}, hr.precipitation_mm != null ? hr.precipitation_mm.toFixed(1) : "—"),
        h("td", {}, hr.wind_kph != null ? `${Math.round(hr.wind_kph)}` : "—"),
        h("td", {}, hr.uv_index != null && hr.is_day ? String(Math.round(hr.uv_index)) : "—"))))));

    const daily = h("ol", { class: "ddays" }, (w.daily || []).map((d) => h("li", { class: "dday" },
      h("span", { class: "dday__name" }, ctx.dayName(d.date)),
      h("span", { class: "dday__icon", html: ctx.weatherArt(d.condition, true) }),
      h("span", { class: "dday__desc" }, d.description),
      h("span", { class: "dday__temps" }, ctx.temp(d.high_c), h("span", {}, ctx.temp(d.low_c))),
      h("span", { class: "dday__extra" },
        [d.precipitation_chance_pct != null ? `${d.precipitation_chance_pct}% rain` : null,
         d.precipitation_mm != null ? `${d.precipitation_mm.toFixed(1)} mm` : null,
         d.uv_index_max != null ? `UV ${Math.round(d.uv_index_max)}` : null,
         d.sunrise && d.sunset ? `${t(d.sunrise)}–${t(d.sunset)}` : null].filter(Boolean).join(" · ")))));

    return [
      hero, weatherStatusLine(p),
      section("metrics", "Right now", h("dl", { class: "dmetrics" }, metrics)),
      skySection(p),
      section("hourly", "Next 24 hours", hourly),
      section("daily", `${(w.daily || []).length}-day forecast`, daily.children.length ? daily : note("No daily forecast available.")),
      section("advice", "Before you go", adviceList(p)),
    ];
  }

  // ----- advice -----
  const PRIORITY = { high: "Important", medium: "Worth knowing", low: "Nice to know" };

  function adviceList(p) {
    const recs = p?.recommendations_all?.length ? p.recommendations_all : (p?.recommendations || []);
    if (!p?.data || !recs.length) {
      return h("div", { class: "dempty" }, h("span", { html: ctx.icon("info") }),
        h("div", {}, h("strong", {}, "No advice yet"),
          h("p", {}, p?.reason === "no_location" ? "Set your home location to get advice based on its weather."
            : "Suggestions are based on live weather and will appear once it's available.")));
    }
    return h("ul", { class: "dadvice" }, recs.map((r) => h("li", { class: "dadvice__item", "data-priority": r.priority },
      h("span", { class: "advice__icon", html: ctx.icon(r.icon) }),
      h("div", {}, h("div", { class: "dadvice__title" }, r.title, h("span", { class: `dprio dprio--${r.priority}` }, PRIORITY[r.priority])),
        h("p", { class: "dadvice__detail" }, r.detail)))));
  }

  function renderAdvice() {
    const p = ctx.weather();
    const out = [adviceList(p)];
    if (p?.data) {
      $("detail-sub").textContent = p.data.city;
      out.unshift(note(`Based on the forecast for the next ${p.travel_window_hours || 6} hours in ${p.data.city}, not just the current conditions.`));
      out.push(weatherStatusLine(p));
      out.push(note("Bike and car suggestions use weather only; AURA doesn't check road traffic for these tips. See the Commute card for traffic.", "dnote--muted"));
    }
    return out;
  }

  // ----- commute -----
  const TRAFFIC = { light: "Light", moderate: "Moderate", heavy: "Heavy", severe: "Severe" };
  const BASIS = {
    live: ["Live traffic", "TomTom's current traffic speeds and incidents, for leaving now."],
    forecast: ["Forecast", "TomTom's prediction for a future departure, from historical traffic patterns."],
    historical: ["Your history", "Median of AURA's own past live checks for this weekday and time (needs at least 3)."],
  };

  function renderCommute() {
    const c = ctx.commute();
    if (!c) return [note("Loading commute…")];
    const t = (iso) => ctx.formatTimeIn(c.timezone, new Date(iso));
    $("detail-sub").textContent = `${c.home_label} → ${c.office_label}`;
    const out = [];

    if (c.state === "unconfigured" || c.state === "disabled") {
      out.push(h("div", { class: "dempty" }, h("span", { html: ctx.icon("car") }),
        h("div", {}, h("strong", {}, c.state === "disabled" ? "Commute monitoring is off" : "Traffic isn't connected yet"),
          h("p", {}, c.message))));
      out.push(note("No journey times are shown without a traffic provider; AURA never estimates them on its own."));
      return out;
    }

    out.push(h("p", { class: "dstatus" }, h("span", {}, c.message)));
    const rec = c.recommendation;
    if (rec) {
      const [blabel] = BASIS[rec.basis] || [rec.basis_label];
      out.push(h("div", { class: "drec" },
        h("div", { class: "drec__label" }, rec.basis === "historical" ? "Usually best to leave" : "Best time to leave"),
        h("div", { class: "drec__time" }, rec.leave_now ? "Now" : t(rec.departure_at)),
        h("dl", { class: "dmetrics dmetrics--tight" },
          metric("Journey", `${rec.duration_min} min`, rec.no_traffic_min ? `${rec.no_traffic_min} min with no traffic` : null),
          metric("Arrive", t(rec.arrival_at)),
          metric("Traffic", rec.traffic_level ? TRAFFIC[rec.traffic_level] : "Not reported", rec.delay_min ? `+${rec.delay_min} min delay` : null),
          metric("Distance", rec.length_km != null ? `${rec.length_km} km` : null)),
        h("span", { class: `basis basis--${rec.basis}` }, rec.basis_label || blabel)));
    }
    if (c.live && !(rec && rec.leave_now)) {
      out.push(h("p", { class: "dnote" }, `Leaving now (live traffic): ${c.live.duration_min} min, arrive ${t(c.live.arrival_at)}`,
        c.live.traffic_level ? ` · ${TRAFFIC[c.live.traffic_level]} traffic` : ""));
    }

    if (c.slots.length) {
      const rows = c.slots.map((s) => h("tr", { class: [s.past ? "is-past" : "", s.best ? "is-best" : ""].join(" ").trim() || null },
        h("th", { scope: "row" }, t(s.departure_at), s.best ? h("span", { class: "dbest" }, "Best") : null),
        h("td", { "data-label": "Journey" }, s.duration_min != null ? `${s.duration_min} min` : "No data"),
        h("td", { "data-label": "Arrive" }, s.arrival_at && s.duration_min != null ? t(s.arrival_at) : "—"),
        h("td", { "data-label": "Traffic" }, s.traffic_level ? h("span", { class: `traffic traffic--${s.traffic_level}` }, TRAFFIC[s.traffic_level]) : "—"),
        h("td", { "data-label": "Source" }, s.basis ? (BASIS[s.basis]?.[0] || s.basis) + (s.samples ? ` (${s.samples})` : "") : "—"),
        h("td", { "data-label": "Checked" }, s.observed_at ? t(s.observed_at) : "—")));
      out.push(section("slots", c.state === "after_window" ? "Earlier today" : "Departure options",
        h("div", { class: "dtable-wrap" }, h("table", { class: "dtable dtable--slots" },
          h("thead", {}, h("tr", {}, ["Leave", "Journey", "Arrive", "Traffic", "Source", "Checked"].map((x) => h("th", { scope: "col" }, x)))),
          h("tbody", {}, rows)))));
    }

    const days = (c.days || []).map((d) => d[0].toUpperCase() + d.slice(1)).join(", ");
    const sched = [metric("Monitoring", c.window_start ? `${t(c.window_start)}–${t(c.window_end)}` : "—", `${days} · ${c.timezone}`),
      metric("Next check", c.next_window_start ? new Intl.DateTimeFormat("en-GB", { timeZone: c.timezone, weekday: "long" }).format(new Date(c.next_window_start)) : "—",
        c.next_window_start ? `window opens ${t(c.next_window_start)}` : null),
      metric("Provider", c.provider || "—", c.last_success_at ? `updated ${t(c.last_success_at)}` : "no data yet today"),
      metric("Requests today", `${c.requests_today} / ${c.request_cap}`)];
    out.push(section("schedule", "Schedule & source", h("dl", { class: "dmetrics" }, sched),
      c.last_error ? note(`Last problem: ${c.last_error}`, "dnote--warn") : null,
      h("dl", { class: "dlegend" }, Object.values(BASIS).map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]),
        h("dt", {}, "No data"), h("dd", {}, "Not fetched yet or unavailable; never estimated."))));
    return out;
  }

  // ---------- wiring ----------

  /** One delegated listener for every tile on the dashboard. */
  function onTileClick(e) {
    const opener = e.target.closest("[data-open]");
    if (!opener || !e.currentTarget.contains(opener)) return;
    // A real control inside the tile does its own thing (checkbox, link, Location button...).
    const control = e.target.closest("button, a[href], input, select, textarea, label, summary, [role=button], [role=checkbox]");
    if (control && !control.hasAttribute("data-open") && opener.contains(control)) return;
    if (window.getSelection && String(window.getSelection()).length > 0) return;  // selecting text, not tapping
    const [view, sec] = opener.dataset.open.split("#");
    if (view === "groceries") {
      if (opener.tagName !== "A") location.href = "/groceries?from=dashboard";
      return;   // links navigate by themselves
    }
    e.preventDefault();
    open(view, sec || null);
  }

  function init(context) {
    ctx = context;
    $("shell").addEventListener("click", onTileClick);
    $("detail-back").addEventListener("click", requestClose);
    $("detail").addEventListener("cancel", (e) => { e.preventDefault(); requestClose(); });   // Esc
    $("detail").addEventListener("click", (e) => { if (e.target === e.currentTarget) requestClose(); });  // backdrop
    window.addEventListener("popstate", onPopState);
    const deep = parseHash(location.hash);
    if (deep) {
      // Deep link (e.g. bookmark #/commute): open without adding history.
      history.replaceState({ auraDetail: deep }, "", location.hash);
      open(deep.view, deep.section, { fromHistory: true });
      pushed = false;
    }
  }

  return { init, open, close: requestClose, refresh, isOpen: () => !!current };
})();
