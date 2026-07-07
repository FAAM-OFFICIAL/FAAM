/* FAAM — frontend logic */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  watchlist: [],
  active: null,        // symbol string
  range: "1mo",
  interval: "1d",
  chart: null,
  chatHistory: [],
  smaOn: false,        // moving-average overlay
  lastQuote: null,     // last loaded stock for re-rendering the chart
  me: null,            // logged-in account
  view: "line",        // chart view: line | candles | returns | forecast (Pro+)
  horizon: 30,         // forecast horizon in days
  forecast: null,      // last forecast payload
  forecastModel: "apollo", // selected forecasting model (a saved setting)
  predictionMarkets: false, // prediction-markets add-on (Max & Elite)
  interests: {},            // portfolio focus: themeId -> interest rating (1–5)
  beginner: false,          // beginner mode (guided tour + plain-language tips)
  gameOn: false,            // Game of Stocks mode
  game: null,               // game state from /api/game
  aiEnabled: false,         // server has an AI key (from /api/health)
  aiControl: false,         // user let the assistant drive the dashboard (fill orders)
};

let positionMode = "shares"; // "shares" | "dollars"

// Order-ticket handoff state. FAAM prepares orders; the user places them.
let orderSide = "buy";
let orderMode = "shares";
let orderTicket = null;
let orderEstTimer = null;

// Where "Review at broker" sends you — the ticker's page, where YOU place the
// trade. FAAM never submits an order; these are plain public links.
const BROKERS = {
  robinhood: { name: "Robinhood", url: (s, crypto) => crypto
      ? `https://robinhood.com/crypto/${s.replace("-USD", "")}`
      : `https://robinhood.com/stocks/${s}` },
  fidelity: { name: "Fidelity", url: (s) => `https://digital.fidelity.com/prgw/digital/research/quote/dashboard/summary?symbol=${encodeURIComponent(s)}` },
  schwab: { name: "Charles Schwab", url: (s) => `https://www.schwab.com/research/stocks/quotes?symbol=${encodeURIComponent(s)}` },
  etrade: { name: "E*TRADE", url: (s) => `https://us.etrade.com/etx/mkt/quotes#/${encodeURIComponent(s)}` },
  webull: { name: "Webull", url: (s) => `https://www.webull.com/quote/${s.toLowerCase()}` },
  coinbase: { name: "Coinbase", url: (s) => `https://www.coinbase.com/price/${s.replace("-USD", "").toLowerCase()}` },
  other: { name: "your broker", url: (s) => `https://www.google.com/search?q=${encodeURIComponent("buy " + s + " stock")}` },
};

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// Open an external URL in the user's real browser. Inside the native FAAM app
// this bridges to the host (NSWorkspace); in a browser it falls back to window.open.
/* ---------- Loading splash ("FAAM is loading…") + TikTok promo ----------
   The intro/ad is mandatory on Lite & free; Pro/Max/Elite can remove it. */
let _bootStart = 0, _bootReady = false;
const BOOT_SKIP_TIER = 2;               // Pro and up may skip the intro
function bootCanSkip() { try { return localStorage.getItem("faam-can-skip") === "1"; } catch (e) { return false; } }

function renderBootTier(canSkip) {
  const row = $("#bootSkipRow"), note = $("#bootWatchNote");
  if (row) row.hidden = !canSkip;
  if (note) note.hidden = !!canSkip;
}

// Confirmed once we know the real plan (called from loadPro). Paid tiers get the
// "skip next time" control; Lite/free get the "must watch" note + an upsell.
function applyBootTier() {
  const canSkip = (pro.tier || 0) >= BOOT_SKIP_TIER;
  try {
    if (canSkip) localStorage.setItem("faam-can-skip", "1");
    else localStorage.removeItem("faam-can-skip");   // also stops the pre-paint skip
  } catch (e) {}
  renderBootTier(canSkip);
}

function initBoot() {
  const b = $("#bootScreen");
  if (!b) return;                       // skipped via the inline "skip intro" check
  _bootStart = Date.now();
  renderBootTier(bootCanSkip());        // fast guess from cache; loadPro confirms
  const enter = $("#bootEnter");
  if (enter) enter.addEventListener("click", dismissBoot);
  const tok = $("#bootTokBtn");
  if (tok) tok.addEventListener("click", (e) => { e.preventDefault(); openExternal(tok.href); });
  const up = $("#bootUpsell");
  if (up) up.addEventListener("click", () => { dismissBoot(); if (typeof openProDialog === "function") openProDialog(); });
  b.addEventListener("click", (e) => { if (e.target === b && _bootReady) dismissBoot(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && _bootReady && $("#bootScreen")) dismissBoot();
  });
  setTimeout(bootReady, 5000);          // safety: never hang on the splash
}
function bootReady() {
  if (_bootReady || !$("#bootScreen")) return;
  const elapsed = Date.now() - _bootStart;
  if (elapsed < 1300) { setTimeout(bootReady, 1300 - elapsed); return; }  // min display
  _bootReady = true;
  const bar = $("#bootBar"); if (bar) { bar.style.animation = "none"; bar.style.width = "100%"; }
  const sub = $("#bootSub"); if (sub) sub.textContent = "Ready when you are.";
  const btn = $("#bootEnter");
  if (btn) { btn.disabled = false; btn.textContent = "Enter FAAM →"; btn.classList.add("ready"); }
}
function dismissBoot() {
  const b = $("#bootScreen"); if (!b) return;
  // Only paid tiers can make the intro stay gone (Lite/free always re-watch).
  try {
    if (bootCanSkip() && $("#bootSkip") && $("#bootSkip").checked) {
      localStorage.setItem("faam-skip-splash", "1");
    }
  } catch (e) {}
  b.classList.add("hide");
  setTimeout(() => b.remove(), 450);
}

/* ---------- Auto-update ("FAAM is updating…") ----------
   When a new build is deployed, the server's /api/version changes. Open clients
   notice (on a timer and when refocused), show the updating screen, and reload
   to pick up the new version. Works the same in every FAAM app — Mac, Windows,
   Linux, and the browser all load this web app. */
let _appVersion = null, _updating = false;

async function initVersion() {
  try {
    const d = await (await fetch("/api/version", { cache: "no-store" })).json();
    _appVersion = d.version || null;
    markWhatsNew(d.release);
  } catch (e) { /* offline — try again on the next check */ }
}

/* ---------- What's new (changelog + roadmap) ---------- */
const CL_TAG = { new: "New", improved: "Improved", fixed: "Fixed" };

function markWhatsNew(release) {
  const dot = $("#whatsNewDot");
  if (!dot) return;
  let seen = null;
  try { seen = localStorage.getItem("faam-changelog-seen"); } catch (e) {}
  dot.hidden = !(release && release !== seen);   // show a dot when there's an unseen release
}

async function openChangelog() {
  const dot = $("#whatsNewDot"); if (dot) dot.hidden = true;
  const list = $("#changelogList"), coming = $("#changelogComing");
  list.innerHTML = '<p class="muted small">Loading…</p>'; coming.innerHTML = "";
  openDialog("changelogDialog");
  try {
    const d = await (await fetch("/api/changelog", { cache: "no-store" })).json();
    try { if (d.version) localStorage.setItem("faam-changelog-seen", d.version); } catch (e) {}
    renderChangelog(d);
  } catch (e) {
    list.innerHTML = '<p class="muted small">Couldn\'t load updates right now.</p>';
  }
}

function renderChangelog(d) {
  $("#changelogList").innerHTML = (d.releases || []).map((r) => `
    <div class="cl-release">
      <div class="cl-rhead">
        <span class="cl-ver">v${escapeHtml(r.version)}</span>
        <span class="cl-title">${escapeHtml(r.title || "")}</span>
        <span class="cl-date muted small">${escapeHtml(r.date || "")}</span>
      </div>
      <ul class="cl-items">
        ${(r.items || []).map((it) => `<li><span class="cl-tag cl-${escapeHtml(it.tag || "new")}">${CL_TAG[it.tag] || "New"}</span><span>${escapeHtml(it.text || "")}</span></li>`).join("")}
      </ul>
    </div>`).join("");
  const items = d.coming || [];
  $("#changelogComing").innerHTML = items.length ? `
    <div class="cl-coming-head">Coming soon</div>
    <div class="cl-roadmap">
      ${items.map((c) => `<div class="cl-road"><span class="cl-road-dot" aria-hidden="true"></span><div><div class="cl-road-t">${escapeHtml(c.title || "")}</div><div class="cl-road-d muted small">${escapeHtml(c.text || "")}</div></div></div>`).join("")}
    </div>` : "";
}

/* ---------- AI trade ideas (strategist) ---------- */
const IDEA_META = {
  buy:   { label: "Buy",   cls: "idea-buy" },
  swing: { label: "Swing", cls: "idea-buy" },
  short: { label: "Short", cls: "idea-short" },
  pairs: { label: "Pairs", cls: "idea-pairs" },
  hedge: { label: "Hedge", cls: "idea-hedge" },
  watch: { label: "Watch", cls: "idea-watch" },
};

function openIdeas() {
  openDialog("ideasDialog");
  loadIdeas();
}

/* ---------- Titan — chat with the local model, and teach it ---------- */
let titanPendingQ = "";
/* ---------- Vector icons (replace emoji) ---------- */
const ICON_SVGS = {
  bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>',
  sport: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"/><path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"/><path d="M4 22h16"/><path d="M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22"/><path d="M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22"/><path d="M18 2H6v7a6 6 0 0 0 12 0V2Z"/></svg>',
  star: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2.5l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 18.4 6.1 21l1.2-6.5L2.5 9.4l6.6-.9z"/></svg>',
  news: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h13v16H5a1 1 0 0 1-1-1V4Z"/><path d="M17 8h2a1 1 0 0 1 1 1v9a2 2 0 0 1-2 2"/><path d="M7 8h7M7 12h7M7 16h4"/></svg>',
  chart: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 14l3-3 3 3 5-6"/></svg>',
};
function iconSvg(type) { return ICON_SVGS[type] || ICON_SVGS.star; }
const THUMB_UP = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v11"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/></svg>';
const THUMB_DOWN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 14V3"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/></svg>';

function openTitan() {
  openDialog("titanDialog", "titanInput");
  loadTitanStat();
  const log = $("#titanLog");
  if (log && !log.children.length) {
    titanBubble("bot", "Hi, I'm Titan — FAAM's built-in AI. Ask me anything about stocks, investing, or how FAAM works. Rate my answers, and correct me when I'm wrong.");
  }
}
function titanBubble(who, text) {
  const log = $("#titanLog");
  const d = document.createElement("div");
  d.className = "titan-msg " + (who === "you" ? "titan-you" : "titan-bot");
  d.textContent = text;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
  return d;
}
async function loadTitanStat() {
  try {
    const d = await (await fetch("/api/titan")).json();
    const el = $("#titanStat");
    if (el) el.textContent = "";
  } catch { /* ignore */ }
}
function titanAddFeedback(bubble, question, answer) {
  const fb = document.createElement("div");
  fb.className = "titan-fb";
  fb.innerHTML =
    '<span class="titan-fb-q">Helpful?</span>' +
    '<button class="titan-fb-btn" data-good="1" title="Good answer">'+THUMB_UP+'</button>' +
    '<button class="titan-fb-btn" data-good="0" title="Wrong — teach the right answer">'+THUMB_DOWN+'</button>';
  bubble.appendChild(fb);
  fb.querySelector('[data-good="1"]').addEventListener("click", () => titanFeedback(question, answer, true, fb));
  fb.querySelector('[data-good="0"]').addEventListener("click", () => titanFeedback(question, answer, false, fb));
}
async function titanFeedback(question, answer, good, fbEl) {
  try {
    await fetch("/api/titan/feedback", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ question, answer, good }),
    });
  } catch { /* ignore */ }
  if (good) {
    if (fbEl) fbEl.innerHTML = '<span class="titan-fb-done">Thanks — Titan will keep that.</span>';
  } else {
    if (fbEl) fbEl.innerHTML = '<span class="titan-fb-done">Teach Titan the right answer below ↓</span>';
    titanPendingQ = question;
    $("#titanTeach").hidden = false;
    setTimeout(() => $("#titanAnswer")?.focus(), 60);
  }
  loadTitanStat();
}
async function titanAsk(q) {
  titanBubble("you", q);
  $("#titanTeach").hidden = true;
  const thinking = titanBubble("bot", "…");
  try {
    const r = await fetch("/api/titan/ask", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ question: q }),
    });
    const d = await r.json();
    if (d.known) {
      thinking.textContent = d.answer;
      titanAddFeedback(thinking, q, d.answer);   // thumbs up/down on every answer
    } else {
      thinking.textContent = "I don't know that yet. Can you teach me the answer?";
      titanPendingQ = q;
      $("#titanTeach").hidden = false;
      setTimeout(() => $("#titanAnswer")?.focus(), 60);
    }
    loadTitanStat();
  } catch {
    thinking.textContent = "Couldn't reach Titan. Try again.";
  }
}
async function titanTeach() {
  const a = ($("#titanAnswer").value || "").trim();
  if (!a || !titanPendingQ) return;
  try {
    await fetch("/api/titan/teach", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ question: titanPendingQ, answer: a }),
    });
    $("#titanTeach").hidden = true;
    $("#titanAnswer").value = "";
    titanBubble("bot", "Got it — I've learned that. Ask me again anytime.");
    titanPendingQ = "";
    loadTitanStat();
  } catch { /* ignore */ }
}

async function loadIdeas() {
  const list = $("#ideasList");
  list.innerHTML = '<div class="ideas-loading muted small">Thinking up ideas from your watchlist…</div>';
  $("#ideasDisclaimer").textContent = "";
  try {
    const r = await fetch("/api/ideas", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
    const d = await r.json();
    if (!r.ok || d.error) { list.innerHTML = `<div class="muted small">${escapeHtml(d.message || d.error || "Couldn't load ideas.")}</div>`; return; }
    renderIdeas(d);
  } catch (e) {
    list.innerHTML = '<div class="muted small">Couldn\'t load ideas right now.</div>';
  }
}

function renderIdeas(d) {
  const list = $("#ideasList");
  const ideas = d.ideas || [];
  if (!ideas.length) {
    list.innerHTML = '<div class="muted small">Add a few stocks to your watchlist and I\'ll suggest ideas.</div>';
  } else {
    list.innerHTML = ideas.map((it) => {
      const m = IDEA_META[it.type] || IDEA_META.watch;
      const ticks = (it.tickers || []).map((t) =>
        `<button type="button" class="idea-tick" data-tick="${escapeHtml(t)}">${escapeHtml(t)}</button>`).join("");
      return `<div class="idea-card">
        <div class="idea-head">
          <span class="idea-badge ${m.cls}">${m.label}</span>
          <span class="idea-title">${escapeHtml(it.title || "")}</span>
          ${it.horizon ? `<span class="idea-horizon muted small">${escapeHtml(it.horizon)}</span>` : ""}
        </div>
        ${it.thesis ? `<p class="idea-thesis">${escapeHtml(it.thesis)}</p>` : ""}
        ${it.action ? `<p class="idea-action"><strong>Idea:</strong> ${escapeHtml(it.action)}</p>` : ""}
        ${it.risk ? `<p class="idea-risk">⚠ ${escapeHtml(it.risk)}</p>` : ""}
        <div class="idea-foot">
          ${ticks}
          ${(it.tickers && it.tickers.length) ? `<button type="button" class="idea-prep" data-prep="${escapeHtml(it.tickers[0])}">Set up order ▸</button>` : ""}
        </div>
      </div>`;
    }).join("");
  }
  $("#ideasDisclaimer").textContent = d.disclaimer || "";
  const intro = $("#ideasIntro");
  if (intro) intro.textContent = d.source === "ai"
    ? "Generated by GPT-4.1 mini from your watchlist — long, short, pairs and hedge plays."
    : "From your watchlist's moves — long, short, pairs and hedge plays.";
}

async function checkForUpdate() {
  if (_updating || document.hidden) return;
  try {
    const d = await (await fetch("/api/version", { cache: "no-store" })).json();
    if (!_appVersion) { _appVersion = d.version || null; return; }
    if (d.version && d.version !== _appVersion) showUpdating();
  } catch (e) { /* server unreachable — leave the app as-is */ }
}

function showUpdating() {
  if (_updating) return;
  _updating = true;
  const el = $("#updateScreen");
  if (!el) { location.reload(); return; }
  el.hidden = false;
  requestAnimationFrame(() => el.classList.add("show"));
  // Give the screen a beat to render, then reload into the new build.
  setTimeout(() => { try { location.reload(); } catch (e) { location.href = location.pathname; } }, 2200);
}

function openExternal(url) {
  try {
    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.faamOpen) {
      window.webkit.messageHandlers.faamOpen.postMessage(String(url)); // macOS WKWebView
      return true;
    }
    if (window.pywebview && window.pywebview.api && window.pywebview.api.open_external) {
      window.pywebview.api.open_external(String(url)); // Windows WebView2 (pywebview)
      return true;
    }
  } catch (e) { /* fall through to window.open */ }
  return !!window.open(url, "_blank", "noopener,noreferrer");
}

// True inside a FAAM native window — macOS (WKWebView) or Windows (pywebview/WebView2).
function isNativeApp() {
  return !!(
    (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.faamOpen) ||
    window.pywebview
  );
}

// Pop the running FAAM out of the native window into the default web browser.
// The native app hosts the server at this same origin (http://localhost:8765),
// so the browser reaches the very same FAAM.
function openInBrowser() {
  openExternal(location.origin + "/dashboard");
  toast("Opening FAAM in your browser — sign in there to continue.");
}

function assetBadge(type) {
  const t = (type || "").toUpperCase();
  if (t.includes("CRYPTO")) return ' <span class="tbadge crypto">CRYPTO</span>';
  if (t === "ETF") return ' <span class="tbadge etf">ETF</span>';
  if (t === "INDEX") return ' <span class="tbadge index">IX</span>';
  return "";
}

function extTag(s) {
  if (s.extPct == null) return "";
  const up = s.extPct >= 0;
  const lbl = s.extLabel === "Pre-market" ? "PRE" : "AH";
  return ` <span class="ext-tag ${up ? "up" : "down"}">${lbl} ${fmt.pct(s.extPct)}</span>`;
}

