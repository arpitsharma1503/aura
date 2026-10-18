/* AURA Settings Center. Plain JS, no build step.
   Each editable section is a <form data-section="...">. Fields declare
   data-field="name" (or "home.latitude" for nested values). Save sends only
   the fields that changed; the server validates everything and returns
   field-level errors, which are shown next to the inputs. */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = { settings: null, info: null, snapshot: {}, canEdit: false, canAdmin: false, scan: [], target: null };
  const LABELS = { network: "Network", locations: "Locations", weather: "Weather", commute: "Commute",
    display: "Display", groceries: "Groceries" };

  // ---------- API ----------
  class ApiError extends Error {
    constructor(status, message, body) { super(message); this.status = status; this.body = body; }
  }
  async function api(url, { method = "GET", body } = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), method === "POST" && url.includes("wifi/connect") ? 70000 : 15000);
    try {
      const res = await fetch(url, {
        method, signal: ctrl.signal, cache: "no-store", credentials: "same-origin",
        headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
      let data = null;
      try { data = await res.json(); } catch { /* empty */ }
      if (!res.ok) {
        let msg = data?.detail;
        if (msg && typeof msg === "object") msg = msg.message || "Please check the highlighted fields.";
        if (Array.isArray(data?.detail)) msg = data.detail.map((d) => d.msg).join("; ");
        throw new ApiError(res.status, msg || `Request failed (${res.status})`, data);
      }
      return data;
    } catch (err) {
      if (err instanceof ApiError) throw err;
      throw new ApiError(0, err.name === "AbortError" ? "The Pi didn't respond in time." : "Can't reach AURA.");
    } finally { clearTimeout(timer); }
  }

  // ---------- small helpers ----------
  function el(tag, attrs = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v; else n.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k != null && k !== false) n.append(k instanceof Node ? k : document.createTextNode(String(k)));
    return n;
  }
  const row = (label, value, sub) => el("div", {}, el("dt", {}, label), el("dd", {}, value ?? "—", sub ? el("small", {}, sub) : null));
  const fmtTime = (iso) => iso ? new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso)) : "—";
  const get = (obj, path) => path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
  function setPath(obj, path, value) {
    const keys = path.split("."); let o = obj;
    keys.slice(0, -1).forEach((k) => { o[k] = o[k] ?? {}; o = o[k]; });
    o[keys.at(-1)] = value;
  }

  // ---------- confirm dialog ----------
  function confirmDialog(title, body, ok = "Continue") {
    return new Promise((resolve) => {
      const d = $("s-confirm");
      $("sc-title").textContent = title; $("sc-body").textContent = body; $("sc-ok").textContent = ok;
      const done = (v) => { d.close(); resolve(v); };
      $("sc-ok").onclick = () => done(true); $("sc-cancel").onclick = () => done(false);
      d.oncancel = () => resolve(false);
      d.showModal(); $("sc-cancel").focus();
    });
  }

  // ---------- form <-> values ----------
  function fieldsOf(form) { return [...form.querySelectorAll("[data-field]")]; }

  function readField(node) {
    const type = node.dataset.type;
    if (node.matches("fieldset.s-choice")) return node.querySelector("input:checked")?.value ?? null;
    if (type === "days") return [...node.querySelectorAll("input:checked")].map((i) => i.value);
    if (type === "list") return [...node.querySelectorAll("li")].map((li) => li.dataset.value);
    if (node.type === "checkbox") return node.checked;
    const v = node.value.trim();
    if (type === "int") return v === "" ? null : Number(v);
    if (/\.(latitude|longitude)$/.test(node.dataset.field)) return v === "" ? null : (Number.isFinite(Number(v)) ? Number(v) : v);
    if (node.dataset.field === "preferred_arrival") return v || null;
    return v;
  }

  function writeField(node, value) {
    const type = node.dataset.type;
    if (node.matches("fieldset.s-choice")) {
      node.querySelectorAll("input").forEach((i) => { i.checked = i.value === value; });
    } else if (type === "days") {
      node.querySelectorAll("input").forEach((i) => { i.checked = (value || []).includes(i.value); });
    } else if (type === "list") {
      renderQuickAdd(value || []);
    } else if (node.type === "checkbox") {
      node.checked = !!value;
    } else if (node.tagName === "SELECT" && value != null && ![...node.options].some((o) => o.value === String(value))) {
      node.append(new Option(`${value}`, String(value)));
      node.value = String(value);
    } else {
      node.value = value ?? "";
    }
  }

  function formValues(form) {
    const out = {};
    for (const f of fieldsOf(form)) setPath(out, f.dataset.field, readField(f));
    return out;
  }

  function fillForm(form) {
    const values = state.settings[form.dataset.section];
    for (const f of fieldsOf(form)) writeField(f, get(values, f.dataset.field));
    state.snapshot[form.dataset.section] = JSON.stringify(formValues(form));
    clearErrors(form);
    updateDirty(form);
  }

  const isDirty = (form) => JSON.stringify(formValues(form)) !== state.snapshot[form.dataset.section];

  function updateDirty(form) {
    const dirty = isDirty(form);
    form.classList.toggle("is-dirty", dirty);
    form.querySelector(".s-save").disabled = !dirty || !state.canEdit;
    form.querySelector(".s-cancel").disabled = !dirty;
  }

  function clearErrors(form) {
    form.querySelectorAll(".s-err[data-for]").forEach((e) => e.remove());
    form.querySelectorAll("[aria-invalid]").forEach((e) => e.removeAttribute("aria-invalid"));
  }

  function showErrors(form, errors) {
    clearErrors(form);
    let first = null;
    for (const { field, message } of errors) {
      const node = form.querySelector(`[data-field="${CSS.escape(field)}"]`)
        || form.querySelector(`[data-field^="${CSS.escape(field.split(".")[0])}"]`);
      const target = node || form.querySelector(".s-status");
      const id = `err-${form.dataset.section}-${field.replace(/\W/g, "-")}`;
      const msg = el("small", { class: "s-err", id, "data-for": field, role: "alert" }, message);
      if (node) {
        node.setAttribute("aria-invalid", "true");
        node.setAttribute("aria-describedby", id);
        (node.closest(".s-field, fieldset, .s-switch") || node).append(msg);
        first = first || node;
      } else {
        target.after(msg);
      }
    }
    (first?.matches("fieldset") ? first.querySelector("input") : first)?.focus();
  }

  function setStatus(form, text, kind = "") {
    const s = form.querySelector(".s-status");
    s.textContent = text;
    s.dataset.kind = kind;
  }

  // ---------- save / cancel / reset ----------
  function changedFields(form) {
    const before = JSON.parse(state.snapshot[form.dataset.section]);
    const now = formValues(form);
    const changes = {};
    for (const k of Object.keys(now)) {
      if (JSON.stringify(now[k]) !== JSON.stringify(before[k])) changes[k] = now[k];
    }
    return changes;
  }

  async function save(form) {
    const section = form.dataset.section;
    const changes = changedFields(form);
    if (!Object.keys(changes).length) return;
    const btn = form.querySelector(".s-save");
    btn.disabled = true;
    setStatus(form, "Saving…");
    try {
      const res = await api(`/api/settings/${section}`, { method: "PATCH", body: changes });
      state.settings[section] = res.values;
      fillForm(form);
      setStatus(form, res.applied ? `Saved. ${res.applied}` : "Saved.", "ok");
      refreshInfo();
    } catch (err) {
      if (err.status === 422 && err.body?.detail?.errors) {
        showErrors(form, err.body.detail.errors);
        setStatus(form, "Not saved: please fix the highlighted fields.", "error");
      } else if (err.status === 401) {
        lock(err.message);
        setStatus(form, err.message, "error");
      } else {
        setStatus(form, `Not saved: ${err.message}`, "error");
      }
      updateDirty(form);
    }
  }

  async function reset(form) {
    const section = form.dataset.section;
    const ok = await confirmDialog(`Reset ${LABELS[section]} settings?`,
      "Everything in this section goes back to the values configured on the Pi (.env). Other sections aren't affected.", "Reset");
    if (!ok) return;
    try {
      const res = await api(`/api/settings/${section}/reset`, { method: "POST" });
      state.settings[section] = res.values;
      fillForm(form);
      setStatus(form, "Reset to defaults.", "ok");
      refreshInfo();
    } catch (err) {
      setStatus(form, `Couldn't reset: ${err.message}`, "error");
    }
  }

  function addFooter(form) {
    const foot = el("footer", { class: "s-foot" },
      el("p", { class: "s-status", role: "status", "aria-live": "polite" }),
      el("div", { class: "s-actions" },
        el("button", { type: "button", class: "btn s-reset" }, "Reset to defaults"),
        el("button", { type: "button", class: "btn s-cancel", disabled: true }, "Cancel"),
        el("button", { type: "submit", class: "btn btn--primary s-save", disabled: true }, "Save")));
    form.append(foot);
    form.addEventListener("input", () => { updateDirty(form); setStatus(form, isDirty(form) ? "Unsaved changes" : ""); });
    form.addEventListener("change", () => { updateDirty(form); setStatus(form, isDirty(form) ? "Unsaved changes" : ""); });
    form.addEventListener("submit", (e) => { e.preventDefault(); save(form); });
    foot.querySelector(".s-cancel").addEventListener("click", () => { fillForm(form); setStatus(form, "Changes discarded."); });
    foot.querySelector(".s-reset").addEventListener("click", () => reset(form));
  }

  // ---------- quick-add chip editor ----------
  function renderQuickAdd(items) {
    $("qa-list").replaceChildren(...items.map((name) => {
      const btn = el("button", { type: "button", class: "s-chip__x", "aria-label": `Remove ${name}` }, "×");
      const li = el("li", { class: "s-chip", "data-value": name }, el("span", {}, name), btn);
      btn.addEventListener("click", () => {
        li.remove();
        const form = $("groceries");
        updateDirty(form); setStatus(form, isDirty(form) ? "Unsaved changes" : "");
        $("qa-new").focus();
      });
      return li;
    }));
  }
  function addQuickAdd() {
    const input = $("qa-new");
    const name = input.value.trim();
    if (!name) return;
    const exists = [...$("qa-list").querySelectorAll("li")].some((li) => li.dataset.value.toLowerCase() === name.toLowerCase());
    if (!exists) renderQuickAdd([...readField($("qa-list")), name]);
    input.value = "";
    const form = $("groceries");
    updateDirty(form); setStatus(form, isDirty(form) ? "Unsaved changes" : "");
  }

  // ---------- place search (fills coordinates; nothing saved until Save) ----------
  let searchTimer = null;
  function wirePlaceSearch() {
    for (const input of document.querySelectorAll(".s-place-search")) {
      const list = document.querySelector(`.s-place-results[data-target="${input.dataset.target}"]`);
      input.addEventListener("input", () => {
        clearTimeout(searchTimer);
        const q = input.value.trim();
        if (q.length < 2) { list.replaceChildren(); return; }
        searchTimer = setTimeout(async () => {
          try {
            const results = await api(`/api/location/search?q=${encodeURIComponent(q)}`);
            list.replaceChildren(...results.slice(0, 6).map((r) => {
              const b = el("button", { type: "button" }, el("span", { class: "results__name" }, r.name),
                el("span", { class: "results__sub" }, [r.district, r.region, r.country].filter(Boolean).join(", ")));
              b.addEventListener("click", async () => {
                try {
                  const place = await api(`/api/settings/place/${r.id}`);
                  const t = input.dataset.target;
                  const form = $("locations");
                  form.querySelector(`[data-field="${t}.latitude"]`).value = place.latitude;
                  form.querySelector(`[data-field="${t}.longitude"]`).value = place.longitude;
                  list.replaceChildren(); input.value = "";
                  updateDirty(form);
                  setStatus(form, `Filled in ${place.name}. Press Save to keep it.`);
                } catch (err) { setStatus($("locations"), err.message, "error"); }
              });
              return el("li", {}, b);
            }));
            if (!results.length) list.replaceChildren(el("li", { class: "s-help" }, "No places found."));
          } catch (err) { list.replaceChildren(el("li", { class: "s-help" }, err.message)); }
        }, 350);
      });
    }
  }

  // ---------- read-only info ----------
  function renderInfo() {
    const i = state.info;
    if (!i) return;
    const w = i.weather;
    $("weather-info").replaceChildren(
      row("Weather location", w.location?.city || "Not set", w.location?.label ? `${w.location.label}${w.location.provider ? ` · ${w.location.provider}` : ""}` : null),
      row("Using", w.using_home_coordinates ? "Home coordinates" : "City above"),
      row("Last update", w.last_update ? fmtTime(w.last_update) : "No data yet", w.provider || null),
      row("Detection", w.location?.detection_error ? `Failed: ${w.location.detection_error}` : (w.location?.source === "ip" ? "From internet connection (approximate)" : w.location?.source === "manual" ? "Chosen by you" : "—")),
    );
    const c = i.commute;
    $("commute-info").replaceChildren(
      row("Traffic provider", c.provider || "None", c.key_configured ? "API key set on the Pi" : "No API key (see README → Commute)"),
      row("Monitor", c.running ? "Running" : "Not running", c.state ? c.state.replace("_", " ") : null),
      row("Last update", c.last_success_at ? fmtTime(c.last_success_at) : "None yet"),
      row("Requests today", `${c.requests_today} / ${c.request_cap}`),
    );
    const d = i.display;
    $("display-info").replaceChildren(
      row("Theme", "Dark", "the only theme AURA has"),
      row("Dashboard", "Entrance display", "the only layout"),
      row("Screen sleep", d.screen_blanking, d.screen_blanking_help),
    );
    const cals = i.calendar || [];
    $("cal-info").replaceChildren(...(cals.length ? cals.map((f) => row(f.label,
      f.ok ? "Up to date" : (f.error ? `Problem: ${f.error}` : "Not loaded yet"),
      f.last_success ? `updated ${fmtTime(f.last_success)}` : null)) : [row("Calendars", "None connected")]));
    const g = i.groceries;
    $("groceries-info").replaceChildren(
      row("Shared list", `${g.items} item${g.items === 1 ? "" : "s"}`, `${g.open} to buy`),
      row("Live sync", `${g.connections} screen${g.connections === 1 ? "" : "s"} connected`, `revision ${g.revision}`),
    );
  }

  async function refreshInfo() {
    try { state.info = (await api("/api/settings")).info; renderInfo(); } catch { /* keep old */ }
  }

  // ---------- network ----------
  async function loadNetwork() {
    $("net-msg").textContent = "Checking…";
    try {
      const n = await api("/api/system/network");
      const wifi = n.interfaces.find((x) => x.type === "wifi");
      const eth = n.interfaces.find((x) => x.type === "ethernet");
      $("net-status").replaceChildren(
        row("Internet", n.connectivity === "full" ? "Connected" : (n.connectivity || "Unknown"), n.state),
        row("Wi-Fi", n.wifi_connected ? n.wifi_ssid || "Connected" : (wifi ? "Not connected" : "No Wi-Fi adapter"),
          n.wifi_connected ? `${n.wifi_signal ?? "?"}% signal${wifi?.ipv4 ? ` · ${wifi.ipv4}` : ""}` : (n.wifi_radio === "enabled" ? "radio on" : n.wifi_radio)),
        row("Ethernet", eth ? (eth.state === "connected" ? "Connected" : "Not connected") : "—", eth?.ipv4 || null),
        row("Hostname", `${n.hostname}`, `${n.hostname}.local`),
        row("Address on home network", n.lan_ip || "—", n.default_interface ? `via ${n.default_interface}` : null),
        row("Saved Wi-Fi networks", n.saved_wifi.length ? String(n.saved_wifi.length) : "None"),
      );
      $("net-msg").textContent = n.message || "";
      state.network = n;
      const adminNote = state.canAdmin ? "" : " Changes are only allowed on the entrance display itself (or after unlocking with the household PIN).";
      $("wifi-help").textContent = n.can_manage
        ? `Scan to see nearby networks, then connect. ${n.interfaces.some((x) => x.type === "ethernet" && x.state === "connected") ? "Ethernet stays connected while Wi-Fi changes." : ""}${adminNote}`
        : (n.manage_reason || "Wi-Fi can't be managed from here.") + " Use one of the other ways below.";
      $("wifi-scan").disabled = !n.can_manage || !state.canAdmin;
    } catch (err) {
      $("net-msg").textContent = `Couldn't read network status: ${err.message}`;
    }
  }

  function bars(signal) {
    const n = signal >= 75 ? 4 : signal >= 50 ? 3 : signal >= 25 ? 2 : 1;
    return el("span", { class: `s-bars s-bars--${n}`, "aria-hidden": "true" }, el("i"), el("i"), el("i"), el("i"));
  }

  async function scanWifi() {
    const btn = $("wifi-scan");
    btn.disabled = true; btn.textContent = "Scanning…";
    $("wifi-form").hidden = true;
    try {
      const nets = await api("/api/system/wifi/scan", { method: "POST" });
      state.scan = nets;
      $("wifi-list").replaceChildren(...nets.map((n) => {
        const b = el("button", { type: "button", class: "s-net", "aria-label": `${n.ssid}, ${n.signal}% signal, ${n.security ? "secured" : "open"}${n.in_use ? ", connected" : ""}` },
          bars(n.signal), el("span", { class: "s-net__name" }, n.ssid),
          el("span", { class: "s-net__meta" }, [n.band, n.security ? "🔒" : "open", n.in_use ? "connected" : n.saved ? "saved" : null].filter(Boolean).join(" · ")));
        b.addEventListener("click", () => chooseNetwork(n));
        return el("li", {}, b);
      }));
      if (!nets.length) $("wifi-list").replaceChildren(el("li", { class: "s-help" }, "No Wi-Fi networks found."));
    } catch (err) {
      $("net-msg").textContent = err.message;
    } finally {
      btn.disabled = false; btn.textContent = "Scan again";
    }
  }

  function chooseNetwork(n) {
    state.target = n;
    $("wifi-target").textContent = n.ssid;
    const needsPass = !!n.security && !n.saved;
    $("wifi-pass-row").hidden = !n.security;
    $("wifi-pass").value = "";
    $("wifi-pass").placeholder = n.saved ? "Leave empty to use the saved password" : "";
    $("wifi-pass-err").textContent = "";
    $("wifi-form").hidden = false;
    (needsPass ? $("wifi-pass") : $("wifi-connect")).focus();
  }

  async function connectWifi(e) {
    e.preventDefault();
    const n = state.target;
    if (!n) return;
    const password = $("wifi-pass").value;
    if (n.security && !n.saved && password.length < 8) {
      $("wifi-pass-err").textContent = "WPA passwords are at least 8 characters.";
      $("wifi-pass").focus();
      return;
    }
    const ethUp = state.network?.interfaces.some((x) => x.type === "ethernet" && x.state === "connected");
    const ok = await confirmDialog(`Connect to ${n.ssid}?`,
      ethUp ? "The Pi's Wi-Fi will switch to this network. Ethernet stays connected, so you won't lose access."
        : "The Pi's Wi-Fi will switch to this network. If it fails, the Pi goes back to its previous network.", "Connect");
    if (!ok) return;
    await doConnect(n, password, false);
  }

  async function doConnect(n, password, allowDisconnect) {
    const btn = $("wifi-connect");
    btn.disabled = true; btn.textContent = "Connecting…";
    $("net-msg").textContent = `Connecting to ${n.ssid}… this can take up to a minute.`;
    try {
      const r = await api("/api/system/wifi/connect", { method: "POST",
        body: { ssid: n.ssid, password, confirm: true, allow_disconnect: allowDisconnect } });
      if (r.code === "risky") {
        const go = await confirmDialog("Wi-Fi is the only connection", r.message, "Connect anyway");
        if (go) return doConnect(n, password, true);
        $("net-msg").textContent = "Not changed.";
        return;
      }
      $("net-msg").textContent = r.message + (r.ipv4 ? ` (address ${r.ipv4})` : "");
      $("net-msg").dataset.kind = r.ok ? "ok" : "error";
      if (r.ok) { $("wifi-form").hidden = true; loadNetwork(); }
      else if (r.code === "wrong_password") { $("wifi-pass-err").textContent = r.message; $("wifi-pass").focus(); }
    } catch (err) {
      $("net-msg").textContent = err.message;
      $("net-msg").dataset.kind = "error";
    } finally {
      $("wifi-pass").value = "";            // never keep the password around
      btn.disabled = false; btn.textContent = "Connect";
    }
  }

  // ---------- system ----------
  async function loadSystem() {
    try {
      const [s, h] = await Promise.all([api("/api/system/info"), api("/api/health").catch(() => null)]);
      const gb = (v) => (v == null ? "—" : `${v} GB`);
      const dur = (sec) => { if (sec == null) return "—"; const d = Math.floor(sec / 86400), hh = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60); return d ? `${d} d ${hh} h` : hh ? `${hh} h ${m} min` : `${m} min`; };
      $("sys-info").replaceChildren(
        row("Backend", h?.status === "ok" ? "Healthy" : "Not responding", s.service),
        row("AURA version", s.app_version, `started ${fmtTime(s.app_started_at)}`),
        row("Device", s.model || "—", s.hostname),
        row("Operating system", s.os, `kernel ${s.kernel}`),
        row("Python", s.python),
        row("System uptime", dur(s.system_uptime_s)),
        row("Storage free", gb(s.disk_free_gb), s.disk_total_gb ? `of ${s.disk_total_gb} GB` : null),
        row("Memory available", s.memory_available_mb != null ? `${(s.memory_available_mb / 1024).toFixed(1)} GB` : "—", s.memory_total_mb ? `of ${(s.memory_total_mb / 1024).toFixed(1)} GB` : null),
        row("CPU temperature", s.cpu_temperature_c != null ? `${s.cpu_temperature_c} °C` : "—", s.load_1m != null ? `load ${s.load_1m}` : null),
      );
    } catch (err) {
      $("sys-info").replaceChildren(row("System", `Unavailable: ${err.message}`));
    }
  }

  // ---------- access ----------
  function lock(message) {
    $("s-locked").hidden = false;
    if (message) $("s-locked-text").textContent = message;
    document.querySelectorAll("form[data-section] .s-save, form[data-section] .s-reset").forEach((b) => { b.disabled = true; });
  }

  async function unlock(e) {
    e.preventDefault();
    try {
      await api("/api/session", { method: "POST", body: { pin: $("s-pin").value.trim() } });
      location.reload();
    } catch (err) { $("s-locked-text").textContent = err.message; }
  }

  // ---------- boot ----------
  async function boot() {
    for (const form of document.querySelectorAll("form[data-section]")) addFooter(form);
    try {
      const tzs = Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : [];
      $("tz-list").replaceChildren(...tzs.map((t) => el("option", { value: t })));
    } catch { /* optional */ }
    $("qa-add").addEventListener("click", addQuickAdd);
    $("qa-new").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addQuickAdd(); } });
    $("net-refresh").addEventListener("click", loadNetwork);
    $("sys-refresh").addEventListener("click", loadSystem);
    $("wifi-scan").addEventListener("click", scanWifi);
    $("wifi-form").addEventListener("submit", connectWifi);
    $("wifi-cancel").addEventListener("click", () => { $("wifi-form").hidden = true; $("wifi-pass").value = ""; });
    $("wifi-show").addEventListener("click", () => {
      const p = $("wifi-pass"); const show = p.type === "password";
      p.type = show ? "text" : "password";
      $("wifi-show").setAttribute("aria-pressed", String(show)); $("wifi-show").textContent = show ? "Hide" : "Show";
    });
    $("s-pin-form").addEventListener("submit", unlock);
    $("loc-detect").addEventListener("click", async () => {
      const form = $("weather");
      setStatus(form, "Detecting…");
      try { await api("/api/location/detect", { method: "POST" }); setStatus(form, "Location detected. Check it's right.", "ok"); refreshInfo(); }
      catch (err) { setStatus(form, err.message, "error"); }
    });
    // Back: return to the dashboard as it was when we came from it.
    $("s-back").addEventListener("click", (e) => {
      try {
        const ref = document.referrer ? new URL(document.referrer) : null;
        if (ref && ref.origin === location.origin && ref.pathname === "/" && history.length > 1) { e.preventDefault(); history.back(); }
      } catch { /* plain link */ }
    });
    window.addEventListener("beforeunload", (e) => {
      if ([...document.querySelectorAll("form[data-section]")].some(isDirty)) { e.preventDefault(); e.returnValue = ""; }
    });
    wirePlaceSearch();

    try {
      const data = await api("/api/settings");
      state.settings = data.values;
      state.info = data.info;
      state.canEdit = data.can_edit;
      state.canAdmin = data.can_admin;
      for (const form of document.querySelectorAll("form[data-section]")) fillForm(form);
      renderInfo();
      $("s-load").hidden = true;
      $("s-main").hidden = false;
      if (!data.can_edit) lock();
      if (location.hash) document.querySelector(location.hash)?.scrollIntoView();
    } catch (err) {
      if (err.status === 401) { lock(err.message); $("s-load").textContent = ""; }
      else $("s-load").textContent = `Couldn't load settings: ${err.message}`;
      return;
    }
    loadNetwork();
    loadSystem();
  }

  boot();
})();
