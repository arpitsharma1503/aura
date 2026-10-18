/* AURA groceries page (phones). No framework.
   Live sync: Server-Sent Events tell us the list revision changed; we refetch. */
"use strict";

(() => {
  const { icon } = window.AuraIcons;
  const $ = (id) => document.getElementById(id);
  const TIMEOUT_MS = 10000;
  const FALLBACK_POLL_MS = 30000;
  const TOAST_MS = 5000;

  const state = {
    items: [],
    revision: -1,
    canEdit: false,
    pinConfigured: true,
    categories: [],
    suggestions: [],
    filter: readFilter(),
    loaded: false,
    editing: null,
    pendingAfterSignin: null,
    newIds: new Set(),
  };

  function readFilter(fallback = "needed") {
    try {
      const f = localStorage.getItem("aura.groceries.filter");
      return ["needed", "purchased", "all"].includes(f) ? f : fallback;
    } catch { return fallback; }
  }

  function setFilter(f) {
    state.filter = f;
    try { localStorage.setItem("aura.groceries.filter", f); } catch { /* private mode */ }
    if (state.loaded) render();
    else syncFilterButtons();
  }

  function syncFilterButtons() {
    for (const b of document.querySelectorAll(".g-filter__btn")) {
      b.setAttribute("aria-selected", String(b.dataset.filter === state.filter));
    }
  }

  // ---------- networking ----------

  class ApiError extends Error {
    constructor(status, detail, body) { super(detail); this.status = status; this.body = body; }
  }

  async function api(url, { method = "GET", body } = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      const res = await fetch(url, {
        method,
        signal: ctrl.signal,
        cache: "no-store",
        credentials: "same-origin",
        headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
      if (res.status === 204) return null;
      let data = null;
      try { data = await res.json(); } catch { /* empty */ }
      if (!res.ok) {
        let detail = data?.detail;
        if (Array.isArray(detail)) detail = detail.map((d) => `${d.loc?.at(-1) ?? ""} ${d.msg}`.trim()).join("; ");
        throw new ApiError(res.status, detail || `Request failed (${res.status})`, data);
      }
      return data;
    } catch (err) {
      if (err instanceof ApiError) throw err;
      throw new ApiError(0, err.name === "AbortError" ? "The Pi didn't respond in time" : "Can't reach AURA on the home network");
    } finally {
      clearTimeout(timer);
    }
  }

  // ---------- toast ----------

  let toastTimer = null;
  function toast(text, { action, onAction, error = false } = {}) {
    const el = $("g-toast");
    $("g-toast-text").textContent = text;
    el.classList.toggle("is-error", error);
    const btn = $("g-toast-action");
    btn.hidden = !action;
    btn.textContent = action || "";
    btn.onclick = action ? () => { el.hidden = true; onAction(); } : null;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, TOAST_MS);
  }

  function handleError(err, fallback = "Something went wrong") {
    if (err.status === 401) {
      setCanEdit(false);
      openSignin();
      return;
    }
    toast(err.message || fallback, { error: true });
  }

  // ---------- rendering ----------

  function ring() {
    return `<span class="g-check__ring">${icon("check", 3)}</span>`;
  }

  function itemRow(item) {
    const li = document.createElement("li");
    li.className = "g-item" + (item.purchased ? " is-purchased" : "") + (state.newIds.has(item.id) ? " is-new" : "");
    li.dataset.id = item.id;
    li.innerHTML = `
      <button type="button" class="g-check">${ring()}</button>
      <button type="button" class="g-body">
        <span class="g-body__top"><span class="g-name"></span><span class="g-qtytag"></span></span>
        <span class="g-notes"></span>
      </button>`;
    li.querySelector(".g-name").textContent = item.name;
    const qty = li.querySelector(".g-qtytag");
    if (item.quantity) qty.textContent = item.quantity; else qty.remove();
    const notes = li.querySelector(".g-notes");
    if (item.notes) notes.textContent = item.notes; else notes.remove();
    const check = li.querySelector(".g-check");
    check.setAttribute("role", "checkbox");
    check.setAttribute("aria-checked", String(item.purchased));
    check.setAttribute("aria-label", `${item.name}: ${item.purchased ? "purchased" : "mark as purchased"}`);
    check.addEventListener("click", () => togglePurchased(item, li));
    const body = li.querySelector(".g-body");
    body.setAttribute("aria-label", `Edit ${item.name}`);
    body.addEventListener("click", () => openEdit(item));
    return li;
  }

  function showState(kind, title, text, iconName) {
    const el = $("g-state");
    el.className = `g-state${kind === "error" ? " g-state--error" : ""}`;
    el.innerHTML = icon(iconName);
    const h = document.createElement("h2"); h.textContent = title;
    const p = document.createElement("p"); p.textContent = text;
    el.append(h, p);
    if (kind === "error") {
      const b = document.createElement("button");
      b.type = "button"; b.className = "btn"; b.textContent = "Try again";
      b.addEventListener("click", () => load(true));
      el.append(b);
    }
    el.hidden = false;
  }

  function render() {
    const open = state.items.filter((i) => !i.purchased);
    const done = state.items.filter((i) => i.purchased);
    $("g-loading").hidden = true;
    $("g-main").setAttribute("aria-busy", "false");
    $("g-state").hidden = true;

    $("g-sub").textContent = open.length
      ? `${open.length} to buy${done.length ? ` · ${done.length} purchased` : ""}`
      : done.length ? "Everything's bought" : "Nothing on the list";

    syncFilterButtons();
    $("f-needed").textContent = open.length ? String(open.length) : "";
    $("f-purchased").textContent = done.length ? String(done.length) : "";
    const showOpen = state.filter !== "purchased";
    const showDone = state.filter !== "needed";

    const groups = new Map();
    for (const item of showOpen ? open : []) {
      if (!groups.has(item.category_label)) groups.set(item.category_label, []);
      groups.get(item.category_label).push(item);
    }
    $("g-groups").replaceChildren(...[...groups].map(([label, items]) => {
      const sec = document.createElement("section");
      sec.className = "g-group";
      const h = document.createElement("h2");
      h.className = "g-group__title";
      h.textContent = label;
      const ul = document.createElement("ul");
      ul.className = "g-items";
      ul.append(...items.map(itemRow));
      sec.append(h, ul);
      return sec;
    }));

    const addHint = state.canEdit ? "Type below or tap a suggestion to add something." : "Enter the household PIN to start adding items.";
    if (state.filter === "purchased" && !done.length) {
      showState("empty", "Nothing purchased yet", "Tap the circle next to an item when you've bought it.", "check");
    } else if (state.filter !== "purchased" && !open.length && (state.filter === "needed" || !done.length)) {
      showState("empty", done.length ? "All done" : "Your list is empty", addHint, "cart");
    }

    $("g-done").hidden = !showDone || done.length === 0;
    $("g-done-count").textContent = done.length ? `(${done.length})` : "";
    $("g-done-list").replaceChildren(...done.map(itemRow));
    $("g-clear").hidden = !state.canEdit;
    state.newIds.clear();
    renderSuggestions();
  }

  function renderSuggestions() {
    const onList = new Set(state.items.filter((i) => !i.purchased).map((i) => i.name.toLowerCase()));
    const chips = state.suggestions.filter((s) => !onList.has(s.name.toLowerCase())).slice(0, 10);
    $("g-suggest").replaceChildren(...chips.map((s) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "g-chip";
      b.setAttribute("role", "listitem");
      b.textContent = s.name;
      b.setAttribute("aria-label", `Add ${s.name}`);
      b.addEventListener("click", () => addItem({ name: s.name, category: s.category }));
      return b;
    }));
    $("g-suggest").hidden = chips.length === 0;
  }

  // ---------- data ----------

  async function load(showErrors = false) {
    try {
      const data = await api("/api/groceries");
      state.revision = data.revision;
      if (data.status === "error") {
        showState("error", "Couldn't load the list", data.message || "The list couldn't be read on the Pi.", "alert");
        $("g-loading").hidden = true;
        return;
      }
      state.items = data.items;
      state.loaded = true;
      render();
    } catch (err) {
      if (!state.loaded || showErrors) {
        $("g-loading").hidden = true;
        $("g-groups").replaceChildren();
        showState("error", "Can't reach AURA", `${err.message}. Make sure this phone is on the home Wi-Fi.`, "alert");
      }
    }
  }

  async function loadSuggestions() {
    try { state.suggestions = await api("/api/groceries/suggestions"); renderSuggestions(); } catch { /* optional */ }
  }

  async function loadSession() {
    try {
      const s = await api("/api/session");
      state.pinConfigured = s.pin_configured;
      setCanEdit(s.can_edit);
    } catch {
      setCanEdit(false);
    }
  }

  function setCanEdit(can) {
    state.canEdit = can;
    $("g-banner").hidden = can;
    $("g-composer").classList.toggle("is-locked", !can);
    $("g-banner-text").textContent = state.pinConfigured
      ? "View only on this phone."
      : "Editing from phones is off: no household PIN is set on the Pi.";
    $("g-banner-btn").hidden = !state.pinConfigured;
    if (state.loaded) render();
  }

  // ---------- actions ----------

  function requireEdit(retry) {
    if (state.canEdit) return true;
    state.pendingAfterSignin = retry;
    openSignin();
    return false;
  }

  async function addItem(payload) {
    if (!requireEdit(() => addItem(payload))) return;
    $("g-add").disabled = true;
    try {
      const item = await api("/api/groceries", { method: "POST", body: payload });
      state.newIds.add(item.id);
      await load();
      loadSuggestions();
      toast(`Added ${item.name}`, { action: "Undo", onAction: () => removeItem(item, { silent: true }) });
      return item;
    } catch (err) {
      if (err.status === 409 && err.body?.code === "duplicate") {
        toast(`${err.body.item.name} is already on the list`);
        state.newIds.add(err.body.item.id);
        load();
      } else {
        handleError(err);
      }
      return null;
    } finally {
      $("g-add").disabled = false;
    }
  }

  async function togglePurchased(item, li) {
    const next = !item.purchased;
    if (!requireEdit(() => togglePurchased(item, li))) return;
    li.classList.add("is-busy");
    li.classList.toggle("is-purchased", next);
    try {
      await api(`/api/groceries/${item.id}`, { method: "PATCH", body: { purchased: next } });
      await load();
      if (next) {
        toast(`${item.name} purchased`, {
          action: "Undo",
          onAction: () => togglePurchased({ ...item, purchased: true }, li),
        });
      }
    } catch (err) {
      li.classList.remove("is-busy");
      li.classList.toggle("is-purchased", item.purchased);
      if (err.status === 404) { toast("That item was removed on another device"); load(); return; }
      if (err.status === 409) { toast(err.message); load(); return; }
      handleError(err);
    }
  }

  async function removeItem(item, { silent = false } = {}) {
    try {
      await api(`/api/groceries/${item.id}`, { method: "DELETE" });
      if (!silent) toast(`Deleted ${item.name}`);
    } catch (err) {
      if (err.status !== 404) handleError(err);
    }
    await load();
    loadSuggestions();
  }

  // ---------- composer ----------

  function stepQuantity(delta) {
    const input = $("g-qty");
    const m = input.value.trim().match(/^(\d+(?:\.\d+)?)(.*)$/);
    let n = m ? parseFloat(m[1]) : (delta > 0 ? 0 : 1);
    const unit = m ? m[2] : "";
    n = Math.max(1, Math.round((n + delta) * 100) / 100);
    input.value = `${n}${unit}`;
  }

  function wireComposer() {
    const form = $("g-composer");
    const name = $("g-name");
    name.addEventListener("focus", () => { $("g-extra").hidden = false; });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $("g-form-error");
      err.hidden = true;
      const value = name.value.trim();
      if (!value) { name.focus(); return; }
      if (value.length > 60) { err.textContent = "Keep item names under 60 characters."; err.hidden = false; return; }
      const payload = { name: value };
      const qty = $("g-qty").value.trim();
      if (qty) payload.quantity = qty;
      if ($("g-cat").value) payload.category = $("g-cat").value;
      const item = await addItem(payload);
      if (item) {
        name.value = "";
        $("g-qty").value = "";
        $("g-cat").value = "";
        name.focus();
      }
    });
    for (const b of form.querySelectorAll(".g-step")) {
      b.addEventListener("click", () => stepQuantity(Number(b.dataset.step)));
    }
    // Collapse extras when focus leaves the composer entirely.
    form.addEventListener("focusout", () => {
      setTimeout(() => {
        if (!form.contains(document.activeElement) && !name.value && !$("g-qty").value) $("g-extra").hidden = true;
      }, 150);
    });
  }

  // ---------- edit sheet ----------

  function fillCategories(select, includeAuto) {
    select.replaceChildren();
    if (includeAuto) select.append(new Option("Auto category", ""));
    for (const c of state.categories) select.append(new Option(c.label, c.id));
  }

  function fillEdit(item) {
    state.editing = item;
    $("e-name").value = item.name;
    $("e-qty").value = item.quantity || "";
    $("e-cat").value = item.category;
    $("e-notes").value = item.notes || "";
  }

  function openEdit(item) {
    if (!requireEdit(() => openEdit(item))) return;
    fillEdit(item);
    $("e-status").textContent = "";
    $("e-status").classList.remove("is-error");
    $("g-edit").showModal();
  }

  async function saveEdit(e) {
    e.preventDefault();
    const item = state.editing;
    const name = $("e-name").value.trim();
    const status = $("e-status");
    if (!name) { status.textContent = "The item needs a name."; status.classList.add("is-error"); return; }
    const body = {
      name,
      quantity: $("e-qty").value.trim() || null,
      category: $("e-cat").value,
      notes: $("e-notes").value.trim() || null,
      version: item.version,
    };
    $("e-save").disabled = true;
    try {
      await api(`/api/groceries/${item.id}`, { method: "PATCH", body });
      $("g-edit").close();
      toast("Saved");
      load();
    } catch (err) {
      if (err.status === 409 && err.body?.code === "conflict") {
        fillEdit(err.body.item);
        status.textContent = "Changed on another phone. Showing the latest. Make your edit again.";
        status.classList.add("is-error");
        load();
      } else if (err.status === 409) {
        status.textContent = err.message;
        status.classList.add("is-error");
      } else if (err.status === 404) {
        $("g-edit").close();
        toast("That item was removed on another device");
        load();
      } else if (err.status === 401) {
        $("g-edit").close();
        handleError(err);
      } else {
        status.textContent = err.message;
        status.classList.add("is-error");
      }
    } finally {
      $("e-save").disabled = false;
    }
  }

  // ---------- confirm ----------

  function confirmAction(title, body, okLabel) {
    return new Promise((resolve) => {
      const dlg = $("g-confirm");
      $("c-title").textContent = title;
      $("c-body").textContent = body;
      $("c-ok").textContent = okLabel;
      const done = (v) => { dlg.close(); resolve(v); };
      $("c-ok").onclick = () => done(true);
      $("c-cancel").onclick = () => done(false);
      dlg.oncancel = () => resolve(false);
      dlg.showModal();
      $("c-cancel").focus();
    });
  }

  // ---------- sign in ----------

  function openSignin() {
    if (!state.pinConfigured) {
      toast("Editing from phones is off: set AURA_HOUSEHOLD_PIN on the Pi.", { error: true });
      return;
    }
    $("s-status").textContent = "";
    $("s-pin").value = "";
    $("g-signin").showModal();
    $("s-pin").focus();
  }

  async function submitSignin(e) {
    e.preventDefault();
    const pin = $("s-pin").value.trim();
    const status = $("s-status");
    if (!pin) return;
    $("s-submit").disabled = true;
    try {
      await api("/api/session", { method: "POST", body: { pin } });
      $("g-signin").close();
      setCanEdit(true);
      toast("This phone can now edit the list");
      const retry = state.pendingAfterSignin;
      state.pendingAfterSignin = null;
      if (retry) retry();
    } catch (err) {
      status.textContent = err.message;
      status.classList.add("is-error");
      $("s-pin").select();
    } finally {
      $("s-submit").disabled = false;
    }
  }

  // ---------- live updates ----------

  let source = null;
  let pollTimer = null;

  function setLive(kind) {
    const el = $("g-live");
    el.dataset.state = kind;
    $("g-live-text").textContent = { live: "Live", offline: "Reconnecting", connecting: "Connecting" }[kind];
  }

  function connect() {
    if (source) source.close();
    if (!("EventSource" in window)) { setLive("offline"); return; }
    source = new EventSource("/api/events");
    setLive("connecting");
    const sync = (e) => {
      try {
        const data = JSON.parse(e.data);
        const rev = data.groceries ?? data.revision;
        if (rev !== state.revision) load();
      } catch { load(); }
    };
    source.addEventListener("hello", (e) => { setLive("live"); sync(e); });
    source.addEventListener("groceries", sync);
    source.onerror = () => setLive("offline");   // EventSource retries by itself
  }

  function wireLive() {
    connect();
    // Phones suspend background tabs: re-sync and reconnect when we're visible again.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        load();
        if (!source || source.readyState === EventSource.CLOSED) connect();
      }
    });
    clearInterval(pollTimer);
    pollTimer = setInterval(() => { if (!source || source.readyState !== EventSource.OPEN) load(); }, FALLBACK_POLL_MS);
  }

  // ---------- wiring ----------

  function wire() {
    wireComposer();
    // Back to the dashboard: use history when we came from it (keeps its state).
    $("g-home").addEventListener("click", (e) => {
      try {
        const ref = document.referrer ? new URL(document.referrer) : null;
        if (ref && ref.origin === location.origin && ref.pathname === "/" && history.length > 1) {
          e.preventDefault();
          history.back();
        }
      } catch { /* fall back to the plain link */ }
    });
    $("g-banner-btn").addEventListener("click", openSignin);
    for (const b of document.querySelectorAll(".g-filter__btn")) {
      b.addEventListener("click", () => setFilter(b.dataset.filter));
    }
    syncFilterButtons();
    $("g-clear").addEventListener("click", async () => {
      const n = state.items.filter((i) => i.purchased).length;
      if (!requireEdit(null)) return;
      const ok = await confirmAction(`Clear ${n} purchased item${n === 1 ? "" : "s"}?`,
        "They'll be removed from the list on every screen. This can't be undone.", "Clear");
      if (!ok) return;
      try {
        const r = await api("/api/groceries/clear-purchased", { method: "POST" });
        toast(`Cleared ${r.deleted} item${r.deleted === 1 ? "" : "s"}`);
        load();
      } catch (err) { handleError(err); }
    });
    $("g-edit-form").addEventListener("submit", saveEdit);
    $("e-delete").addEventListener("click", async () => {
      const item = state.editing;
      const ok = await confirmAction(`Delete ${item.name}?`,
        "It will be removed from the list on every screen.", "Delete");
      if (!ok) return;
      $("g-edit").close();
      removeItem(item);
    });
    $("g-signin-form").addEventListener("submit", submitSignin);
    for (const b of document.querySelectorAll("[data-close]")) {
      b.addEventListener("click", () => b.closest("dialog").close());
    }
  }

  async function boot() {
    wire();
    try {
      state.categories = await api("/api/groceries/categories");
    } catch { state.categories = []; }
    fillCategories($("g-cat"), true);
    fillCategories($("e-cat"), false);
    await loadSession();
    try {
      // Settings → Groceries → "Phones open on" applies until this phone picks a tab itself.
      const cfg = await api("/api/config");
      state.filter = readFilter(cfg.groceries?.default_filter || "needed");
      document.body.classList.toggle("force-reduce", cfg.display?.reduced_motion === "reduce");
    } catch { /* keep default */ }
    await load(true);
    loadSuggestions();
    wireLive();
  }

  boot();
})();
