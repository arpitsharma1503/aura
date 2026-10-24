/* AURA Smart Home view: devices by room, device pages with capability-based controls,
   adding devices from Home Assistant, scenes, automations and the connection.
   Everything shown comes from /api/smarthome (Home Assistant's real states and
   AURA's own registry). A control only shows "Done" when the device confirmed it. */
"use strict";

window.AuraSmartHome = (() => {
  const $ = (id) => document.getElementById(id);
  const TABS = [["devices", "Devices"], ["add", "Add devices"], ["scenes", "Scenes"], ["automations", "Automations"]];
  const KIND = {
    light: ["bulb", "Light"], switch: ["plug", "Switch"], plug: ["plug", "Plug"], fan: ["fan", "Fan"],
    climate: ["thermo", "Climate"], cover: ["blinds", "Curtain / blind"], sensor: ["thermo", "Sensor"],
    motion: ["motion", "Motion sensor"], door: ["door", "Door / window"], smoke: ["alert", "Smoke sensor"],
    leak: ["droplet", "Leak sensor"], presence: ["motion", "Presence"], lock: ["lock", "Lock"], media: ["tv", "Media"],
    camera: ["camera", "Camera"], alarm: ["shield", "Alarm"], energy: ["bolt", "Energy"], other: ["chip", "Device"],
  };
  const ACTION_LABEL = { turn_on: "Turn on", turn_off: "Turn off", set_brightness: "Brightness %", set_color_temp: "Colour temperature K",
    set_fan_speed: "Fan speed %", set_hvac_mode: "Mode", set_temperature: "Temperature °C", open: "Open", close: "Close", stop: "Stop",
    set_position: "Position %", play: "Play", pause: "Pause", set_volume: "Volume %", lock: "Lock" };
  const NEEDS = { turn_on: "on_off", turn_off: "on_off", set_brightness: "brightness", set_color_temp: "color_temp", set_fan_speed: "fan_speed",
    set_hvac_mode: "hvac_mode", set_temperature: "target_temperature", open: "open_close", close: "open_close", stop: "stop",
    set_position: "position", play: "play_pause", pause: "play_pause", set_volume: "volume", lock: "lock" };

  let ctx = { icon: () => "", formatTime: (d) => d.toLocaleTimeString() };
  let root = null;
  let tab = "devices";
  let page = null;            // { device: entity_id } | { scene: id | "new" }
  let data = null;            // GET /api/smarthome
  let discovery = null;       // POST /api/smarthome/discover
  let rooms = [];
  let pending = {};           // entity_id -> "Sending…" etc.
  let flashTimer = null;
  let flashMsg = null;        // { text, kind }: survives repaints until it times out

  // ---------- helpers ----------
  async function api(url, { method = "GET", body } = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 20000);
    try {
      const res = await fetch(url, {
        method, signal: ctrl.signal, cache: "no-store", credentials: "same-origin",
        headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
      if (res.status === 204) return null;
      let out = null;
      try { out = await res.json(); } catch { /* empty */ }
      if (!res.ok) {
        const d = out?.detail;
        const e = new Error(typeof d === "string" ? d : Array.isArray(d) ? (d[0]?.msg || "Please check the values.") : `HTTP ${res.status}`);
        e.status = res.status; throw e;
      }
      return out;
    } catch (err) {
      if (err.name === "AbortError") { const e = new Error("No answer in time; nothing is known to have changed."); e.status = 0; throw e; }
      throw err;
    } finally { clearTimeout(timer); }
  }
  function el(tag, attrs = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "html") n.innerHTML = v;                 // our own SVG icons only
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat(Infinity)) if (k != null && k !== false) n.append(k instanceof Node ? k : document.createTextNode(String(k)));
    return n;
  }
  const note = (text, cls = "") => el("p", { class: `dnote ${cls}` }, text);
  const rid = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`).replace(/[^A-Za-z0-9_-]/g, "");
  const kindIcon = (k) => ctx.icon((KIND[k] || KIND.other)[0]);
  const kindName = (k) => (KIND[k] || KIND.other)[1];
  const device = (id) => data?.devices.find((d) => d.entity_id === id);
  function flash(text, kind = "info") {
    flashMsg = { text, kind };
    const f = root?.querySelector(".sh-flash");
    if (f) { f.textContent = text; f.dataset.kind = kind; f.hidden = false; }
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { flashMsg = null; const g = root?.querySelector(".sh-flash"); if (g) g.hidden = true; }, 6000);
  }

  /** Short state text, only from what Home Assistant reports. */
  function stateText(d) {
    if (d.availability === "offline") return "Offline";
    if (d.availability === "unknown" || d.state == null) return "Unknown";
    const a = d.attributes || {};
    if (d.kind === "light" && d.state === "on" && a.brightness != null) return `On · ${Math.round(a.brightness / 2.55)}%`;
    if (d.kind === "climate") return [d.state === "off" ? "Off" : d.state, a.current_temperature != null ? `${a.current_temperature}°` : null].filter(Boolean).join(" · ");
    if (d.kind === "cover") return a.current_position != null ? `${d.state} · ${a.current_position}%` : d.state;
    if (["sensor", "energy"].includes(d.kind)) return `${d.state}${a.unit_of_measurement ? ` ${a.unit_of_measurement}` : ""}`;
    if (d.kind === "motion" || d.kind === "presence") return d.state === "on" ? "Detected" : "Clear";
    if (d.kind === "door") return d.state === "on" ? "Open" : "Closed";
    if (d.kind === "leak") return d.state === "on" ? "Water detected" : "Dry";
    if (d.kind === "smoke") return d.state === "on" ? "Smoke detected" : "Clear";
    return d.state.charAt(0).toUpperCase() + d.state.slice(1);
  }
  const isOn = (d) => ["on", "open", "playing", "unlocked", "heat", "cool", "auto", "dry", "fan_only", "heat_cool"].includes(d.state);

  // ---------- data ----------
  async function load() {
    try {
      const [s, cfg] = await Promise.all([api("/api/smarthome"), api("/api/config")]);
      data = s; rooms = cfg.home?.rooms || [];
    } catch (err) { if (!data) { paintError(err); return; } }
    paint();
  }

  async function command(d, action, value, { confirm = false } = {}) {
    if (pending[d.entity_id]) return;
    pending[d.entity_id] = "Sending…"; paint();
    try {
      const r = await api(`/api/smarthome/devices/${encodeURIComponent(d.entity_id)}/command`,
        { method: "POST", body: { action, value, request_id: rid(), confirm } });
      Object.assign(d, r.device);
      if (r.result === "confirmed") flash(`${d.name}: done.`, "ok");
      else flash(`${d.name}: ${r.message}`, "warn");
    } catch (err) {
      flash(`${d.name}: ${err.message}`, "error");
    } finally {
      delete pending[d.entity_id]; paint();
    }
  }

  // ---------- rendering ----------
  function build() { root = el("div", { class: "sh" }); return root; }

  function paintError(err) {
    if (!root) return;
    root.replaceChildren(el("div", { class: "dempty" }, el("span", { html: ctx.icon("alert") }),
      el("div", {}, el("strong", {}, "Couldn't load the smart home"), el("p", {}, err.message))));
  }

  function controls() {
    return el("div", { class: "mp-controls" }, el("div", { class: "mp-tabs", role: "tablist", "aria-label": "Smart home sections" },
      TABS.map(([id, label]) => el("button", { type: "button", role: "tab", class: "mp-tab", "aria-selected": String(tab === id && !page),
        "data-tab": id, onclick: () => show(id) }, label))));
  }

  function paint() {
    if (!root) return;
    if (!data) { root.replaceChildren(note("Loading…")); return; }
    const c = data.connection;
    $("detail-sub").textContent = c.status === "connected" ? `Home Assistant ${c.version || ""} · ${data.counts.total} device${data.counts.total === 1 ? "" : "s"}`
      : "Home Assistant not connected";
    const body = page?.device ? devicePage(page.device) : page?.scene != null ? scenePage(page.scene)
      : tab === "add" ? addTab() : tab === "scenes" ? scenesTab() : tab === "automations" ? automationsTab() : devicesTab();
    root.replaceChildren(...[controls(), connectionBanner(), el("p", { class: "sh-flash", role: "status", hidden: !flashMsg, "data-kind": flashMsg?.kind }, flashMsg?.text || ""), ...[].concat(body)].filter(Boolean));
  }

  function connectionBanner() {
    const c = data.connection;
    if (c.status === "connected") return null;
    return el("div", { class: `sh-banner sh-banner--${c.status}` }, el("span", { html: ctx.icon(c.status === "connecting" ? "retry" : "alert") }),
      el("div", {}, el("strong", {}, { not_configured: "Not connected yet", connecting: "Connecting…", auth_failed: "Access token refused",
        unreachable: "Home Assistant isn't answering" }[c.status] || c.status), el("p", {}, c.message || ""),
        c.status === "not_configured" ? el("p", {}, "Steps: open Home Assistant, create your account, then create a long-lived access token and run ",
          el("code", {}, "~/aura/deploy/homeassistant/connect.sh"), " on the Pi.") : null,
        c.ha_url ? el("p", {}, el("a", { href: c.ha_url, target: "_blank", rel: "noopener" }, `Open Home Assistant (${c.ha_url})`)) : null));
  }

  // ----- Devices (by room) -----
  function quick(d) {
    const busy = pending[d.entity_id];
    if (busy) return el("span", { class: "sh-busy" }, busy);
    if (d.availability !== "online" || !d.capabilities.includes("on_off")) return null;
    const on = d.state === "on" || d.state === "playing";
    return el("button", { type: "button", class: `sh-toggle${on ? " is-on" : ""}`, role: "switch", "aria-checked": String(on),
      "aria-label": `${d.name}: turn ${on ? "off" : "on"}`, onclick: (e) => { e.stopPropagation(); command(d, on ? "turn_off" : "turn_on"); } },
      el("span", { class: "sh-toggle__knob" }));
  }
  function card(d) {
    return el("li", { class: `sh-card${isOn(d) ? " is-on" : ""}`, "data-availability": d.availability, "data-kind": d.kind },
      el("button", { type: "button", class: "sh-card__open", onclick: () => { page = { device: d.entity_id }; paint(); },
        "aria-label": `${d.name}, ${stateText(d)}. Open details` },
        el("span", { class: "sh-card__icon", html: kindIcon(d.kind) }),
        el("span", { class: "sh-card__text" }, el("b", {}, d.name), el("small", {}, stateText(d)))),
      quick(d));
  }
  function devicesTab() {
    if (!data.devices.length) {
      return el("div", { class: "dempty" }, el("span", { html: ctx.icon("home") }), el("div", {},
        el("strong", {}, "No devices in AURA yet"),
        el("p", {}, data.connection.status === "connected"
          ? "Devices you set up in Home Assistant can be added here. Open “Add devices” to see what Home Assistant has."
          : "Connect Home Assistant first; AURA never shows devices it can't actually reach.")));
    }
    const out = [];
    if (data.alerts.length) out.push(el("ul", { class: "dproblems" }, data.alerts.map((t) => el("li", {}, t))));
    const groups = rooms.map((r) => [r.name, data.devices.filter((d) => d.room === r.id)]).filter(([, l]) => l.length);
    const none = data.devices.filter((d) => !d.room);
    if (none.length) groups.push(["No room", none]);
    for (const [name, list] of groups) {
      const on = list.filter(isOn).length, off = list.filter((d) => d.availability === "offline").length;
      out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, name,
        el("small", { class: "sh-roomsum" }, ` · ${list.length} device${list.length === 1 ? "" : "s"}${on ? ` · ${on} on` : ""}${off ? ` · ${off} offline` : ""}`)),
        el("ul", { class: "sh-grid" }, list.map(card))));
    }
    return out;
  }

  // ----- Device page -----
  function control(d, label, input) { return el("div", { class: "sh-ctl" }, el("span", { class: "sh-ctl__label" }, label), input); }
  function slider(d, action, value, min, max, step, unit) {
    const out = el("output", {}, value != null ? `${Math.round(value)}${unit}` : "—");
    const r = el("input", { type: "range", min, max, step, value: value ?? min, disabled: !!pending[d.entity_id] || d.availability !== "online",
      "aria-label": ACTION_LABEL[action], oninput: (e) => { out.textContent = `${e.target.value}${unit}`; },
      onchange: (e) => command(d, action, Number(e.target.value)) });
    return el("div", { class: "sh-slider" }, r, out);
  }
  function buttons(d, list) {
    return el("div", { class: "sh-btns" }, list.map(([action, label, extra]) => el("button", { type: "button", class: "btn",
      disabled: !!pending[d.entity_id] || d.availability !== "online", onclick: () => command(d, action, extra) }, label)));
  }
  function devicePage(id) {
    const d = device(id);
    if (!d) { page = null; return devicesTab(); }
    const a = d.attributes || {};
    const caps = new Set(d.capabilities);
    const ctl = [];
    if (caps.has("on_off")) ctl.push(control(d, "Power", quick(d) || el("span", { class: "sh-busy" }, stateText(d))));
    if (caps.has("brightness")) ctl.push(control(d, "Brightness", slider(d, "set_brightness", a.brightness != null ? a.brightness / 2.55 : null, 1, 100, 1, "%")));
    if (caps.has("color_temp")) ctl.push(control(d, "Colour temperature",
      slider(d, "set_color_temp", a.color_temp_kelvin, a.min_color_temp_kelvin || 2000, a.max_color_temp_kelvin || 6500, 100, " K")));
    if (caps.has("fan_speed")) ctl.push(control(d, "Speed", slider(d, "set_fan_speed", a.percentage, 0, 100, 1, "%")));
    if (caps.has("hvac_mode")) ctl.push(control(d, "Mode", buttons(d, (a.hvac_modes || []).map((m) => ["set_hvac_mode", m.replace("_", " "), m]))));
    if (caps.has("target_temperature")) ctl.push(control(d, "Set to", slider(d, "set_temperature", a.temperature, a.min_temp || 16, a.max_temp || 30, 0.5, "°")));
    if (caps.has("open_close")) ctl.push(control(d, "Curtain", buttons(d, [["open", "Open"], ...(caps.has("stop") ? [["stop", "Stop"]] : []), ["close", "Close"]])));
    if (caps.has("position")) ctl.push(control(d, "Position", slider(d, "set_position", a.current_position, 0, 100, 1, "%")));
    if (caps.has("play_pause")) ctl.push(control(d, "Playback", buttons(d, [["play", "Play"], ["pause", "Pause"]])));
    if (caps.has("volume")) ctl.push(control(d, "Volume", slider(d, "set_volume", a.volume_level != null ? a.volume_level * 100 : null, 0, 100, 1, "%")));
    if (caps.has("lock")) {
      ctl.push(control(d, "Lock", el("div", { class: "sh-btns" },
        el("button", { type: "button", class: "btn", disabled: d.availability !== "online", onclick: () => command(d, "lock") }, "Lock"),
        el("button", { type: "button", class: "btn btn--danger", disabled: d.availability !== "online", onclick: () => {
          if (window.confirm(`Unlock ${d.name}? This opens the door lock.`)) command(d, "unlock", null, { confirm: true });
        } }, "Unlock…"))));
    }
    const roomSel = el("select", { class: "sheet__input", "aria-label": "Room", onchange: (e) => save(d, { room: e.target.value }) },
      el("option", { value: "" }, "No room"), rooms.map((r) => el("option", { value: r.id, selected: r.id === d.room }, r.name)));
    const nameIn = el("input", { class: "sheet__input", value: d.name, maxlength: 40, "aria-label": "Name" });
    const det = d.details || {};
    const out = [
      el("button", { type: "button", class: "mp-back", onclick: () => { page = null; paint(); } }, "‹ Devices"),
      el("div", { class: "sh-head" }, el("span", { class: "sh-card__icon sh-card__icon--big", html: kindIcon(d.kind) }),
        el("div", {}, el("h3", { class: "sh-title" }, d.name), el("p", { class: "sh-sub" }, `${kindName(d.kind)} · ${d.room_name || "No room"} · `,
          el("span", { class: `sh-avail sh-avail--${d.availability}` }, d.availability === "online" ? "Online" : d.availability === "offline" ? "Offline" : "Unknown"))),
        el("button", { type: "button", class: `btn sh-fav${d.favorite ? " is-on" : ""}`, "aria-pressed": String(d.favorite),
          onclick: () => save(d, { favorite: !d.favorite }) }, el("span", { html: ctx.icon("star") }), d.favorite ? "Favourite" : "Add to favourites")),
      el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Now"),
        el("p", { class: "sh-state" }, stateText(d), d.last_updated ? el("small", {}, ` · updated ${ctx.formatTime(new Date(d.last_updated))}`) : null),
        ctl.length ? el("div", { class: "sh-ctls" }, ctl) : note("This device reports readings only; it has no controls.", "dnote--muted")),
    ];
    if (d.availability === "offline") {
      out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Why it may be offline"),
        el("ul", { class: "sh-help" }, el("li", {}, "Is it powered (switch on the wall, batteries)?"),
          el("li", {}, "Wi-Fi devices: is it still on your home Wi-Fi? Try the maker's app."),
          el("li", {}, "Zigbee/Thread devices: is it within range of the coordinator or a mains-powered device?"),
          el("li", {}, "Check the device in Home Assistant → Settings → Devices."))));
    }
    out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Settings in AURA"),
      el("div", { class: "sh-form" },
        el("label", { class: "s-field" }, el("span", {}, "Name"), nameIn),
        el("button", { type: "button", class: "btn", onclick: () => nameIn.value.trim() && save(d, { name: nameIn.value.trim() }) }, "Rename"),
        el("label", { class: "s-field" }, el("span", {}, "Room"), roomSel)),
      note("Rooms are the same rooms as on the dashboard (Settings → Home & rooms).", "dnote--muted")));
    out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Connection"),
      el("dl", { class: "dmetrics" },
        el("div", { class: "dmetric" }, el("dt", {}, "Through"), el("dd", {}, d.integration, det.platform ? el("small", {}, det.platform) : null)),
        el("div", { class: "dmetric" }, el("dt", {}, "Device"), el("dd", {}, det.manufacturer || "—", det.model ? el("small", {}, det.model) : null)),
        el("div", { class: "dmetric" }, el("dt", {}, "Home Assistant id"), el("dd", {}, el("code", {}, d.entity_id)))),
      el("div", { class: "mp-actions" }, el("button", { type: "button", class: "btn", onclick: () => remove(d) }, "Remove from AURA")),
      note("Removing only hides it in AURA; the device stays set up in Home Assistant.", "dnote--muted")));
    return out;
  }
  async function save(d, patch) {
    try {
      Object.assign(d, await api(`/api/smarthome/devices/${encodeURIComponent(d.entity_id)}`, { method: "PATCH", body: patch }));
      flash("Saved.", "ok");
    } catch (err) { flash(err.message, "error"); }
    paint();
  }
  async function remove(d) {
    if (!window.confirm(`Remove ${d.name} from AURA? It stays in Home Assistant.`)) return;
    try {
      await api(`/api/smarthome/devices/${encodeURIComponent(d.entity_id)}`, { method: "DELETE" });
      page = null; await load(); flash(`${d.name} removed from AURA.`);
    } catch (err) { flash(err.message, "error"); }
  }

  // ----- Add devices -----
  async function scan() {
    discovery = { scanning: true };
    paint();
    try { discovery = await api("/api/smarthome/discover", { method: "POST" }); }
    catch (err) { discovery = { error: err.message }; }
    paint();
  }
  async function add(c, room) {
    try {
      await api("/api/smarthome/devices", { method: "POST", body: { entity_id: c.entity_id, room: room || null } });
      discovery.candidates = discovery.candidates.filter((x) => x.entity_id !== c.entity_id);
      await load(); flash(`${c.name} added.`, "ok");
    } catch (err) { flash(err.message, "error"); paint(); }
  }
  function addTab() {
    const out = [el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Find devices"),
      note("AURA asks Home Assistant what it has set up, and what it has found on your home network that still needs setting up. Nothing outside your home network is scanned.", "dnote--muted"),
      el("div", { class: "mp-actions" }, el("button", { type: "button", class: "btn btn--primary", id: "sh-scan", disabled: discovery?.scanning || data.connection.status !== "connected", onclick: scan },
        discovery?.scanning ? "Looking…" : "Look for devices")))];
    if (discovery?.error) out.push(note(discovery.error, "dnote--warn"));
    if (discovery?.candidates) {
      const rows = discovery.candidates.map((c) => {
        const sel = el("select", { class: "sheet__input", "aria-label": `Room for ${c.name}` }, el("option", { value: "" }, "No room"),
          rooms.map((r) => el("option", { value: r.id }, r.name)));
        return el("li", { class: "sh-cand" }, el("span", { class: "sh-card__icon", html: kindIcon(c.kind) }),
          el("span", { class: "sh-card__text" }, el("b", {}, c.name),
            el("small", {}, [kindName(c.kind), [c.manufacturer, c.model].filter(Boolean).join(" "), c.connection, c.availability === "online" ? null : c.availability].filter(Boolean).join(" · "))),
          sel, el("button", { type: "button", class: "btn sh-add", onclick: () => add(c, sel.value) }, "Add"));
      });
      out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Ready to add (set up in Home Assistant)"),
        rows.length ? el("ul", { class: "sh-cands" }, rows) : note("Nothing new: every device Home Assistant has is already in AURA, or none is set up yet.")));
      out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Found on the network, needs setup in Home Assistant"),
        discovery.discovered.length ? el("ul", { class: "sh-cands" }, discovery.discovered.map((f) => el("li", { class: "sh-cand" },
          el("span", { class: "sh-card__icon", html: ctx.icon("wifi") }),
          el("span", { class: "sh-card__text" }, el("b", {}, f.name), el("small", {}, `${f.integration} · found by ${f.found_by}`)),
          data.connection.ha_url ? el("a", { class: "btn", href: `${data.connection.ha_url}/config/integrations/dashboard`, target: "_blank", rel: "noopener" }, "Finish in Home Assistant") : null)))
          : note("Home Assistant hasn't found any new devices on the network.", "dnote--muted")));
    }
    out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "How to connect each kind of device"),
      el("dl", { class: "sh-guides" }, (data.guides || []).map((g) => [el("dt", {}, g.title), el("dd", {}, g.text)]))));
    return out;
  }

  // ----- Scenes -----
  async function runScene(s) {
    const full = data.scene_details?.find((x) => x.id === s.id);
    const names = (full?.actions || []).map((a) => `• ${device(a.entity_id)?.name || a.entity_id}: ${ACTION_LABEL[a.action] || a.action}${a.value != null ? ` ${a.value}` : ""}`);
    if (!window.confirm(`Run “${s.name}”? This will change:\n${names.join("\n")}`)) return;
    try {
      const r = await api(`/api/smarthome/scenes/${s.id}/run`, { method: "POST", body: { request_id: rid() } });
      const bad = r.results.filter((x) => x.result !== "confirmed");
      flash(`${r.scene}: ${r.confirmed} of ${r.total} confirmed${bad.length ? ` · ${bad.map((x) => `${device(x.entity_id)?.name || x.entity_id}: ${x.message || x.result}`).join("; ")}` : ""}.`, bad.length ? "warn" : "ok");
    } catch (err) { flash(err.message, "error"); }
    await load();
  }
  function scenesTab() {
    const list = data.scene_details || [];
    return [el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Scenes"),
      note("A scene changes only the devices and settings you put in it. Unlocking can't be part of a scene.", "dnote--muted"),
      list.length ? el("ul", { class: "sh-scenes" }, list.map((s) => el("li", { class: "sh-scene" },
        el("span", { class: "sh-card__text" }, el("b", {}, s.name), el("small", {}, `${new Set(s.actions.map((a) => a.entity_id)).size} devices · ${s.actions.length} actions`)),
        el("button", { type: "button", class: "btn", onclick: () => { page = { scene: s.id }; paint(); } }, "Edit"),
        el("button", { type: "button", class: "btn btn--primary", onclick: () => runScene(s) }, "Run"))))
        : note("No scenes yet."),
      el("div", { class: "mp-actions" }, el("button", { type: "button", class: "btn", disabled: !data.devices.length,
        onclick: () => { page = { scene: "new" }; paint(); } }, data.devices.length ? "New scene" : "Add devices first")))];
  }
  function scenePage(id) {
    const existing = (data.scene_details || []).find((s) => s.id === id);
    const actions = existing ? existing.actions.map((a) => ({ ...a })) : [];
    const nameIn = el("input", { class: "sheet__input", value: existing?.name || "", maxlength: 30, placeholder: "e.g. Movie Time", "aria-label": "Scene name" });
    const list = el("ul", { class: "sh-steps" });
    const controllable = data.devices.filter((d) => d.capabilities.some((c) => Object.values(NEEDS).includes(c)));
    function redraw() {
      list.replaceChildren(...actions.map((a, i) => {
        const d = device(a.entity_id);
        const allowed = Object.entries(NEEDS).filter(([, need]) => d?.capabilities.includes(need)).map(([act]) => act);
        const devSel = el("select", { class: "sheet__input", "aria-label": "Device", onchange: (e) => { a.entity_id = e.target.value; a.action = null; redraw(); } },
          controllable.map((x) => el("option", { value: x.entity_id, selected: x.entity_id === a.entity_id }, x.name)));
        const actSel = el("select", { class: "sheet__input", "aria-label": "Action", onchange: (e) => { a.action = e.target.value; redraw(); } },
          allowed.map((x) => el("option", { value: x, selected: x === a.action }, ACTION_LABEL[x])));
        if (!a.action || !allowed.includes(a.action)) a.action = allowed[0];
        const needsValue = /^set_/.test(a.action || "");
        const val = needsValue ? el("input", { class: "sheet__input", value: a.value ?? "", "aria-label": "Value", size: 6,
          oninput: (e) => { a.value = a.action === "set_hvac_mode" ? e.target.value : Number(e.target.value); } }) : null;
        return el("li", {}, devSel, actSel, val, el("button", { type: "button", class: "btn", "aria-label": "Remove step", onclick: () => { actions.splice(i, 1); redraw(); } }, "✕"));
      }));
    }
    redraw();
    async function saveScene() {
      const body = { name: nameIn.value.trim(), actions: actions.map(({ entity_id, action, value }) => ({ entity_id, action, value: /^set_/.test(action) ? value : null })) };
      try {
        await api(existing ? `/api/smarthome/scenes/${existing.id}` : "/api/smarthome/scenes", { method: existing ? "PUT" : "POST", body });
        page = null; tab = "scenes"; await load(); flash(`Scene “${body.name}” saved.`, "ok");
      } catch (err) { flash(err.message, "error"); }
    }
    async function deleteScene() {
      if (!window.confirm(`Delete the scene “${existing.name}”?`)) return;
      try { await api(`/api/smarthome/scenes/${existing.id}`, { method: "DELETE" }); page = null; await load(); }
      catch (err) { flash(err.message, "error"); }
    }
    return [el("button", { type: "button", class: "mp-back", onclick: () => { page = null; paint(); } }, "‹ Scenes"),
      el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, existing ? "Edit scene" : "New scene"),
        el("label", { class: "s-field" }, el("span", {}, "Name"), nameIn),
        note("Each step is one device and what it should do. Nothing else is touched.", "dnote--muted"), list,
        el("div", { class: "mp-actions" },
          el("button", { type: "button", class: "btn", onclick: () => { actions.push({ entity_id: controllable[0]?.entity_id, action: null, value: null }); redraw(); } }, "Add a step"),
          el("button", { type: "button", class: "btn btn--primary", onclick: saveScene }, "Save scene"),
          existing ? el("button", { type: "button", class: "btn", onclick: deleteScene }, "Delete") : null))];
  }

  // ----- Automations -----
  async function toggleAutomation(a) {
    try {
      const r = await api(`/api/smarthome/automations/${encodeURIComponent(a.entity_id)}`, { method: "POST", body: { enabled: !a.enabled } });
      flash(r.result === "confirmed" ? `${a.name} ${a.enabled ? "turned off" : "turned on"}.` : "Home Assistant accepted it but hasn't confirmed yet.", r.result === "confirmed" ? "ok" : "warn");
    } catch (err) { flash(err.message, "error"); }
    await load();
  }
  function automationsTab() {
    const list = data.automation_list || [];
    return [el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Automations"),
      note("Automations run in Home Assistant, so they keep working even if AURA restarts. Create and edit them in Home Assistant; here you can see them and switch them on or off. AURA never creates one on its own.", "dnote--muted"),
      list.length ? el("ul", { class: "sh-scenes" }, list.map((a) => el("li", { class: "sh-scene" },
        el("span", { class: "sh-card__text" }, el("b", {}, a.name), el("small", {}, a.last_triggered ? `last ran ${ctx.formatTime(new Date(a.last_triggered))}` : "hasn't run yet")),
        el("button", { type: "button", class: `sh-toggle${a.enabled ? " is-on" : ""}`, role: "switch", "aria-checked": String(a.enabled), "aria-label": `${a.name}: ${a.enabled ? "turn off" : "turn on"}`,
          onclick: () => toggleAutomation(a) }, el("span", { class: "sh-toggle__knob" })))))
        : note(data.connection.status === "connected" ? "No automations in Home Assistant yet." : "Connect Home Assistant to see automations."),
      data.connection.ha_url ? el("p", {}, el("a", { class: "btn", href: `${data.connection.ha_url}/config/automation/dashboard`, target: "_blank", rel: "noopener" }, "Open automations in Home Assistant")) : null)];
  }

  function show(id) { tab = id; page = null; paint(); }

  // ---------- public ----------
  return {
    init(opts) { ctx = { ...ctx, ...opts }; },
    view() { if (!root) build(); data = null; paint(); load(); return root; },
    refresh() { if (root?.isConnected) load(); },
    openDevice(id) { page = { device: id }; paint(); },
  };
})();