const fmt = {
  price(v) {
    if (v == null || Number.isNaN(v)) return "—";
    return v >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : v.toFixed(2);
  },
  money(v) {
    if (v == null || Number.isNaN(v)) return "—";
    return "$" + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  },
  signedMoney(v) {
    if (v == null || Number.isNaN(v)) return "—";
    return (v >= 0 ? "+" : "−") + "$" + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  },
  pct(v) {
    if (v == null || Number.isNaN(v)) return "—";
    return (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
  },
  delta(v) {
    if (v == null || Number.isNaN(v)) return "—";
    return (v >= 0 ? "+" : "") + v.toFixed(2);
  },
  shares(v) {
    if (v == null || Number.isNaN(v)) return "—";
    return v % 1 === 0 ? v.toString() : v.toFixed(4).replace(/0+$/, "");
  },
  big(v) {
    if (v == null) return "—";
    const u = ["", "K", "M", "B", "T"];
    let i = 0, n = v;
    while (Math.abs(n) >= 1000 && i < u.length - 1) { n /= 1000; i++; }
    return n.toFixed(n >= 100 ? 0 : 1) + u[i];
  },
};

/* ---------- Theme ---------- */
function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("faam-theme", theme); } catch (e) {}
  // Chart colors are baked at creation — rebuild so axes/tooltips match.
  if (state.chart) {
    state.chart.destroy();
    state.chart = null;
    if (state.active) loadChart();
  }
}

function themeColors() {
  const dark = currentTheme() === "dark";
  return dark
    ? { grid: "rgba(255,255,255,0.06)", border: "rgba(255,255,255,0.10)", tick: "#8A97AD",
        ttBg: "#111826", ttBorder: "#2A3A55", ttTitle: "#E7ECF3", ttBody: "#C2CBD9",
        up: "#2BD787", down: "#FF6B83" }
    : { grid: "rgba(16,24,40,0.05)", border: "rgba(16,24,40,0.08)", tick: "#667085",
        ttBg: "#FFFFFF", ttBorder: "#D0D5DD", ttTitle: "#0B1220", ttBody: "#344054",
        up: "#027A48", down: "#B42318" };
}

/* ---------- Health & status ---------- */
async function checkHealth() {
  try {
    const r = await fetch("/api/health");
    const data = await r.json();
    state.aiEnabled = !!data.ai_enabled;
    state.titan = data.titan || {};
    const status = $("#aiStatus");
    const wrap = status.closest(".status");
    status.title = "";
    if (data.ai_enabled) {
      status.textContent = "AI online";
      wrap.classList.remove("off"); wrap.classList.add("live");
    } else {
      status.textContent = "AI offline";
      wrap.classList.add("off");
    }
    setAdviserActive(!!data.adviser_loaded);
    const vb = $("#voiceBtn");
    if (vb) {
      vb.disabled = !data.voice_enabled;
      vb.title = data.voice_enabled ? "Voice mode — talk to FAAM" : "Voice is unavailable right now";
    }
  } catch {
    $("#aiStatus").textContent = "server unreachable";
  }
}

/* ---------- Adviser profile ---------- */
const ADVISER_TEMPLATE = `# Financial Adviser Profile

## Persona
You are a measured, plain-spoken financial adviser. You explain trade-offs
clearly and never hype.

## Priorities
- Risk first: always surface downside and position sizing.
- Favor diversified, low-cost, long-horizon exposure.
- Distinguish facts (prices, ranges) from opinion.

## Style
- Lead with the bottom line, then 2-3 supporting bullets.
- Define jargon in a few words the first time it appears.

## Hard rules
- Never tell the user to buy or sell a specific amount.
- Always remind the user this is information, not personalized advice.`;

function setAdviserActive(active) {
  const btn = $("#adviserBtn");
  if (btn) btn.classList.toggle("active", active);
  const badge = document.querySelector(".ai-badge");
  if (badge) {
    badge.classList.toggle("adviser-on", active);
    badge.title = active ? "Adviser profile active" : "";
  }
}

async function openAdviser() {
  try {
    const r = await fetch("/api/adviser");
    const data = await r.json();
    $("#adviserText").value = data.text || "";
  } catch {
    $("#adviserText").value = "";
  }
  updateAdviserCount();
  $("#adviserErr").textContent = "";
  openDialog("adviserDialog", "adviserText");
}

function updateAdviserCount() {
  const n = ($("#adviserText").value || "").length;
  $("#adviserCount").textContent = n.toLocaleString() + " chars";
}

async function saveAdviser(text) {
  const r = await fetch("/api/adviser", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ text }),
  });
  const data = await r.json();
  if (!r.ok || data.error) throw new Error(data.error || "could not save");
  setAdviserActive(!!data.adviser_loaded);
  return data;
}

/* ---------- Sparkline ---------- */
function sparkline(values, w = 144, h = 28) {
  if (!values || values.length < 2) return "";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const step = w / (values.length - 1);
  const pts = values
    .map((v, i) => `${(i * step).toFixed(2)},${(h - ((v - min) / range) * h).toFixed(2)}`)
    .join(" ");
  const up = values[values.length - 1] >= values[0];
  const c = themeColors();
  const color = up ? c.up : c.down;
  const fill = up ? "rgba(2,122,72,0.12)" : "rgba(180,35,24,0.10)";
  const area = `0,${h} ${pts} ${w},${h}`;
  return `
    <svg class="tspark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <polygon points="${area}" fill="${fill}" />
      <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round" />
    </svg>`;
}

/* ---------- Watchlist rail ---------- */
async function loadWatchlist(preserveActive = true) {
  const rail = $("#tickerRail");
  try {
    const r = await fetch("/api/watchlist");
    const data = await r.json();
    state.watchlist = data.stocks || [];
    renderWatchlist();
    if (state.watchlist.length && (!preserveActive || !state.active)) {
      const first = state.watchlist.find((s) => !s.error);
      if (first) selectStock(first.symbol);
    }
  } catch (e) {
    rail.innerHTML = `<div class="muted small" style="padding:8px">Failed to load watchlist: ${e.message}</div>`;
  } finally {
    bootReady();                        // the dashboard has data — let the splash clear
  }
}

function renderWatchlist() {
  const rail = $("#tickerRail");
  const cards = state.watchlist
    .map((s) => {
      if (s.error) {
        return `<div class="tcard" data-symbol="${s.symbol}">
          <button class="tremove" data-remove="${s.symbol}" title="Remove">×</button>
          <div class="tsym">${s.symbol}</div>
          <div class="tname muted">unavailable</div>
        </div>`;
      }
      const up = (s.pct ?? 0) >= 0;
      const cls = up ? "up" : "down";
      const active = s.symbol === state.active ? "active" : "";
      return `
        <div class="tcard ${cls} ${active}" data-symbol="${s.symbol}">
          <button class="tremove" data-remove="${s.symbol}" title="Remove">×</button>
          <div class="tsym">${s.symbol}${assetBadge(s.quoteType)}</div>
          <div class="tpct">${fmt.pct(s.pct)}</div>
          <div class="tname">${s.name || ""}</div>
          <div class="tprice">$${fmt.price(s.price)}${extTag(s)}</div>
          ${sparkline(s.spark)}
        </div>`;
    })
    .join("");

  const addCard = `
    <div class="tcard add-card" id="addTickerCard" title="Add a ticker">
      <div class="add-inner"><span class="add-plus">+</span><span>Add</span></div>
    </div>`;

  rail.innerHTML = cards + addCard;

  $$(".tcard[data-symbol]").forEach((el) => {
    el.addEventListener("click", (e) => {
      if (e.target.closest(".tremove")) return;
      selectStock(el.dataset.symbol);
    });
  });
  $$(".tremove").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      removeTicker(btn.dataset.remove);
    });
  });
  $("#addTickerCard").addEventListener("click", () => openDialog("tickerDialog", "tickerSymbol"));
}

async function addTicker(symbol) {
  const r = await fetch("/api/watchlist/add", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ symbol }),
  });
  const data = await r.json();
  if (!r.ok || data.error) throw new Error(data.error || "could not add");
  await loadWatchlist();
  selectStock(symbol.toUpperCase());
  toast(`Added ${data.name || symbol.toUpperCase()} to watchlist.`);
}

async function removeTicker(symbol) {
  try {
    await fetch("/api/watchlist/remove", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ symbol }),
    });
    if (state.active === symbol) state.active = null;
    await loadWatchlist();
    toast(`Removed ${symbol}.`);
  } catch (e) {
    toast("Could not remove: " + e.message);
  }
}

/* ---------- Stock detail + chart ---------- */
async function selectStock(symbol) {
  state.active = symbol;
  reportActivity("view", symbol);   // personalization: note what you look at
  renderWatchlist();
  $("#symName").textContent = "Loading…";
  $("#symSymbol").textContent = symbol;
  $("#symPrice").textContent = "—";
  $("#symChange").textContent = "—";
  await Promise.all([loadChart(), loadAIInsight()]);
}

async function loadChart() {
  if (!state.active) return;
  if (state.view === "forecast") return loadForecast();
  try {
    const url = `/api/stock/${encodeURIComponent(state.active)}?range=${state.range}&interval=${state.interval}`;
    const r = await fetch(url);
    const q = await r.json();
    if (q.error) throw new Error(q.error);
    renderHeader(q);
    renderActiveView(q);
    renderKPI(q);
  } catch (e) {
    $("#symName").textContent = "Error";
    toast(`Could not load ${state.active}: ${e.message}`);
  }
}

function destroyChart() {
  if (state.chart) { state.chart.destroy(); state.chart = null; }
}

// Dispatch to the active chart view (the price line, candlesticks, or % return).
// Forecast has its own loader since it fetches a separate projection payload.
function renderActiveView(q) {
  if (state.view === "candles") return renderCandleChart(q);
  if (state.view === "returns") return renderReturnsChart(q);
  return renderChart(q);
}

// Switch chart view. Anything beyond the basic line is a Pro+ feature.
function setView(view) {
  if (view !== "line" && !gate("forecast")) return;
  state.view = view;
  $$("#viewTabs button[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $("#forecastPanel").hidden = view !== "forecast";
  const sma = $("#smaToggle");
  if (sma) sma.style.display = view === "line" ? "" : "none";
  loadChart();
}

function renderHeader(q) {
  $("#symName").textContent = q.name || q.symbol;
  $("#symSymbol").textContent = q.symbol;
  $("#symExchange").textContent = q.exchange || "";
  $("#symPrice").textContent = "$" + fmt.price(q.price);
  const ch = $("#symChange");
  const up = (q.pct ?? 0) >= 0;
  ch.className = "change " + (up ? "up" : "down");
  ch.textContent = `${fmt.delta(q.change)} (${fmt.pct(q.pct)})`;

  // Extended-hours line (only shows during live pre/post-market trading).
  const ext = $("#symExt");
  if (q.extPrice != null && q.extPct != null) {
    const eu = q.extPct >= 0;
    ext.hidden = false;
    ext.className = "ext-change " + (eu ? "up" : "down");
    ext.innerHTML = `<span class="ext-dot"></span>${q.extLabel}: $${fmt.price(q.extPrice)} `
      + `<span class="ext-delta">${fmt.delta(q.extChange)} (${fmt.pct(q.extPct)})</span>`;
  } else {
    ext.hidden = true;
    ext.innerHTML = "";
  }
}

function renderKPI(q) {
  $("#kpiLow").textContent  = q.low  != null ? "$" + fmt.price(q.low)  : "—";
  $("#kpiHigh").textContent = q.high != null ? "$" + fmt.price(q.high) : "—";
  $("#kpiVol").textContent  = q.volume != null ? fmt.big(q.volume) : "—";
  $("#kpi52L").textContent  = q.fiftyTwoWeekLow  != null ? "$" + fmt.price(q.fiftyTwoWeekLow)  : "—";
  $("#kpi52H").textContent  = q.fiftyTwoWeekHigh != null ? "$" + fmt.price(q.fiftyTwoWeekHigh) : "—";
}

function computeSMA(points, period) {
  const out = [];
  let sum = 0;
  for (let i = 0; i < points.length; i++) {
    sum += points[i].y;
    if (i >= period) sum -= points[i - period].y;
    if (i >= period - 1) out.push({ x: points[i].x, y: sum / period });
  }
  return out;
}

function renderChart(q) {
  state.lastQuote = q;
  destroyChart();
  const ctx = $("#mainChart");
  const c = themeColors();
  const points = (q.history || []).map((p) => ({ x: p.t * 1000, y: p.c }));
  // Colour the line by the *displayed range's* move (first→last point), so a
  // 1Y chart that's up doesn't look red just because today dipped.
  const up = points.length > 1 ? points[points.length - 1].y >= points[0].y : (q.pct ?? 0) >= 0;
  const accent = up ? c.up : c.down;
  const accentSoft = up ? "rgba(2,122,72,0.18)" : "rgba(180,35,24,0.16)";

  const grad = ctx.getContext("2d").createLinearGradient(0, 0, 0, 320);
  grad.addColorStop(0, accentSoft);
  grad.addColorStop(1, "rgba(0,0,0,0)");

  const dataset = {
    label: q.symbol,
    data: points,
    parsing: false,
    borderColor: accent,
    borderWidth: 1.8,
    backgroundColor: grad,
    fill: true,
    pointRadius: 0,
    pointHoverRadius: 4,
    pointHoverBackgroundColor: accent,
    tension: 0.25,
  };

  const datasets = [dataset];
  if (state.smaOn && points.length > 20) {
    datasets.push({
      label: "SMA 20", data: computeSMA(points, 20), parsing: false,
      borderColor: "#6E56CF", borderWidth: 1.3, pointRadius: 0, fill: false, tension: 0.25,
    });
    if (points.length > 50) {
      datasets.push({
        label: "SMA 50", data: computeSMA(points, 50), parsing: false,
        borderColor: "#E8A93B", borderWidth: 1.3, pointRadius: 0, fill: false, tension: 0.25,
      });
    }
  }

  state.chart = new Chart(ctx, {
    type: "line",
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { intersect: false, mode: "index" },
      scales: {
        x: {
          type: "time",
          time: { tooltipFormat: "MMM d, HH:mm" },
          grid: { color: c.grid },
          border: { color: c.border },
          ticks: { color: c.tick, maxRotation: 0, font: { size: 10 } },
        },
        y: {
          position: "right",
          grid: { color: c.grid },
          border: { color: c.border },
          ticks: { color: c.tick, font: { size: 10 }, callback: (v) => "$" + fmt.price(v) },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: c.ttBg,
          borderColor: c.ttBorder,
          borderWidth: 1,
          titleColor: c.ttTitle,
          bodyColor: c.ttBody,
          padding: 10,
          callbacks: { label: (ctx) => "$" + fmt.price(ctx.parsed.y) },
        },
      },
    },
  });
}

