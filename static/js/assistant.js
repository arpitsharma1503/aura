/* AURA assistant: mic button, "Ask AURA" view, transcript, spoken replies.
   - The microphone is on the Pi; only the entrance screen may switch it on
     (the server enforces this). Other devices can type a question.
   - Listening state comes from the server over Server-Sent Events, so every
     open screen shows when the microphone is live.
   - Text only via textContent; icons are our own SVG strings. */
"use strict";

window.AuraAssistant = (() => {
  const $ = (id) => document.getElementById(id);
  const EXAMPLES = ["What's the weather today?", "Will it rain before I leave?", "How is my commute?",
    "What's on my calendar today?", "What groceries do I need?", "Summarize my day"];
  const STATES = {
    idle: ["Ready", "Press the microphone and ask a question."],
    listening: ["Listening…", "Speak now. I'll stop when you pause."],
    processing: ["Thinking…", "Turning your words into an answer."],
    answered: ["Ready", "Ask another question."],
    error: ["Something went wrong", ""],
    off: ["Assistant is off", "Turn it on in Settings → Assistant."],
    unavailable: ["Voice isn't available", ""],
  };

  let icon = () => "";
  let openView = () => {};
  let info = null;                 // last GET /api/assistant
  let state = "idle";
  let errorText = "";
  let canRetry = false;
  let root = null;                 // the view's DOM (built once, updated in place)
  let audio = null;
  let pendingVoice = false;        // this screen started the current listen

  // ---------- network ----------
  async function api(url, { method = "GET", body, timeout = 15000 } = {}) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeout);
    try {
      const res = await fetch(url, {
        method, signal: ctrl.signal, cache: "no-store",
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (res.status === 204) return null;
      let data = null;
      try { data = await res.json(); } catch { /* empty */ }
      if (!res.ok) {
        const err = new Error((data && typeof data.detail === "string") ? data.detail : `HTTP ${res.status}`);
        err.status = res.status;
        err.retry = data?.retry !== false;
        throw err;
      }
      return data;
    } catch (err) {
      if (err.name === "AbortError") { const e = new Error("The Pi took too long to answer."); e.retry = true; throw e; }
      throw err;
    } finally { clearTimeout(t); }
  }

  async function refresh() {
    try {
      info = await api("/api/assistant");
      if (!pendingVoice && ["listening", "processing"].includes(info.state) === false && state !== "error") state = "idle";
      if (["listening", "processing"].includes(info.state)) state = info.state;
    } catch { /* keep last */ }
    paint();
  }

  // ---------- actions ----------
  const micReady = () => info && info.enabled && info.mic_allowed && info.voice_available
    && info.voice?.stt?.state === "ready" && info.voice?.mic?.present;

  async function listen() {
    if (!info) await refresh();
    openView();
    if (state === "listening") return stop();
    if (state === "processing") return;
    if (!micReady()) { paint(); return; }
    stopAudio();
    pendingVoice = true;
    setState("listening");
    try {
      const turn = await api("/api/assistant/listen", { method: "POST", timeout: ((info.max_listen_seconds || 10) + 45) * 1000 });
      if (turn) { addTurn(turn); speak(turn); }
      setState("idle");
    } catch (err) {
      setState("error", err.message, err.retry !== false);
    } finally {
      pendingVoice = false;
    }
  }

  async function stop() {
    try { await api("/api/assistant/stop", { method: "POST" }); } catch { /* the listen call reports */ }
  }

  async function ask(text) {
    text = (text || "").trim();
    if (!text) return;
    stopAudio();
    setState("processing");
    try {
      const turn = await api("/api/assistant/ask", { method: "POST", body: { text } });
      addTurn(turn);
      if (info?.mic_allowed) speak(turn);         // speak only on the entrance screen
      setState("idle");
      const input = root?.querySelector("#asst-input");
      if (input) input.value = "";
    } catch (err) {
      setState("error", err.message, true);
    }
  }

  async function clearTranscript() {
    try {
      await api("/api/assistant/transcript", { method: "DELETE" });
      if (info) info.transcript = [];
      paint();
    } catch (err) { setState("error", err.message, false); }
  }

  function speak(turn) {
    if (!info?.spoken_replies || info?.voice?.tts?.state !== "ready" || !turn?.id) return;
    stopAudio();
    audio = new Audio(`/api/assistant/speech/${turn.id}`);
    audio.play().catch(() => { /* autoplay blocked or no audio device: the text is on screen */ });
  }
  function stopAudio() { if (audio) { audio.pause(); audio = null; } }

  function addTurn(turn) {
    if (!info) return;
    info.transcript = (info.transcript || []).filter((t) => t.id !== turn.id).concat(turn).slice(-20);
  }

  function setState(s, msg = "", retry = false) {
    state = s; errorText = msg; canRetry = retry;
    paint();
  }

  /** Server-Sent "assistant" events (from any screen). */
  function onEvent(data) {
    if (data.state === "listening" || data.state === "processing") {
      state = data.state;
    } else if (data.state === "error" && !pendingVoice) {
      state = "idle";       // another screen's error; ours arrives with our request
    } else if (["answered", "cleared", "settings"].includes(data.state)) {
      if (!pendingVoice && state !== "error") state = "idle";
      refresh();
      return;
    } else if (data.state === "idle" && !pendingVoice) {
      state = "idle";
    }
    paint();
  }

  // ---------- rendering ----------
  function el(tag, attrs = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "html") n.innerHTML = v;      // our own SVG icons only
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k != null && k !== false) n.append(k instanceof Node ? k : document.createTextNode(String(k)));
    return n;
  }

  function build() {
    root = el("div", { class: "asst" },
      el("div", { class: "asst__hero", id: "asst-hero" },
        el("button", { class: "asst__orb", id: "asst-orb", type: "button" },
          el("span", { class: "asst__orb-icon", id: "asst-orb-icon", "aria-hidden": "true" })),
        el("div", { class: "asst__status" },
          el("p", { class: "asst__state", id: "asst-state", role: "status", "aria-live": "polite" }),
          el("p", { class: "asst__hint", id: "asst-hint" }),
          el("div", { class: "asst__actions", id: "asst-actions" }))),
      el("form", { class: "asst__ask", id: "asst-form", autocomplete: "off" },
        el("label", { class: "sr-only", for: "asst-input" }, "Type a question"),
        el("input", { id: "asst-input", type: "text", maxlength: "300", placeholder: "Or type a question…", enterkeyhint: "send" }),
        el("button", { type: "submit", class: "asst__send", "aria-label": "Ask", html: icon("send") })),
      el("div", { class: "asst__examples", id: "asst-examples" },
        EXAMPLES.map((q) => el("button", { type: "button", class: "asst__chip", "data-q": q }, q))),
      el("section", { class: "dsec", "aria-labelledby": "asst-tr-t" },
        el("div", { class: "asst__trhead" },
          el("h3", { class: "dsec__title", id: "asst-tr-t" }, "Conversation"),
          el("button", { type: "button", class: "asst__clear", id: "asst-clear" }, "Clear")),
        el("ol", { class: "asst__transcript", id: "asst-transcript" })),
      el("p", { class: "dnote dnote--muted", id: "asst-privacy" },
        "Speech is processed on the Pi. Audio is kept in memory only until it's transcribed, then discarded. "
        + "The conversation is kept in memory (not saved) until you clear it or AURA restarts."));
    root.querySelector("#asst-orb").addEventListener("click", () => (state === "listening" ? stop() : listen()));
    root.querySelector("#asst-form").addEventListener("submit", (e) => { e.preventDefault(); ask(root.querySelector("#asst-input").value); });
    root.querySelector("#asst-examples").addEventListener("click", (e) => {
      const b = e.target.closest("[data-q]"); if (b) ask(b.dataset.q);
    });
    root.querySelector("#asst-clear").addEventListener("click", clearTranscript);
    return root;
  }

  function viewState() {
    if (info && !info.enabled) return "off";
    if (state === "idle" && info && info.mic_allowed && !micReady()) return "unavailable";
    return state;
  }

  function unavailableReason() {
    if (!info) return "";
    if (!info.voice_available) return "The voice service isn't running on the Pi (aura-voice). You can still type.";
    if (!info.voice?.mic?.present) return "No microphone is connected to the Pi. You can still type.";
    if (info.voice?.stt?.state !== "ready") return `Speech recognition isn't ready: ${info.voice.stt.error || info.voice.stt.state}.`;
    return "";
  }

  function paint() {
    paintMicButton();
    if (!root) return;
    const vs = viewState();
    const [title, hint] = STATES[vs] || STATES.idle;
    root.dataset.state = vs;
    root.querySelector("#asst-state").textContent = title;
    let hintText = hint;
    if (vs === "error") hintText = errorText || "Please try again.";
    if (vs === "unavailable") hintText = unavailableReason();
    if ((vs === "idle" || vs === "answered") && info && !info.mic_allowed) {
      hintText = "The microphone can only be started on the entrance screen. Type a question here, or allow other devices in Settings → Assistant.";
    }
    root.querySelector("#asst-hint").textContent = hintText;

    const orb = root.querySelector("#asst-orb");
    const micUsable = vs !== "off" && info?.mic_allowed && micReady();
    orb.disabled = !(micUsable && vs !== "processing");
    orb.setAttribute("aria-label", vs === "listening" ? "Stop listening" : "Speak");
    root.querySelector("#asst-orb-icon").innerHTML = icon(vs === "listening" ? "stop" : (micUsable ? "mic" : "micOff"), 1.6);

    const actions = root.querySelector("#asst-actions");
    actions.replaceChildren();
    if (vs === "error" && canRetry && micUsable && errorText) {
      const b = el("button", { type: "button", class: "btn btn--ghost", id: "asst-retry", html: icon("retry") }, "Try again");
      b.addEventListener("click", listen);
      actions.append(b);
    }
    if (vs === "listening") {
      const b = el("button", { type: "button", class: "btn btn--ghost", id: "asst-stop" }, "Done speaking");
      b.addEventListener("click", stop);
      actions.append(b);
    }

    const off = vs === "off";
    root.querySelector("#asst-input").disabled = off || vs === "processing";
    root.querySelectorAll(".asst__chip, .asst__send").forEach((b) => { b.disabled = off || vs === "processing"; });

    const list = root.querySelector("#asst-transcript");
    const turns = info?.transcript || [];
    list.replaceChildren(...(turns.length ? turns.map(renderTurn) : [el("li", { class: "asst__empty" }, "No questions yet.")]));
    root.querySelector("#asst-clear").hidden = !turns.length;
    if (turns.length) list.lastElementChild?.scrollIntoView?.({ block: "nearest" });
  }

  function renderTurn(t) {
    const when = new Date(t.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    return el("li", { class: `asst__turn${t.answer.ok ? "" : " is-limited"}`, "data-intent": t.answer.intent },
      el("p", { class: "asst__q" }, el("span", { class: "asst__who" }, `You · ${t.source === "voice" ? "spoken" : "typed"} · ${when}`), t.question),
      el("p", { class: "asst__a" }, t.answer.text));
  }

  function paintMicButton() {
    const btn = $("mic-btn");
    if (!btn) return;
    const live = state === "listening";
    btn.dataset.state = state;
    btn.hidden = !!(info && !info.enabled);
    btn.classList.toggle("is-live", live);
    btn.setAttribute("aria-label", live ? "Listening, press to stop" : "Ask AURA");
    btn.title = live ? "Listening… press to stop" : "Ask AURA";
    const pill = $("listening-pill");
    if (pill) pill.hidden = !live;
    paintTile();
  }

  /** The dashboard's "Ask AURA" tile: real state only (listening only while recording). */
  function paintTile() {
    const tile = $("assistant-tile");
    if (!tile || !info) return;
    const vs = viewState();
    const value = { idle: "Ready", answered: "Ready", listening: "Listening…", processing: "Thinking…",
      error: "Ready", off: "Turned off", unavailable: "Voice unavailable" }[vs] || "Ready";
    tile.dataset.state = { listening: "listening", processing: "processing", off: "off", unavailable: "alert" }[vs] || "ok";
    $("assistant-status").textContent = !info.mic_allowed && vs !== "off" && vs !== "listening" && vs !== "processing" ? "Type a question" : value;
    const last = (info.transcript || []).at(-1);
    let sub;
    if (vs === "off") sub = "Turn it on in Settings → Assistant.";
    else if (vs === "unavailable") sub = unavailableReason();
    else if (vs === "listening") sub = "Speak now. I'll stop when you pause.";
    else if (last) sub = `“${last.question}” — ${last.answer.text}`;
    else if (info.mic_allowed) sub = "Press the microphone and ask about weather, your commute, calendar or groceries.";
    else sub = "Ask about weather, your commute, calendar or groceries. To use the Pi's microphone from here, allow it in Settings → Assistant.";
    $("assistant-sub").textContent = sub;
  }

  // ---------- wiring ----------
  const SHORTCUTS = new Set(["m", "M", "ColorF0Red", "MediaPlayPause", "MediaPlay", "F2"]);

  function init(opts) {
    icon = opts.icon;
    openView = opts.openView;
    const btn = $("mic-btn");
    if (btn) {
      btn.innerHTML = icon("mic", 1.6);
      btn.addEventListener("click", () => {
        if (info?.mic_allowed) listen(); else openView();
      });
    }
    document.addEventListener("keydown", (e) => {
      if (!SHORTCUTS.has(e.key) || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.target.closest?.("input, textarea, select, [contenteditable]")) return;
      if (!info?.mic_allowed) return;
      e.preventDefault();
      listen();
    });
    refresh();
  }

  return { init, onEvent, refresh, view: () => root || build(), paintView: paint, listen, ask };
})();
