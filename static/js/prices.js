/* AURA Price Tracker: gold (24K, 22K, 20K, 18K per 10 g) and silver (per kg) in India.
   Prices are IBJA benchmarks read once a day at 12:00 (app/prices.py). Changes are against the
   previous day's reading; the chart shows only readings AURA actually recorded. */
"use strict";

window.AuraPrices = (() => {
  const $ = (id) => document.getElementById(id);
  const RANGES = [[7, "7 days"], [30, "30 days"], [90, "90 days"], [365, "1 year"]];
  const INR = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
  const NUM = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
  let ctx = { icon: () => "", formatTime: (d) => d.toLocaleTimeString(), refresh: async () => null };
  let data = null;
  let root = null;
  let pick = "gold24", range = 30, flash = null;

  function el(tag, attrs = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "html") n.innerHTML = v;                    // our own SVG only
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat(Infinity)) if (k != null && k !== false) n.append(k instanceof Node ? k : document.createTextNode(String(k)));
    return n;
  }
  const svg = (tag, attrs) => {
    const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  };

  /** "▲ ₹1,630 (+1.16%)" with direction for colour, and words for screen readers. */
  function change(i) {
    if (i.change == null) return { dir: "none", text: "first reading", label: "No earlier reading yet" };
    const dir = i.change > 0 ? "up" : i.change < 0 ? "down" : "flat";
    const arrow = { up: "▲", down: "▼", flat: "▬" }[dir];
    const pct = i.change_pct == null ? "" : ` (${i.change_pct > 0 ? "+" : ""}${i.change_pct.toFixed(2)}%)`;
    const word = { up: "up", down: "down", flat: "unchanged" }[dir];
    return { dir, text: `${arrow} ${INR.format(Math.abs(i.change))}${pct}`,
      label: dir === "flat" ? "Unchanged since the previous day" : `${word} ${INR.format(Math.abs(i.change))}${pct} since ${i.previous_day}` };
  }
  const updated = (d) => d?.fetched_at
    ? `${new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "Asia/Kolkata" }).format(new Date(d.fetched_at))}, ${ctx.formatTime(new Date(d.fetched_at))}`
    : "—";

  // ---------- tile ----------
  function renderTile(d, size) {
    data = d;
    const tile = $("prices-tile"), body = $("prices-body");
    $("prices-aside").textContent = d?.day ? `IBJA · ${updated(d)}` : "IBJA";
    if (!d || d.status === "not_configured" || d.status === "unavailable") {
      tile.dataset.state = d?.status === "not_configured" ? "off" : "error";
      body.replaceChildren(el("p", { class: "tile__empty" }, el("b", {}, d?.status === "not_configured" ? "Not set up" : "No prices yet"),
        d?.message || "Loading…"));
      return;
    }
    tile.dataset.state = d.status === "stale" ? "alert" : "ok";
    const shown = size === "s" ? d.items.filter((i) => i.id === "gold24" || i.id === "silver") : d.items;
    body.replaceChildren(el("ul", { class: "pt-list", "data-size": size },
      shown.map((i) => {
        const c = change(i);
        return el("li", { class: "pt-row", "data-id": i.id },
          el("span", { class: "pt-row__name" }, i.label, el("small", {}, `/ ${i.unit}${i.estimate ? " · est." : ""}`)),
          el("span", { class: "pt-row__price" }, INR.format(i.price)),
          el("span", { class: "pt-chg", "data-dir": c.dir, "aria-label": c.label }, c.text));
      })),
      ...(d.status === "stale" ? [el("p", { class: "pt-stale" }, d.message)] : []));
  }

  // ---------- detail ----------
  function chart(series, unit) {
    const W = 760, H = 240, L = 92, R = 16, T = 16, B = 34;
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "pt-chart__svg", role: "img",
      "aria-label": `Price history, ${series.length} daily readings, ₹ per ${unit}` });
    const vals = series.map((p) => p.v);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (hi === lo) { hi += hi * 0.005 || 1; lo -= lo * 0.005 || 1; }
    const pad = (hi - lo) * 0.12; lo -= pad; hi += pad;
    const x = (i) => L + (series.length === 1 ? (W - L - R) / 2 : (i / (series.length - 1)) * (W - L - R));
    const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    for (let k = 0; k <= 3; k++) {                          // y grid with ₹ labels
      const v = lo + ((hi - lo) * k) / 3;
      s.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "pt-grid" }));
      const t = svg("text", { x: L - 8, y: y(v) + 4, "text-anchor": "end", class: "pt-axis" });
      t.textContent = `₹${NUM.format(v)}`;
      s.append(t);
    }
    const fmtDay = (iso) => new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }).format(new Date(`${iso}T12:00:00Z`));
    for (const i of [0, series.length - 1].filter((v, k, a) => a.indexOf(v) === k)) {
      const t = svg("text", { x: x(i), y: H - 10, "text-anchor": i === 0 && series.length > 1 ? "start" : "end", class: "pt-axis" });
      t.textContent = fmtDay(series[i].day);
      s.append(t);
    }
    if (series.length > 1) {
      const d = series.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
      s.append(svg("path", { d: `${d} L${x(series.length - 1)},${H - B} L${x(0)},${H - B} Z`, class: "pt-area" }));
      s.append(svg("path", { d, class: "pt-line" }));
    }
    series.forEach((p, i) => {
      const c = svg("circle", { cx: x(i), cy: y(p.v), r: series.length > 40 ? 2 : 3.5, class: "pt-dot" });
      const tt = svg("title", {}); tt.textContent = `${fmtDay(p.day)}: ${INR.format(p.v)} / ${unit}`;
      c.append(tt); s.append(c);
    });
    return s;
  }

  async function doRefresh() {
    flash = { text: "Checking…", kind: "info" }; paint();
    try {
      data = await ctx.refresh();
      flash = { text: "Updated.", kind: "ok" };
    } catch (err) { flash = { text: err.message, kind: "error" }; }
    paint();
  }

  function paint() {
    if (!root) return;
    const d = data;
    $("detail-sub").textContent = "Gold & Silver · India";
    if (!d) { root.replaceChildren(el("p", { class: "dnote" }, "Loading prices…")); return; }
    const head = el("div", { class: "pt-head" },
      el("div", {}, el("p", { class: "pt-src" }, d.source), el("p", { class: "pt-meta" },
        d.day ? `Last reading ${updated(d)} · ${d.schedule}` : d.schedule)),
      el("button", { type: "button", class: "btn", onclick: doRefresh }, "Refresh"));
    const out = [head];
    if (flash) out.push(el("p", { class: "sh-flash", "data-kind": flash.kind, role: "status" }, flash.text));
    if (d.status !== "ok") out.push(el("p", { class: `dnote ${d.status === "stale" ? "dnote--warn" : ""}` }, d.message));
    if (d.items.length) {
      out.push(el("div", { class: "pt-cards" }, d.items.map((i) => {
        const c = change(i);
        return el("button", { type: "button", class: `pt-card${pick === i.id ? " is-picked" : ""}`, "aria-pressed": String(pick === i.id),
          onclick: () => { pick = i.id; paint(); } },
          el("span", { class: "pt-card__name" }, i.label),
          el("span", { class: "pt-card__price" }, INR.format(i.price), el("small", {}, ` / ${i.unit}`)),
          el("span", { class: "pt-chg", "data-dir": c.dir, "aria-label": c.label }, c.text),
          el("span", { class: "pt-card__note" }, i.estimate ? `Estimate: 24K × ${i.id.slice(4)}/24` : i.note));
      })));
      const item = d.items.find((i) => i.id === pick) || d.items[0];
      const since = new Date(Date.now() - range * 86400000).toISOString().slice(0, 10);
      const series = d.history.filter((h) => h.day >= since).map((h) => ({ day: h.day, v: h[item.id] }));
      out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, `${item.label} · ₹ per ${item.unit}`),
        el("div", { class: "mp-tabs pt-ranges", role: "tablist", "aria-label": "Time range" }, RANGES.map(([n, label]) =>
          el("button", { type: "button", role: "tab", class: "mp-tab", "aria-selected": String(range === n), onclick: () => { range = n; paint(); } }, label))),
        series.length ? el("div", { class: "pt-chart" }, chart(series, item.unit)) : null,
        el("p", { class: "dnote dnote--muted" }, series.length < 2
          ? `History builds up one reading a day at 12:00, starting ${d.history[0]?.day || "with the first reading"}. Only real recorded readings are drawn.`
          : `${series.length} daily readings recorded by AURA (12:00 IST), ${series[0].day} to ${series.at(-1).day}.`)));
    }
    out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "About these prices"),
      el("ul", { class: "pt-about" },
        el("li", {}, d.basis),
        el("li", {}, "24K and silver are IBJA's published 999 rates. 22K, 20K and 18K are estimates (24K × 22/24, 20/24, 18/24), not jewellers' quotes."),
        el("li", {}, "A national benchmark, not a price in a particular city or shop."),
        el("li", {}, `Change is since the previous day's reading. Requests this month: ${d.requests_this_month} of the free plan's 100.`))));
    root.replaceChildren(...out);
  }

  return {
    init(opts) { ctx = { ...ctx, ...opts }; },
    renderTile,
    view() { root = el("div", { class: "pt" }); flash = null; paint(); return root; },
    refresh(d) { if (d) data = d; if (root?.isConnected) paint(); },
  };
})();