/* ---------- Candlestick view (Pro+) ---------- */
function renderCandleChart(q) {
  state.lastQuote = q;
  destroyChart();
  const ctx = $("#mainChart");
  const c = themeColors();
  const candles = (q.history || [])
    .filter((p) => p.o != null && p.h != null && p.l != null && p.c != null)
    .map((p) => ({ x: p.t * 1000, o: p.o, h: p.h, l: p.l, c: p.c }));
  if (candles.length < 2) { renderChart(q); return; }  // no OHLC → fall back to line

  const ys = candles.flatMap((p) => [p.h, p.l]);
  const pad = (Math.max(...ys) - Math.min(...ys)) * 0.06 || 1;
  const yMin = Math.min(...ys) - pad, yMax = Math.max(...ys) + pad;

  const candlePlugin = {
    id: "faamCandles",
    afterDatasetsDraw(chart) {
      const { ctx: cx, scales: { x, y } } = chart;
      const n = candles.length;
      const spacing = (x.getPixelForValue(candles[n - 1].x) - x.getPixelForValue(candles[0].x)) / (n - 1);
      const w = Math.max(1, Math.min(16, Math.abs(spacing) * 0.62));
      cx.save();
      cx.lineWidth = 1;
      candles.forEach((p) => {
        const px = x.getPixelForValue(p.x);
        const col = p.c >= p.o ? c.up : c.down;
        cx.strokeStyle = col; cx.fillStyle = col;
        cx.beginPath();
        cx.moveTo(px, y.getPixelForValue(p.h));
        cx.lineTo(px, y.getPixelForValue(p.l));
        cx.stroke();
        const yo = y.getPixelForValue(p.o), yc = y.getPixelForValue(p.c);
        cx.fillRect(px - w / 2, Math.min(yo, yc), w, Math.max(1, Math.abs(yc - yo)));
      });
      cx.restore();
    },
  };

  state.chart = new Chart(ctx, {
    type: "line",
    data: { datasets: [{ data: candles.map((p) => ({ x: p.x, y: p.c })), parsing: false, borderWidth: 0, pointRadius: 0, showLine: false }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { intersect: false, mode: "index" },
      scales: {
        x: { type: "time", time: { tooltipFormat: "MMM d, HH:mm" }, grid: { color: c.grid }, border: { color: c.border }, ticks: { color: c.tick, maxRotation: 0, font: { size: 10 } } },
        y: { position: "right", min: yMin, max: yMax, grid: { color: c.grid }, border: { color: c.border }, ticks: { color: c.tick, font: { size: 10 }, callback: (v) => "$" + fmt.price(v) } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: c.ttBg, borderColor: c.ttBorder, borderWidth: 1, titleColor: c.ttTitle, bodyColor: c.ttBody, padding: 10,
          callbacks: {
            label: (ti) => {
              const p = candles[ti.dataIndex];
              return p ? [`O ${fmt.price(p.o)}   H ${fmt.price(p.h)}`, `L ${fmt.price(p.l)}   C ${fmt.price(p.c)}`] : "";
            },
          },
        },
      },
    },
    plugins: [candlePlugin],
  });
}

/* ---------- % Return view (Pro+) ---------- */
function renderReturnsChart(q) {
  state.lastQuote = q;
  destroyChart();
  const ctx = $("#mainChart");
  const c = themeColors();
  const hist = (q.history || []).filter((p) => p.c != null);
  if (hist.length < 2) { renderChart(q); return; }
  const base = hist[0].c || 1;
  const points = hist.map((p) => ({ x: p.t * 1000, y: (p.c / base - 1) * 100 }));
  const up = points[points.length - 1].y >= 0;
  const accent = up ? c.up : c.down;
  const grad = ctx.getContext("2d").createLinearGradient(0, 0, 0, 320);
  grad.addColorStop(0, up ? "rgba(2,122,72,0.18)" : "rgba(180,35,24,0.16)");
  grad.addColorStop(1, "rgba(0,0,0,0)");

  state.chart = new Chart(ctx, {
    type: "line",
    data: { datasets: [{ label: q.symbol, data: points, parsing: false, borderColor: accent, borderWidth: 1.8, backgroundColor: grad, fill: true, pointRadius: 0, pointHoverRadius: 4, tension: 0.25 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { intersect: false, mode: "index" },
      scales: {
        x: { type: "time", time: { tooltipFormat: "MMM d, HH:mm" }, grid: { color: c.grid }, border: { color: c.border }, ticks: { color: c.tick, maxRotation: 0, font: { size: 10 } } },
        y: { position: "right", grid: { color: c.grid }, border: { color: c.border }, ticks: { color: c.tick, font: { size: 10 }, callback: (v) => (v >= 0 ? "+" : "") + v.toFixed(1) + "%" } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: c.ttBg, borderColor: c.ttBorder, borderWidth: 1, titleColor: c.ttTitle, bodyColor: c.ttBody, padding: 10,
          callbacks: { label: (ti) => (ti.parsed.y >= 0 ? "+" : "") + ti.parsed.y.toFixed(2) + "%" },
        },
      },
    },
  });
}

/* ---------- Forecast view (Pro+ predictive model) ---------- */
async function loadForecast() {
  if (!state.active) return;
  // Keep the header/KPIs populated from a normal quote.
  try {
    const r0 = await fetch(`/api/stock/${encodeURIComponent(state.active)}?range=3mo&interval=1d`);
    const q0 = await r0.json();
    if (!q0.error) { renderHeader(q0); renderKPI(q0); }
  } catch (e) { /* header is best-effort */ }

  $("#forecastPanel").hidden = false;
  renderNewsPanel(null);
  renderMarketsPanel(null);
  const loadingNote = state.forecastModel === "artemis" ? "Running Artemis on"
    : state.forecastModel === "perseverance" ? "Running Perseverance on" : "Running Apollo on";
  $("#fcStats").innerHTML = `<div class="fc-loading"><span class="pulse-dots"><span></span><span></span><span></span></span> ${loadingNote} ${escapeHtml(state.active)}…</div>`;
  try {
    const r = await fetch(`/api/forecast/${encodeURIComponent(state.active)}?horizon=${state.horizon}&model=${encodeURIComponent(state.forecastModel || "gbm")}`);
    const d = await r.json();
    if (handleUsageLimit(d)) { setView("line"); return; }
    if (d.error) throw new Error(d.error);
    state.forecast = d;
    renderForecastChart(d);
    renderForecastStats(d);
    renderNewsPanel(d.news);
    renderMarketsPanel(d.probabilities);
  } catch (e) {
    destroyChart();
    $("#fcStats").innerHTML = `<div class="fc-err">Forecast unavailable: ${escapeHtml(e.message)}</div>`;
  }
}

function renderForecastChart(d) {
  destroyChart();
  const ctx = $("#mainChart");
  const c = themeColors();
  const up = d.stats.direction === "up";
  const accent = up ? c.up : c.down;
  const histPts = (d.history || []).map((p) => ({ x: p.t * 1000, y: p.c }));
  const fc = d.forecast || [];
  const at = (k) => fc.map((p) => ({ x: p.t * 1000, y: p[k] }));
  const band90 = up ? "rgba(2,122,72,0.08)" : "rgba(180,35,24,0.07)";
  const band68 = up ? "rgba(2,122,72,0.17)" : "rgba(180,35,24,0.15)";

  // Order matters: each lower band fills *up to* the dataset index above it.
  const datasets = [
    { label: "hi90", data: at("hi90"), parsing: false, borderColor: "rgba(0,0,0,0)", pointRadius: 0, fill: false, tension: 0.2 },
    { label: "lo90", data: at("lo90"), parsing: false, borderColor: "rgba(0,0,0,0)", backgroundColor: band90, pointRadius: 0, fill: 0, tension: 0.2 },
    { label: "hi68", data: at("hi68"), parsing: false, borderColor: "rgba(0,0,0,0)", pointRadius: 0, fill: false, tension: 0.2 },
    { label: "lo68", data: at("lo68"), parsing: false, borderColor: "rgba(0,0,0,0)", backgroundColor: band68, pointRadius: 0, fill: 2, tension: 0.2 },
    { label: "Projection", data: at("mean"), parsing: false, borderColor: accent, borderWidth: 2, borderDash: [6, 4], pointRadius: 0, fill: false, tension: 0.2 },
    { label: d.symbol, data: histPts, parsing: false, borderColor: c.tick, borderWidth: 1.8, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.25 },
  ];

  state.chart = new Chart(ctx, {
    type: "line",
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { intersect: false, mode: "index" },
      scales: {
        x: { type: "time", time: { tooltipFormat: "MMM d" }, grid: { color: c.grid }, border: { color: c.border }, ticks: { color: c.tick, maxRotation: 0, font: { size: 10 } } },
        y: { position: "right", grid: { color: c.grid }, border: { color: c.border }, ticks: { color: c.tick, font: { size: 10 }, callback: (v) => "$" + fmt.price(v) } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: c.ttBg, borderColor: c.ttBorder, borderWidth: 1, titleColor: c.ttTitle, bodyColor: c.ttBody, padding: 10,
          filter: (item) => item.dataset.label === "Projection" || item.dataset.label === d.symbol,
          callbacks: { label: (ti) => `${ti.dataset.label === d.symbol ? "Actual" : "Projected"}: $${fmt.price(ti.parsed.y)}` },
        },
      },
    },
  });
}

function renderForecastStats(d) {
  const s = d.stats || {};
  const dirCls = s.direction === "up" ? "up" : "down";
  const arrow = s.direction === "up" ? "▲" : "▼";
  if (d.model) {
    $("#fcModelName").textContent = d.model.name || "Model";
    $("#fcModelKind").textContent = d.model.kind || "";
  }
  $("#fcStats").innerHTML = `
    <div class="fc-stat hero ${dirCls}">
      <label>${d.horizon}-day target</label>
      <span class="v">$${fmt.price(s.target)}</span>
      <span class="sub">${arrow} ${fmt.pct(s.expReturnPct)}</span>
    </div>
    <div class="fc-stat">
      <label>Likely range · 90%</label>
      <span class="v">$${fmt.price(s.lo)} – $${fmt.price(s.hi)}</span>
    </div>
    <div class="fc-stat">
      <label>Annualized volatility</label>
      <span class="v">${s.annVolPct}%</span>
    </div>
    <div class="fc-stat">
      <label>Daily drift</label>
      <span class="v ${(s.driftDailyPct ?? 0) >= 0 ? "up" : "down"}">${fmt.pct(s.driftDailyPct)}</span>
    </div>
    <div class="fc-stat">
      <label>Trend fit · R²</label>
      <span class="v">${s.trendR2}</span>
    </div>`;
}

/* ---------- News & sentiment (News-aware model) ---------- */
function sentimentLabel(v) {
  if (v >= 0.15) return { t: "Bullish", cls: "up" };
  if (v <= -0.15) return { t: "Bearish", cls: "down" };
  return { t: "Neutral", cls: "flat" };
}

function relTime(epoch) {
  const s = Math.max(0, Date.now() / 1000 - (epoch || 0));
  if (!epoch) return "";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

function renderNewsPanel(news) {
  const panel = $("#newsPanel");
  if (!panel) return;
  if (!news) { panel.hidden = true; panel.innerHTML = ""; return; }
  panel.hidden = false;

  if ((news.error && !(news.headlines || []).length)) {
    panel.innerHTML = `<div class="news-head"><span class="news-title">News &amp; Sentiment</span></div>
      <div class="news-empty">${escapeHtml(news.error)}</div>`;
    return;
  }

  const overall = news.overall ?? 0;
  const lbl = sentimentLabel(overall);
  const items = (news.headlines || []).map((h) => {
    const s = h.sentiment ?? 0;
    const sl = sentimentLabel(s);
    const arrow = s >= 0.15 ? "▲" : s <= -0.15 ? "▼" : "–";
    const title = h.link
      ? `<a href="${escapeHtml(h.link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(h.title)}</a>`
      : escapeHtml(h.title);
    const meta = [escapeHtml(h.publisher || ""), relTime(h.time)].filter(Boolean).join(" · ");
    return `<li class="news-item">
        <span class="news-chip ${sl.cls}" title="${(s >= 0 ? "+" : "") + s.toFixed(2)}">${arrow}</span>
        <span class="news-text">${title}<span class="news-src">${meta}</span></span>
      </li>`;
  }).join("");

  panel.innerHTML = `
    <div class="news-head">
      <span class="news-title">News &amp; Sentiment</span>
      <span class="news-overall ${lbl.cls}">${lbl.t} ${(overall >= 0 ? "+" : "") + overall.toFixed(2)}</span>
    </div>
    ${news.summary ? `<p class="news-summary">“${escapeHtml(news.summary)}”</p>` : ""}
    <ul class="news-list">${items}</ul>
    <p class="news-foot">AI read of ${news.count} recent headline${news.count === 1 ? "" : "s"} — ${(news.tiltPct != null && Math.abs(news.tiltPct) > 0.001) ? `tilts the daily drift ${news.tiltPct >= 0 ? "+" : ""}${news.tiltPct.toFixed(2)}%` : "neutral tilt"}, doesn't drive it.</p>`;
}

/* ---------- Forecast model (a saved setting — Apollo or Artemis) ---------- */
const FORECAST_MODELS = [
  { id: "apollo", name: "Apollo", kind: "Drift & volatility engine — fast, no AI cost", minTier: 0 },
  { id: "artemis", name: "Artemis", kind: "Monte Carlo simulation + live news sentiment", minTier: 0 },
  { id: "perseverance", name: "Perseverance", kind: "Apollo + Artemis ensemble — Elite", minTier: 4 },
  { id: "juno", name: "Juno", kind: "Deep model trained on historical stock data", comingSoon: true },
];

function modelAvailable(m) {
  if (m && m.comingSoon) return false;     // teaser only — not selectable yet
  return pro.tier >= ((m && m.minTier) || 0);
}

function renderModelChoices() {
  const wrap = $("#modelChoices");
  if (!wrap) return;
  const current = state.forecastModel || "apollo";
  wrap.innerHTML = FORECAST_MODELS.map((m) => {
    const avail = modelAvailable(m);
    const sel = m.id === current && avail;
    const badge = m.comingSoon
      ? '<span class="mc-lock soon">Coming soon</span>'
      : (avail ? "" : '<span class="mc-lock">Elite</span>');
    return `<button type="button" class="model-choice${sel ? " sel" : ""}${avail ? "" : " locked"}${m.comingSoon ? " coming" : ""}" data-model="${m.id}" aria-pressed="${sel}"${m.comingSoon ? ' aria-disabled="true"' : ""}>
       <span class="mc-radio"></span>
       <span class="mc-main"><span class="mc-name">${escapeHtml(m.name)}</span><span class="mc-kind">${escapeHtml(m.kind)}</span></span>
       ${badge}
     </button>`;
  }).join("");
  wrap.querySelectorAll(".model-choice").forEach((el) => {
    el.addEventListener("click", () => selectForecastModel(el.dataset.model));
  });
}

function selectForecastModel(id) {
  const m = FORECAST_MODELS.find((x) => x.id === id);
  if (!m) return;
  if (m.comingSoon) { toast(`${m.name} is coming soon — trained on historical stock data.`); return; }
  if (!modelAvailable(m)) { toast(`${m.name} is an Elite model.`); openProDialog(); return; }
  state.forecastModel = id;
  try { localStorage.setItem("faam-forecast-model", id); } catch (e) {}
  renderModelChoices();
  const n = $("#fcModelName"), k = $("#fcModelKind");
  if (n) n.textContent = m.name;
  if (k) k.textContent = m.kind;
  if (state.view === "forecast") loadForecast();
}

function openSettings() {
  renderModelChoices();
  renderAddons();
  renderModes();
  renderAiControl();
  renderInterestGrid($("#interestGridSettings"));
  openDialog("settingsDialog");
}

/* ---------- Prediction markets add-on (Max & Elite) ---------- */
function predMarketsAvailable() {
  const need = (pro.features && pro.features.predictions) || 3;
  return pro.tier >= need;
}

function renderAddons() {
  const row = $("#predMarketsRow");
  const toggle = $("#predMarketsToggle");
  if (!toggle) return;
  const avail = predMarketsAvailable();
  if (row) row.classList.toggle("locked", !avail);
  const on = avail && state.predictionMarkets;
  toggle.classList.toggle("on", on);
  toggle.setAttribute("aria-checked", on ? "true" : "false");
}

function togglePredMarkets() {
  if (!predMarketsAvailable()) { gate("predictions"); return; }
  state.predictionMarkets = !state.predictionMarkets;
  try { localStorage.setItem("faam-pred-markets", state.predictionMarkets ? "1" : "0"); } catch (e) {}
  renderAddons();
  if (state.view === "forecast" && state.forecast) renderMarketsPanel(state.forecast.probabilities);
}

function renderMarketsPanel(probs) {
  const panel = $("#marketsPanel");
  if (!panel) return;
  const show = predMarketsAvailable() && state.predictionMarkets && probs && probs.length;
  if (!show) { panel.hidden = true; panel.innerHTML = ""; return; }
  panel.hidden = false;
  const rows = probs.map((m) => {
    const pct = Math.round((m.p ?? 0) * 100);
    return `<div class="market-row">
      <span class="market-label">${escapeHtml(m.label)}</span>
      <div class="market-bar"><span class="market-fill ${m.side}" style="width:${pct}%"></span></div>
      <span class="market-pct ${m.side}">${pct}%</span>
    </div>`;
  }).join("");
  const modelName = (state.forecast && state.forecast.model && state.forecast.model.name) || "Apollo";
  panel.innerHTML = `
    <div class="markets-head"><span class="markets-title">Prediction Markets</span><span class="markets-tag">Model-implied odds</span></div>
    <div class="market-list">${rows}</div>
    <p class="markets-foot">Odds from the ${escapeHtml(modelName)} cone over ${state.horizon} days — estimates, not guarantees.</p>`;
}

/* ---------- Portfolio focus (interests + first-run onboarding) ---------- */
// What a new user can tell FAAM to focus their portfolio on. Each theme maps to
// a few representative, liquid US tickers used to seed the watchlist.
const THEMES = [
  { id: "tech",      name: "Technology",        blurb: "Big tech, software & chips",        tickers: ["AAPL", "MSFT", "NVDA"] },
  { id: "ai",        name: "AI & Data",         blurb: "Chips and platforms behind AI",     tickers: ["NVDA", "AMD", "PLTR"] },
  { id: "music",     name: "Music",             blurb: "Streaming, labels & audio gear",    tickers: ["SPOT", "WMG", "SONO"] },
  { id: "movies",    name: "Movies & TV",       blurb: "Studios and streaming",             tickers: ["DIS", "NFLX", "WBD"] },
  { id: "gaming",    name: "Gaming",            blurb: "Game studios and platforms",        tickers: ["EA", "TTWO", "RBLX"] },
  { id: "factories", name: "Factories & Industry", blurb: "Industrials and machinery",      tickers: ["CAT", "GE", "HON"] },
  { id: "autos",     name: "Autos & EV",        blurb: "Carmakers and electric vehicles",   tickers: ["TSLA", "F", "GM"] },
  { id: "finance",   name: "Finance",           blurb: "Banks, cards & payments",           tickers: ["JPM", "V", "MA"] },
  { id: "energy",    name: "Energy",            blurb: "Oil, gas & renewables",             tickers: ["XOM", "CVX", "NEE"] },
  { id: "health",    name: "Healthcare",        blurb: "Pharma and care",                   tickers: ["JNJ", "PFE", "UNH"] },
  { id: "consumer",  name: "Consumer & Retail", blurb: "Brands and stores",                 tickers: ["AMZN", "NKE", "MCD"] },
  { id: "crypto",    name: "Crypto-linked",     blurb: "Crypto-exposed equities",           tickers: ["COIN", "MSTR"] },
];

function loadInterests() {
  try {
    const raw = localStorage.getItem("faam-interests");
    state.interests = raw ? (JSON.parse(raw) || {}) : {};
  } catch (e) { state.interests = {}; }
  return state.interests;
}
function saveInterests() {
  try { localStorage.setItem("faam-interests", JSON.stringify(state.interests)); } catch (e) {}
}
function setInterest(id, rating) {
  rating = Math.max(0, Math.min(5, rating | 0));
  if (rating <= 0) delete state.interests[id];
  else state.interests[id] = rating;
  saveInterests();
}

// One filled/empty star glyph as SVG (no emoji, crisp at any size).
const STAR_SVG = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M12 2.6l2.9 5.88 6.49.95-4.7 4.58 1.11 6.46L12 17.93 6.2 20.47l1.11-6.46-4.7-4.58 6.49-.95z"/></svg>';

function ratingControl(id, rating) {
  let html = `<div class="stars" role="radiogroup" aria-label="Interest level">`;
  for (let i = 1; i <= 5; i++) {
    const on = i <= rating;
    html += `<button type="button" class="star${on ? " on" : ""}" data-theme="${id}" data-val="${i}" role="radio" aria-checked="${i === rating}" aria-label="${i} of 5 stars">${STAR_SVG}</button>`;
  }
  return html + `</div>`;
}

// Render the rateable theme cards into a container. Uses one delegated click
// handler so re-rendering innerHTML never leaves dangling listeners.
function renderInterestGrid(container) {
  if (!container) return;
  container.innerHTML = THEMES.map((t) => {
    const r = state.interests[t.id] || 0;
    return `<div class="theme-card${r ? " rated" : ""}" data-theme="${t.id}">
      <div class="theme-info">
        <span class="theme-name">${escapeHtml(t.name)}</span>
        <span class="theme-blurb">${escapeHtml(t.blurb)}</span>
        <span class="theme-tickers">${t.tickers.join(" · ")}</span>
      </div>
      ${ratingControl(t.id, r)}
    </div>`;
  }).join("");
  if (!container._wired) {
    container.addEventListener("click", (e) => {
      const btn = e.target.closest(".star");
      if (!btn || !container.contains(btn)) return;
      const id = btn.dataset.theme;
      let val = parseInt(btn.dataset.val, 10) || 0;
      if ((state.interests[id] || 0) === val) val = 0; // tap the active star again to clear
      setInterest(id, val);
      renderInterestGrid(container);
    });
    container._wired = true;
  }
}

// Pick representative tickers from the rated themes, strongest interest first.
function suggestedTickers(limit = 6) {
  const rated = Object.keys(state.interests)
    .map((id) => ({ r: state.interests[id], t: THEMES.find((x) => x.id === id) }))
    .filter((x) => x.t && x.r > 0)
    .sort((a, b) => b.r - a.r);
  const out = [];
  for (let depth = 0; depth < 3 && out.length < limit; depth++) {
    for (const x of rated) {
      if (out.length >= limit) break;
      const s = x.t.tickers[depth];
      if (s && !out.includes(s)) out.push(s);
    }
  }
  return out;
}

async function seedWatchlistFromInterests(limit = 6) {
  const syms = suggestedTickers(limit);
  const added = [];
  for (const s of syms) {
    try {
      const r = await fetch("/api/watchlist/add", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ symbol: s }),
      });
      const d = await r.json();
      if (r.ok && !d.error) added.push(s);
    } catch (e) { /* skip a ticker that fails to fetch */ }
  }
  if (added.length) {
    await loadWatchlist();
    selectStock(added[0]);
  }
  return added;
}

