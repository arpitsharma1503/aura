/* AURA meal plan view ("1-Month Body Recomposition Plan").
   Tabs: Today (meals, portions, Prepare for Tomorrow) · Groceries · Recipes · Plan.
   A recipe opens as a page inside the view. Everything shown comes from
   /api/meals/* - i.e. from the plan PDF - and the household's own ticks. */
"use strict";

window.AuraMeals = (() => {
  const $ = (id) => document.getElementById(id);
  const MEAL = { breakfast: "Breakfast", lunch: "Lunch", dinner: "Dinner" };
  const TABS = [["today", "Today"], ["groceries", "Groceries"], ["recipes", "Recipes"], ["plan", "Plan & training"]];
  const PERSON_KEY = "aura.meals.person";

  let icon = () => "";
  let root = null;
  let tab = "today";
  let person = null;            // "person1" | "person2" | "both"
  let recipe = null;            // { code, servings, data } when a recipe page is open
  let day = null, groceries = null, recipes = null, plan = null;
  let busy = false;

  // ---------- helpers ----------
  async function api(url, { method = "GET", body } = {}) {
    const res = await fetch(url, {
      method, cache: "no-store",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* empty */ }
    if (!res.ok) {
      const e = new Error(typeof data?.detail === "string" ? data.detail : `HTTP ${res.status}`);
      e.status = res.status; throw e;
    }
    return data;
  }
  function el(tag, attrs = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "html") n.innerHTML = v;            // our own SVG icons only
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat(Infinity)) if (k != null && k !== false) n.append(k instanceof Node ? k : document.createTextNode(String(k)));
    return n;
  }
  const fmtDate = (iso, opts = { weekday: "long", day: "numeric", month: "long" }) =>
    new Intl.DateTimeFormat("en-GB", { ...opts, timeZone: "UTC" }).format(new Date(`${iso}T12:00:00Z`));
  const names = () => day?.people || { person1: "Person 1", person2: "Person 2" };
  const macros = (b) => `${b.kcal} kcal · ${b.protein_g} g protein · ${b.carbs_g} g carbs · ${b.fat_g} g fat · ${b.fibre_g} g fibre`;
  const note = (text, cls = "") => el("p", { class: `dnote ${cls}` }, text);
  const plus = (t) => (t.startsWith("+") ? t.replace(/^\+\s*/, "+ ") : `+ ${t}`);
  const hindi = (text) => el("p", { class: "mp-hindi", lang: "hi-Latn" }, text);

  // ---------- data ----------
  async function loadDay() {
    day = await api("/api/meals/day");
    if (!person) {
      try { person = localStorage.getItem(PERSON_KEY); } catch { /* private mode */ }
      if (!["person1", "person2", "both"].includes(person)) person = day.people.default_person || "both";
    }
  }

  async function refresh() {
    if (!root || !$("detail")?.open || $("detail").dataset.view !== "meals") return;
    try { await loadDay(); if (tab === "groceries") groceries = await api("/api/meals/groceries"); } catch { /* keep */ }
    paint();
  }

  async function show(nextTab) {
    tab = nextTab; recipe = null;
    paint();
    try {
      if (tab === "groceries") groceries = await api("/api/meals/groceries");
      if (tab === "recipes" && !recipes) recipes = await api("/api/meals/recipes");
      if (tab === "plan" && !plan) plan = await api("/api/meals/plan");
    } catch (err) { paintError(err); return; }
    paint();
  }

  async function openRecipe(code, servings = 2) {
    try {
      const data = await api(`/api/meals/recipe/${encodeURIComponent(code)}?servings=${servings}`);
      recipe = { code, servings, data };
    } catch (err) { paintError(err); return; }
    paint();
    $("detail-body")?.scrollTo?.({ top: 0 });
  }

  // ---------- actions ----------
  async function markEaten(meal, who, done) {
    if (busy) return; busy = true;
    try {
      const r = await api("/api/meals/eaten", { method: "POST", body: { date: day.date, meal, person: who, done } });
      day.eaten = r.eaten;
    } catch (err) { flash(err.message); }
    busy = false; paint();
  }
  async function markPrep(task, done) {
    try {
      const r = await api("/api/meals/prep", { method: "POST", body: { date: day.tomorrow.date, task, done } });
      for (const t of day.tomorrow.tasks) t.done = !!r.done[t.id];
    } catch (err) { flash(err.message); }
    paint();
  }
  async function markBought(item, purchased) {
    try {
      const r = await api("/api/meals/groceries/check", { method: "POST", body: { week: groceries.week_key, item: item.id, purchased } });
      for (const c of groceries.categories) for (const i of c.items) i.purchased = !!r.purchased[i.id];
    } catch (err) { flash(err.message); }
    paint();
  }
  async function addToList(items) {
    if (!items.length) return;
    try {
      const r = await api("/api/meals/groceries/add", { method: "POST", body: { items } });
      groceries = await api("/api/meals/groceries");
      flash(r.added.length ? `Added ${r.added.length} to the shopping list${r.skipped.length ? ` · ${r.skipped.length} already there` : ""}.`
        : "Already on the shopping list.");
    } catch (err) { flash(err.message); }
    paint();
  }
  async function addRestOfMonth() {
    try {
      const r = await api("/api/meals/groceries/rest-of-month", { method: "POST" });
      groceries = await api("/api/meals/groceries");
      flash(`Added ${r.added.length}${r.updated.length ? ` · updated ${r.updated.length}` : ""}${r.skipped.length ? ` · ${r.skipped.length} already on the list` : ""}.`);
    } catch (err) { flash(err.message); }
    paint();
  }
  function batchLabel(b) {
    const m = /-(rest|month|w(\d)-fresh)$/.exec(b.batch);
    const what = !m ? b.batch : m[1] === "rest" ? "Rest of the month (dry goods + this week's fresh)"
      : m[1] === "month" ? "Whole month's dry goods" : `Week ${m[2]} fresh items`;
    return el("li", {}, what, el("small", {}, b.added || b.updated ? `${b.added} added${b.updated ? `, ${b.updated} updated` : ""}`
      : "covered by an earlier batch"));
  }
  function autoSection(a) {
    const shown = a.batches.filter((b) => b.added || b.updated || /-rest$/.test(b.batch));
    const canRest = !a.month_done && !a.batches.some((b) => /-rest$/.test(b.batch));
    return el("section", { class: "dsec", id: "dsec-auto-groceries" }, el("h3", { class: "dsec__title" }, "Automatic shopping list"),
      note(a.enabled ? `${a.rule} Pulses, flours and other dry goods come as one monthly amount; paneer, tofu, dairy, vegetables and fruit week by week. Items you added yourself are never changed.`
        : "Turned off in Settings → Meal plan.", "dnote--muted"),
      shown.length ? el("ul", { class: "mp-batches" }, shown.map(batchLabel)) : null,
      canRest ? el("div", { class: "mp-actions" }, el("button", { type: "button", class: "btn btn--primary", id: "mp-rest", onclick: addRestOfMonth },
        "Add the rest of this month")) : null);
  }
  async function saveNote(text) {
    try { day.note = (await api("/api/meals/note", { method: "POST", body: { date: day.date, text } })).note; flash("Note saved."); }
    catch (err) { flash(err.message); }
  }
  let flashTimer = null;
  let flashText = null;          // kept until it times out, so a repaint right after doesn't wipe it
  function flash(text) {
    flashText = text;
    const f = root?.querySelector(".mp-flash");
    if (f) { f.textContent = text; f.hidden = false; }
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { flashText = null; const g = root?.querySelector(".mp-flash"); if (g) g.hidden = true; }, 4000);
  }

  // ---------- rendering ----------
  function build() {
    root = el("div", { class: "mp" });
    return root;
  }

  function paintError(err) {
    if (!root) return;
    root.replaceChildren(el("div", { class: "dempty" }, el("span", { html: icon("alert") }),
      el("div", {}, el("strong", {}, err.status === 404 ? "Meal plan is off" : "Couldn't load the meal plan"),
        el("p", {}, err.message))));
  }

  function controls() {
    const tabs = el("div", { class: "mp-tabs", role: "tablist", "aria-label": "Meal plan sections" },
      TABS.map(([id, label]) => el("button", { type: "button", role: "tab", class: "mp-tab", "aria-selected": String(tab === id && !recipe),
        "data-tab": id, onclick: () => show(id) }, label)));
    const who = el("div", { class: "mp-seg", role: "radiogroup", "aria-label": "Show portions for" },
      [["person1", names().person1], ["person2", names().person2], ["both", "Both · shared cooking"]].map(([id, label]) =>
        el("button", { type: "button", role: "radio", class: "mp-seg__btn", "aria-checked": String(person === id), "data-person": id,
          onclick: () => { person = id; try { localStorage.setItem(PERSON_KEY, id); } catch { /* */ } paint(); } }, label)));
    return el("div", { class: "mp-controls" }, tabs, who);
  }

  function paint() {
    if (!root) return;
    if (!day) { root.replaceChildren(note("Loading the meal plan…")); return; }
    $("detail-sub").textContent = `Day ${day.plan_day} of 30 · ${fmtDate(day.date)}`;
    const body = recipe ? recipePage() : tab === "groceries" ? groceriesTab() : tab === "recipes" ? recipesTab()
      : tab === "plan" ? planTab() : todayTab();
    root.replaceChildren(controls(), el("p", { class: "mp-flash", role: "status", hidden: !flashText }, flashText || ""), ...[].concat(body));
  }

  // ----- Today -----
  function portionLine(m) {
    const n = names();
    const p1 = m.person1_extra;
    const extraBits = p1.kcal != null ? ` (+${p1.kcal} kcal, +${p1.protein_g} g protein)` : "";
    if (person === "person2") return [el("p", { class: "mp-portion" }, el("b", {}, `${n.person2}: `), "base plate"), el("p", { class: "mp-macros" }, macros(m.base_serving))];
    if (person === "person1") return [el("p", { class: "mp-portion" }, el("b", {}, `${n.person1}: `), `base plate ${plus(p1.text)}${extraBits}`),
      el("p", { class: "mp-macros" }, `Base plate: ${macros(m.base_serving)}`)];
    return [el("p", { class: "mp-portion" }, el("b", {}, "Cook: "), "one batch (recipe makes 2 servings). ",
      el("b", {}, `${n.person2}: `), "base plate. ", el("b", {}, `${n.person1}: `), `base plate ${plus(p1.text)}${extraBits}.`),
      el("p", { class: "mp-macros" }, `Base plate: ${macros(m.base_serving)}`)];
  }

  function eatenButtons(m) {
    const who = person === "both" ? ["person1", "person2"] : [person];
    return el("div", { class: "mp-eaten" }, who.map((p) => {
      const done = day.eaten[m.meal][p];
      return el("button", { type: "button", class: "mp-check", "aria-pressed": String(done),
        "aria-label": `${MEAL[m.meal]} eaten by ${names()[p]}`,
        onclick: (e) => { e.stopPropagation(); markEaten(m.meal, p, !done); } },
        el("span", { class: "mp-check__box", html: icon("check", 2.6) }), person === "both" ? names()[p] : "Eaten");
    }));
  }

  function mealCard(m, { compact = false } = {}) {
    return el("li", { class: `mp-meal${compact ? " mp-meal--compact" : ""}`, "data-meal": m.meal },
      el("button", { type: "button", class: "mp-meal__open", "aria-label": `${MEAL[m.meal]}: ${m.name} — open recipe`, onclick: () => openRecipe(m.code) },
        el("span", { class: "mp-meal__type" }, MEAL[m.meal]),
        el("span", { class: "mp-meal__name" }, m.name),
        el("span", { class: "mp-meal__tags" }, el("span", { class: "mp-code" }, m.code), m.paneer ? el("span", { class: "mp-badge" }, "paneer") : null),
        el("span", { class: "mp-meal__arrow", "aria-hidden": "true" }, "›")),
      compact ? null : portionLine(m),
      compact ? null : eatenButtons(m));
  }

  function totals() {
    const n = names(), t = day.totals;
    const cell = (p) => el("div", { class: "dmetric" }, el("dt", {}, n[p]), el("dd", {}, `${t.kcal[p].toLocaleString("en-IN")} kcal`,
      el("small", {}, `${t.protein_g[p]} g protein`)));
    const who = person === "both" ? ["person1", "person2"] : [person];
    return el("dl", { class: "dmetrics mp-totals" }, who.map(cell));
  }

  function prepSection() {
    const t = day.tomorrow;
    const tonight = t.tasks.filter((x) => x.when === "tonight");
    const tomorrow = t.tasks.filter((x) => x.when === "tomorrow");
    const task = (x) => el("li", { class: `mp-task${x.done ? " is-done" : ""}` },
      el("label", {}, el("input", { type: "checkbox", checked: x.done, onchange: (e) => markPrep(x.id, e.target.checked) }),
        el("span", {}, x.text,
          el("small", {}, x.recipe ? `${x.recipe} · from the recipe: “${x.source}”` : `Plan: ${x.source}`),
          x.hindi ? el("small", { class: "mp-hindi", lang: "hi-Latn" }, x.hindi) : null)));
    const nothing = (when) => el("li", { class: "mp-task mp-task--none" },
      when === "tonight" ? "No soaking or overnight preparation is specified for tomorrow's meals."
        : "Nothing to do in advance tomorrow — cook as the recipes say.");
    return el("section", { class: "dsec", id: "dsec-prepare", "aria-labelledby": "mp-prep-t" },
      el("h3", { class: "dsec__title", id: "mp-prep-t" }, "Prepare for tomorrow"),
      el("p", { class: "mp-when" }, `${fmtDate(t.date)} · Day ${t.plan_day}`, t.month_restart ? " · new month, the plan starts again" : ""),
      el("ul", { class: "mp-meals mp-meals--compact" }, t.meals.map((m) => mealCard(m, { compact: true }))),
      el("div", { class: "mp-tasks" },
        el("div", {}, el("h4", {}, "Tonight"), el("ul", {}, tonight.length ? tonight.map(task) : nothing("tonight"))),
        el("div", {}, el("h4", {}, "Tomorrow, before cooking"), el("ul", {}, tomorrow.length ? tomorrow.map(task) : nothing("tomorrow")))));
  }

  function trainingLine() {
    const tr = day.training, n = names();
    const s = (p) => tr[p].session ? `${n[p]}: ${tr[p].session}` : `${n[p]}: rest day`;
    return note(`Training today (Week ${tr.week}): ${s("person1")} · ${s("person2")}`, "dnote--muted");
  }

  function todayTab() {
    const out = [];
    const head = el("div", { class: "mp-dayhead" },
      el("div", {}, el("p", { class: "mp-day" }, `Day ${day.plan_day}`), el("p", { class: "mp-date" }, fmtDate(day.date))),
      totals());
    out.push(head);
    if (day.beyond_plan) out.push(note(`The plan has 30 days, so the ${new Date(`${day.date}T12:00:00Z`).getUTCDate()}st follows Day 30. It restarts at Day 1 on the 1st.`, "dnote--muted"));
    if (day.week_note) out.push(note(day.week_note, "dnote--muted"));
    out.push(el("ul", { class: "mp-meals" }, day.meals.map((m) => mealCard(m))));
    if (person === "both") out.push(note(`${day.portion_rule.plating_tip}`, "dnote--muted"), hindi(day.portion_rule.hindi));
    out.push(prepSection());
    out.push(trainingLine());
    const ta = el("textarea", { class: "sheet__input mp-note", maxlength: "500", rows: "2", placeholder: "Note for today (optional)" });
    ta.value = day.note || "";
    out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Notes"), ta,
      el("button", { type: "button", class: "btn", onclick: () => saveNote(ta.value) }, "Save note")));
    out.push(note("Nutrition figures are the plan's estimates, not medical advice or measurements.", "dnote--muted"));
    return out;
  }

  // ----- Recipe page -----
  function recipePage() {
    const r = recipe.data, n = names();
    const p1 = r.person1_extra;
    const extraBits = p1.kcal != null ? ` (+${p1.kcal} kcal, +${p1.protein_g} g protein)` : "";
    const stepper = el("div", { class: "mp-stepper", role: "group", "aria-label": "Servings" },
      el("button", { type: "button", class: "mp-step", "aria-label": "Fewer servings", disabled: recipe.servings <= 1, onclick: () => openRecipe(r.code, recipe.servings - 1) }, "−"),
      el("span", { class: "mp-step__val" }, `${recipe.servings} serving${recipe.servings === 1 ? "" : "s"}`),
      el("button", { type: "button", class: "mp-step", "aria-label": "More servings", disabled: recipe.servings >= 8, onclick: () => openRecipe(r.code, recipe.servings + 1) }, "+"));
    const portions = el("dl", { class: "dmetrics" },
      el("div", { class: "dmetric" }, el("dt", {}, `${n.person2} · base plate`), el("dd", {}, `${r.base_serving.kcal} kcal`, el("small", {}, `${r.base_serving.protein_g} g protein`))),
      el("div", { class: "dmetric" }, el("dt", {}, `${n.person1} · base + extras`),
        el("dd", {}, p1.kcal != null ? `${r.base_serving.kcal + p1.kcal} kcal` : "—", el("small", {}, p1.kcal != null ? `${r.base_serving.protein_g + p1.protein_g} g protein` : "extras not quantified"))));
    const out = [
      el("button", { type: "button", class: "mp-back", onclick: () => { recipe = null; paint(); } },
        "‹ ", { today: "Today", recipes: "All recipes", plan: "Plan & training", groceries: "Groceries" }[tab] || "Back"),
      el("header", { class: "mp-rhead" },
        el("p", { class: "mp-meal__type" }, `${MEAL[r.meal]} · ${r.code}`, r.paneer ? " · paneer" : ""),
        el("h3", { class: "mp-rtitle" }, r.name),
        el("p", { class: "mp-used" }, `On the plan: Day ${r.used_on_days.join(", ")}`)),
      el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Per base serving (Person 2's plate)"),
        el("p", { class: "mp-macros mp-macros--big" }, macros(r.base_serving)), portions,
        el("p", { class: "mp-portion" }, el("b", {}, `${n.person1} extra: `), `${p1.text}${extraBits}.`)),
      el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Ingredients"),
        el("div", { class: "mp-scalebar" }, stepper,
          el("small", {}, recipe.servings === (r.batch_servings || 2)
            ? `The plan's quantities: one shared batch (${r.batch_servings || 2} servings), before ${n.person1}'s extras.`
            : `Scaled ×${r.factor} from the plan's ${r.batch_servings || 2}-serving batch. Nutrition per serving doesn't change.`)),
        el("ul", { class: "mp-ings" }, r.scaled_ingredients.map((i) => el("li", {},
          i.scaled, i.scaled !== i.text ? el("small", {}, `plan: ${i.text}`) : null))),
        recipe.servings === 2 ? null : note("Only amounts written in the plan are scaled; items without an amount (\"salt\", \"a handful\") stay as written.", "dnote--muted"),
        el("p", { class: "mp-portion" }, el("b", {}, `Plus for ${n.person1}: `), p1.text)),
      el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Method"),
        el("ol", { class: "mp-steps" }, r.method.map((s) => el("li", {}, s))),
        note("Preparation time isn't given in the plan.", "dnote--muted")),
    ];
    if (r.prep.length) {
      out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Advance preparation"),
        el("ul", { class: "mp-plain" }, r.prep.map((p) => el("li", {}, `${p.when === "tonight" ? "The evening before" : "On the day"}: ${p.text}`)))));
    }
    if (r.notes.length) {
      out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Notes"),
        r.notes.map((x) => x.lang === "hi" ? hindi(x.text) : el("p", {}, x.text))));
    }
    return out;
  }

  // ----- Groceries -----
  function groceriesTab() {
    if (!groceries) return note("Loading groceries…");
    const g = groceries;
    const toItem = (i) => ({ name: i.name, quantity: i.quantity || null, category: i.grocery_category || null });
    const weekRows = g.categories.map((c) => el("section", { class: "mp-gcat" }, el("h4", {}, c.category),
      el("ul", { class: "mp-glist" }, c.items.map((i) => el("li", { class: `mp-gitem${i.purchased ? " is-done" : ""}` },
        el("label", {}, el("input", { type: "checkbox", checked: i.purchased, onchange: (e) => markBought(i, e.target.checked) }),
          el("span", {}, i.name, i.quantity ? el("small", {}, i.quantity) : null)),
        i.on_list ? el("span", { class: "mp-onlist" }, "On list")
          : el("button", { type: "button", class: "btn mp-add", "aria-label": `Add ${i.name} to the shopping list`, onclick: () => addToList([toItem(i)]) }, "Add"))))));
    const notBought = g.categories.flatMap((c) => c.items).filter((i) => !i.purchased && !i.on_list);
    const t = g.tomorrow;
    const tomorrowRows = t.items.map((i) => el("li", { class: `mp-gitem${i.staple ? " is-staple" : ""}` },
      el("span", {}, i.name, i.quantity ? el("small", {}, i.quantity) : el("small", {}, "amount not given"),
        el("small", { class: "mp-src" }, i.recipes.join(", "))),
      i.on_list ? el("span", { class: "mp-onlist" }, "On list") : i.staple ? el("span", { class: "mp-onlist" }, "Pantry")
        : el("button", { type: "button", class: "btn mp-add", "aria-label": `Add ${i.name} to the shopping list`, onclick: () => addToList([{ name: i.name, quantity: i.quantity }]) }, "Add")));
    const tomorrowAddable = t.items.filter((i) => !i.on_list && !i.staple);
    return [
      g.auto ? autoSection(g.auto) : null,
      el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, `Week ${g.week} list from the plan`),
        note(`${g.note} These are weekly estimates for both people.`, "dnote--muted"),
        el("div", { class: "mp-actions" },
          el("button", { type: "button", class: "btn btn--primary", disabled: !notBought.length, onclick: () => addToList(notBought.map(toItem)) },
            notBought.length ? `Add ${notBought.length} unbought to shopping list` : "Everything is bought or on the list"),
          el("a", { class: "btn", href: "/groceries?from=dashboard" }, "Open shopping list")),
        weekRows, note(g.pantry, "dnote--muted"), hindi(g.pantry_hindi)),
      el("section", { class: "dsec", id: "dsec-tomorrow-groceries" }, el("h3", { class: "dsec__title" }, `For tomorrow's recipes · Day ${t.plan_day}`),
        note(`Exact amounts written in ${t.recipes.join(", ")} (one batch each, before Person 1's extras). Same ingredient and unit are added together; different units stay separate.`, "dnote--muted"),
        el("div", { class: "mp-actions" }, el("button", { type: "button", class: "btn", disabled: !tomorrowAddable.length,
          onclick: () => addToList(tomorrowAddable.map((i) => ({ name: i.name, quantity: i.quantity }))) },
          tomorrowAddable.length ? `Add ${tomorrowAddable.length} to shopping list` : "Nothing new to add")),
        el("ul", { class: "mp-glist" }, tomorrowRows)),
    ];
  }

  // ----- Recipes -----
  function recipesTab() {
    if (!recipes) return note("Loading recipes…");
    return ["breakfast", "lunch", "dinner"].map((meal) => el("section", { class: "dsec" },
      el("h3", { class: "dsec__title" }, MEAL[meal]),
      el("ul", { class: "mp-rlist" }, recipes.filter((r) => r.meal === meal)
        .sort((a, b) => Number(a.code.slice(1)) - Number(b.code.slice(1)))
        .map((r) => el("li", {}, el("button", { type: "button", class: "mp-ritem", onclick: () => openRecipe(r.code) },
          el("span", { class: "mp-code" }, r.code), el("span", { class: "mp-ritem__name" }, r.name),
          r.paneer ? el("span", { class: "mp-badge" }, "paneer") : null,
          el("small", {}, `${r.base_serving.kcal} kcal · ${r.base_serving.protein_g} g P · Day ${r.used_on_days.join(", ")}`)))))));
  }

  // ----- Plan & training -----
  function table(cols, rows) {
    return el("div", { class: "dtable-wrap" }, el("table", { class: "dtable" },
      el("thead", {}, el("tr", {}, cols.map((c) => el("th", { scope: "col" }, c)))),
      el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c, i) => i ? el("td", {}, c) : el("th", { scope: "row" }, c)))))));
  }
  function planTab() {
    if (!plan) return note("Loading the plan…");
    const c = plan.context, n = plan.people;
    const out = [];
    out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Targets"),
      note(c.targets.method, "dnote--muted"),
      table(["Metric", `${n.person1} · ${plan.people_info.person1.summary}`, `${n.person2} · ${plan.people_info.person2.summary}`], c.targets.rows),
      note(c.targets.before_you_start, "dnote--muted")));
    const days = el("ol", { class: "mp-month" }, plan.schedule.map((d) => el("li", { class: d.day === Number(day?.plan_day) ? "is-today" : null },
      el("b", {}, `Day ${d.day}`), ["breakfast", "lunch", "dinner"].map((m) =>
        el("button", { type: "button", class: "mp-mini", title: d[m].label, onclick: () => { tab = "plan"; openRecipe(d[m].code); } }, d[m].code)))));
    out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "The 30 days"),
      note("Day 1 is always the 1st of the month; the 30th and 31st both use Day 30.", "dnote--muted"), days));
    for (const p of ["person1", "person2"]) {
      const t = c.training[p];
      out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, `Training · ${n[p]}`),
        el("p", { class: "mp-portion" }, el("b", {}, t.title)), note(t.why, "dnote--muted"),
        el("dl", { class: "dmetrics" }, Object.entries(t.days).map(([d, s]) => el("div", { class: "dmetric" }, el("dt", {}, d.toUpperCase()), el("dd", {}, s))),
          el("div", { class: "dmetric" }, el("dt", {}, "Rest"), el("dd", {}, t.rest))),
        el("p", {}, el("b", {}, "Warm-up: "), t.warmup), hindi(t.warmup_hindi),
        el("ul", { class: "mp-plain" }, t.rules.map((r) => el("li", {}, r))),
        t.notation ? note(t.notation, "dnote--muted") : null,
        Object.entries(t.week1).map(([session, rows]) => el("div", {}, el("h4", { class: "mp-h4" }, `Week 1 · ${session}`), table(t.week1_columns, rows))),
        el("ul", { class: "mp-plain" }, Object.values(t.weeks).slice(1).map((w) => el("li", {}, w)))));
    }
    const r = c.recovery;
    out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Recovery & hydration"),
      el("h4", { class: "mp-h4" }, "Sleep"), el("ul", { class: "mp-plain" }, r.sleep.map((x) => el("li", {}, x))),
      el("h4", { class: "mp-h4" }, "Hydration"), el("ul", { class: "mp-plain" }, r.hydration.map((x) => el("li", {}, x))),
      el("h4", { class: "mp-h4" }, "Daily steps"), el("p", {}, `${n.person1}: ${r.steps.person1.join(", ")} · ${n.person2}: ${r.steps.person2.join(", ")}`),
      note(r.steps.note, "dnote--muted"), hindi(r.steps.hindi),
      el("h4", { class: "mp-h4" }, "Stretching (5–8 min)"), el("ul", { class: "mp-plain" }, r.stretching.map((x) => el("li", {}, x))),
      el("h4", { class: "mp-h4" }, "Mobility (2–3× per week)"), el("ul", { class: "mp-plain" }, r.mobility.map((x) => el("li", {}, x))),
      el("h4", { class: "mp-h4" }, "Recovery tips"), el("ul", { class: "mp-plain" }, r.tips.map((x) => el("li", {}, x))),
      note(c.training.cardio, "dnote--muted")));
    out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Supplements (optional)"),
      note(c.supplements.intro, "dnote--muted"), table(["Supplement", "Dose", "Why / notes"], c.supplements.rows),
      note(c.supplements.outro, "dnote--warn")));
    out.push(el("section", { class: "dsec" }, el("h3", { class: "dsec__title" }, "Meal-prep tips"),
      el("ul", { class: "mp-plain" }, c.meal_prep.map((x) => el("li", {}, x))), hindi(c.meal_prep_hindi),
      note(c.monthly_adjust, "dnote--muted")));
    out.push(note(plan.disclaimer, "dnote--warn"));
    return out;
  }

  // ---------- wiring ----------
  function view() {
    if (!root) build();
    if (!day) {
      loadDay().then(paint).catch(paintError);
      root.replaceChildren(note("Loading the meal plan…"));
    } else {
      paint();
    }
    return root;
  }

  function init(opts) { icon = opts.icon; }

  return { init, view, refresh, paintView: paint, openRecipe, show };
})();