function ratedThemeNames() {
  return Object.keys(state.interests)
    .filter((id) => state.interests[id] > 0)
    .sort((a, b) => state.interests[b] - state.interests[a])
    .map((id) => (THEMES.find((t) => t.id === id) || {}).name)
    .filter(Boolean);
}

/* ---------- First-run onboarding (FAAM Assistant) ---------- */
function isOnboarded() {
  try { return localStorage.getItem("faam-onboarded") === "1"; } catch (e) { return true; }
}
function markOnboarded() {
  try { localStorage.setItem("faam-onboarded", "1"); } catch (e) {}
}
function maybeOnboard() {
  if (isOnboarded()) return;
  setTimeout(openOnboarding, 650); // let the dashboard paint before greeting
}
function openOnboarding() {
  renderInterestGrid($("#interestGridOnboard"));
  openDialog("onboardDialog");
}
function skipOnboarding() {
  markOnboarded();
  const dlg = $("#onboardDialog");
  if (dlg && dlg.open) dlg.close();
  toast("No problem — set your focus anytime in Settings.");
}
async function finishOnboarding() {
  markOnboarded();
  const names = ratedThemeNames();
  const dlg = $("#onboardDialog");
  if (!names.length) {
    if (dlg && dlg.open) dlg.close();
    toast("Saved. Add areas you like anytime in Settings.");
    return;
  }
  const btn = $("#onboardBuild");
  if (btn) { btn.disabled = true; btn.textContent = "Building your dashboard…"; }
  const added = await seedWatchlistFromInterests(6);
  if (btn) { btn.disabled = false; btn.textContent = "Build my portfolio"; }
  if (dlg && dlg.open) dlg.close();
  toast(added.length
    ? `Tailored to ${names.slice(0, 3).join(", ")} — added ${added.length} to your watchlist.`
    : "Saved your focus. Add tickers anytime from the watchlist.");
}

/* ===================================================================
   Modes: Beginner (guided tour) + Game of Stocks (gamification)
   =================================================================== */
function reducedMotion() {
  try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { return false; }
}

function applyModes() {
  document.body.classList.toggle("beginner", !!state.beginner);
  document.body.classList.toggle("game-on", !!state.gameOn);
  const chip = $("#tokenChip"), gbtn = $("#gameBtn");
  if (chip) chip.hidden = !state.gameOn;
  if (gbtn) gbtn.hidden = !state.gameOn;
}

function renderModes() {
  const set = (el, on) => { if (el) { el.classList.toggle("on", !!on); el.setAttribute("aria-checked", on ? "true" : "false"); } };
  set($("#beginnerToggle"), state.beginner);
  set($("#gameToggle"), state.gameOn);
}

function toggleBeginner() {
  state.beginner = !state.beginner;
  try { localStorage.setItem("faam-beginner", state.beginner ? "1" : "0"); } catch (e) {}
  applyModes(); renderModes();
  const banner = $("#beginnerBanner");
  if (banner) banner.classList.remove("dismissed");
  if (state.beginner) openCoach(0);
  else toast("Beginner mode off.");
}

function toggleGame() {
  state.gameOn = !state.gameOn;
  try { localStorage.setItem("faam-game", state.gameOn ? "1" : "0"); } catch (e) {}
  applyModes(); renderModes();
  if (state.gameOn) {
    loadGame();
    toast("Game of Stocks on — tap the controller to play.");
  }
}

/* ---------- AI control of the dashboard (permission-gated) ----------
   The assistant can take a plain-English request, open
   the right stock, and FILL an order ticket for you. It asks for permission the
   first time, and it still NEVER places a trade or moves money — you review and
   submit at your broker. */
let _aiControlResolver = null;

function renderAiControl() {
  const t = $("#aiControlToggle");
  if (t) { t.classList.toggle("on", !!state.aiControl); t.setAttribute("aria-checked", state.aiControl ? "true" : "false"); }
}

function setAiControl(on) {
  state.aiControl = !!on;
  try { localStorage.setItem("faam-ai-control", on ? "1" : "0"); } catch (e) {}
  renderAiControl();
}

// Toggle from Settings — the toggle itself is the consent.
function toggleAiControl() {
  if (state.aiControl) { setAiControl(false); toast("Assistant control turned off."); }
  else { setAiControl(true); toast("Assistant can now fill order tickets for you."); }
}

// Ask the user before the assistant drives the dashboard. Resolves true once
// they've allowed it (now or earlier). Granting is remembered on this device.
function requestAiControl() {
  if (state.aiControl) return Promise.resolve(true);
  const dlg = $("#aiControlDialog");
  if (!dlg) return Promise.resolve(false);
  return new Promise((resolve) => {
    _aiControlResolver = resolve;
    if (!dlg.open) dlg.showModal();
  });
}

function resolveAiControl(granted) {
  const dlg = $("#aiControlDialog");
  if (dlg && dlg.open) dlg.close();
  if (granted) setAiControl(true);
  if (_aiControlResolver) { _aiControlResolver(!!granted); _aiControlResolver = null; }
}

// A few common company names so plain requests work even before /api/order/parse.
const ORDER_COMPANY_HINT = /\b(apple|tesla|nvidia|nvda|microsoft|amazon|alphabet|google|meta|facebook|netflix|disney|nike|coca[- ]?cola|pepsi|amd|intel|boeing|ford|gm|starbucks|walmart|costco|visa|mastercard|paypal|uber|lyft|airbnb|spotify|palantir|coinbase|robinhood|broadcom|qualcomm|oracle|salesforce|adobe|shopify)\b/;

// Does this chat line read as an order COMMAND (vs. a question / advice)?
function looksLikeOrder(text) {
  const t = text.toLowerCase().trim();
  if (/\?\s*$/.test(t)) return false; // a question → send to chat as advice
  if (!/\b(buy|sell|purchase|invest|short|offload|sell off|put .* into)\b/.test(t)) return false;
  if (/\b(should i|should we|is it|is now|worth|recommend|do you think|good time|when should|how much should|why)\b/.test(t)) return false;
  return /\$\s?\d|\b\d+\s*(shares?|sh)\b|\b[A-Z]{1,5}\b/.test(text) || ORDER_COMPANY_HINT.test(t);
}

// Append an assistant "action" line to the chat log (and history).
function aiSay(text, cls = "ai-action") {
  const el = appendMsg("ai", text);
  if (cls) el.classList.add(cls);
  state.chatHistory.push({ role: "assistant", content: text });
  return el;
}

function flashOrderDialog() {
  const dlg = $("#orderDialog");
  if (!dlg || reducedMotion()) return;
  dlg.classList.remove("ai-controlled");
  void dlg.offsetWidth; // reflow so the animation restarts
  dlg.classList.add("ai-controlled");
  setTimeout(() => dlg.classList.remove("ai-controlled"), 1700);
}

// The agent: permission → understand the order → drive the dashboard → fill the
// ticket. It stops at a ready-to-review ticket; it does not submit anything.
async function runAiOrder(text) {
  const ok = await requestAiControl();
  if (!ok) {
    aiSay("No problem — I won't touch your dashboard. Ask me anything instead.");
    return;
  }
  const loadingEl = appendMsg("ai", "On it — reading your order…", { loading: true });
  let d;
  try {
    const r = await fetch("/api/order/parse", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
    });
    d = await r.json();
    if (!r.ok || d.error) {
      loadingEl.classList.remove("loading");
      let msg = (d && d.error) || "I couldn't read that as an order. Try “buy $500 of Apple”.";
      if (!state.aiEnabled) msg += "";
      loadingEl.textContent = msg;
      state.chatHistory.push({ role: "assistant", content: msg });
      return;
    }
  } catch (e) {
    loadingEl.classList.remove("loading");
    loadingEl.classList.add("err");
    loadingEl.textContent = "Network error: " + e.message;
    return;
  }
  loadingEl.remove();

  const nm = String(d.name || d.symbol).replace(/\.+$/, "");
  // 1) Navigate the dashboard to the stock.
  if (d.symbol && d.valid && d.symbol !== state.active) {
    aiSay(`Opening ${nm} (${d.symbol})…`);
    try { await selectStock(d.symbol); } catch (e) { /* keep going */ }
  }
  // 2) Open the order ticket and fill it in — without submitting.
  await openOrderTicket();
  setOrderSide(d.side === "sell" ? "sell" : "buy");
  setOrderMode(d.mode === "dollars" ? "dollars" : "shares");
  $("#orderSymbol").value = d.symbol || "";
  if (d.qty != null) $("#orderQty").value = d.qty;
  await updateEstimate();
  flashOrderDialog();

  const sideWord = d.side === "sell" ? "Sell" : "Buy";
  const qtyStr = d.qty == null ? "" :
    (d.mode === "dollars" ? "$" + d.qty : d.qty + (Number(d.qty) === 1 ? " share" : " shares"));
  aiSay(`Filled a ${sideWord} ticket — ${qtyStr} ${nm}. Review it and place it at your broker. I never submit trades or move money.`);
}

/* ---------- Custom dashboard layout (Default · Build your own · AI) ----------
   The user can keep the default, hand-build their layout (toggle + reorder
   panels), or ask GPT-4.1 mini to design one. Stored in localStorage and
   applied by toggling/reordering the real dashboard sections. */
const DASH_BLOCKS = ["watchlist", "chart", "portfolio"];          // reorderable stack
const DASH_TOGGLES = ["watchlist", "insights", "kpis", "portfolio"];
const DASH_META = {
  watchlist: { name: "Watchlist", desc: "Your ticker rail", toggle: true },
  chart:     { name: "Price chart", desc: "Main chart & price — always on", toggle: false },
  portfolio: { name: "Portfolio", desc: "Your holdings table", toggle: true },
  insights:  { name: "AI Agent Insights", desc: "The AI take, beside the chart", toggle: true },
  kpis:      { name: "Day stats", desc: "Day low/high, volume, 52-week range", toggle: true },
};
let _railHome = null;

function dashDefault() {
  return { mode: "default", order: DASH_BLOCKS.slice(),
           widgets: { watchlist: true, insights: true, kpis: true, portfolio: true } };
}
function dashSanitize(obj) {
  const out = dashDefault();
  if (obj && typeof obj === "object") {
    if (obj.widgets && typeof obj.widgets === "object") {
      DASH_TOGGLES.forEach((k) => { if (k in obj.widgets) out.widgets[k] = !!obj.widgets[k]; });
    }
    if (Array.isArray(obj.order)) {
      const uniq = [];
      obj.order.forEach((x) => { if (DASH_BLOCKS.includes(x) && !uniq.includes(x)) uniq.push(x); });
      DASH_BLOCKS.forEach((x) => { if (!uniq.includes(x)) uniq.push(x); });
      out.order = uniq;
    }
    if (["default", "custom", "ai"].includes(obj.mode)) out.mode = obj.mode;
  }
  return out;
}
function loadDashLayout() {
  try {
    const raw = localStorage.getItem("faam-dash-layout");
    state.dashLayout = raw ? dashSanitize(JSON.parse(raw)) : dashDefault();
  } catch (e) { state.dashLayout = dashDefault(); }
}
function saveDashLayout() {
  try { localStorage.setItem("faam-dash-layout", JSON.stringify(state.dashLayout)); } catch (e) {}
}
function applyDashLayout() {
  const L = state.dashLayout || (state.dashLayout = dashDefault());
  const content = document.querySelector(".content");
  const rail = $("#tickerRail");
  if (!content || !rail) return;
  if (!_railHome) _railHome = { parent: rail.parentNode, next: rail.nextSibling };
  const custom = L.mode !== "default";
  document.body.classList.toggle("dash-custom", custom);

  // The watchlist lives above the scroll area by default; pull it inside so it
  // can be reordered with the other blocks when the user customizes.
  if (custom) {
    if (rail.parentNode !== content) content.insertBefore(rail, content.firstChild);
  } else if (rail.parentNode !== _railHome.parent) {
    _railHome.parent.insertBefore(rail, _railHome.next);
  }

  const show = (el, on) => { if (el) el.hidden = !on; };
  show(rail, custom ? L.widgets.watchlist : true);
  show(document.querySelector(".ai-panel"), custom ? L.widgets.insights : true);
  show(document.querySelector(".kpi-row"), custom ? L.widgets.kpis : true);
  show(document.querySelector(".portfolio-panel"), custom ? L.widgets.portfolio : true);
  const grid = document.querySelector(".content .grid") || document.querySelector(".grid");
  if (grid) grid.classList.toggle("no-insights", custom && !L.widgets.insights);

  const map = { watchlist: rail, chart: grid, portfolio: document.querySelector(".portfolio-panel") };
  if (custom) L.order.forEach((id, i) => { const el = map[id]; if (el) el.style.order = String(i); });
  else Object.values(map).forEach((el) => { if (el) el.style.order = ""; });
}

function openLayout() {
  const m = state.dashLayout ? state.dashLayout.mode : "default";
  setLayoutPane(m === "default" ? "default" : (m === "ai" ? "ai" : "custom"));
  renderLayoutList();
  openDialog("layoutDialog");
}
function setLayoutPane(mode) {
  $$("#layoutSeg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("#layPaneDefault").classList.toggle("hide", mode !== "default");
  $("#layPaneCustom").classList.toggle("hide", mode !== "custom");
  $("#layPaneAi").classList.toggle("hide", mode !== "ai");
  if (mode === "custom") renderLayoutList();
}
function renderLayoutList() {
  const list = $("#layList"); if (!list) return;
  const L = state.dashLayout; list.innerHTML = "";
  L.order.forEach((id, i) => {
    const m = DASH_META[id];
    const on = m.toggle ? L.widgets[id] : true;
    const row = document.createElement("div");
    row.className = "lay-row";
    row.innerHTML =
      `<span class="lay-grip" aria-hidden="true">⠿</span>`
      + `<div class="lay-main"><span class="lay-name">${m.name}</span><span class="lay-desc muted small">${escapeHtml(m.desc)}</span></div>`
      + (m.toggle
          ? `<button type="button" class="addon-toggle ${on ? "on" : ""}" data-toggle="${id}" role="switch" aria-checked="${on}" aria-label="Toggle ${m.name}"><span class="addon-knob"></span></button>`
          : `<span class="lay-lock muted small">On</span>`)
      + `<div class="lay-arrows">`
      + `<button type="button" class="lay-arrow" data-move="up" data-i="${i}" ${i === 0 ? "disabled" : ""} aria-label="Move ${m.name} up">▲</button>`
      + `<button type="button" class="lay-arrow" data-move="down" data-i="${i}" ${i === L.order.length - 1 ? "disabled" : ""} aria-label="Move ${m.name} down">▼</button>`
      + `</div>`;
    list.appendChild(row);
  });
  ["insights", "kpis"].forEach((id) => {
    const m = DASH_META[id]; const on = L.widgets[id];
    const row = document.createElement("div");
    row.className = "lay-row lay-sub";
    row.innerHTML =
      `<div class="lay-main"><span class="lay-name">${m.name}</span><span class="lay-desc muted small">${escapeHtml(m.desc)}</span></div>`
      + `<button type="button" class="addon-toggle ${on ? "on" : ""}" data-toggle="${id}" role="switch" aria-checked="${on}" aria-label="Toggle ${m.name}"><span class="addon-knob"></span></button>`;
    list.appendChild(row);
  });
}
function dashToCustom() { if (state.dashLayout.mode === "default") state.dashLayout.mode = "custom"; }
function toggleDashWidget(id) {
  state.dashLayout.widgets[id] = !state.dashLayout.widgets[id];
  dashToCustom(); saveDashLayout(); applyDashLayout(); renderLayoutList();
}
function moveDashBlock(i, dir) {
  const order = state.dashLayout.order;
  const j = dir === "up" ? i - 1 : i + 1;
  if (j < 0 || j >= order.length) return;
  [order[i], order[j]] = [order[j], order[i]];
  dashToCustom(); saveDashLayout(); applyDashLayout(); renderLayoutList();
}
function useDefaultLayout() {
  state.dashLayout = dashDefault(); saveDashLayout(); applyDashLayout();
  setLayoutPane("default"); toast("Default dashboard restored.");
}
async function aiDesignLayout() {
  const prompt = $("#layAiPrompt").value.trim();
  if (!prompt) { $("#layAiPrompt").focus(); return; }
  const btn = $("#layAiGo"); const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Designing…";
  const note = $("#layAiNote"); note.hidden = true;
  try {
    const r = await fetch("/api/dashboard/layout", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    const d = await r.json();
    if (!r.ok || d.error) { note.hidden = false; note.textContent = d.error || "Couldn't design that — try rephrasing."; return; }
    state.dashLayout = dashSanitize({ ...d, mode: "ai" });
    saveDashLayout(); applyDashLayout();
    note.hidden = false;
    note.textContent = d.source === "ai"
      ? "Designed by GPT-4.1 mini ✓ — fine-tune it under “Build your own.”"
      : "Designed for you ✓ — fine-tune it under “Build your own.”";
    setTimeout(() => setLayoutPane("custom"), 800);
    toast("FAAM designed your dashboard.");
  } catch (e) {
    note.hidden = false; note.textContent = "Network error: " + e.message;
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}

/* ---------- Beginner coach (guided tour) ---------- */
const COACH_LESSONS = [
  { k: "Welcome", t: "Welcome to FAAM", x: "FAAM is your personal market dashboard with a built-in AI analyst. This 60-second tour shows you around — no jargon, promise.", art: "spark" },
  { k: "Watchlist", t: "Your watchlist", x: "The cards along the top are stocks you're following. Click any one to load its chart and details below. Use “Add to watchlist” to track more.", art: "cards" },
  { k: "The chart", t: "Reading the chart", x: "The line is the stock's price over time. Green means it rose over the period, red means it fell. Tap the ranges (1D…5Y) to zoom out or in.", art: "chart" },
  { k: "Ask the AI", t: "Your AI analyst", x: "“Agent Insights” gives a plain-English read on the stock. You can also type a question at the bottom — like “is this a good time to buy?”.", art: "ai" },
  { k: "Forecast", t: "Forecasts are odds, not promises", x: "The Forecast view draws a cone of where the price might head. A wider cone means more uncertainty. It's a guide — you always make the call.", art: "cone" },
  { k: "Safe", t: "You're always in control", x: "FAAM never trades or moves your money. It prepares ideas and hands them to you. Explore freely — you can't break anything.", art: "shield" },
];
let coachI = 0;

function coachArt(kind) {
  const inner = {
    spark: '<path fill="#fff" d="M48 20l5 17 17 5-17 5-5 17-5-17-17-5 17-5z"/>',
    cards: '<rect x="26" y="34" width="20" height="30" rx="3" fill="#fff" opacity=".55"/><rect x="40" y="28" width="20" height="36" rx="3" fill="#fff" opacity=".8"/><rect x="54" y="38" width="18" height="26" rx="3" fill="#fff"/>',
    chart: '<polyline points="24,64 38,52 48,58 60,38 72,30" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/><circle cx="72" cy="30" r="4.5" fill="#fff"/>',
    ai: '<rect x="24" y="28" width="48" height="32" rx="9" fill="#fff"/><path d="M40 60l-6 8v-8z" fill="#fff"/><text x="48" y="50" text-anchor="middle" font-size="16" font-weight="800" font-family="Inter,system-ui" fill="#7c5cff">AI</text>',
    cone: '<polygon points="30,48 72,30 72,66" fill="#fff" opacity=".5"/><polyline points="22,52 30,48" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round"/><polyline points="30,48 72,48" fill="none" stroke="#fff" stroke-width="3" stroke-dasharray="5 5"/>',
    shield: '<path fill="#fff" d="M48 22l20 8v14c0 13-9 20-20 24-11-4-20-11-20-24V30z"/><path d="M40 47l6 6 12-13" fill="none" stroke="#7c5cff" stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round"/>',
  }[kind] || '';
  return `<svg viewBox="0 0 96 96" width="120" height="120" aria-hidden="true">
    <circle cx="48" cy="48" r="44" fill="url(#coachGrad)"/>
    <defs><linearGradient id="coachGrad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#5B8BFF"/><stop offset="100%" stop-color="#8B6FFF"/>
    </linearGradient></defs>${inner}</svg>`;
}

function openCoach(i) { coachI = i || 0; renderCoach(); openDialog("coachDialog"); }

function renderCoach() {
  const L = COACH_LESSONS[coachI];
  $("#coachTitle").textContent = L.t;
  $("#coachText").textContent = L.x;
  $("#coachKicker").textContent = `Beginner mode · ${coachI + 1} of ${COACH_LESSONS.length}`;
  $("#coachArt").innerHTML = coachArt(L.art);
  $("#coachDots").innerHTML = COACH_LESSONS.map((_, i) => `<span class="coach-dot${i === coachI ? " on" : ""}"></span>`).join("");
  $("#coachPrev").style.visibility = coachI === 0 ? "hidden" : "visible";
  $("#coachNext").textContent = coachI === COACH_LESSONS.length - 1 ? "Got it" : "Next";
  const step = document.querySelector(".coach-step");
  const art = $("#coachArt");
  [step, art].forEach((el) => { if (!el) return; el.classList.remove("pop"); void el.offsetWidth; el.classList.add("pop"); });
}

function coachNext() {
  if (coachI >= COACH_LESSONS.length - 1) { $("#coachDialog").close(); toast("You're all set — tips stay on while Beginner mode is on."); }
  else { coachI++; renderCoach(); }
}
function coachPrev() { if (coachI > 0) { coachI--; renderCoach(); } }

/* ---------- Game of Stocks ---------- */
async function loadGame() {
  try {
    const r = await fetch("/api/game");
    const d = await r.json();
    state.game = d && d.auth ? d : null;
  } catch (e) { state.game = null; }
  updateTokenChip();
  if ($("#gameDialog") && $("#gameDialog").open) renderGame();
}

function updateTokenChip() {
  const el = $("#tokenChipVal");
  if (!el) return;
  const next = state.game ? state.game.tokens : 0;
  const prev = parseInt((el.textContent || "0").replace(/[^\d]/g, ""), 10) || 0;
  el.textContent = next.toLocaleString();
  if (next > prev) {
    const chip = $("#tokenChip");
    if (chip) { chip.classList.remove("bump"); void chip.offsetWidth; chip.classList.add("bump"); }
  }
}

async function openGame() {
  openDialog("gameDialog");
  renderGame();
  await Promise.all([loadGame(), loadLeaderboard()]);
  renderGame();
}

function renderGame() {
  const g = state.game;
  const btn = $("#claimBtn"), note = $("#claimNote"), card = $("#gDailyCard");
  if (!g) {
    $("#gTokens").textContent = "0";
    $("#gLevel").textContent = "Level 1";
    if (btn) { btn.disabled = true; btn.textContent = "Claim"; }
    if (note) note.textContent = "Log in to play.";
    return;
  }
  countUp($("#gTokens"), g.tokens);
  const lv = g.level || { level: 1, into: 0, span: 200 };
  $("#gLevel").textContent = "Level " + lv.level;
  $("#gLevelMeta").textContent = `${lv.into} / ${lv.span}`;
  const pct = Math.max(4, Math.round((lv.into / lv.span) * 100));
  const f = $("#gBarFill"); if (f) f.style.width = pct + "%"; // CSS transition animates the fill
  $("#gRank").textContent = "#" + g.rank;
  $("#gPlayers").textContent = `of ${g.players}`;
  $("#gStreak").textContent = g.streak;
  $("#gBest").textContent = g.best_streak;
  if (g.claimable) {
    btn.disabled = false; btn.textContent = `Claim +${g.reward_preview}`;
    card.classList.add("ready");
    note.textContent = g.streak > 0 ? `Keep your ${g.streak}-day streak alive!` : "Start your streak today.";
  } else {
    btn.disabled = true; btn.textContent = "Claimed today";
    card.classList.remove("ready");
    note.textContent = "Come back tomorrow for more.";
  }
}

async function loadLeaderboard() {
  try {
    const r = await fetch("/api/game/leaderboard");
    const d = await r.json();
    renderLeaderboard(d.leaderboard || []);
  } catch (e) {}
}

function renderLeaderboard(rows) {
  const ol = $("#gLeaderboard");
  if (!ol) return;
  ol.innerHTML = rows.slice(0, 10).map((r) => {
    const badge = r.rank <= 3 ? `<span class="lb-medal m${r.rank}">${r.rank}</span>` : `<span class="lb-rank">${r.rank}</span>`;
    const flame = r.streak > 0 ? `<svg class="lb-fl" viewBox="0 0 24 24" width="11" height="11" aria-hidden="true"><path fill="currentColor" d="M12 2s5 4 5 9a5 5 0 0 1-10 0c0-1.5.6-2.7 1.3-3.6C8.7 8.5 9 10 10 10c.9 0 1-1.2.5-2.5C9.8 5.6 12 4 12 2z"/></svg>${r.streak}` : "";
    return `<li class="lb-row${r.you ? " you" : ""}" style="--i:${Math.min(r.rank, 10)}">
      ${badge}
      <span class="lb-name">${escapeHtml(r.name)}${r.you ? ' <span class="lb-youtag">you</span>' : ""}</span>
      <span class="lb-streak">${flame}</span>
      <span class="lb-tokens">${(r.tokens || 0).toLocaleString()}</span>
    </li>`;
  }).join("");
}

async function claimDaily() {
  if (!state.game) { toast("Log in to play."); return; }
  if (!state.game.claimable) return;
  const btn = $("#claimBtn");
  btn.disabled = true;
  try {
    const r = await fetch("/api/game/claim", { method: "POST" });
    const d = await r.json();
    if (d.error) { toast(d.error); renderGame(); return; }
    const before = state.game.tokens;
    state.game = { ...state.game, ...d, claimable: false };
    confettiBurst();
    countUp($("#gTokens"), d.tokens, before);
    updateTokenChip();
    renderGame();
    loadLeaderboard();
    toast(`+${d.reward} tokens · ${d.streak}-day streak!`);
  } catch (e) {
    toast("Could not claim: " + e.message);
    if (state.game) state.game.claimable = true;
    renderGame();
  }
}

function countUp(el, to, from) {
  if (!el) return;
  to = (+to) || 0;
  from = from == null ? (parseInt((el.textContent || "0").replace(/[^\d-]/g, ""), 10) || 0) : from;
  // rAF is paused while the tab is hidden — set the value directly so it's never stale.
  if (reducedMotion() || from === to || document.hidden) { el.textContent = to.toLocaleString(); return; }
  const t0 = performance.now(), dur = 700;
  el.textContent = from.toLocaleString();
  function tick(t) {
    const p = Math.min(1, (t - t0) / dur);
    const v = Math.round(from + (to - from) * (1 - Math.pow(1 - p, 3)));
    el.textContent = v.toLocaleString();
    if (p < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

function confettiBurst() {
  const cv = $("#confettiCanvas");
  const dlg = $("#gameDialog");
  if (!cv || !dlg || reducedMotion()) return;
  const rect = dlg.getBoundingClientRect();
  cv.width = rect.width; cv.height = rect.height;
  const ctx = cv.getContext("2d");
  const colors = ["#5B8BFF", "#8B6FFF", "#2ee6a8", "#ffd66e", "#ff5c7a"];
  const parts = [];
  for (let i = 0; i < 130; i++) {
    parts.push({
      x: rect.width / 2, y: rect.height * 0.3,
      vx: (Math.random() - 0.5) * 10, vy: Math.random() * -10 - 3, g: 0.3,
      s: 4 + Math.random() * 5, c: colors[i % colors.length],
      r: Math.random() * Math.PI, vr: (Math.random() - 0.5) * 0.35, life: 1,
    });
  }
  const t0 = performance.now();
  function frame(t) {
    ctx.clearRect(0, 0, cv.width, cv.height);
    let alive = false;
    for (const p of parts) {
      p.vy += p.g; p.x += p.vx; p.y += p.vy; p.r += p.vr; p.life -= 0.0075;
      if (p.life > 0 && p.y < rect.height + 24) {
        alive = true;
        ctx.save(); ctx.globalAlpha = Math.max(0, p.life);
        ctx.translate(p.x, p.y); ctx.rotate(p.r);
        ctx.fillStyle = p.c; ctx.fillRect(-p.s / 2, -p.s / 2, p.s, p.s * 0.62);
        ctx.restore();
      }
    }
    if (alive && t - t0 < 2800) requestAnimationFrame(frame);
    else ctx.clearRect(0, 0, cv.width, cv.height);
  }
  requestAnimationFrame(frame);
}

/* ---------- Portfolio ---------- */
async function loadPortfolio() {
  try {
    const r = await fetch("/api/portfolio");
    const data = await r.json();
    renderPortfolio(data);
  } catch (e) {
    $("#pfBody").innerHTML = `<tr><td colspan="7" class="muted small">Failed to load portfolio: ${e.message}</td></tr>`;
  }
}

function renderPortfolio(data) {
  const body = $("#pfBody");
  const empty = $("#pfEmpty");
  const summary = $("#pfSummary");
  const positions = data.positions || [];

  if (!positions.length) {
    body.innerHTML = "";
    empty.style.display = "block";
    summary.innerHTML = "";
    return;
  }
  empty.style.display = "none";

  body.innerHTML = positions
    .map((p) => {
      const pnlCls = p.pnl >= 0 ? "up" : "down";
      return `<tr>
        <td><span class="pf-sym">${p.symbol}</span> <span class="pf-name">${p.name || ""}</span></td>
        <td>${fmt.shares(p.shares)}</td>
        <td>$${fmt.price(p.cost)}</td>
        <td>$${fmt.price(p.price)}</td>
        <td>${fmt.money(p.marketValue)}</td>
        <td class="${pnlCls}">${fmt.signedMoney(p.pnl)} <span class="small">(${fmt.pct(p.pnlPct)})</span></td>
        <td><button class="pf-remove" data-remove-id="${p.id}" title="Remove">×</button></td>
      </tr>`;
    })
    .join("");

  const t = data.totals || {};
  const pnlCls = (t.pnl ?? 0) >= 0 ? "up" : "down";
  summary.innerHTML = `
    <div class="pf-stat"><label>Market value</label><span class="v">${fmt.money(t.value)}</span></div>
    <div class="pf-stat"><label>Total P&amp;L</label><span class="v ${pnlCls}">${fmt.signedMoney(t.pnl)} (${fmt.pct(t.pnlPct)})</span></div>`;

  $$(".pf-remove").forEach((btn) => {
    btn.addEventListener("click", () => removePosition(btn.dataset.removeId));
  });
}

async function addPositionReq(body) {
  const r = await fetch("/api/portfolio/add", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok || data.error) throw new Error(data.error || "could not add");
  await loadPortfolio();
  return data;
}

async function removePosition(id) {
  try {
    await fetch("/api/portfolio/remove", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ id }),
    });
    await loadPortfolio();
  } catch (e) {
    toast("Could not remove position: " + e.message);
  }
}

/* ---------- AI insights ---------- */
async function loadAIInsight() {
  if (!state.active) return;
  const body = $("#aiBody");
  body.classList.add("loading");
  body.innerHTML = `<div class="pulse"><span></span><span></span><span></span></div>`;
  try {
    const r = await fetch("/api/analyze", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ symbol: state.active }),
    });
    const data = await r.json();
    body.classList.remove("loading");
    if (data.error) {
      if (handleUsageLimit(data)) {
        body.innerHTML = `<span class="muted small">Usage limit reached for your plan.</span>`;
        return;
      }
      body.innerHTML = `<span class="muted small">AI unavailable: ${data.error}</span>`;
      return;
    }
    body.textContent = data.text || "No insight returned.";
  } catch (e) {
    body.classList.remove("loading");
    body.innerHTML = `<span class="muted small">AI error: ${e.message}</span>`;
  }
}

/* ---------- Chat ---------- */
async function sendChat(text, opts = { openDialog: true }) {
  if (!text.trim()) return;
  state.chatHistory.push({ role: "user", content: text });
  if (opts.openDialog) openChatDialog();
  appendMsg("user", text);
  // If this reads like an order command, the assistant drives the dashboard and
  // fills the ticket (after asking permission) instead of just replying.
  if (looksLikeOrder(text)) { await runAiOrder(text); return; }
  const loadingEl = appendMsg("ai", "…", { loading: true });
  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ symbol: state.active, messages: state.chatHistory }),
    });
    const data = await r.json();
    if (data.error) {
      if (handleUsageLimit(data)) {
        loadingEl.classList.add("err");
        loadingEl.textContent = data.message;
        return;
      }
      loadingEl.classList.add("err");
      loadingEl.textContent = `${data.error}${data.detail ? "\n" + data.detail : ""}`;
      return;
    }
    if (data.source === "titan") {
      loadingEl.classList.add("titan");
      loadingEl.title = `Answered by ${(data.titan && data.titan.version) || "Titan 1.1 Beta"} — learned locally, AI is offline`;
      loadingEl.textContent = data.text;
    } else {
      loadingEl.textContent = data.text;
    }
    state.chatHistory.push({ role: "assistant", content: data.text });
  } catch (e) {
    loadingEl.classList.add("err");
    loadingEl.textContent = "Network error: " + e.message;
  }
}

function appendMsg(role, text, opts = {}) {
  const log = $("#chatLog");
  const el = document.createElement("div");
  el.className = "msg " + role + (opts.loading ? " loading" : "");
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function openChatDialog() {
  const dlg = $("#chatDialog");
  if (!dlg.open) dlg.showModal();
}

/* ---------- Dialog helpers ---------- */
function openDialog(id, focusId) {
  const dlg = document.getElementById(id);
  if (!dlg.open) dlg.showModal();
  if (focusId) setTimeout(() => document.getElementById(focusId)?.focus(), 50);
}

/* ---------- AI Screener ---------- */
function openScreener() {
  $("#screenerResults").innerHTML = "";
  $("#screenerErr").textContent = "";
  openDialog("screenerDialog", "screenerInput");
}

async function runScreen(criteria) {
  const results = $("#screenerResults");
  const err = $("#screenerErr");
  err.textContent = "";
  results.innerHTML = `<div class="screen-empty"><span class="pulse-dots"><span></span><span></span><span></span></span> Scanning the market…</div>`;
  try {
    const r = await fetch("/api/screen", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ criteria }),
    });
    const data = await r.json();
    if (handleUsageLimit(data)) { results.innerHTML = ""; return; }
    if (!r.ok || data.error) throw new Error(data.error || "scan failed");
    renderScreenResults(data);
  } catch (e) {
    results.innerHTML = "";
    err.textContent = e.message;
  }
}

function renderScreenResults(data) {
  const wrap = $("#screenerResults");
  const list = data.results || [];
  if (!list.length) {
    wrap.innerHTML = `<div class="screen-empty">No matches — try rephrasing your scan.</div>`;
    return;
  }
  wrap.innerHTML = list.map((m) => {
    const up = (m.pct ?? 0) >= 0;
    return `<div class="screen-row" data-symbol="${m.symbol}">
      <div class="screen-sym">${m.symbol}</div>
      <div class="screen-mid">
        <div class="screen-name">${escapeHtml(m.name || "")}</div>
        <div class="screen-reason">${escapeHtml(m.reason || "")}</div>
      </div>
      <div class="screen-right">
        <div class="screen-price">$${fmt.price(m.price)}</div>
        <div class="screen-pct ${up ? "up" : "down"}">${fmt.pct(m.pct)}</div>
      </div>
    </div>`;
  }).join("");
  $$("#screenerResults .screen-row").forEach((el) => {
    el.addEventListener("click", () => {
      selectStock(el.dataset.symbol);
      $("#screenerDialog").close();
    });
  });
}

/* ---------- Learn (AI tutor) ---------- */
function openLearn() {
  $("#learnAnswer").innerHTML = "";
  $("#learnErr").textContent = "";
  openDialog("learnDialog", "learnInput");
}

async function askLearn(question) {
  const ans = $("#learnAnswer");
  const err = $("#learnErr");
  err.textContent = "";
  ans.innerHTML = `<div class="answer-box"><span class="pulse-dots"><span></span><span></span><span></span></span></div>`;
  try {
    const r = await fetch("/api/learn", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await r.json();
    if (handleUsageLimit(data)) { ans.innerHTML = ""; return; }
    if (!r.ok || data.error) throw new Error(data.error || "failed");
    ans.innerHTML = `<div class="answer-box"></div>`;
    ans.querySelector(".answer-box").textContent = data.text || "";
  } catch (e) {
    ans.innerHTML = "";
    err.textContent = e.message;
  }
}

/* ---------- Add-position mode (shares / dollars) ---------- */
function setPosMode(mode) {
  positionMode = mode;
  $$("#posModeSeg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("#sharesMode").hidden = mode !== "shares";
  $("#dollarMode").hidden = mode !== "dollars";
}

function openPositionDialog() {
  setPosMode("shares");
  $("#posErr").textContent = "";
  openDialog("positionDialog", "posSymbol");
}

/* ---------- Order ticket (broker handoff — FAAM never places trades) ---------- */
function setOrderSide(side) {
  orderSide = side;
  $$("#orderSideSeg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.side === side));
  updateReviewLabel();
  scheduleEstimate();
}

function updateReviewLabel() {
  const b = BROKERS[$("#brokerSelect").value] || BROKERS.other;
  const verb = orderSide === "sell" ? "Sell at" : "Buy at";
  $("#orderReview").textContent = `${verb} ${b.name} ▸`;
}

function setOrderMode(mode) {
  orderMode = mode;
  $$("#orderModeSeg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("#orderQtyLabel").textContent = mode === "dollars" ? "Amount ($)" : "Shares";
  $("#orderQty").placeholder = mode === "dollars" ? "500" : "1";
  scheduleEstimate();
}

function scheduleEstimate() {
  clearTimeout(orderEstTimer);
  orderEstTimer = setTimeout(updateEstimate, 350);
}

async function updateEstimate() {
  const sym = $("#orderSymbol").value.trim();
  const qty = parseFloat($("#orderQty").value);
  const est = $("#orderEstimate");
  $("#orderErr").textContent = "";
  if (!sym || !(qty > 0)) { est.textContent = "—"; orderTicket = null; return; }
  est.textContent = "Estimating…";
  try {
    const r = await fetch("/api/order/prepare", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ symbol: sym, side: orderSide, mode: orderMode, qty }),
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || "could not estimate");
    orderTicket = d.ticket;
    const t = d.ticket;
    est.innerHTML = `<strong>${t.side.toUpperCase()} ~${fmt.shares(t.shares)} ${t.symbol}</strong> · est. ${fmt.money(t.estCost)} <span class="muted">at $${fmt.price(t.price)}/sh</span>`;
  } catch (e) {
    est.textContent = "";
    orderTicket = null;
    $("#orderErr").textContent = e.message;
  }
}

// AI fill: turn a plain-English description into the order fields (and jump the
// dashboard to that stock). FAAM still never places the trade — you review it.
async function orderAiFill() {
  const text = $("#orderAiText").value.trim();
  if (!text) { $("#orderAiText").focus(); return; }
  if (!(await requestAiControl())) { $("#orderErr").textContent = "Allow assistant control to auto-fill this ticket."; return; }
  const btn = $("#orderAiFill");
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "…";
  $("#orderErr").textContent = "";
  try {
    const r = await fetch("/api/order/parse", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const d = await r.json();
    if (!r.ok || d.error) { $("#orderErr").textContent = d.error || "Couldn't read that."; return; }
    setOrderSide(d.side === "sell" ? "sell" : "buy");
    setOrderMode(d.mode === "dollars" ? "dollars" : "shares");
    $("#orderSymbol").value = d.symbol || "";
    if (d.qty != null) $("#orderQty").value = d.qty;
    if (d.symbol && d.valid && d.symbol !== state.active) selectStock(d.symbol); // drive the dashboard
    updateEstimate();
    const qtyStr = d.qty != null ? (d.mode === "dollars" ? "$" + d.qty : d.qty + " sh") : "";
    toast(`Filled: ${d.side} ${qtyStr} ${d.name || d.symbol}`.replace(/\s+/g, " ").trim());
  } catch (e) {
    $("#orderErr").textContent = e.message;
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}

async function openOrderTicket() {
  orderTicket = null;
  setOrderSide("buy");
  setOrderMode("shares");
  $("#orderAiText").value = "";
  $("#orderSymbol").value = state.active || "";
  $("#orderQty").value = "1";
  $("#orderErr").textContent = "";
  $("#orderEstimate").textContent = "—";
  try {
    const r = await fetch("/api/broker");
    const d = await r.json();
    if (d.broker && BROKERS[d.broker]) $("#brokerSelect").value = d.broker;
  } catch (e) { /* no saved broker yet */ }
  updateReviewLabel();
  openDialog("orderDialog", "orderSymbol");
  updateEstimate();
}

/* ---------- FAAM Pro (Stripe, tiered) ---------- */
const pro = { tier: 0, plan: "", planName: "", configured: false, plans: [], features: {} };

const FEATURE_LABELS = {
  voice: "Speaking mode", screener: "the AI screener",
  forecast: "Forecasts & advanced charts", recap: "the Daily Recap video",
  learn: "Learning features", adviser: "a custom adviser",
  predictions: "Prediction markets",
};

async function loadPro() {
  try {
    const r = await fetch("/api/pro");
    const d = await r.json();
    pro.tier = d.tier || 0;
    pro.plan = d.plan || "";
    pro.planName = d.planName || "";
    pro.configured = !!d.configured;
    pro.plans = d.plans || [];
    pro.features = d.features || {};
    pro.beta = !!d.beta;           // beta: everything free & unlocked
  } catch (e) { /* leave defaults */ }
  // If the saved forecast model needs a higher tier than the user has, fall back.
  const cm = FORECAST_MODELS.find((m) => m.id === state.forecastModel);
  if (cm && !modelAvailable(cm)) state.forecastModel = "apollo";
  renderProUI();
  applyBootTier();   // gate the intro/ad: paid can skip, Lite/free must watch
}

function planNameForTier(t) {
  const p = (pro.plans || []).find((x) => x.tier === t);
  return p ? p.name : "a higher plan";
}

function gate(feature) {
  const need = (pro.features && pro.features[feature]) || 0;
  if (pro.tier >= need) return true;
  toast(`${FEATURE_LABELS[feature] || "This"} is on ${planNameForTier(need)} & up.`);
  openProDialog();
  return false;
}

// When an AI call returns a usage-limit error, nudge to upgrade (no caps shown).
function handleUsageLimit(data) {
  if (data && data.upgrade) {
    toast(data.message || "You've reached your plan's usage limit. Upgrade for more.");
    openProDialog();
    return true;
  }
  return false;
}

function renderProUI() {
  const pill = $("#proBtn");
  if (pill) {
    if (pro.beta) {
      pill.textContent = "BETA · FREE";
      pill.classList.add("is-pro", "is-beta");
      pill.title = "FAAM is free while it's in beta — everything's unlocked";
    } else {
      pill.textContent = pro.tier > 0 ? (pro.planName || "Pro").toUpperCase() : "Upgrade";
      pill.classList.toggle("is-pro", pro.tier > 0);
      pill.title = pro.tier > 0 ? `You're on FAAM ${pro.planName}` : "Upgrade to FAAM Pro";
    }
  }
  const lockBtn = (sel, feature) => {
    const el = $(sel);
    if (el) el.classList.toggle("locked", pro.tier < ((pro.features && pro.features[feature]) || 0));
  };
  lockBtn("#screenerBtn", "screener");
  lockBtn("#learnBtn", "learn");
  lockBtn("#recapBtn", "recap");
  lockBtn("#adviserBtn", "adviser");

  // Advanced chart views (Candles / % Return / Forecast) are Pro+.
  const needForecast = (pro.features && pro.features.forecast) || 0;
  const lockViews = pro.tier < needForecast;
  $$("#viewTabs button[data-pro]").forEach((b) => b.classList.toggle("locked", lockViews));
  const hint = $("#viewProHint");
  if (hint) hint.hidden = !lockViews;
}

function renderPlanCards() {
  const grid = $("#proPlans");
  if (!grid) return;
  grid.innerHTML = (pro.plans || []).map((p) => {
    const current = pro.plan === p.id;
    const dollars = "$" + (p.price / 100).toFixed(p.price % 100 ? 2 : 0);
    const perks = (p.perks || []).map((x) => `<li>${escapeHtml(x)}</li>`).join("");
    return `<div class="plan-card ${p.popular ? "popular" : ""} ${current ? "current" : ""}">
      ${p.popular ? '<span class="plan-tag">Popular</span>' : ""}
      <div class="plan-name">${escapeHtml(p.name)}</div>
      <div class="plan-price">${dollars}<span>/mo</span></div>
      <ul class="plan-perks">${perks}</ul>
      <button class="plan-choose" data-plan="${p.id}" ${current ? "disabled" : ""}>${current ? "Current plan" : "Choose " + escapeHtml(p.name)}</button>
    </div>`;
  }).join("");
  grid.querySelectorAll(".plan-choose").forEach((b) => {
    if (!b.disabled) b.addEventListener("click", () => choosePlan(b.dataset.plan));
  });
}

function openProDialog() {
  $("#proErr").textContent = "";
  // Beta: hide the paywall entirely and show a friendly "all free" message.
  if (pro.beta) {
    $("#proConnect").hidden = true;
    $("#proPlans").style.display = "none";
    const act = $("#proActive");
    act.hidden = false;
    act.innerHTML = "🎉 <strong>Everything's free while FAAM is in beta.</strong><br>" +
      "Every model, forecast, and tool is unlocked — no upgrade needed.";
    openDialog("proDialog");
    return;
  }
  $("#proConnect").hidden = pro.configured;
  $("#proPlans").style.display = pro.configured ? "grid" : "none";
  renderPlanCards();
  const act = $("#proActive");
  if (pro.tier > 0) { act.hidden = false; act.textContent = `✓ You're on FAAM ${pro.planName}. Pick another tier to switch.`; }
  else act.hidden = true;
  openDialog("proDialog", pro.configured ? null : "stripeKey");
}

async function connectStripe() {
  const err = $("#proErr");
  err.textContent = "";
  const key = $("#stripeKey").value.trim();
  if (!key.startsWith("sk_")) { err.textContent = "Enter a Stripe secret key (starts with sk_)."; return; }
  try {
    const r = await fetch("/api/stripe/key", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ key }),
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || "could not save key");
    pro.configured = true;
    $("#stripeKey").value = "";
    openProDialog();
  } catch (e) { err.textContent = e.message; }
}

async function choosePlan(plan) {
  const err = $("#proErr");
  err.textContent = "";
  try {
    const r = await fetch("/api/checkout", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ plan }),
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || "checkout failed");
    openExternal(d.url);
    toast("Finish in the Stripe tab, then come back — FAAM will unlock your plan.");
    $("#proDialog").close();
  } catch (e) { err.textContent = e.message; }
}


/* ---------- Account ---------- */
async function loadMe() {
  try {
    const r = await fetch("/api/me");
    const d = await r.json();
    state.me = d.auth ? d : null;
  } catch (e) { state.me = null; }
  renderAccount();
}

function renderAccount() {
  const m = state.me;
  const av = $("#accountBtn");
  if (av && m) av.title = `${m.username}${m.admin ? " · admin" : ""}`;
}

function openAccount() {
  const m = state.me || {};
  const name = m.username || "Guest";
  $("#acctName").textContent = name;
  $("#acctAvatar").textContent = name[0] || "U";
  const badge = $("#acctBadge");
  if (m.admin) { badge.textContent = "ADMIN"; badge.className = "acct-badge admin"; }
  else if (m.tier > 0) { badge.textContent = (m.plan || "pro").toUpperCase(); badge.className = "acct-badge pro"; }
  else { badge.textContent = "FREE"; badge.className = "acct-badge"; }
  $("#acctMeta").textContent = (m.email ? m.email + " · " : "") + (m.provider === "google" ? "Google account" : "Local account");
  openDialog("accountDialog");
}

async function logout() {
  try { await fetch("/api/logout", { method: "POST" }); } catch (e) {}
  location.href = "/login";
}

/* ---------- Daily recap video ---------- */
const recap = { data: null, slides: [], audio: null, audioTrack: null, actx: null, playing: false, _raf: 0 };

function pickVideoMime() {
  const c = ["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm"];
  for (const m of c) { if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m; }
  return "";
}

function buildSlides(d) {
  const slides = [{ type: "title", headline: d.headline || "FAAM Daily Recap", date: d.date }];
  slides.push({ type: "market", mood: d.market && d.market.mood, avg: d.market ? d.market.avgPct : 0, count: d.market ? d.market.count : 0 });
  (d.slides || []).forEach((s) => slides.push({ type: "mover", ...s }));
  slides.push({ type: "outro" });
  return slides;
}

async function openRecap() {
  const dlg = $("#recapDialog");
  recap.playing = false;
  cancelAnimationFrame(recap._raf);
  $("#recapMsg").hidden = false;
  $("#recapMsg").textContent = "Generating today's recap…";
  $("#recapPlay").disabled = true;
  $("#recapDownload").disabled = true;
  $("#recapStatus").textContent = "";
  if (!dlg.open) dlg.showModal();
  try {
    const r = await fetch("/api/recap");
    const data = await r.json();
    if (handleUsageLimit(data)) { $("#recapMsg").textContent = "Usage limit reached for your plan."; return; }
    if (!r.ok || data.error) throw new Error(data.error || "recap failed");
    recap.data = data;
    recap.slides = buildSlides(data);
    drawSlide(recap.slides[0], 1, 0);
    $("#recapMsg").hidden = true;
    $("#recapPlay").disabled = false;
    $("#recapStatus").textContent = "Preparing narration…";
    await prepareNarration(data.script);
    $("#recapStatus").textContent = recap.audio ? "Ready — ▶ Play" : (data.ai ? "Ready (silent)" : "Ready (data reel)");
    $("#recapDownload").disabled = false;
  } catch (e) {
    $("#recapMsg").hidden = false;
    $("#recapMsg").textContent = "Recap error: " + e.message;
  }
}

async function prepareNarration(script) {
  recap.audio = null; recap.audioTrack = null;
  if (!script) return;
  try {
    const r = await fetch("/api/speak", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ text: script }),
    });
    if (!r.ok) return;
    const buf = await r.arrayBuffer();
    const url = URL.createObjectURL(new Blob([buf], { type: "audio/mpeg" }));
    const audio = new Audio(url);
    audio.preload = "auto";
    await new Promise((res) => { audio.onloadedmetadata = res; audio.onerror = res; });
    recap.audio = audio;
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const actx = new Ctx();
      const src = actx.createMediaElementSource(audio);
      const dest = actx.createMediaStreamDestination();
      src.connect(dest); src.connect(actx.destination);
      recap.actx = actx;
      recap.audioTrack = dest.stream.getAudioTracks()[0];
    } catch (e) { /* recording audio unavailable; element playback still works */ }
  } catch (e) { recap.audio = null; }
}

function playReel(record) {
  if (recap.playing || !recap.slides.length) return;
  const canvas = $("#recapCanvas");
  const slides = recap.slides;
  const audio = recap.audio;
  const total = (audio && audio.duration && isFinite(audio.duration) && audio.duration > 1)
    ? audio.duration : slides.length * 5;
  const weights = slides.map((s) => s.type === "mover" ? 1.4 : (s.type === "title" ? 1.1 : 1));
  const sum = weights.reduce((a, b) => a + b, 0);
  const durs = weights.map((w) => total * w / sum);
  const starts = []; let acc = 0; for (const d of durs) { starts.push(acc); acc += d; }

  let rec = null; const chunks = [];
  if (record) {
    try {
      const vstream = canvas.captureStream(30);
      const tracks = [...vstream.getVideoTracks()];
      if (recap.audioTrack) tracks.push(recap.audioTrack);
      const mime = pickVideoMime();
      rec = new MediaRecorder(new MediaStream(tracks), mime ? { mimeType: mime } : undefined);
      rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
      rec.onstop = () => {
        const blob = new Blob(chunks, { type: "video/webm" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `FAAM-recap-${((recap.data && recap.data.date) || "today").replace(/[ ,]/g, "-")}.webm`;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 5000);
        $("#recapStatus").textContent = "Saved video ✓";
      };
      rec.start();
    } catch (e) { rec = null; toast("Video capture not supported here."); }
  }

  recap.playing = true;
  $("#recapMsg").hidden = true;
  if (audio) {
    try {
      if (recap.actx && recap.actx.state === "suspended") recap.actx.resume();
      audio.currentTime = 0; audio.play().catch(() => {});
    } catch (e) {}
  }
  $("#recapStatus").textContent = record ? "Recording…" : "Playing…";
  const t0 = performance.now();
  function frame() {
    const el = (performance.now() - t0) / 1000;
    let i = 0; while (i < starts.length - 1 && el >= starts[i + 1]) i++;
    const p = Math.min(1, (el - starts[i]) / Math.max(0.01, durs[i]));
    drawSlide(slides[i], p, i);
    if (el < total && recap.playing) recap._raf = requestAnimationFrame(frame);
    else stopReel(rec);
  }
  recap._raf = requestAnimationFrame(frame);
}

function stopReel(rec) {
  recap.playing = false;
  cancelAnimationFrame(recap._raf);
  if (recap.audio) { try { recap.audio.pause(); } catch (e) {} }
  if (rec && rec.state !== "inactive") { try { rec.stop(); } catch (e) {} }
  else if (!rec) $("#recapStatus").textContent = "Done — ▶ to replay";
}

function closeRecap() {
  stopReel(null);
  const dlg = $("#recapDialog");
  if (dlg.open) dlg.close();
}

/* canvas drawing */
function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function drawSparkCanvas(ctx, vals, x, y, w, h, up) {
  if (!vals || vals.length < 2) return;
  const min = Math.min(...vals), max = Math.max(...vals), rng = max - min || 1;
  const step = w / (vals.length - 1);
  ctx.lineWidth = 3; ctx.strokeStyle = up ? "#2BD787" : "#FF6B83"; ctx.lineJoin = "round";
  ctx.beginPath();
  vals.forEach((v, i) => {
    const px = x + i * step, py = y + h - ((v - min) / rng) * h;
    i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
  });
  ctx.stroke();
}

function wrapText(ctx, text, x, y, maxW, lh) {
  const words = (text || "").split(" ");
  let line = ""; const lines = [];
  for (const w of words) {
    const test = line ? line + " " + w : w;
    if (ctx.measureText(test).width > maxW && line) { lines.push(line); line = w; } else line = test;
  }
  if (line) lines.push(line);
  const startY = y - (lines.length - 1) * lh / 2;
  lines.forEach((ln, i) => ctx.fillText(ln, x, startY + i * lh));
}

function drawSlide(slide, p) {
  const canvas = $("#recapCanvas");
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  const g = ctx.createLinearGradient(0, 0, W, H);
  g.addColorStop(0, "#0a0f1a"); g.addColorStop(1, "#0e1428");
  ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
  const rg = ctx.createRadialGradient(W * 0.82, 0, 0, W * 0.82, 0, W * 0.7);
  rg.addColorStop(0, "rgba(79,140,255,0.16)"); rg.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = rg; ctx.fillRect(0, 0, W, H);

  ctx.globalAlpha = Math.min(1, p * 3);
  ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
  ctx.fillStyle = "#9bbcff"; ctx.font = "800 26px Inter, system-ui, sans-serif";
  ctx.fillText("FAAM", 56, 72);
  ctx.fillStyle = "#5e6b82"; ctx.font = "500 18px Inter, system-ui, sans-serif";
  ctx.fillText("Daily Recap", 134, 72);

  if (slide.type === "title") {
    ctx.textAlign = "center";
    ctx.fillStyle = "#fff"; ctx.font = "800 66px Inter, system-ui, sans-serif";
    wrapText(ctx, slide.headline, W / 2, H / 2 - 10, W - 220, 72);
    ctx.fillStyle = "#8a97ad"; ctx.font = "500 28px Inter, system-ui, sans-serif";
    ctx.fillText(slide.date || "", W / 2, H / 2 + 80);
  } else if (slide.type === "market") {
    const up = slide.mood === "up";
    ctx.textAlign = "center";
    ctx.fillStyle = "#8a97ad"; ctx.font = "700 24px Inter, system-ui, sans-serif";
    ctx.fillText("MARKET TODAY", W / 2, H / 2 - 96);
    ctx.fillStyle = up ? "#2BD787" : "#FF6B83"; ctx.font = "800 96px Inter, system-ui, sans-serif";
    ctx.fillText(`${slide.avg >= 0 ? "+" : ""}${(slide.avg || 0).toFixed(2)}%`, W / 2, H / 2 + 14);
    ctx.fillStyle = "#cfd6e3"; ctx.font = "500 26px Inter, system-ui, sans-serif";
    ctx.fillText(`Average of ${slide.count || 0} watchlist names · ${up ? "broadly higher" : "broadly lower"}`, W / 2, H / 2 + 84);
  } else if (slide.type === "mover") {
    const up = (slide.pct || 0) >= 0;
    ctx.fillStyle = "rgba(255,255,255,0.04)"; roundRect(ctx, 56, 150, W - 112, H - 250, 24); ctx.fill();
    ctx.strokeStyle = "rgba(255,255,255,0.08)"; ctx.lineWidth = 1.5; roundRect(ctx, 56, 150, W - 112, H - 250, 24); ctx.stroke();
    ctx.textAlign = "left";
    ctx.fillStyle = "#fff"; ctx.font = "800 74px Inter, system-ui, sans-serif";
    ctx.fillText(slide.symbol, 100, 272);
    ctx.fillStyle = "#8a97ad"; ctx.font = "500 26px Inter, system-ui, sans-serif";
    ctx.fillText(slide.name || "", 100, 314);
    ctx.textAlign = "right";
    ctx.fillStyle = "#fff"; ctx.font = "800 62px Inter, system-ui, sans-serif";
    ctx.fillText("$" + fmt.price(slide.price), W - 100, 272);
    ctx.fillStyle = up ? "#2BD787" : "#FF6B83"; ctx.font = "700 34px Inter, system-ui, sans-serif";
    ctx.fillText(`${up ? "▲" : "▼"} ${fmt.pct(slide.pct)}`, W - 100, 320);
    ctx.textAlign = "left";
    drawSparkCanvas(ctx, slide.spark, 100, 360, W - 200, 120, up);
    if (slide.comment) {
      ctx.fillStyle = "#cfd6e3"; ctx.font = "500 26px Inter, system-ui, sans-serif";
      wrapText(ctx, slide.comment, 100, H - 130, W - 200, 36);
    }
  } else if (slide.type === "outro") {
    ctx.textAlign = "center";
    ctx.fillStyle = "#fff"; ctx.font = "800 56px Inter, system-ui, sans-serif";
    ctx.fillText("That's your market recap.", W / 2, H / 2 - 6);
    ctx.fillStyle = "#5e6b82"; ctx.font = "500 24px Inter, system-ui, sans-serif";
    ctx.fillText("Information, not financial advice.", W / 2, H / 2 + 52);
    ctx.fillStyle = "#9bbcff"; ctx.font = "800 26px Inter, system-ui, sans-serif";
    ctx.fillText("FAAM", W / 2, H - 70);
  }
  ctx.globalAlpha = 1;
}

/* ---------- Voice mode (speak to FAAM) ---------- */
const voice = { rec: null, chunks: [], stream: null, vstate: "idle", audio: null };

function voiceSupported() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
}

function setVoiceState(s, status) {
  voice.vstate = s;
  const dlg = $("#voiceDialog");
  dlg.classList.remove("listening", "thinking", "speaking");
  if (s === "listening" || s === "thinking" || s === "speaking") dlg.classList.add(s);
  const map = {
    idle: "Tap the mic and start talking",
    listening: "Listening… tap to stop",
    transcribing: "Transcribing…",
    thinking: "FAAM is thinking…",
    speaking: "Speaking…",
  };
  $("#voiceStatus").textContent = status || map[s] || "";
  const vb = $("#voiceBtn");
  if (vb) vb.classList.toggle("recording", s === "listening");
}

function openVoice() {
  if (!voiceSupported()) { toast("Voice needs mic access in a supported browser (Chrome/Safari)."); return; }
  $("#voiceYou").textContent = "";
  $("#voiceAI").textContent = "";
  setVoiceState("idle");
  const dlg = $("#voiceDialog");
  if (!dlg.open) dlg.showModal();
  startListening();
}

function pickMime() {
  const cands = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"];
  for (const c of cands) { if (window.MediaRecorder && MediaRecorder.isTypeSupported(c)) return c; }
  return "";
}
function extFor(mime) {
  if (!mime) return "webm";
  if (mime.includes("webm")) return "webm";
  if (mime.includes("mp4")) return "mp4";
  if (mime.includes("ogg")) return "ogg";
  if (mime.includes("mpeg")) return "mp3";
  return "webm";
}

async function startListening() {
  if (voice.vstate === "transcribing" || voice.vstate === "thinking") return;
  try {
    voice.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    setVoiceState("idle", "Mic blocked — allow access, then tap to retry.");
    return;
  }
  voice.chunks = [];
  const mime = pickMime();
  voice.rec = new MediaRecorder(voice.stream, mime ? { mimeType: mime } : undefined);
  voice.rec.ondataavailable = (e) => { if (e.data && e.data.size) voice.chunks.push(e.data); };
  voice.rec.onstop = onListenStop;
  voice.rec.start();
  setVoiceState("listening");
}

function stopListening() {
  if (voice.rec && voice.rec.state !== "inactive") voice.rec.stop();
}

function releaseMic() {
  if (voice.stream) { voice.stream.getTracks().forEach((t) => t.stop()); voice.stream = null; }
}

async function onListenStop() {
  const mime = (voice.rec && voice.rec.mimeType) || pickMime() || "audio/webm";
  releaseMic();
  const blob = new Blob(voice.chunks, { type: mime });
  if (!blob.size) { setVoiceState("idle"); return; }
  setVoiceState("transcribing");
  try {
    const ext = extFor(mime);
    const r = await fetch("/api/transcribe", {
      method: "POST",
      headers: { "Content-Type": mime, "X-Audio-Ext": ext },
      body: blob,
    });
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    const text = (data.text || "").trim();
    if (!text) { setVoiceState("idle", "Didn't catch that — tap to retry."); return; }
    $("#voiceYou").textContent = text;
    setVoiceState("thinking");
    const reply = await voiceChat(text);
    $("#voiceAI").textContent = reply;
    setVoiceState("speaking");
    await speak(reply);
    setVoiceState("idle", "Tap the mic to talk again");
  } catch (e) {
    setVoiceState("idle", "Voice error — tap to retry");
    toast("Voice: " + e.message);
  }
}

async function voiceChat(text) {
  state.chatHistory.push({ role: "user", content: text });
  appendMsg("user", text);
  const r = await fetch("/api/chat", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ symbol: state.active, mode: "voice", messages: state.chatHistory }),
  });
  const data = await r.json();
  if (data.error) throw new Error(data.error + (data.detail ? " — " + data.detail : ""));
  state.chatHistory.push({ role: "assistant", content: data.text });
  appendMsg("ai", data.text);
  return data.text;
}

async function speak(text) {
  try {
    const r = await fetch("/api/speak", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.error || ("TTS " + r.status)); }
    const buf = await r.arrayBuffer();
    const url = URL.createObjectURL(new Blob([buf], { type: "audio/mpeg" }));
    stopSpeaking();
    voice.audio = new Audio(url);
    await voice.audio.play().catch(() => {});
    await new Promise((res) => { voice.audio.onended = res; voice.audio.onerror = res; });
    URL.revokeObjectURL(url);
  } catch (e) {
    // TTS is optional — the reply text is already on screen.
    toast("Voice playback: " + e.message);
  }
}

function stopSpeaking() {
  if (voice.audio) { try { voice.audio.pause(); } catch (e) {} voice.audio = null; }
}

function closeVoice() {
  stopListening();
  releaseMic();
  stopSpeaking();
  setVoiceState("idle");
  const dlg = $("#voiceDialog");
  if (dlg.open) dlg.close();
}

function toggleListen() {
  if (voice.vstate === "listening") stopListening();
  else if (voice.vstate === "speaking") { stopSpeaking(); setVoiceState("idle", "Tap the mic to talk again"); }
  else if (voice.vstate === "idle") startListening();
}

/* ---------- UI wiring ---------- */
/* ---------- Personalized FAAM (Beta) — dev only ---------- */
let persState = { available: false, enabled: false, questions: [], profile: {} };
let persTimer = null;
let persSeen = new Set();

async function loadPersonalize() {
  try {
    const r = await fetch("/api/personalize");
    if (!r.ok) { persState.available = false; return; }
    persState = await r.json();
  } catch { persState.available = false; return; }
  const row = $("#personalizeRow");
  if (row) row.hidden = !persState.available;
  $("#personalizeToggle")?.setAttribute("aria-checked", persState.enabled ? "true" : "false");
  updateForYouBtn();
  if (persState.enabled) startPersFeed();
}
function openConsent() {
  $("#consentCheck").checked = false;
  $("#consentAgree").disabled = true;
  openDialog("consentDialog");
}
async function setPersonalize(agree) {
  try {
    await fetch("/api/personalize/consent", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ agree }) });
  } catch { /* ignore */ }
  persState.enabled = agree;
  $("#personalizeToggle")?.setAttribute("aria-checked", agree ? "true" : "false");
  updateForYouBtn();
  if (agree) { openPersOnboarding(); startPersFeed(); toast("Personalized FAAM is on"); }
  else { stopPersFeed(); toast("Personalization turned off"); }
}
function openPersOnboarding() {
  const wrap = $("#persQuestions");
  wrap.innerHTML = (persState.questions || []).map((q) =>
    `<div class="pers-q"><label>${escapeHtml(q.q)}</label><input class="pers-a" data-id="${escapeHtml(q.id)}" value="${escapeHtml((persState.profile || {})[q.id] || "")}" placeholder="Type your answer…"></div>`
  ).join("");
  openDialog("onboardPersDialog", ".pers-a");
}
async function savePersAnswers() {
  const answers = [...document.querySelectorAll(".pers-a")].map((i) => ({ id: i.dataset.id, answer: i.value.trim() })).filter((a) => a.answer);
  let added = [];
  try {
    const d = await (await fetch("/api/personalize/answers", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ answers }) })).json();
    persState.profile = d.profile || {};
    added = d.added || [];
  } catch { /* ignore */ }
  $("#onboardPersDialog").close();
  if (added.length) { toast("Added " + added.join(", ") + " to your watchlist"); loadWatchlist(); }
  else toast("Got it — FAAM will tailor things to you");
  refreshPersFeed();
}
function reportActivity(event, symbol) {
  if (!persState.enabled) return;
  fetch("/api/personalize/activity", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ event, symbol: symbol || "" }) }).catch(() => {});
}
function startPersFeed() { if (persTimer) return; refreshPersFeed(); persTimer = setInterval(refreshPersFeed, 45000); }
function stopPersFeed() { if (persTimer) { clearInterval(persTimer); persTimer = null; } const p = $("#persPopups"); if (p) p.innerHTML = ""; }
async function refreshPersFeed() {
  if (document.hidden) return;
  let d;
  try { d = await (await fetch("/api/personalize/feed")).json(); } catch { return; }
  if (!d || !d.enabled) return;
  (d.cards || []).forEach((c) => {
    if (persSeen.has(c.key)) return;
    persSeen.add(c.key);
    if (persSeen.size > 300) persSeen = new Set([...persSeen].slice(-150));
    showPersPopup(c);
  });
}
function showPersPopup(c) {
  const el = document.createElement("div");
  el.className = "pers-pop" + (c.live ? " live" : "");
  el.innerHTML =
    `<div class="pers-pop-ic">${iconSvg(c.icon)}</div>` +
    `<div class="pers-pop-main"><div class="pers-pop-kind">${escapeHtml(c.kind || "For you")}${c.live ? ' <span class="pers-live">● LIVE</span>' : ""}</div>` +
    `<div class="pers-pop-title">${escapeHtml(c.title || "")}</div>` +
    (c.detail ? `<div class="pers-pop-detail">${escapeHtml(c.detail)}</div>` : "") + `</div>` +
    `<button class="pers-pop-x" aria-label="Dismiss">×</button>`;
  $("#persPopups").prepend(el);
  el.querySelector(".pers-pop-x").addEventListener("click", (e) => { e.stopPropagation(); el.remove(); });
  if (c.symbol) {
    el.classList.add("clickable");
    el.addEventListener("click", () => selectStock(c.symbol));
  } else if (c.tickers && c.tickers.length) {
    el.classList.add("clickable");
    el.addEventListener("click", async () => {
      for (const t of c.tickers) { try { await addTicker(t); } catch (e) {} }
      toast("Added " + c.tickers.join(", ") + " to your watchlist");
      el.remove();
    });
  } else if (c.link) {
    el.classList.add("clickable");
    el.addEventListener("click", () => window.open(c.link, "_blank", "noopener"));
  }
  requestAnimationFrame(() => el.classList.add("show"));
  if (!c.live) setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 400); }, 13000);
}

/* ---------- For You — persistent feed + price alerts ---------- */
function updateForYouBtn() {
  const b = $("#forYouBtn");
  if (b) b.hidden = !(persState.available && persState.enabled);
}
async function openForYou() { openDialog("forYouDialog"); refreshForYou(); }
async function refreshForYou() {
  let d;
  try { d = await (await fetch("/api/personalize/feed")).json(); } catch { return; }
  renderForYou(d.cards || [], d.alerts || []);
}
function renderForYou(cards, alerts) {
  const wrap = $("#forYouCards");
  if (!cards.length) {
    wrap.innerHTML = '<p class="muted small" style="text-align:center;padding:14px 0">Nothing here yet — answer a few questions and use FAAM, and your feed fills in.</p>';
  } else {
    wrap.innerHTML = "";
    cards.forEach((c) => {
      const el = document.createElement("div");
      el.className = "foryou-card" + (c.live ? " live" : "");
      el.innerHTML =
        `<div class="pers-pop-ic">${iconSvg(c.icon)}</div>` +
        `<div class="pers-pop-main"><div class="pers-pop-kind">${escapeHtml(c.kind || "For you")}${c.live ? ' <span class="pers-live">● LIVE</span>' : ""}</div>` +
        `<div class="pers-pop-title">${escapeHtml(c.title || "")}</div>` +
        (c.detail ? `<div class="pers-pop-detail">${escapeHtml(c.detail)}</div>` : "") + `</div>`;
      if (c.symbol) { el.classList.add("clickable"); el.addEventListener("click", () => { selectStock(c.symbol); $("#forYouDialog").close(); }); }
      else if (c.tickers) { el.classList.add("clickable"); el.addEventListener("click", async () => { for (const t of c.tickers) { try { await addTicker(t); } catch (e) {} } toast("Added " + c.tickers.join(", ")); refreshForYou(); }); }
      else if (c.link) { el.classList.add("clickable"); el.addEventListener("click", () => window.open(c.link, "_blank", "noopener")); }
      wrap.appendChild(el);
    });
  }
  const al = $("#alertList");
  al.innerHTML = alerts.length
    ? alerts.map((a) => `<div class="alert-row"><span><span class="alert-bell">${iconSvg('bell')}</span> <b>${escapeHtml(a.symbol)}</b> ${escapeHtml(a.dir)} $${Number(a.price).toFixed(2)}</span><button class="alert-x" data-id="${escapeHtml(a.id)}" aria-label="Remove">×</button></div>`).join("")
    : '<p class="muted small" style="margin:4px 0">No alerts yet — add one below.</p>';
  al.querySelectorAll(".alert-x").forEach((b) => b.addEventListener("click", () => removeAlert(b.dataset.id)));
}
async function addAlert() {
  const sym = ($("#alertSym").value || "").trim().toUpperCase();
  const dir = $("#alertDir").value;
  const price = parseFloat($("#alertPrice").value);
  if (!sym || !(price > 0)) { toast("Enter a ticker and a price"); return; }
  try {
    const d = await (await fetch("/api/personalize/alert", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ symbol: sym, dir, price }) })).json();
    if (d.error) { toast(d.error); return; }
  } catch { return; }
  $("#alertSym").value = ""; $("#alertPrice").value = "";
  toast(`Alert set — ${sym} ${dir} $${price}`);
  refreshForYou();
}
async function removeAlert(id) {
  try { await fetch("/api/personalize/alert/remove", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ id }) }); } catch { /* ignore */ }
  refreshForYou();
}

/* ---------- Beginner course ---------- */
let courseIdx = 0, courseData = [];
async function openCourse() {
  if (!courseData.length) {
    try { courseData = (await (await fetch("/api/course")).json()).lessons || []; } catch { /* ignore */ }
  }
  courseIdx = Math.min(parseInt(localStorage.getItem("faam_course_idx") || "0", 10) || 0, Math.max(0, courseData.length - 1));
  renderCourse();
  openDialog("courseDialog");
}
function renderCourse() {
  const l = courseData[courseIdx]; if (!l) return;
  $("#courseTitle").textContent = l.t;
  $("#courseText").textContent = l.b;
  $("#courseCount").textContent = `Lesson ${courseIdx + 1} of ${courseData.length}`;
  $("#courseFill").style.width = ((courseIdx + 1) / courseData.length * 100) + "%";
  $("#coursePrev").disabled = courseIdx === 0;
  $("#courseNext").textContent = courseIdx === courseData.length - 1 ? "Finish ✓" : "Next →";
  try { localStorage.setItem("faam_course_idx", String(courseIdx)); } catch (e) {}
}

function wire() {
  $$("#rangeTabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$("#rangeTabs button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.range = btn.dataset.range;
      state.interval = btn.dataset.interval;
      loadChart();
    });
  });

  $("#refreshAI").addEventListener("click", loadAIInsight);

  // Chat
  $("#chatForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("#chatInput");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    sendChat(text);
  });
  $("#dialogForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("#dialogInput");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    sendChat(text, { openDialog: false });
  });
  $("#closeChat").addEventListener("click", () => $("#chatDialog").close());

  // Add position dialog (portfolio tracker)
  $("#addPositionBtn").addEventListener("click", openPositionDialog);
  $("#closePosition").addEventListener("click", () => $("#positionDialog").close());

  // Invest → order-ticket handoff (FAAM prepares; you place it at your broker)
  $("#investBtn").addEventListener("click", openOrderTicket);
  $("#orderAiFill").addEventListener("click", orderAiFill);
  $("#orderAiText").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); orderAiFill(); }
  });
  $("#headerBuyBtn").addEventListener("click", openOrderTicket);
  $("#closeOrder").addEventListener("click", () => $("#orderDialog").close());
  $$("#orderSideSeg .seg-btn").forEach((b) => b.addEventListener("click", () => setOrderSide(b.dataset.side)));
  $$("#orderModeSeg .seg-btn").forEach((b) => b.addEventListener("click", () => setOrderMode(b.dataset.mode)));
  $("#orderSymbol").addEventListener("input", scheduleEstimate);
  $("#orderQty").addEventListener("input", scheduleEstimate);
  $("#brokerSelect").addEventListener("change", updateReviewLabel);
  $$("#amountChips .chip").forEach((c) => c.addEventListener("click", () => {
    setOrderMode("dollars");
    $("#orderQty").value = c.dataset.amt;
    updateEstimate();
  }));
  $("#orderTrack").addEventListener("click", async () => {
    if (!orderTicket) { $("#orderErr").textContent = "Set a ticker and quantity first."; return; }
    try {
      await addPositionReq({ symbol: orderTicket.symbol, shares: orderTicket.shares, cost: orderTicket.price });
      toast(`Tracking ~${fmt.shares(orderTicket.shares)} ${orderTicket.symbol} in your portfolio.`);
    } catch (ex) { $("#orderErr").textContent = ex.message; }
  });
  $("#orderForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("#orderErr").textContent = "";
    const sym = $("#orderSymbol").value.trim().toUpperCase();
    if (!sym) { $("#orderErr").textContent = "Enter a ticker."; return; }
    if (!orderTicket) await updateEstimate();
    if (!orderTicket) { $("#orderErr").textContent = "Enter a valid quantity."; return; }
    const brokerKey = $("#brokerSelect").value;
    fetch("/api/broker", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ broker: brokerKey }),
    }).catch(() => {});
    const b = BROKERS[brokerKey] || BROKERS.other;
    const isCrypto = (orderTicket.quoteType || "").toUpperCase().includes("CRYPTO");
    openExternal(b.url(sym, isCrypto));
    toast(`Opening ${b.name} — review and place the ${orderSide} yourself. FAAM doesn't submit trades.`);
  });
  $$("#posModeSeg .seg-btn").forEach((b) => b.addEventListener("click", () => setPosMode(b.dataset.mode)));
  $("#positionForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const sym = $("#posSymbol").value.trim();
    const err = $("#posErr");
    err.textContent = "";
    if (!sym) { err.textContent = "Enter a ticker."; return; }
    let body;
    if (positionMode === "dollars") {
      const amount = parseFloat($("#posAmount").value);
      if (!(amount > 0)) { err.textContent = "Enter a positive dollar amount."; return; }
      body = { symbol: sym, amount };
    } else {
      const shares = parseFloat($("#posShares").value);
      const cost = parseFloat($("#posCost").value);
      if (!(shares > 0) || !(cost >= 0)) { err.textContent = "Enter positive shares and a valid cost."; return; }
      body = { symbol: sym, shares, cost };
    }
    try {
      const data = await addPositionReq(body);
      toast(positionMode === "dollars"
        ? `Added ~${fmt.shares(data.shares)} ${sym.toUpperCase()} at $${fmt.price(data.price)}.`
        : `Added ${body.shares} ${sym.toUpperCase()} @ $${body.cost}.`);
      $("#positionForm").reset();
      setPosMode("shares");
      $("#positionDialog").close();
    } catch (ex) {
      err.textContent = ex.message;
    }
  });

  // AI Screener (Pro+)
  $("#screenerBtn").addEventListener("click", () => { if (gate("screener")) openScreener(); });
  $("#closeScreener").addEventListener("click", () => $("#screenerDialog").close());
  $("#screenerForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#screenerInput").value.trim();
    if (!q) { $("#screenerErr").textContent = "Describe what to screen for."; return; }
    runScreen(q);
  });
  $$("#screenerChips .chip").forEach((c) => c.addEventListener("click", () => {
    $("#screenerInput").value = c.dataset.q;
    runScreen(c.dataset.q);
  }));

  // Learn (Max+)
  $("#learnBtn").addEventListener("click", () => { if (gate("learn")) openLearn(); });
  $("#closeLearn").addEventListener("click", () => $("#learnDialog").close());
  $("#learnForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#learnInput").value.trim();
    if (!q) { $("#learnErr").textContent = "Ask a question."; return; }
    askLearn(q);
  });
  $$("#learnChips .chip").forEach((c) => c.addEventListener("click", () => {
    $("#learnInput").value = c.dataset.q;
    askLearn(c.dataset.q);
  }));

  // FAAM Pro
  $("#proBtn").addEventListener("click", openProDialog);
  $("#closePro").addEventListener("click", () => $("#proDialog").close());
  $("#stripeConnectBtn").addEventListener("click", connectStripe);

  // Daily recap video (Max+)
  $("#recapBtn").addEventListener("click", () => { if (gate("recap")) openRecap(); });
  $("#recapClose").addEventListener("click", closeRecap);
  $("#recapDialog").addEventListener("cancel", (e) => { e.preventDefault(); closeRecap(); });
  $("#recapPlay").addEventListener("click", () => playReel(false));
  $("#recapDownload").addEventListener("click", () => playReel(true));

  // SMA indicator overlay (line view only)
  $("#smaToggle").addEventListener("click", () => {
    state.smaOn = !state.smaOn;
    $("#smaToggle").classList.toggle("active", state.smaOn);
    if (state.lastQuote) renderActiveView(state.lastQuote);
  });

  // Chart views — Line is free; Candles / % Return / Forecast are Pro+.
  $$("#viewTabs button[data-view]").forEach((b) =>
    b.addEventListener("click", () => setView(b.dataset.view)));

  // Forecast horizon (1W / 1M / 3M)
  $$("#fcHorizonTabs button").forEach((b) =>
    b.addEventListener("click", () => {
      $$("#fcHorizonTabs button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.horizon = parseInt(b.dataset.h, 10) || 30;
      if (state.view === "forecast") loadForecast();
    }));

  // Forecast model + add-ons live in Settings now.
  $("#closeSettings").addEventListener("click", () => $("#settingsDialog").close());
  $("#predMarketsToggle").addEventListener("click", togglePredMarkets);
  $("#interestApply").addEventListener("click", async () => {
    if (!ratedThemeNames().length) { toast("Rate a few areas first."); return; }
    const btn = $("#interestApply");
    btn.disabled = true; const old = btn.textContent; btn.textContent = "Adding…";
    const added = await seedWatchlistFromInterests(6);
    btn.disabled = false; btn.textContent = old;
    toast(added.length ? `Added ${added.length} tickers to your watchlist.` : "Those are already on your watchlist.");
  });

  // AI trade ideas
  $("#ideasBtn").addEventListener("click", openIdeas);
  $("#closeIdeas").addEventListener("click", () => $("#ideasDialog").close());
  $("#titanBtn")?.addEventListener("click", openTitan);
  $("#closeTitan")?.addEventListener("click", () => $("#titanDialog").close());
  $("#titanForm")?.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = ($("#titanInput").value || "").trim();
    if (q) { $("#titanInput").value = ""; titanAsk(q); }
  });
  $("#titanSaveBtn")?.addEventListener("click", titanTeach);
  $("#titanSkip")?.addEventListener("click", () => { $("#titanTeach").hidden = true; titanPendingQ = ""; });

  // Personalized FAAM (Beta)
  $("#personalizeToggle")?.addEventListener("click", () => { if (persState.enabled) setPersonalize(false); else openConsent(); });
  $("#closeConsent")?.addEventListener("click", () => $("#consentDialog").close());
  $("#consentCancel")?.addEventListener("click", () => $("#consentDialog").close());
  $("#consentCheck")?.addEventListener("change", (e) => { $("#consentAgree").disabled = !e.target.checked; });
  $("#consentAgree")?.addEventListener("click", () => { $("#consentDialog").close(); setPersonalize(true); });
  $("#closeOnboardPers")?.addEventListener("click", () => $("#onboardPersDialog").close());
  $("#persSkip")?.addEventListener("click", () => $("#onboardPersDialog").close());
  $("#persSave")?.addEventListener("click", savePersAnswers);
  // For You feed + price alerts
  $("#forYouBtn")?.addEventListener("click", openForYou);
  $("#closeForYou")?.addEventListener("click", () => $("#forYouDialog").close());
  $("#alertAdd")?.addEventListener("click", addAlert);
  $("#alertPrice")?.addEventListener("keydown", (e) => { if (e.key === "Enter") addAlert(); });
  // Beginner course
  $("#openCourseBtn")?.addEventListener("click", openCourse);
  $("#closeCourse")?.addEventListener("click", () => $("#courseDialog").close());
  $("#coursePrev")?.addEventListener("click", () => { if (courseIdx > 0) { courseIdx--; renderCourse(); } });
  $("#courseNext")?.addEventListener("click", () => {
    if (courseIdx < courseData.length - 1) { courseIdx++; renderCourse(); }
    else { $("#courseDialog").close(); toast("Course complete — nice work!"); }
  });
  $("#ideasRegen").addEventListener("click", loadIdeas);
  $("#ideasList").addEventListener("click", (e) => {
    const tk = e.target.closest("[data-tick]");
    const pr = e.target.closest("[data-prep]");
    if (tk) { selectStock(tk.dataset.tick); $("#ideasDialog").close(); }
    else if (pr) { $("#ideasDialog").close(); selectStock(pr.dataset.prep); setTimeout(openOrderTicket, 350); }
  });

  // What's new (changelog + roadmap)
  $("#whatsNewBtn").addEventListener("click", openChangelog);
  $("#closeChangelog").addEventListener("click", () => $("#changelogDialog").close());

  // Customize dashboard (Default · Build your own · AI design)
  $("#layoutBtn").addEventListener("click", openLayout);
  $("#closeLayout").addEventListener("click", () => $("#layoutDialog").close());
  $$("#layoutSeg .seg-btn").forEach((b) => b.addEventListener("click", () => setLayoutPane(b.dataset.mode)));
  $("#layUseDefault").addEventListener("click", useDefaultLayout);
  $("#layAiGo").addEventListener("click", aiDesignLayout);
  $$("#layAiChips .chip").forEach((c) => c.addEventListener("click", () => { $("#layAiPrompt").value = c.dataset.p; $("#layAiPrompt").focus(); }));
  $("#layList").addEventListener("click", (e) => {
    const t = e.target.closest("[data-toggle],[data-move]");
    if (!t) return;
    if (t.dataset.toggle) toggleDashWidget(t.dataset.toggle);
    else if (t.dataset.move) moveDashBlock(parseInt(t.dataset.i, 10), t.dataset.move);
  });

  // Assistant control of the dashboard (fills order tickets, with permission)
  $("#aiControlToggle").addEventListener("click", toggleAiControl);
  $("#aiControlAllow").addEventListener("click", () => resolveAiControl(true));
  $("#aiControlDeny").addEventListener("click", () => resolveAiControl(false));
  $("#aiControlDialog").addEventListener("cancel", (e) => { e.preventDefault(); resolveAiControl(false); });

  // Modes: Beginner + Game of Stocks
  $("#beginnerToggle").addEventListener("click", toggleBeginner);
  $("#gameToggle").addEventListener("click", toggleGame);
  $("#beginnerTour").addEventListener("click", () => openCoach(0));
  $("#beginnerBannerX").addEventListener("click", () => $("#beginnerBanner").classList.add("dismissed"));
  $("#coachClose").addEventListener("click", () => $("#coachDialog").close());
  $("#coachNext").addEventListener("click", coachNext);
  $("#coachPrev").addEventListener("click", coachPrev);
  $("#coachDialog").addEventListener("cancel", (e) => { e.preventDefault(); $("#coachDialog").close(); });
  $("#gameBtn").addEventListener("click", openGame);
  $("#tokenChip").addEventListener("click", openGame);
  $("#closeGame").addEventListener("click", () => $("#gameDialog").close());
  $("#claimBtn").addEventListener("click", claimDaily);

  // First-run onboarding (FAAM Assistant)
  $("#onboardSkip").addEventListener("click", skipOnboarding);
  $("#onboardSkipX").addEventListener("click", skipOnboarding);
  $("#onboardBuild").addEventListener("click", finishOnboarding);
  $("#onboardDialog").addEventListener("cancel", (e) => { e.preventDefault(); skipOnboarding(); });

  // Add ticker dialog
  $("#closeTicker").addEventListener("click", () => $("#tickerDialog").close());
  $("#watchBtn").addEventListener("click", () => openDialog("tickerDialog", "tickerSymbol"));
  $("#tickerForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const sym = $("#tickerSymbol").value.trim();
    const err = $("#tickerErr");
    err.textContent = "";
    if (!sym) { err.textContent = "Enter a ticker symbol."; return; }
    try {
      await addTicker(sym);
      $("#tickerForm").reset();
      $("#tickerDialog").close();
    } catch (ex) {
      err.textContent = ex.message;
    }
  });

  // Compare
  $("#compareBtn").addEventListener("click", () => {
    if (!state.active) return toast("Pick a stock first.");
    sendChat(`Compare ${state.active} to its top 3 sector peers — strengths and weaknesses.`);
  });

  // Adviser profile dialog (Max+)
  $("#adviserBtn").addEventListener("click", () => { if (gate("adviser")) openAdviser(); });
  $("#closeAdviser").addEventListener("click", () => $("#adviserDialog").close());
  $("#adviserText").addEventListener("input", updateAdviserCount);
  $("#adviserTemplate").addEventListener("click", () => {
    $("#adviserText").value = ADVISER_TEMPLATE;
    updateAdviserCount();
  });
  $("#adviserFile").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      $("#adviserText").value = String(reader.result || "").slice(0, 20000);
      updateAdviserCount();
      toast(`Loaded ${file.name}.`);
    };
    reader.onerror = () => toast("Could not read that file.");
    reader.readAsText(file);
    e.target.value = "";
  });
  $("#adviserClear").addEventListener("click", async () => {
    $("#adviserText").value = "";
    updateAdviserCount();
    try {
      await saveAdviser("");
      toast("Adviser profile cleared.");
      $("#adviserDialog").close();
      if (state.active) loadAIInsight();
    } catch (ex) {
      $("#adviserErr").textContent = ex.message;
    }
  });
  $("#adviserForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("#adviserErr").textContent = "";
    try {
      const data = await saveAdviser($("#adviserText").value);
      toast(data.adviser_loaded ? "Adviser profile saved — AI will use it." : "Adviser profile cleared.");
      $("#adviserDialog").close();
      if (state.active) loadAIInsight();
    } catch (ex) {
      $("#adviserErr").textContent = ex.message;
    }
  });

  // Voice mode (Pro+)
  $("#voiceBtn").addEventListener("click", () => { if (gate("voice")) openVoice(); });
  $("#voiceOrb").addEventListener("click", toggleListen);
  $("#voiceOrb").addEventListener("keydown", (e) => {
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggleListen(); }
  });
  $("#voiceClose").addEventListener("click", closeVoice);
  $("#voiceDialog").addEventListener("cancel", (e) => { e.preventDefault(); closeVoice(); });

  // "Open in browser" — only meaningful inside the native app; in a plain
  // browser you're already there, so keep it hidden.
  $("#browserBtn").addEventListener("click", openInBrowser);
  if (isNativeApp()) $("#browserBtn").hidden = false;

  $("#settingsBtn").addEventListener("click", openSettings);
  $("#accountBtn").addEventListener("click", openAccount);
  $("#closeAccount").addEventListener("click", () => $("#accountDialog").close());
  $("#logoutBtn").addEventListener("click", logout);
}

function toast(msg, ms = 2400) {
  const dlg = $("#toastDialog");
  dlg.textContent = msg;
  if (dlg.open) dlg.close();
  dlg.show();
  clearTimeout(toast._t);
  toast._t = setTimeout(() => dlg.open && dlg.close(), ms);
}

/* ---------- Market clock ---------- */
function updateMarketClock() {
  const el = document.getElementById("marketClock");
  if (!el) return;
  const now = new Date();
  const f = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour12: false,
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
  const parts = f.formatToParts(now).reduce((a, p) => ((a[p.type] = p.value), a), {});
  const weekend = parts.weekday === "Sat" || parts.weekday === "Sun";
  const mins = parseInt(parts.hour, 10) * 60 + parseInt(parts.minute, 10);
  // ET windows: pre 4:00–9:30, regular 9:30–16:00, after 16:00–20:00.
  let label = "CLOSED", cls = "closed";
  if (!weekend) {
    if (mins >= 570 && mins < 960) { label = "OPEN"; cls = "open"; }
    else if (mins >= 240 && mins < 570) { label = "PRE-MARKET"; cls = "pre"; }
    else if (mins >= 960 && mins < 1200) { label = "AFTER-HOURS"; cls = "post"; }
  }
  el.textContent = `NYSE ${parts.hour}:${parts.minute} ET · ${label}`;
  el.className = "market-clock " + cls;
}

/* ---------- Boot ---------- */
window.addEventListener("DOMContentLoaded", () => {
  try {
    const sm = localStorage.getItem("faam-forecast-model");
    if (sm === "apollo" || sm === "artemis" || sm === "perseverance") state.forecastModel = sm;
    state.predictionMarkets = localStorage.getItem("faam-pred-markets") === "1";
    state.beginner = localStorage.getItem("faam-beginner") === "1";
    state.gameOn = localStorage.getItem("faam-game") === "1";
    state.aiControl = localStorage.getItem("faam-ai-control") === "1";
  } catch (e) {}
  initBoot();
  initVersion();
  setInterval(checkForUpdate, 60_000);              // poll for a new build each minute
  window.addEventListener("focus", checkForUpdate); // and whenever the app refocuses
  loadInterests();
  wire();
  applyModes();
  renderModes();
  loadDashLayout();
  applyDashLayout();
  loadMe();
  checkHealth();
  loadWatchlist();
  loadPortfolio();
  loadPro();
  loadPersonalize();
  if (state.gameOn) loadGame();
  updateMarketClock();
  setInterval(updateMarketClock, 30_000);
  maybeOnboard();
  // Re-check Pro when returning from the Stripe checkout tab.
  window.addEventListener("focus", loadPro);
});
