#!/usr/bin/env python3
"""FAAM — Financial AI Agent Manager.

Single-file backend, stdlib only. Serves static UI + proxies stock data
(Yahoo Finance public chart endpoint) and OpenAI GPT chat.
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import hmac
import io
import json
import math
import os
import random
import re
import secrets
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT") or os.environ.get("FAAM_PORT") or "8765")
# Public origin used for outward links (Stripe checkout redirects, Google OAuth
# callback). On a host set FAAM_BASE_URL=https://your-domain; locally it's
# localhost so nothing changes for self-hosted/dev use.
BASE_URL = (os.environ.get("FAAM_BASE_URL") or f"http://localhost:{PORT}").rstrip("/")
# FAAM_ROOT lets a frozen launcher (e.g. the Windows .exe) point at where its
# bundled static/ and advisers/ were unpacked. Defaults to this file's folder.
ROOT = Path(os.environ.get("FAAM_ROOT") or Path(__file__).resolve().parent)
STATIC = ROOT / "static"


def _compute_version() -> str:
    """A build signature that changes whenever the app's code/assets change, so
    open clients can detect a new deploy and refresh ("FAAM is updating…")."""
    env = os.environ.get("FAAM_VERSION")
    if env:
        return env.strip()
    try:
        newest = 0.0
        for ext in ("*.js", "*.html", "*.css"):
            for p in STATIC.rglob(ext):
                newest = max(newest, p.stat().st_mtime)
        newest = max(newest, (ROOT / "app.py").stat().st_mtime)
        return str(int(newest))
    except Exception:  # noqa: BLE001
        return "1"


APP_VERSION = _compute_version()

# Request-body caps so a malicious client can't exhaust memory with a huge body.
MAX_BODY_BYTES = 2 * 1024 * 1024          # JSON request bodies (2 MB)
MAX_AUDIO_BYTES = 26 * 1024 * 1024        # voice uploads (OpenAI caps audio ~25 MB)

# What's-new feed: shown in-app, and the top version drives the "new" badge.
# Add a new entry at the top each release; tags: "new" | "improved" | "fixed".
CHANGELOG = [
    {"version": "1.5", "date": "2026-06-20", "title": "Free for everyone — beta", "items": [
        {"tag": "new", "text": "FAAM is in beta: every plan, model and tool is unlocked and free for everyone."},
        {"tag": "improved", "text": "Billing is paused — no card needed. Paid plans return at launch."},
    ]},
    {"version": "1.4", "date": "2026-06-20", "title": "Always up to date", "items": [
        {"tag": "new", "text": "Auto-updates — when a new FAAM ships, the app refreshes itself, no re-downloading."},
        {"tag": "new", "text": "This What's-New panel, with a live roadmap of what's coming."},
        {"tag": "improved", "text": "Security hardening across the backend (request limits, safer cookies & headers)."},
    ]},
    {"version": "1.3", "date": "2026-06-18", "title": "Make it yours", "items": [
        {"tag": "new", "text": "Customizable dashboard — build your own layout or let GPT-4.1 mini design it."},
        {"tag": "new", "text": "The assistant can fill in order tickets for you, with your permission."},
        {"tag": "new", "text": "“Are you a robot?” verification on sign-up and sign-in."},
    ]},
    {"version": "1.2", "date": "2026-06-15", "title": "Learn & play", "items": [
        {"tag": "new", "text": "Beginner mode — a guided tour and plain-language tips."},
        {"tag": "new", "text": "Game of Stocks — tokens, streaks, daily rewards and a leaderboard."},
        {"tag": "new", "text": "Windows & Linux downloads, plus a browser version."},
    ]},
    {"version": "1.1", "date": "2026-06-12", "title": "Sharper forecasts", "items": [
        {"tag": "new", "text": "Onboarding that tailors your watchlist to what you care about."},
        {"tag": "improved", "text": "A cleaner light theme across the whole dashboard."},
    ]},
]
ROADMAP = [
    {"title": "Juno", "text": "A deep model trained on historical stock data — in training now."},
    {"title": "FAAM in the cloud", "text": "Use FAAM in any browser with nothing to install."},
    {"title": "Price & news alerts", "text": "Get pinged when your stocks move or the story changes."},
    {"title": "Mobile apps", "text": "FAAM for iOS and Android."},
]
CHANGELOG_VERSION = CHANGELOG[0]["version"] if CHANGELOG else ""

# Beta: every feature is unlocked and free for everyone, and billing is paused.
# The Stripe key & checkout code stay wired up (kept for launch) — they're just
# not used while BETA is on. Set FAAM_BETA=0 to go paid.
BETA = os.environ.get("FAAM_BETA", "1") != "0"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("FAAM_MODEL", "gpt-4.1-mini")
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# Voice mode (speech-to-text + text-to-speech), all via the same OpenAI key.
STT_MODEL = os.environ.get("FAAM_STT_MODEL", "whisper-1")
TTS_MODEL = os.environ.get("FAAM_TTS_MODEL", "tts-1")
TTS_VOICE = os.environ.get("FAAM_TTS_VOICE", "alloy")
OPENAI_STT_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"

# Approx OpenAI rates for usage metering (USD). Adjust if pricing changes.
CHAT_PRICING = {  # model prefix -> (input_per_1M, output_per_1M)
    "gpt-4.1-nano": (0.10, 0.40), "gpt-4.1-mini": (0.40, 1.60), "gpt-4.1": (2.00, 8.00),
    "gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00),
}
TTS_PER_1M_CHARS = 15.0     # tts-1
WHISPER_FLAT = 0.006        # whisper-1, ~1 min assumed per clip

# Stripe (FAAM Pro subscription). Secret key from env or ~/.faam/stripe_key.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_API = "https://api.stripe.com"

# Subscription tiers (price in cents). Higher tier unlocks more.
PLANS = {
    "lite":  {"name": "Lite",  "tier": 1, "price": 500,   "tagline": "Try FAAM AI",
              "perks": ["Enough usage to test FAAM AI", "Complete access 24/7"]},
    "pro":   {"name": "Pro",   "tier": 2, "price": 1500,  "tagline": "For regulars",
              "perks": ["5× more usage than Lite", "Speaking (voice) mode",
                        "Graphs & prediction models", "Complete access 24/7"]},
    "max":   {"name": "Max",   "tier": 3, "price": 2500,  "tagline": "Most popular", "popular": True,
              "perks": ["10× more usage than Pro", "Everything in Pro",
                        "Daily recap video", "Learning + customizable AI",
                        "Prediction markets (add-on)"]},
    "elite": {"name": "Elite", "tier": 4, "price": 10000, "tagline": "Everything, first",
              "perks": ["Everything in Max", "Perseverance model (Apollo + Artemis)",
                        "Prediction markets (add-on)",
                        "Customer support (request features)",
                        "Beta tester — all beta features", "Priority access"]},
}
# Minimum tier required to use each premium feature.
FEATURE_MIN_TIER = {
    "voice": 2,      # Speaking mode → Pro+
    "screener": 2,   # AI screener / custom scans → Pro+
    "forecast": 2,   # Graphs & prediction models → Pro+
    "recap": 3,      # Daily recap video → Max+
    "learn": 3,      # Learning features → Max+
    "adviser": 3,    # Customizable AI → Max+
    "predictions": 3,  # Prediction markets add-on → Max & Elite
}

# Monthly OpenAI spend cap per tier, in cents. None = unlimited (Elite).
# Kept server-side only — the exact caps are never shown to users.
TIER_CAP_CENTS = {0: 1, 1: 2, 2: 10, 3: 100, 4: None}

# Universe the AI screener ("Custom Scans") ranks over. Liquid, well-known names
# across sectors plus a few ETFs and crypto. Override with FAAM_UNIVERSE (CSV).
SCREENER_UNIVERSE = (os.environ.get("FAAM_UNIVERSE") or ",".join([
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AMD", "AVGO", "NFLX",
    "CRM", "ORCL", "ADBE", "INTC", "QCOM", "JPM", "BAC", "V", "MA", "WMT",
    "COST", "KO", "PEP", "XOM", "CVX", "JNJ", "UNH", "DIS", "BA", "PLTR",
    "SPY", "QQQ", "BTC-USD", "ETH-USD",
])).split(",")

# Lightweight in-memory quote cache so repeat screens are fast.
_QUOTE_CACHE: dict = {}
_PREVCLOSE_CACHE: dict = {}
_QUOTE_TTL = 90  # seconds

DEFAULT_TICKERS = [
    "AAPL", "NVDA", "TSLA", "GOOGL", "MSFT",
    "AMZN", "META", "AMD", "SPY", "QQQ",
    "BTC-USD", "ETH-USD",
]

SYSTEM_PROMPT = (
    "You are FAAM, a sharp financial AI agent. "
    "You analyze stocks, markets, and portfolios with clarity and rigor. "
    "Be concise — short paragraphs, bullet points for lists. "
    "Cite specific numbers from the provided context when available. "
    "Always remind the user this is information, not financial advice."
)

# ---------- Persistent user data (~/.faam, or FAAM_DATA_DIR on a host) ----------
DATA_DIR = Path(os.environ.get("FAAM_DATA_DIR") or (Path.home() / ".faam"))
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
PORTFOLIO_FILE = DATA_DIR / "portfolio.json"
ADVISER_FILE = DATA_DIR / "adviser.md"
BROKER_FILE = DATA_DIR / "broker.json"
STRIPE_KEY_FILE = DATA_DIR / "stripe_key"
USAGE_FILE = DATA_DIR / "usage.json"
USERS_FILE = DATA_DIR / "users.json"
SESSION_SECRET_FILE = DATA_DIR / "session_secret"
GOOGLE_OAUTH_FILE = DATA_DIR / "google_oauth.json"
SESSION_TTL = 60 * 60 * 24 * 30  # 30 days
ADVISER_MAX = 20000  # chars; keeps token use sane

# Per-request user context (thread-local; ThreadingHTTPServer uses threads).
_req = threading.local()


def _set_req(username: str, tier: int) -> None:
    _req.username = username or "anon"
    _req.tier = int(tier or 0)


def _req_username() -> str:
    return getattr(_req, "username", "anon")


def _req_tier() -> int:
    return getattr(_req, "tier", 0)


def _load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        pass
    return default


_DATA_LOCK = threading.Lock()


def _save_json(path: Path, data) -> bool:
    # Serialized + atomic (temp file → os.replace) so concurrent requests on the
    # threaded server can't interleave writes and corrupt accounts/usage data.
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _DATA_LOCK:
            tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001
        return False


def load_watchlist() -> list:
    wl = _load_json(WATCHLIST_FILE, None)
    if not isinstance(wl, list) or not wl:
        return list(DEFAULT_TICKERS)
    return [str(s).upper() for s in wl]


def save_watchlist(symbols: list) -> bool:
    return _save_json(WATCHLIST_FILE, symbols)


def load_portfolio() -> list:
    pf = _load_json(PORTFOLIO_FILE, [])
    return pf if isinstance(pf, list) else []


def save_portfolio(positions: list) -> bool:
    return _save_json(PORTFOLIO_FILE, positions)


def load_broker() -> dict:
    b = _load_json(BROKER_FILE, {})
    return b if isinstance(b, dict) else {}


def save_broker(pref: dict) -> bool:
    return _save_json(BROKER_FILE, pref)


# ---------- FAAM Pro (Stripe) ----------
def stripe_key() -> str:
    if STRIPE_SECRET_KEY:
        return STRIPE_SECRET_KEY
    try:
        if STRIPE_KEY_FILE.exists():
            return STRIPE_KEY_FILE.read_text().strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def save_stripe_key(key: str) -> bool:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STRIPE_KEY_FILE.write_text(key.strip())
        os.chmod(STRIPE_KEY_FILE, 0o600)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------- Accounts & sessions ----------
def load_users() -> dict:
    u = _load_json(USERS_FILE, {})
    return u if isinstance(u, dict) else {}


def save_users(users: dict) -> bool:
    return _save_json(USERS_FILE, users)


def hash_password(pw: str, salt: str | None = None) -> dict:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200000).hex()
    return {"salt": salt, "hash": h}


def verify_password(pw: str, rec) -> bool:
    if not rec or not isinstance(rec, dict):
        return False
    try:
        h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(rec["salt"]), 200000).hex()
        return hmac.compare_digest(h, rec["hash"])
    except Exception:  # noqa: BLE001
        return False


def seed_users() -> None:
    """Ensure the built-in dev admin account (Elite) exists.

    The password is stored as a salted PBKDF2 hash (not plaintext) so the
    secret never ships in source.
    """
    users = load_users()
    if "dev" not in users:
        users["dev"] = {
            "pw": {
                "salt": "96aec5bed10d9f94e0f5828e90b194a7",
                "hash": "8f36b455563c32d12add087db431e19d230d67608dd30e6a293eee6b25115db1",
            },
            "tier": 4, "plan": "elite", "admin": True,
            "email": "", "provider": "local",
            "created": datetime.now().isoformat(),
        }
        save_users(users)


def set_user_plan(username: str, plan: str) -> bool:
    info = PLANS.get(plan)
    users = load_users()
    if username in users and info:
        users[username]["plan"] = plan
        users[username]["tier"] = info["tier"]
        save_users(users)
        return True
    return False


def _session_secret() -> bytes:
    # On a host, set FAAM_SESSION_SECRET so sessions stay valid across restarts
    # and multiple instances (otherwise a random per-process secret logs everyone out).
    env = os.environ.get("FAAM_SESSION_SECRET")
    if env:
        return env.encode("utf-8")
    try:
        if SESSION_SECRET_FILE.exists():
            return SESSION_SECRET_FILE.read_bytes()
    except Exception:  # noqa: BLE001
        pass
    sec = secrets.token_bytes(32)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_SECRET_FILE.write_bytes(sec)
        os.chmod(SESSION_SECRET_FILE, 0o600)
    except Exception:  # noqa: BLE001
        pass
    return sec


def make_session(username: str) -> str:
    exp = str(int(time.time()) + SESSION_TTL)
    msg = f"{username}|{exp}"
    sig = hmac.new(_session_secret(), msg.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{msg}|{sig}".encode()).decode()


def read_session(token: str):
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        username, exp, sig = raw.rsplit("|", 2)
        good = hmac.new(_session_secret(), f"{username}|{exp}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig) or int(exp) < time.time():
            return None
        return username
    except Exception:  # noqa: BLE001
        return None


def sign_state(value: str) -> str:
    sig = hmac.new(_session_secret(), value.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{value}.{sig}"


def verify_state(token: str):
    try:
        value, sig = token.rsplit(".", 1)
        good = hmac.new(_session_secret(), value.encode(), hashlib.sha256).hexdigest()[:16]
        return value if hmac.compare_digest(good, sig) else None
    except Exception:  # noqa: BLE001
        return None


def google_creds():
    cid = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    csec = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if cid and csec:
        return cid, csec
    g = _load_json(GOOGLE_OAUTH_FILE, {})
    if isinstance(g, dict) and g.get("client_id") and g.get("client_secret"):
        return g["client_id"], g["client_secret"]
    return "", ""


# ---------- Usage metering (per-user monthly OpenAI spend cap) ----------
def _usage_month() -> str:
    return datetime.now().strftime("%Y-%m")


def current_usage_cents(username: str | None = None) -> float:
    username = username or _req_username()
    u = _load_json(USAGE_FILE, {})
    rec = u.get(username) if isinstance(u, dict) else None
    if not isinstance(rec, dict) or rec.get("month") != _usage_month():
        return 0.0  # new month resets the meter
    try:
        return float(rec.get("cents") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def record_cost(dollars: float) -> None:
    if not dollars or dollars <= 0:
        return
    username = _req_username()
    u = _load_json(USAGE_FILE, {})
    if not isinstance(u, dict):
        u = {}
    rec = u.get(username) or {}
    base = float(rec.get("cents") or 0.0) if rec.get("month") == _usage_month() else 0.0
    u[username] = {"month": _usage_month(), "cents": round(base + dollars * 100.0, 6)}
    _save_json(USAGE_FILE, u)


def tier_cap_cents():
    return TIER_CAP_CENTS.get(_req_tier(), 1)


def usage_blocked() -> bool:
    cap = tier_cap_cents()
    if cap is None:  # Elite — unlimited
        return False
    return current_usage_cents() >= cap


def _rate_for(model: str):
    for k, v in CHAT_PRICING.items():
        if (model or "").startswith(k):
            return v
    return (0.40, 1.60)


def chat_cost(result: dict) -> float:
    try:
        usage = result.get("usage") or {}
        ri, ro = _rate_for(result.get("model") or OPENAI_MODEL)
        return usage.get("prompt_tokens", 0) / 1e6 * ri + usage.get("completion_tokens", 0) / 1e6 * ro
    except Exception:  # noqa: BLE001
        return 0.0


USAGE_LIMIT_MSG = {
    "error": "usage_limit", "upgrade": True,
    "message": "You've reached your plan's usage limit. Upgrade for more.",
}


def stripe_request(method: str, path: str, params=None):
    """Call the Stripe REST API (form-encoded). Returns (json, error)."""
    key = stripe_key()
    if not key:
        return None, {"error": "Stripe is not configured. Add your Stripe secret key first."}
    data = urllib.parse.urlencode(params or [], doseq=True).encode("utf-8") if params else None
    req = urllib.request.Request(
        STRIPE_API + path, data=data, method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(detail).get("error", {}).get("message", f"Stripe error {e.code}")
        except Exception:  # noqa: BLE001
            msg = f"Stripe error {e.code}"
        return None, {"error": msg}
    except urllib.error.URLError as e:
        return None, {"error": f"network error: {e.reason}"}


def load_adviser() -> str:
    """The user's uploaded 'financial adviser' instructions, if any."""
    try:
        if ADVISER_FILE.exists():
            return ADVISER_FILE.read_text()[:ADVISER_MAX].strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def save_adviser(text: str) -> bool:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ADVISER_FILE.write_text((text or "")[:ADVISER_MAX])
        return True
    except Exception:  # noqa: BLE001
        return False


def effective_system(base: str = SYSTEM_PROMPT) -> str:
    """Layer the user's adviser instructions on top of the base prompt."""
    adviser = load_adviser()
    if adviser:
        return (
            base
            + "\n\n--- The user has loaded a custom Financial Adviser profile. "
            + "Adopt this persona and follow these instructions while keeping the "
            + "no-financial-advice disclaimer: ---\n"
            + adviser
        )
    return base


def _prev_session_close(symbol: str):
    """Prior regular-session close — the correct baseline for a daily % change.

    Yahoo's chart meta only includes `previousClose` on short ranges; on 1mo+ it
    is omitted and `chartPreviousClose` becomes the close before the whole window
    (which would turn "today's change" into the change over the entire range).
    A lightweight 1d request always carries the true prior close.
    """
    now = time.time()
    hit = _PREVCLOSE_CACHE.get(symbol)
    if hit and (now - hit[0]) < _QUOTE_TTL:
        return hit[1]
    pc = None
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{urllib.parse.quote(symbol)}?range=1d&interval=5m"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (FAAM dashboard)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        m = ((d.get("chart", {}).get("result") or [{}])[0] or {}).get("meta", {}) or {}
        pc = m.get("previousClose") or m.get("chartPreviousClose")
    except Exception:  # noqa: BLE001
        pc = None
    _PREVCLOSE_CACHE[symbol] = (now, pc)
    return pc


def _market_state(meta: dict) -> str:
    """PRE / REGULAR / POST / CLOSED, derived from the trading-period epochs."""
    ctp = meta.get("currentTradingPeriod") or {}
    now = time.time()
    pre = ctp.get("pre") or {}
    reg = ctp.get("regular") or {}
    post = ctp.get("post") or {}
    try:
        if reg and reg["start"] <= now < reg["end"]:
            return "REGULAR"
        if pre and pre["start"] <= now < pre["end"]:
            return "PRE"
        if post and post["start"] <= now < post["end"]:
            return "POST"
    except Exception:  # noqa: BLE001
        pass
    return "CLOSED"


def _extended_hours(meta: dict, state: str, price: float):
    """Return (ext_price, ext_change, ext_pct, ext_label) vs the regular price."""
    pre_px = meta.get("preMarketPrice")
    post_px = meta.get("postMarketPrice")
    ext_price = ext_label = None
    if state == "PRE" and pre_px:
        ext_price, ext_label = float(pre_px), "Pre-market"
    elif state == "POST" and post_px:
        ext_price, ext_label = float(post_px), "After hours"
    if ext_price and price:
        return ext_price, ext_price - price, (ext_price - price) / price * 100.0, ext_label
    return None, None, None, None


_YH_RANGES = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}
_YH_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo"}


def yahoo_quote(symbol: str, range_: str = "1mo", interval: str = "1d") -> dict:
    """Fetch quote + history from Yahoo Finance public chart API."""
    # Allowlist range/interval so they can't inject extra query params upstream.
    if range_ not in _YH_RANGES:
        range_ = "1mo"
    if interval not in _YH_INTERVALS:
        interval = "1d"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(symbol, safe='')}?range={range_}&interval={interval}"
        f"&includePrePost=true"
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (FAAM dashboard)"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())

    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        err = data.get("chart", {}).get("error") or "no data"
        return {"symbol": symbol, "error": str(err)}

    meta = result.get("meta", {}) or {}
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    vols = quote.get("volume") or []

    def _at(arr, i):
        return arr[i] if i < len(arr) else None

    # Each point keeps `c` (close) for back-compat; OHLC + volume are added when
    # present so the candlestick / volume views can render.
    history = []
    for i, t in enumerate(timestamps):
        c = _at(closes, i)
        if c is None:
            continue
        pt = {"t": t, "c": c}
        o, h, l, v = _at(opens, i), _at(highs, i), _at(lows, i), _at(vols, i)
        if o is not None:
            pt["o"] = o
        if h is not None:
            pt["h"] = h
        if l is not None:
            pt["l"] = l
        if v is not None:
            pt["v"] = v
        history.append(pt)

    price = meta.get("regularMarketPrice") or 0.0
    # Accurate daily change needs the prior *session* close, independent of the
    # chart range. Prefer `previousClose` (present on short ranges); on long
    # ranges fetch it from a 1d request rather than the range-dependent
    # `chartPreviousClose`, which would otherwise report the whole-period move.
    prev_close = meta.get("previousClose")
    if not prev_close:
        prev_close = _prev_session_close(symbol)
    if not prev_close:
        prev_close = meta.get("chartPreviousClose") or (history[0]["c"] if history else 0.0)
    change = price - prev_close
    pct = (change / prev_close * 100.0) if prev_close else 0.0

    # Extended hours: pre-market before the open, post-market after the close.
    # Measured against the regular price (the standard "after hours +X%" display).
    state = _market_state(meta)
    ext_price, ext_change, ext_pct, ext_label = _extended_hours(meta, state, price)

    return {
        "symbol": symbol,
        "name": meta.get("longName") or meta.get("shortName") or symbol,
        "price": float(price),
        "prev_close": float(prev_close),
        "change": float(change),
        "pct": float(pct),
        "marketState": state,
        "extPrice": ext_price,
        "extChange": ext_change,
        "extPct": ext_pct,
        "extLabel": ext_label,
        "currency": meta.get("currency", "USD"),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName", ""),
        "quoteType": meta.get("instrumentType") or meta.get("quoteType") or "",
        "history": history,
        "high": meta.get("regularMarketDayHigh"),
        "low": meta.get("regularMarketDayLow"),
        "volume": meta.get("regularMarketVolume"),
        "fiftyTwoWeekHigh": meta.get("fiftyTwoWeekHigh"),
        "fiftyTwoWeekLow": meta.get("fiftyTwoWeekLow"),
    }


def cached_quote(symbol: str) -> dict:
    """yahoo_quote with a short TTL cache — used by the screener."""
    now = time.time()
    hit = _QUOTE_CACHE.get(symbol)
    if hit and (now - hit[0]) < _QUOTE_TTL:
        return hit[1]
    try:
        q = yahoo_quote(symbol, range_="5d", interval="1d")
    except Exception as e:  # noqa: BLE001
        q = {"symbol": symbol, "error": str(e)}
    _QUOTE_CACHE[symbol] = (now, q)
    return q


# ---------------------------------------------------------------------------
# Predictive models (Pro+). Pure statistics over Yahoo history — no API cost.
# ---------------------------------------------------------------------------
def _linreg(ys: list):
    """Least-squares fit of ys against its index. Returns (slope, intercept, r2)."""
    n = len(ys)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0), 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs) or 1e-9
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys) or 1e-9
    ss_res = sum((ys[i] - (intercept + slope * xs[i])) ** 2 for i in range(n))
    return slope, intercept, max(0.0, 1.0 - ss_res / ss_tot)


def _next_business_days(start_epoch: int, n: int) -> list:
    """The next `n` weekday timestamps after start_epoch (skips Sat/Sun)."""
    out, t, day = [], int(start_epoch), 86400
    while len(out) < n:
        t += day
        if time.gmtime(t).tm_wday < 5:  # 0=Mon … 4=Fri
            out.append(t)
    return out


# Forecasting models. The first two are live (computed below); the rest are
# teased in the "DIFFERENT MODELS" picker as coming soon (beta).
MC_PATHS = 5000
# How hard headline sentiment can tilt the daily drift (±0.15%/day max). Bounded
# so one spicy headline can't run away with the projection.
NEWS_TILT_MAX = 0.0015
FORECAST_MODELS = [
    {"id": "apollo",  "name": "Apollo",  "kind": "Drift & volatility engine",        "live": True},
    {"id": "artemis", "name": "Artemis", "kind": "Simulation + live news sentiment", "live": True},
    {"id": "perseverance", "name": "Perseverance", "kind": "Apollo + Artemis ensemble", "live": True, "minTier": 4},
    # Coming soon — a deep network trained on historical stock data. Not live yet,
    # so compute_forecast() falls back to Apollo if it's ever requested.
    {"id": "juno", "name": "Juno", "kind": "Deep model trained on historical stock data", "live": False, "comingSoon": True},
]
_MODEL_BY_ID = {m["id"]: m for m in FORECAST_MODELS}


def _norm_cdf(z: float) -> float:
    """Standard normal CDF via the error function (stdlib only)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _model_probabilities(s0: float, mu: float, sigma: float, horizon: int) -> list:
    """Model-implied odds at the horizon (log-normal terminal distribution).

    Powers the 'prediction markets' add-on. Returns probability of common
    outcomes as {label, p (0..1), side}.
    """
    sd = sigma * math.sqrt(horizon)
    drift = mu * horizon

    def p_up(x):       # P(return >= +x)
        thr = math.log(1.0 + x)
        if sd <= 0:
            return 1.0 if drift >= thr else 0.0
        return 1.0 - _norm_cdf((thr - drift) / sd)

    def p_down(x):     # P(return <= -x)
        thr = math.log(1.0 - x)
        if sd <= 0:
            return 1.0 if drift <= thr else 0.0
        return _norm_cdf((thr - drift) / sd)

    return [
        {"label": f"Higher in {horizon}d", "p": round(p_up(0.0), 3), "side": "up"},
        {"label": "Up 5% or more",  "p": round(p_up(0.05), 3), "side": "up"},
        {"label": "Up 10% or more", "p": round(p_up(0.10), 3), "side": "up"},
        {"label": "Down 5% or more",  "p": round(p_down(0.05), 3), "side": "down"},
        {"label": "Down 10% or more", "p": round(p_down(0.10), 3), "side": "down"},
    ]


def _percentile(sorted_vals: list, q: float) -> float:
    """Linear-interpolated percentile of an ascending list (q in [0, 1])."""
    if not sorted_vals:
        return 0.0
    idx = q * (len(sorted_vals) - 1)
    lo, hi = math.floor(idx), math.ceil(idx)
    if lo == hi:
        return sorted_vals[int(lo)]
    frac = idx - lo
    return sorted_vals[int(lo)] * (1 - frac) + sorted_vals[int(hi)] * frac


def _cone_gbm(s0, mu, sigma, future_t):
    """Closed-form drift + volatility cone (Geometric Brownian Motion)."""
    z68, z90 = 1.0, 1.645
    out = []
    for k, t in enumerate(future_t, start=1):
        s = sigma * math.sqrt(k)
        out.append({
            "t": t,
            "mean": round(s0 * math.exp(mu * k), 4),
            "lo68": round(s0 * math.exp(mu * k - z68 * s), 4),
            "hi68": round(s0 * math.exp(mu * k + z68 * s), 4),
            "lo90": round(s0 * math.exp(mu * k - z90 * s), 4),
            "hi90": round(s0 * math.exp(mu * k + z90 * s), 4),
        })
    return out


def _cone_montecarlo(s0, mu, sigma, future_t):
    """Monte Carlo cone: simulate many GBM paths, take per-step percentiles."""
    h = len(future_t)
    cols = [[0.0] * MC_PATHS for _ in range(h)]
    gauss = random.gauss
    log_s0 = math.log(s0) if s0 > 0 else 0.0
    for p in range(MC_PATHS):
        logp = log_s0
        for k in range(h):
            logp += mu + sigma * gauss(0.0, 1.0)
            cols[k][p] = math.exp(logp)
    out = []
    for k, t in enumerate(future_t):
        col = sorted(cols[k])
        out.append({
            "t": t,
            "mean": round(_percentile(col, 0.50), 4),
            "lo68": round(_percentile(col, 0.16), 4),
            "hi68": round(_percentile(col, 0.84), 4),
            "lo90": round(_percentile(col, 0.05), 4),
            "hi90": round(_percentile(col, 0.95), 4),
        })
    return out


def compute_forecast(symbol: str, horizon: int = 30, model: str = "apollo",
                     drift_tilt: float = 0.0) -> dict:
    """Project a price path from ~1y of daily closes.

    Two models: "apollo" (closed-form drift & volatility cone — fast, no AI) and
    "artemis" (Monte Carlo simulation with the daily drift nudged by `drift_tilt`
    from live headline sentiment). Both return 68% / 90% bands plus a linear-trend
    line, a history tail, and summary stats.
    """
    spec = _MODEL_BY_ID.get(model)
    if not spec or not spec.get("live"):
        model, spec = "apollo", _MODEL_BY_ID["apollo"]
    horizon = max(5, min(int(horizon or 30), 120))
    q = yahoo_quote(symbol, range_="1y", interval="1d")
    if q.get("error"):
        return {"error": q["error"]}
    hist = [p for p in q.get("history", []) if p.get("c")]
    closes = [p["c"] for p in hist]
    if len(closes) < 30:
        return {"error": "Not enough price history to model this symbol yet."}

    s0 = closes[-1]
    last_t = hist[-1]["t"]
    rets = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]
    n = len(rets)
    mu = sum(rets) / n
    var = sum((r - mu) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    sigma = math.sqrt(var)

    tail = closes[-min(120, len(closes)):]
    slope, intercept, r2 = _linreg(tail)
    base_idx = len(tail) - 1

    # Artemis & Perseverance tilt the daily drift by live headline sentiment (bounded).
    tilt = max(-NEWS_TILT_MAX, min(NEWS_TILT_MAX, drift_tilt)) if model in ("artemis", "perseverance") else 0.0
    mu_eff = mu + tilt

    future_t = _next_business_days(last_t, horizon)
    if model == "perseverance":
        # Ensemble — average Apollo's analytic cone with Artemis's news-tilted simulation.
        a = _cone_gbm(s0, mu, sigma, future_t)
        b = _cone_montecarlo(s0, mu_eff, sigma, future_t)
        band = []
        for i in range(len(a)):
            row = {"t": a[i]["t"]}
            for k in ("mean", "lo68", "hi68", "lo90", "hi90"):
                row[k] = round((a[i][k] + b[i][k]) / 2.0, 4)
            band.append(row)
    elif model == "artemis":
        band = _cone_montecarlo(s0, mu_eff, sigma, future_t)
    else:
        band = _cone_gbm(s0, mu_eff, sigma, future_t)

    # Anchor the cone at "now" so it emerges from the current price, and attach
    # the linear-trend value to every point.
    fc = [{
        "t": last_t, "mean": round(s0, 4), "lo68": round(s0, 4), "hi68": round(s0, 4),
        "lo90": round(s0, 4), "hi90": round(s0, 4), "trend": round(intercept + slope * base_idx, 4),
    }]
    for k, pt in enumerate(band, start=1):
        pt["trend"] = round(intercept + slope * (base_idx + k), 4)
        fc.append(pt)

    target = fc[-1]["mean"]
    exp_ret = (target / s0 - 1.0) * 100.0 if s0 else 0.0
    return {
        "symbol": q["symbol"],
        "name": q.get("name", symbol),
        "price": round(s0, 4),
        "horizon": horizon,
        "anchorT": last_t,
        "history": [{"t": p["t"], "c": p["c"]} for p in hist[-90:]],
        "forecast": fc,
        "stats": {
            "target": round(target, 4),
            "lo": round(fc[-1]["lo90"], 4),
            "hi": round(fc[-1]["hi90"], 4),
            "expReturnPct": round(exp_ret, 2),
            "annVolPct": round(sigma * math.sqrt(252) * 100.0, 1),
            "driftDailyPct": round((math.exp(mu_eff) - 1.0) * 100.0, 3),
            "newsTiltPct": round((math.exp(tilt) - 1.0) * 100.0, 3),
            "trendR2": round(r2, 2),
            "direction": "up" if exp_ret >= 0 else "down",
        },
        "probabilities": _model_probabilities(s0, mu_eff, sigma, horizon),
        "model": spec,
        "models": FORECAST_MODELS,
    }


def screen_universe(universe: list) -> list:
    """Fetch live metrics for the universe (concurrently) for the AI screener."""
    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for q in ex.map(cached_quote, universe):
            if not q or q.get("error"):
                continue
            price = q.get("price") or 0.0
            hi = q.get("fiftyTwoWeekHigh") or 0.0
            lo = q.get("fiftyTwoWeekLow") or 0.0
            out.append({
                "symbol": q.get("symbol"),
                "name": q.get("name"),
                "price": price,
                "pct": q.get("pct") or 0.0,
                "fiftyTwoWeekHigh": hi,
                "fiftyTwoWeekLow": lo,
                "pctFromHigh": ((price - hi) / hi * 100.0) if hi else 0.0,
                "pctFromLow": ((price - lo) / lo * 100.0) if lo else 0.0,
                "quoteType": q.get("quoteType") or "",
            })
    return out


def _parse_json_array(text: str) -> list:
    """Lenient extraction of a JSON array from an LLM reply."""
    if not text:
        return []
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t.split("\n", 1)[1] if "\n" in t else t
    start, end = t.find("["), t.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(t[start:end + 1])
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _parse_json_obj(text: str) -> dict:
    """Lenient extraction of a JSON object from an LLM reply."""
    if not text:
        return {}
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t.split("\n", 1)[1] if "\n" in t else t
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        data = json.loads(t[start:end + 1])
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def openai_chat(messages: list, system: str | None = None) -> dict:
    """Call OpenAI chat completions API."""
    if not OPENAI_API_KEY:
        return {"error": "OPENAI_API_KEY not set on the server."}

    # OpenAI puts the system prompt as the first message in the array.
    full_messages = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    payload = {
        "model": OPENAI_MODEL,
        "messages": full_messages,
        "max_tokens": 2048,
    }

    req = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        return {"error": f"OpenAI API error {e.code}", "detail": detail}
    except urllib.error.URLError as e:
        return {"error": f"network error: {e.reason}"}


def extract_text(api_result: dict) -> str:
    choices = api_result.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message") or {}).get("content") or ""


def fetch_news(symbol: str, limit: int = 8) -> list:
    """Recent headlines for a symbol from Yahoo Finance search (no key needed)."""
    url = (
        "https://query2.finance.yahoo.com/v1/finance/search?"
        f"q={urllib.parse.quote(symbol)}&newsCount={int(limit)}&quotesCount=0"
        "&enableFuzzyQuery=false"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (FAAM dashboard)"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
    except Exception:  # noqa: BLE001
        return []
    out = []
    for n in (data.get("news") or [])[:limit]:
        title = (n.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "publisher": n.get("publisher") or "",
            "link": n.get("link") or "",
            "time": int(n.get("providerPublishTime") or 0),
        })
    return out


def score_news_sentiment(symbol: str, name: str, headlines: list) -> dict:
    """Score how each headline likely affects the stock (LLM). Costs OpenAI usage.

    Returns {"overall": -1..1, "summary": str, "scores": [-1..1...], "api": result}
    or {"error": ...}.
    """
    lines = "\n".join(f"{i + 1}. {h['title']}" for i, h in enumerate(headlines))
    system = (
        "You are a financial news sentiment analyst. For the given stock, rate how each "
        "headline is likely to affect its share price over the next few weeks. Reply with "
        "ONLY a JSON object, no prose: "
        '{"overall": <number from -1 to 1>, "summary": "<=18 words>, plain", '
        '"scores": [<one number from -1 to 1 per headline, same order>]}. '
        "Use -1 for very bearish, 0 for neutral, 1 for very bullish."
    )
    user = f"Stock: {name} ({symbol})\nHeadlines:\n{lines}"
    result = openai_chat([{"role": "user", "content": user}], system=system)
    if "error" in result:
        return {"error": result.get("error"), "api": result}
    parsed = _parse_json_obj(extract_text(result))
    if not parsed:
        return {"error": "could not parse sentiment", "api": result}

    def _clamp(x):
        try:
            return max(-1.0, min(1.0, float(x)))
        except (TypeError, ValueError):
            return 0.0

    raw_scores = parsed.get("scores") or []
    return {
        "overall": _clamp(parsed.get("overall")),
        "summary": str(parsed.get("summary") or "").strip()[:160],
        "scores": [_clamp(s) for s in raw_scores][:len(headlines)],
        "api": result,
    }


def _multipart(fields: dict, file_field: str, filename: str,
               file_bytes: bytes, file_ct: str) -> tuple[str, bytes]:
    """Build a multipart/form-data body (stdlib only — no requests)."""
    boundary = "----FAAM" + uuid.uuid4().hex
    pre = ""
    for k, v in fields.items():
        pre += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
        )
    pre += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: {file_ct}\r\n\r\n"
    )
    body = pre.encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return boundary, body


def openai_transcribe(audio: bytes, ext: str, content_type: str) -> dict:
    """Speech-to-text via OpenAI audio transcriptions (Whisper)."""
    if not OPENAI_API_KEY:
        return {"error": "OPENAI_API_KEY not set on the server."}
    boundary, body = _multipart(
        {"model": STT_MODEL, "response_format": "json"},
        "file", f"audio.{ext}", audio, content_type or "application/octet-stream",
    )
    req = urllib.request.Request(
        OPENAI_STT_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": f"OpenAI transcription error {e.code}",
                "detail": e.read().decode("utf-8", errors="replace")}
    except urllib.error.URLError as e:
        return {"error": f"network error: {e.reason}"}


def openai_tts(text: str, voice: str = TTS_VOICE):
    """Text-to-speech via OpenAI audio speech. Returns (audio_bytes, error)."""
    if not OPENAI_API_KEY:
        return None, {"error": "OPENAI_API_KEY not set on the server."}
    payload = {
        "model": TTS_MODEL,
        "voice": voice or TTS_VOICE,
        "input": text[:4000],
        "response_format": "mp3",
    }
    req = urllib.request.Request(
        OPENAI_TTS_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read(), None
    except urllib.error.HTTPError as e:
        return None, {"error": f"OpenAI TTS error {e.code}",
                      "detail": e.read().decode("utf-8", errors="replace")}
    except urllib.error.URLError as e:
        return None, {"error": f"network error: {e.reason}"}


# ---------- Game of Stocks (gamification: tokens, streaks, daily rewards) ----------
# A light, opt-in engagement layer. Per-user game state lives inside the user
# record (users.json -> user["game"]); no new store, no extra deps.
GAME_DAILY_BASE = 50

# Ambient competitors so the leaderboard always feels alive even with few real
# players. Stable, playful house bots — clearly not real accounts.
_GAME_NPCS = [
    {"name": "DiamondHands",  "tokens": 4200, "streak": 14},
    {"name": "BullRunBella",  "tokens": 3650, "streak": 9},
    {"name": "TrendSurfer",   "tokens": 2975, "streak": 21},
    {"name": "VolatilityVic", "tokens": 2110, "streak": 5},
    {"name": "SteadyEddie",   "tokens": 1485, "streak": 33},
    {"name": "MoonMike",      "tokens": 990,  "streak": 3},
    {"name": "PaperTrader7",  "tokens": 545,  "streak": 2},
]


def _today_utc() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _yesterday_utc() -> str:
    return (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")


def _game_default() -> dict:
    return {"tokens": 0, "streak": 0, "best_streak": 0, "last_claim": "", "claims": 0}


def _game_of(rec) -> dict:
    g = _game_default()
    if isinstance(rec, dict) and isinstance(rec.get("game"), dict):
        g.update(rec["game"])
    return g


def _game_level(tokens: int) -> dict:
    """Levels cost 200, 300, 400, ... tokens (cumulative). Returns progress."""
    lvl, floor, need = 1, 0, 200
    while tokens >= floor + need:
        floor += need
        lvl += 1
        need += 100
    return {"level": lvl, "into": tokens - floor, "span": need,
            "floor": floor, "next": floor + need}


def _daily_reward(streak: int) -> int:
    """Reward for a claim landing on `streak` (post-increment). 50 + 15/day, capped."""
    return GAME_DAILY_BASE + min(max(streak - 1, 0), 9) * 15


def _game_leaderboard(current_username: str) -> list:
    rows = [{"name": n["name"], "tokens": n["tokens"], "streak": n["streak"], "npc": True}
            for n in _GAME_NPCS]
    seen_you = False
    for uname, rec in load_users().items():
        g = rec.get("game") if isinstance(rec, dict) else None
        if isinstance(g, dict) and (g.get("claims") or g.get("tokens")):
            you = (uname == current_username)
            seen_you = seen_you or you
            rows.append({"name": uname, "tokens": int(g.get("tokens") or 0),
                         "streak": int(g.get("streak") or 0), "npc": False, "you": you})
    if current_username and not seen_you:
        cu = _game_of(load_users().get(current_username, {}))
        rows.append({"name": current_username, "tokens": int(cu["tokens"]),
                     "streak": int(cu["streak"]), "npc": False, "you": True})
    rows.sort(key=lambda r: (-r["tokens"], r["name"].lower()))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def _game_state_for(username: str) -> dict:
    g = _game_of(load_users().get(username, {}))
    today = _today_utc()
    next_streak = g["streak"] + 1 if g.get("last_claim") == _yesterday_utc() else 1
    board = _game_leaderboard(username)
    rank = next((r["rank"] for r in board if r.get("you")), len(board))
    return {
        "tokens": int(g["tokens"]), "streak": int(g["streak"]),
        "best_streak": int(g["best_streak"]), "claims": int(g["claims"]),
        "last_claim": g["last_claim"], "claimable": g.get("last_claim") != today,
        "reward_preview": _daily_reward(next_streak),
        "level": _game_level(int(g["tokens"])), "rank": rank, "players": len(board),
    }


def _game_claim(username: str) -> dict:
    users = load_users()
    rec = users.get(username)
    if not rec:
        return {"error": "Not logged in."}
    g = _game_of(rec)
    if g.get("last_claim") == _today_utc():
        return {"error": "Already claimed today.", "already": True}
    new_streak = g["streak"] + 1 if g.get("last_claim") == _yesterday_utc() else 1
    reward = _daily_reward(new_streak)
    g["tokens"] = int(g["tokens"]) + reward
    g["streak"] = new_streak
    g["best_streak"] = max(int(g["best_streak"]), new_streak)
    g["last_claim"] = _today_utc()
    g["claims"] = int(g["claims"]) + 1
    rec["game"] = g
    users[username] = rec
    save_users(users)
    state = _game_state_for(username)
    return {"ok": True, "reward": reward, "streak": new_streak, **state}


# ---------- Windows package (download) ----------
WIN_BAT = """@echo off
cd /d \"%~dp0\"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 is required. Install it from https://www.python.org/downloads/ ^(tick \"Add Python to PATH\"^), then run this again.
  pause
  exit /b 1
)
if not defined OPENAI_API_KEY set /p OPENAI_API_KEY=\"Paste your OpenAI API key (sk-...): \"
echo.
echo Starting FAAM at http://localhost:8765 ...
start \"\" http://localhost:8765/login
python app.py
pause
"""

WIN_README = """FAAM for Windows
================

QUICK START
1) Install Python 3.9+ from https://www.python.org/downloads/
   (tick "Add Python to PATH" during setup).
2) Double-click  Start FAAM.bat
3) Paste your OpenAI API key when prompted.
4) Your browser opens to FAAM — create an account or sign in, and you're in.

FAAM runs locally on your PC and you use it right in your browser.

LINUX: grab the Linux package at /download/linux (or just run python3 app.py).

NOT FINANCIAL ADVICE. FAAM never places trades — it prepares orders for you to review.
"""


def build_win_zip() -> bytes:
    """A Windows-ready FAAM package: the Python app + a double-click .bat launcher."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("app.py", "README.md"):
            p = ROOT / name
            if p.exists():
                zf.writestr(f"FAAM/{name}", p.read_bytes())
        if STATIC.exists():
            for p in STATIC.rglob("*"):
                if p.is_file():
                    zf.writestr(f"FAAM/static/{p.relative_to(STATIC)}", p.read_bytes())
        adv = ROOT / "advisers"
        if adv.exists():
            for p in adv.glob("*.md"):
                zf.writestr(f"FAAM/advisers/{p.name}", p.read_bytes())
        # Files for building the packaged FAAM.exe (its own window via WebView2).
        for name in ("winshell.py", "build_windows.bat", "FAAM.ico", "BUILD-WINDOWS.md"):
            p = ROOT / name
            if p.exists():
                zf.writestr(f"FAAM/{name}", p.read_bytes())
        # .bat / .txt want CRLF line endings on Windows.
        zf.writestr("FAAM/Start FAAM.bat", WIN_BAT.replace("\n", "\r\n").encode("utf-8"))
        zf.writestr("FAAM/README.txt", WIN_README.replace("\n", "\r\n").encode("utf-8"))
    return buf.getvalue()


# ---------- Linux package (pure-Python, zero install) ----------
# Linux ships with Python, so FAAM needs nothing extra: a launcher script starts
# the local server and opens your browser; an optional installer adds a menu entry.
LINUX_RUN_SH = """#!/usr/bin/env bash
# FAAM for Linux — runs the local server and opens FAAM in your browser.
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install it with your package manager, e.g.:"
  echo "  Debian/Ubuntu:  sudo apt install python3"
  echo "  Fedora:         sudo dnf install python3"
  echo "  Arch:           sudo pacman -S python"
  exit 1
fi
# OpenAI key (optional): from the environment, a previous run, or a prompt.
if [ -z "$OPENAI_API_KEY" ] && [ -f "$HOME/.faam/key" ]; then
  OPENAI_API_KEY="$(cat "$HOME/.faam/key")"; export OPENAI_API_KEY
fi
if [ -z "$OPENAI_API_KEY" ]; then
  printf 'Paste your OpenAI API key (sk-...), or press Enter to skip: '
  read -r KEY
  if [ -n "$KEY" ]; then
    mkdir -p "$HOME/.faam"; printf '%s' "$KEY" > "$HOME/.faam/key"
    chmod 600 "$HOME/.faam/key"; export OPENAI_API_KEY="$KEY"
  fi
fi
PORT="${FAAM_PORT:-8765}"; export FAAM_PORT="$PORT"
URL="http://localhost:$PORT/login"
# Open the browser once the server answers, in the background.
( for _ in $(seq 1 40); do
    if command -v curl >/dev/null 2>&1; then
      curl -fsS "http://localhost:$PORT/api/health" >/dev/null 2>&1 && break
    elif command -v wget >/dev/null 2>&1; then
      wget -qO- "http://localhost:$PORT/api/health" >/dev/null 2>&1 && break
    else
      sleep 1.5; break
    fi
    sleep 0.4
  done
  (xdg-open "$URL" >/dev/null 2>&1 || sensible-browser "$URL" >/dev/null 2>&1 \\
    || x-www-browser "$URL" >/dev/null 2>&1 || true) ) &
echo "Starting FAAM at $URL   (press Ctrl+C to stop)"
exec python3 app.py
"""

LINUX_DESKTOP = """[Desktop Entry]
Type=Application
Name=FAAM
GenericName=Financial AI Agent Manager
Comment=Your local financial AI dashboard
Exec=@APP@/run-faam.sh
Icon=@APP@/faam.svg
Terminal=true
Categories=Office;Finance;
"""

LINUX_INSTALL_SH = """#!/usr/bin/env bash
# Optional: add FAAM to your applications menu and a 'faam' command.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.local/share/faam"
mkdir -p "$APP" "$HOME/.local/share/applications" "$HOME/.local/bin"
cp -R "$SRC/." "$APP/"
chmod +x "$APP/run-faam.sh"
sed "s#@APP@#$APP#g" "$SRC/FAAM.desktop" > "$HOME/.local/share/applications/faam.desktop"
ln -sf "$APP/run-faam.sh" "$HOME/.local/bin/faam"
command -v update-desktop-database >/dev/null 2>&1 && \\
  update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
echo "FAAM installed."
echo "  • Launch it from your apps menu (search 'FAAM'), or"
echo "  • run:  faam"
echo "If 'faam' isn't found, add this to your shell profile:"
echo '  export PATH="$HOME/.local/bin:$PATH"'
"""

LINUX_README = """FAAM for Linux
==============

QUICK START
  1) Check Python 3 is installed:   python3 --version
  2) From this folder, run:         ./run-faam.sh
        (or, if it isn't executable:  bash run-faam.sh)
  3) Paste your OpenAI API key if asked — optional, stored at ~/.faam/key.
  4) Your browser opens to FAAM. Create an account or sign in, and you're in.

ADD TO YOUR APPS MENU (optional)
  ./install.sh
  Then launch "FAAM" from your applications menu, or run:  faam

FAAM is pure Python and runs entirely on your machine — nothing to install
beyond Python 3. It never places trades; it prepares orders for you to review.
Not financial advice.
"""


def build_linux_tar() -> bytes:
    """A Linux-ready FAAM package (tar.gz): the Python app + a launcher script."""
    buf = io.BytesIO()
    now = int(time.time())

    def add(tf: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
        info = tarfile.TarInfo(f"FAAM/{name}")
        info.size = len(data)
        info.mode = mode
        info.mtime = now
        tf.addfile(info, io.BytesIO(data))

    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("app.py", "README.md"):
            p = ROOT / name
            if p.exists():
                add(tf, name, p.read_bytes())
        if STATIC.exists():
            for p in STATIC.rglob("*"):
                if p.is_file():
                    add(tf, f"static/{p.relative_to(STATIC)}", p.read_bytes())
        adv = ROOT / "advisers"
        if adv.exists():
            for p in adv.glob("*.md"):
                add(tf, f"advisers/{p.name}", p.read_bytes())
        fav = STATIC / "favicon.svg"
        if fav.exists():
            add(tf, "faam.svg", fav.read_bytes())
        add(tf, "run-faam.sh", LINUX_RUN_SH.encode("utf-8"), 0o755)
        add(tf, "install.sh", LINUX_INSTALL_SH.encode("utf-8"), 0o755)
        add(tf, "FAAM.desktop", LINUX_DESKTOP.encode("utf-8"))
        add(tf, "README.txt", LINUX_README.encode("utf-8"))
    return buf.getvalue()


# ---------- Natural-language order parsing (AI fills the investing form) ----------
# Common company / asset names → US tickers, so "buy $500 of apple" resolves with
# no AI call. When an OpenAI key is set, the endpoint also falls back to the model.
COMPANY_TICKERS = {
    "apple": "AAPL", "microsoft": "MSFT", "tesla": "TSLA", "nvidia": "NVDA",
    "amazon": "AMZN", "google": "GOOGL", "alphabet": "GOOGL", "meta": "META",
    "facebook": "META", "netflix": "NFLX", "disney": "DIS", "amd": "AMD",
    "intel": "INTC", "spotify": "SPOT", "coinbase": "COIN", "ford": "F",
    "boeing": "BA", "walmart": "WMT", "nike": "NKE", "starbucks": "SBUX",
    "mcdonalds": "MCD", "jpmorgan": "JPM", "visa": "V", "mastercard": "MA",
    "palantir": "PLTR", "bitcoin": "BTC-USD", "ethereum": "ETH-USD",
    "s&p": "SPY", "sp500": "SPY", "nasdaq": "QQQ", "berkshire": "BRK-B",
}
_ORDER_STOP = {"BUY", "SELL", "SHORT", "USD", "THE", "OF", "FOR", "AND", "AT",
               "ALL", "NEW", "ETF", "DCA", "IRA", "BUCKS", "SHARE", "SHARES"}


def parse_order_text(text: str) -> dict:
    """Heuristic parse of a plain-English order into {side, mode, qty, symbol}.

    Deterministic and key-free; handles e.g. "buy $500 of Apple", "sell 10 TSLA",
    "buy 5 shares of nvidia". Returns symbol=None when it can't tell.
    """
    raw = text or ""
    t = raw.lower()
    side = "sell" if re.search(r"\b(sell|short|dump|trim)\b", t) else "buy"

    mode, qty = "shares", None
    m_amt = re.search(r"\$\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:dollars|usd|bucks)\b", t)
    m_sh = re.search(r"([\d,]+(?:\.\d+)?)\s*shares?\b", t)
    if m_sh:
        mode, qty = "shares", float(m_sh.group(1).replace(",", ""))
    elif m_amt:
        amt = m_amt.group(1) or m_amt.group(2)
        mode, qty = "dollars", float(amt.replace(",", ""))
    else:
        m_num = re.search(r"\b([\d,]+(?:\.\d+)?)\b", t)
        if m_num:
            mode, qty = "shares", float(m_num.group(1).replace(",", ""))

    sym = None
    for tok in re.findall(r"\b[A-Z]{2,5}(?:-[A-Z]{1,4})?\b", raw):
        if tok not in _ORDER_STOP:
            sym = tok
            break
    if not sym:
        for name, tk in COMPANY_TICKERS.items():
            if name in t:
                sym = tk
                break
    return {"side": side, "mode": mode, "qty": qty, "symbol": sym}


# ---------- Dashboard layout (Default · custom · AI-designed) ----------
# The dashboard is composed of toggleable widgets and three reorderable blocks.
# The AI (or a keyword heuristic) returns a layout the browser then applies.
DASH_TOGGLES = ("watchlist", "insights", "kpis", "portfolio")
DASH_BLOCKS = ("watchlist", "chart", "portfolio")


def dash_default_layout() -> dict:
    return {"order": list(DASH_BLOCKS), "widgets": {w: True for w in DASH_TOGGLES}}


def dash_sanitize_layout(obj) -> dict:
    """Coerce any model/JSON output into a safe, complete layout."""
    out = dash_default_layout()
    if isinstance(obj, dict):
        w = obj.get("widgets")
        if isinstance(w, dict):
            for k in DASH_TOGGLES:
                if k in w:
                    out["widgets"][k] = bool(w[k])
        order = obj.get("order")
        if isinstance(order, list):
            uniq = []
            for x in order:
                if x in DASH_BLOCKS and x not in uniq:
                    uniq.append(x)
            for x in DASH_BLOCKS:
                if x not in uniq:
                    uniq.append(x)
            out["order"] = uniq
    return out


def dash_layout_heuristic(prompt: str) -> dict:
    """Key-free fallback: pick a sensible layout from keywords in the request."""
    t = (prompt or "").lower()
    L = dash_default_layout()

    def hide(target: str) -> bool:
        return bool(re.search(r"\b(no|hide|without|drop|remove|skip|don'?t want)\b[^.]*" + target, t))

    minimal = bool(re.search(r"\b(minimal|clean|simple|declutter|distraction[- ]?free|zen|just the)\b", t))
    trader = bool(re.search(r"\b(day ?trad|trader|technical|candle|scalp|swing|active trad)\b", t))
    longterm = bool(re.search(r"\b(long.?term|buy.?and.?hold|dividend|retire|net ?worth)\b", t))
    portfolio_first = longterm or bool(re.search(r"portfolio (first|on top|at the top)", t))
    portfolio_pos = portfolio_first or bool(re.search(r"\bportfolio\b", t))
    watchlist_first = bool(re.search(r"watch.?list (first|on top|at the top)", t)) or \
        (bool(re.search(r"\bwatch.?list\b", t)) and not trader)

    # Ordering (the chart is the anchor and is always shown).
    if trader:
        L["order"] = ["chart", "watchlist", "portfolio"]
    elif portfolio_first and not hide("portfolio"):
        L["order"] = ["portfolio", "chart", "watchlist"]
    elif watchlist_first:
        L["order"] = ["watchlist", "chart", "portfolio"]

    # Visibility — minimal trims the extras, but won't hide a panel the user asked for.
    if minimal:
        L["widgets"].update(insights=False, kpis=False)
        if not portfolio_pos:
            L["widgets"]["portfolio"] = False

    # Explicit removals always win.
    if hide("portfolio"):
        L["widgets"]["portfolio"] = False
    if hide("watch"):
        L["widgets"]["watchlist"] = False
    if hide(r"(insight|agent|\bai\b)"):
        L["widgets"]["insights"] = False
    if hide(r"(stat|kpi|metric)"):
        L["widgets"]["kpis"] = False
    return L


def dash_design_layout(prompt: str) -> dict:
    """Design a layout from a prompt — GPT-4.1 mini when a key is set, else heuristic."""
    if OPENAI_API_KEY:
        ai = openai_chat(
            [{"role": "user", "content":
              f'Design a dashboard for this user: "{prompt}". '
              'Reply with ONLY compact JSON: '
              '{"order":[...],"widgets":{"watchlist":bool,"insights":bool,"kpis":bool,"portfolio":bool}}. '
              '"order" is a permutation of ["watchlist","chart","portfolio"] (top to bottom). '
              '"chart" is the price chart (always shown). "insights" = AI agent panel beside the chart, '
              '"kpis" = day-stats row, "portfolio" = holdings table, "watchlist" = ticker rail.'}],
            system="You configure a financial dashboard layout. Output strict JSON only, no prose.",
        )
        try:
            j = json.loads(re.search(r"\{.*\}", extract_text(ai), re.S).group(0))
            out = dash_sanitize_layout(j)
            out["source"] = "ai"
            return out
        except Exception:  # noqa: BLE001
            pass
    out = dash_layout_heuristic(prompt)
    out["source"] = "heuristic"
    return out


# ---------------------------------------------------------------------------
# Human verification ("are you a robot?") — a self-contained, no-dependency
# CAPTCHA. The server renders a short distorted-text challenge as SVG, keeps the
# answer in memory (never sent to the client), and verifies it on signup/login.
# Admin/dev accounts are exempt (see is_admin_username). Enforced server-side.
# ---------------------------------------------------------------------------
CAPTCHA_TTL = 300                 # a challenge is valid for 5 minutes
CAPTCHA_LEN = 5
# Skip look-alike characters so the challenge stays readable (no O/0, I/1/L).
_CAPTCHA_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CAPTCHA_STORE: dict[str, dict] = {}   # id -> {"answer": str, "exp": float}
_CAPTCHA_LOCK = threading.Lock()


def _captcha_sweep(now: float) -> None:
    """Drop expired challenges so the in-memory store can't grow unbounded."""
    dead = [k for k, v in _CAPTCHA_STORE.items() if v["exp"] < now]
    for k in dead:
        _CAPTCHA_STORE.pop(k, None)


def _captcha_svg(code: str) -> str:
    """Render the code as a noisy, jittered SVG so humans read it but naive
    scrapers/bots don't get it for free from the page source."""
    w, h = 200, 70
    palette = ["#2E64F0", "#6E56CF", "#0B1220", "#B42318", "#027A48"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" role="img" aria-label="verification challenge">',
        f'<rect width="{w}" height="{h}" rx="10" fill="#F1F3F7"/>',
    ]
    # background noise lines
    for _ in range(5):
        x1, y1, x2, y2 = (random.randint(0, w), random.randint(0, h),
                          random.randint(0, w), random.randint(0, h))
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                     f'stroke="{random.choice(palette)}" stroke-width="1" opacity="0.18"/>')
    # the characters, each jittered and rotated
    step = (w - 36) / len(code)
    for i, ch in enumerate(code):
        cx = 26 + i * step
        cy = 46 + random.randint(-6, 6)
        rot = random.randint(-26, 26)
        size = random.randint(30, 38)
        parts.append(
            f'<text x="{cx:.0f}" y="{cy}" font-family="Inter,Arial,sans-serif" '
            f'font-size="{size}" font-weight="800" fill="{random.choice(palette)}" '
            f'transform="rotate({rot} {cx:.0f} {cy})">{ch}</text>')
    # speckle dots
    for _ in range(36):
        parts.append(f'<circle cx="{random.randint(0, w)}" cy="{random.randint(0, h)}" '
                     f'r="1.2" fill="{random.choice(palette)}" opacity="0.22"/>')
    parts.append("</svg>")
    return "".join(parts)


def make_captcha() -> dict:
    """Create a new challenge; return its id + SVG. The answer stays server-side."""
    code = "".join(secrets.choice(_CAPTCHA_ALPHABET) for _ in range(CAPTCHA_LEN))
    cid = secrets.token_urlsafe(16)
    now = time.time()
    with _CAPTCHA_LOCK:
        _captcha_sweep(now)
        _CAPTCHA_STORE[cid] = {"answer": code, "exp": now + CAPTCHA_TTL}
    return {"id": cid, "svg": _captcha_svg(code)}


def verify_captcha(cid: str, answer: str) -> bool:
    """One-time check of a challenge answer (case-insensitive)."""
    if not cid or not answer:
        return False
    now = time.time()
    with _CAPTCHA_LOCK:
        _captcha_sweep(now)
        rec = _CAPTCHA_STORE.pop(cid, None)   # one-shot: consume on any attempt
    if not rec or rec["exp"] < now:
        return False
    return hmac.compare_digest(rec["answer"].upper(), str(answer).strip().upper())


def is_admin_username(username: str) -> bool:
    """The dev/admin account is exempt from the human-verification gate."""
    if not username:
        return False
    u = load_users().get(username.strip().lower())
    return bool(u and u.get("admin"))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    # Defense-in-depth headers on every response (JSON, static, redirects).
    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        super().end_headers()

    def _json(self, obj, status: int = 200, set_cookie: str | None = None) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, set_cookie: str | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()

    def _cookie(self, name: str):
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == name:
                    return v
        return None

    def _current_user(self):
        token = self._cookie("faam_session")
        if not token:
            return None
        username = read_session(token)
        if not username:
            return None
        u = load_users().get(username)
        return {"username": username, **u} if u else None

    def _apply_user_context(self):
        u = self._current_user()
        # In beta everyone is effectively top-tier, so usage caps don't apply.
        tier = 4 if BETA else int((u or {}).get("tier") or 0)
        _set_req(u["username"] if u else "anon", tier)
        return u

    def _session_cookie(self, token: str = "", clear: bool = False) -> str:
        # Mark the cookie Secure on HTTPS deployments so it never travels over
        # plain HTTP (localhost stays non-Secure so dev still works).
        secure = "; Secure" if BASE_URL.startswith("https") else ""
        if clear:
            return f"faam_session=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax{secure}"
        return f"faam_session={token}; HttpOnly; Path=/; Max-Age={SESSION_TTL}; SameSite=Lax{secure}"

    def _google_callback(self):
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [""])[0]
        if not code or not verify_state(state):
            return self._redirect("/login?g=failed")
        cid, csec = google_creds()
        if not cid:
            return self._redirect("/login?g=unconfigured")
        redirect_uri = f"{BASE_URL}/auth/google/callback"
        try:
            data = urllib.parse.urlencode({
                "code": code, "client_id": cid, "client_secret": csec,
                "redirect_uri": redirect_uri, "grant_type": "authorization_code",
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://oauth2.googleapis.com/token", data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                tok = json.loads(r.read())
            access = tok.get("access_token")
            if not access:
                return self._redirect("/login?g=failed")
            ureq = urllib.request.Request(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access}"})
            with urllib.request.urlopen(ureq, timeout=20) as r:
                info = json.loads(r.read())
        except Exception:  # noqa: BLE001
            return self._redirect("/login?g=failed")
        email = (info.get("email") or "").strip().lower()
        if not email:
            return self._redirect("/login?g=failed")
        users = load_users()
        if email not in users:
            users[email] = {
                "pw": None, "tier": 0, "plan": "", "admin": False,
                "email": email, "name": info.get("name", ""), "provider": "google",
                "created": datetime.now().isoformat(),
            }
            save_users(users)
        token = make_session(email)
        return self._redirect("/dashboard", set_cookie=self._session_cookie(token))

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}
        if length <= 0 or length > MAX_BODY_BYTES:   # reject empty / oversized bodies
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    def _send_file(self, file_path: Path, content_type: str | None = None) -> None:
        if not file_path.exists():
            return self._json({"error": "not found"}, 404)
        data = file_path.read_bytes()
        ct = content_type or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_404(self) -> None:
        """Serve the styled FAAM 404 page (falls back to JSON if missing)."""
        page = STATIC / "404.html"
        if page.exists():
            data = page.read_bytes()
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except BrokenPipeError:
                pass
            return
        self._json({"error": "not found"}, 404)

    def _serve_download(self) -> None:
        """Build a macOS FAAM.app bundle on the fly and stream it as FAAM.zip."""
        # Read the source files we'll embed
        src_files: dict[str, bytes] = {}
        for name in ("app.py", "README.md", "faamview.swift", "AppIcon.icns"):
            p = ROOT / name
            if p.exists():
                src_files[name] = p.read_bytes()
        static_files: dict[str, bytes] = {}
        if STATIC.exists():
            for p in STATIC.rglob("*"):
                if p.is_file():
                    static_files[str(p.relative_to(STATIC))] = p.read_bytes()
        adviser_files: dict[str, bytes] = {}
        adv_dir = ROOT / "advisers"
        if adv_dir.exists():
            for p in adv_dir.glob("*.md"):
                adviser_files[p.name] = p.read_bytes()

        # Launcher: opens FAAM as a native window (compiles a tiny WKWebView app on
        # first run), falling back to a chromeless Chrome window, then the browser.
        launcher = r"""#!/bin/bash
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
MACOS="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
mkdir -p "$HOME/.faam"

if [ -f "$DIR/key" ] && [ ! -f "$HOME/.faam/key" ]; then
  cp "$DIR/key" "$HOME/.faam/key"; chmod 600 "$HOME/.faam/key"
fi
if ! command -v python3 >/dev/null 2>&1; then
  /usr/bin/osascript -e 'display alert "FAAM needs Python 3" message "Run: xcode-select --install, then open FAAM again."'
  exit 1
fi

# --- Native window (preferred) ---
SWIFT_SRC="$DIR/faamview.swift"
NATIVE="$MACOS/faamview"
if [ -f "$SWIFT_SRC" ] && command -v swiftc >/dev/null 2>&1; then
  if [ ! -x "$NATIVE" ] || [ "$SWIFT_SRC" -nt "$NATIVE" ]; then
    swiftc -O -framework Cocoa -framework WebKit "$SWIFT_SRC" -o "$NATIVE" 2>>"$HOME/.faam/run.log" || true
  fi
fi
if [ -x "$NATIVE" ]; then
  exec "$NATIVE" "$DIR"
fi

# --- Fallback: server + app-style window ---
if [ -z "$OPENAI_API_KEY" ] && [ -f "$HOME/.faam/key" ]; then
  OPENAI_API_KEY="$(cat "$HOME/.faam/key")"; export OPENAI_API_KEY
fi
if [ -z "$OPENAI_API_KEY" ]; then
  KEY=$(/usr/bin/osascript <<'OSA'
try
  set dlg to display dialog "Welcome to FAAM.

Paste your OpenAI API key (sk-...). Stored only on this Mac at ~/.faam/key." default answer "" with hidden answer with title "FAAM" buttons {"Cancel","Start"} default button "Start"
  return text returned of dlg
on error
  return ""
end try
OSA
)
  if [ -z "$KEY" ]; then exit 0; fi
  printf '%s' "$KEY" > "$HOME/.faam/key"; chmod 600 "$HOME/.faam/key"; export OPENAI_API_KEY="$KEY"
fi
if [ -f "$HOME/.faam/server.pid" ]; then
  OLDPID="$(cat "$HOME/.faam/server.pid" 2>/dev/null)"
  if [ -n "$OLDPID" ] && /bin/kill -0 "$OLDPID" 2>/dev/null; then /bin/kill "$OLDPID" 2>/dev/null; sleep 0.5; fi
fi
PORT=8765
while /usr/sbin/lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; do
  PORT=$((PORT + 1)); [ "$PORT" -gt 8800 ] && break
done
export FAAM_PORT=$PORT
URL="http://localhost:$PORT/login"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ -x "$CHROME" ]; then
  ( sleep 1.2 && "$CHROME" --app="$URL" --user-data-dir="$HOME/.faam/appwin" --window-size=1320,880 --no-first-run --no-default-browser-check >/dev/null 2>&1 ) &
else
  ( sleep 1.2 && /usr/bin/open "$URL" ) &
fi
echo $$ > "$HOME/.faam/server.pid"
exec /usr/bin/env python3 app.py
"""

        plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>FAAM</string>
  <key>CFBundleDisplayName</key><string>FAAM</string>
  <key>CFBundleIdentifier</key><string>com.faam.dashboard</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>FAAM</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleSignature</key><string>????</string>
  <key>LSMinimumSystemVersion</key><string>10.12</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>FAAM uses your microphone for voice mode.</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
"""

        readme = """FAAM — Financial AI Agent Manager
===================================

QUICK START
1) Drag FAAM.app into your Applications folder.
2) Double-click FAAM.app to launch.
3) On first run you'll be prompted to paste your OpenAI API key.
   The key is stored only on this Mac (~/.faam/key, mode 600).
4) Your browser will open to the dashboard automatically.

IF macOS BLOCKS THE APP
You may see: "FAAM can't be opened because it is from an unidentified
developer." This happens for any unsigned app. To open it once:
  - Right-click (Control-click) FAAM.app -> Open -> Open in the dialog.
  - Or: System Settings -> Privacy & Security -> "Open Anyway".

REQUIREMENTS
  - macOS 10.12 or later
  - Python 3.9+ (preinstalled on modern macOS)
  - An OpenAI API key (https://platform.openai.com/api-keys)

CHANGING YOUR API KEY
  Edit or delete the file:  ~/.faam/key
  FAAM will prompt for a new key on the next launch.

CUSTOMIZING THE WATCHLIST
  Use the + Add card on the rail (or the Add to watchlist button).
  Saved to ~/.faam/watchlist.json.

FINANCIAL ADVISER PROFILE
  Click the folder icon in the top bar to give FAAM's AI a persona and
  house rules. Upload a .md / .txt file or use the built-in template.
  Ready-made profiles ship in this app under:
    FAAM.app/Contents/Resources/advisers/
  Your active profile is saved to ~/.faam/adviser.md (delete to reset).

NOT FINANCIAL ADVICE.
"""

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            def add(arc: str, data: bytes, mode: int = 0o644) -> None:
                info = zipfile.ZipInfo(arc)
                # Preserve unix file mode (critical for the +x launcher).
                info.external_attr = (mode & 0xFFFF) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, data)

            add("README.txt", readme.encode("utf-8"))
            add("FAAM.app/Contents/Info.plist", plist.encode("utf-8"))
            add("FAAM.app/Contents/MacOS/FAAM", launcher.encode("utf-8"), mode=0o755)
            for name, data in src_files.items():
                add(f"FAAM.app/Contents/Resources/{name}", data)
            for rel, data in static_files.items():
                add(f"FAAM.app/Contents/Resources/static/{rel}", data)
            for name, data in adviser_files.items():
                add(f"FAAM.app/Contents/Resources/advisers/{name}", data)

        body = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="FAAM.zip"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        user = self._apply_user_context()

        # Pretty routes
        if path in ("/login", "/signup"):
            return self._send_file(STATIC / "login.html", "text/html; charset=utf-8")
        if path == "/browserversion":
            # Friendly entry point for "open FAAM in the browser": land logged-in
            # users on the dashboard, everyone else on the login screen.
            return self._redirect("/dashboard" if user else "/login")
        if path == "/terms":
            return self._send_file(STATIC / "terms.html", "text/html; charset=utf-8")
        if path == "/privacy":
            return self._send_file(STATIC / "privacy.html", "text/html; charset=utf-8")
        if path == "/dashboard":
            if not user:
                return self._redirect("/login")
            return self._send_file(STATIC / "dashboard.html", "text/html; charset=utf-8")
        if path == "/download":
            return self._serve_download()
        if path == "/download/windows":
            body = build_win_zip()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="FAAM-windows.zip"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/download/linux":
            body = build_linux_tar()
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition", 'attachment; filename="FAAM-linux.tar.gz"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/me":
            if not user:
                return self._json({"auth": False})
            return self._json({
                "auth": True, "username": user["username"],
                "tier": int(user.get("tier") or 0), "plan": user.get("plan", ""),
                "admin": bool(user.get("admin")), "email": user.get("email", ""),
                "provider": user.get("provider", "local"),
            })

        if path == "/api/game":
            if not user:
                return self._json({"auth": False})
            return self._json({"auth": True, **_game_state_for(user["username"])})

        if path == "/api/game/leaderboard":
            return self._json({"leaderboard": _game_leaderboard(user["username"] if user else "")})

        if path == "/auth/google/start":
            cid, _ = google_creds()
            if not cid:
                return self._redirect("/login?g=unconfigured")
            redirect_uri = f"{BASE_URL}/auth/google/callback"
            state = sign_state(f"g{int(time.time())}")
            q = urllib.parse.urlencode({
                "client_id": cid, "redirect_uri": redirect_uri, "response_type": "code",
                "scope": "openid email profile", "access_type": "online",
                "include_granted_scopes": "true", "state": state, "prompt": "select_account",
            })
            return self._redirect("https://accounts.google.com/o/oauth2/v2/auth?" + q)

        if path == "/auth/google/callback":
            return self._google_callback()

        if path == "/api/version":
            # Clients poll this; a changed value means a new build is live.
            return self._json({"version": APP_VERSION, "release": CHANGELOG_VERSION})

        if path == "/api/changelog":
            return self._json({
                "version": CHANGELOG_VERSION,
                "releases": CHANGELOG,
                "coming": ROADMAP,
            })

        if path == "/api/captcha":
            # A fresh human-verification challenge (id + distorted SVG).
            return self._json(make_captcha())

        if path == "/api/health":
            return self._json({
                "ok": True,
                "model": OPENAI_MODEL,
                "provider": "openai",
                "ai_enabled": bool(OPENAI_API_KEY),
                "adviser_loaded": bool(load_adviser()),
                "voice_enabled": bool(OPENAI_API_KEY),
                "tts_voice": TTS_VOICE,
            })

        if path == "/api/adviser":
            return self._json({"text": load_adviser()})

        if path == "/api/broker":
            return self._json(load_broker())

        if path == "/api/pro":
            u = user or {}
            plan = u.get("plan", "")
            real_tier = int(u.get("tier") or 0)
            return self._json({
                "plan": plan,
                "tier": 4 if BETA else real_tier,        # beta unlocks everything
                "realTier": real_tier,
                "beta": BETA,
                "planName": "Beta — all access" if BETA else PLANS.get(plan, {}).get("name", ""),
                "configured": bool(stripe_key()),
                "plans": [{"id": k, **v} for k, v in PLANS.items()],
                "features": FEATURE_MIN_TIER,
            })

        if path == "/pro/success":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            sid = (qs.get("session_id") or [""])[0]
            ok = False
            plan_name = "Pro"
            if sid:
                res, err = stripe_request("GET", f"/v1/checkout/sessions/{urllib.parse.quote(sid)}")
                if res and (res.get("status") == "complete" or res.get("payment_status") == "paid"):
                    plan = ((res.get("metadata") or {}).get("plan") or "").lower()
                    if plan in PLANS and user:
                        set_user_plan(user["username"], plan)
                        plan_name = PLANS[plan]["name"]
                        ok = True
            title = f"Welcome to FAAM {plan_name} 🎉" if ok else "Couldn't confirm payment"
            body = (
                "Your subscription is active. You can close this tab and return to FAAM."
                if ok else
                "We couldn't verify the checkout session. If you were charged, reopen FAAM and try again."
            )
            html = (
                "<!doctype html><meta charset='utf-8'>"
                "<title>FAAM Pro</title>"
                "<style>body{font-family:-apple-system,Inter,sans-serif;background:#0a0e16;color:#e7ecf3;"
                "display:grid;place-items:center;height:100vh;margin:0;text-align:center}"
                ".card{max-width:460px;padding:40px;background:#111826;border:1px solid #1f2a3d;border-radius:16px}"
                "a{display:inline-block;margin-top:18px;background:#635bff;color:#fff;padding:11px 20px;"
                "border-radius:10px;text-decoration:none;font-weight:600}h1{font-size:24px;margin:0 0 10px}"
                "p{color:#8a97ad;line-height:1.6}</style>"
                f"<div class='card'><h1>{title}</h1><p>{body}</p>"
                f"<a href='{BASE_URL}/dashboard'>Back to FAAM →</a></div>"
            )
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/api/watchlist":
            stocks = []
            for sym in load_watchlist():
                try:
                    q = yahoo_quote(sym, range_="5d", interval="60m")
                    spark = [p["c"] for p in q.get("history", [])][-30:]
                    stocks.append({
                        "symbol": q["symbol"],
                        "name": q["name"],
                        "price": q["price"],
                        "change": q["change"],
                        "pct": q["pct"],
                        "quoteType": q.get("quoteType", ""),
                        "marketState": q.get("marketState"),
                        "extPct": q.get("extPct"),
                        "extLabel": q.get("extLabel"),
                        "spark": spark,
                    })
                except Exception as e:  # noqa: BLE001
                    stocks.append({"symbol": sym, "error": str(e)})
            return self._json({"stocks": stocks})

        if path == "/api/portfolio":
            positions = load_portfolio()
            prices: dict = {}
            out = []
            total_value = 0.0
            total_cost = 0.0
            for pos in positions:
                sym = str(pos.get("symbol", "")).upper()
                if sym and sym not in prices:
                    try:
                        q = yahoo_quote(sym, range_="1d", interval="5m")
                        prices[sym] = {
                            "price": q.get("price") or 0.0,
                            "name": q.get("name") or sym,
                            "pct": q.get("pct") or 0.0,
                        }
                    except Exception:  # noqa: BLE001
                        prices[sym] = {"price": 0.0, "name": sym, "pct": 0.0}
                p = prices.get(sym, {"price": 0.0, "name": sym, "pct": 0.0})
                shares = float(pos.get("shares") or 0)
                cost = float(pos.get("cost") or 0)
                mv = shares * p["price"]
                cb = shares * cost
                pnl = mv - cb
                total_value += mv
                total_cost += cb
                out.append({
                    "id": pos.get("id"),
                    "symbol": sym,
                    "name": p["name"],
                    "shares": shares,
                    "cost": cost,
                    "price": p["price"],
                    "dayPct": p["pct"],
                    "marketValue": mv,
                    "costBasis": cb,
                    "pnl": pnl,
                    "pnlPct": (pnl / cb * 100.0) if cb else 0.0,
                })
            total_pnl = total_value - total_cost
            return self._json({
                "positions": out,
                "totals": {
                    "value": total_value,
                    "cost": total_cost,
                    "pnl": total_pnl,
                    "pnlPct": (total_pnl / total_cost * 100.0) if total_cost else 0.0,
                },
            })

        if path == "/api/recap":
            # Builds a daily market-recap "video" payload: GPT-4.1-mini writes the
            # spoken script, the frontend renders an animated reel + TTS narration.
            if usage_blocked():
                return self._json(USAGE_LIMIT_MSG, 402)
            syms = load_watchlist()
            data = []

            def _one(s):
                try:
                    q = yahoo_quote(s, range_="5d", interval="60m")
                    if q.get("error"):
                        return None
                    spark = [p["c"] for p in q.get("history", [])][-30:]
                    return {
                        "symbol": q["symbol"], "name": q.get("name"),
                        "price": q.get("price") or 0.0, "pct": q.get("pct") or 0.0,
                        "spark": spark,
                    }
                except Exception:  # noqa: BLE001
                    return None

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                for r in ex.map(_one, syms):
                    if r:
                        data.append(r)
            if not data:
                return self._json({"error": "no market data for recap"}, 502)

            avg = sum(d["pct"] for d in data) / len(data)
            mood = "up" if avg >= 0 else "down"
            movers = sorted(data, key=lambda d: abs(d["pct"]), reverse=True)[:5]
            gainers = sorted(data, key=lambda d: d["pct"], reverse=True)[:3]
            losers = sorted(data, key=lambda d: d["pct"])[:3]
            date_str = datetime.now().strftime("%B %d, %Y").replace(" 0", " ")

            lines = "\n".join(
                f"{d['symbol']} ({d['name']}): {d['pct']:+.2f}% at ${d['price']:.2f}" for d in data
            )
            prompt = (
                f"Today is {date_str}. Watchlist performance:\n{lines}\n\n"
                f"Average move: {avg:+.2f}%. Write a punchy ~45-second spoken market-recap "
                "script for a video. Return ONLY JSON: "
                '{"headline":"short title","intro":"one spoken sentence",'
                '"movers":[{"symbol":"AAPL","line":"one punchy spoken sentence about it"}],'
                '"outro":"one spoken sentence; end with: This is information, not financial advice."} '
                "Cover the 4-5 biggest movers. Natural for text-to-speech — no symbols, no markdown."
            )
            result = openai_chat(
                [{"role": "user", "content": prompt}],
                system="You are FAAM's market anchor. Output strict JSON only — no prose, no code fences.",
            )
            if "error" not in result:
                record_cost(chat_cost(result))

            ai_ok = False
            headline, intro, outro = "FAAM Daily Recap", "", "This is information, not financial advice."
            movers_txt = []
            if "error" not in result:
                obj = None
                txt = extract_text(result).strip()
                if txt.startswith("```"):
                    txt = txt.strip("`")
                    txt = txt.split("\n", 1)[1] if "\n" in txt else txt
                try:
                    obj = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
                except Exception:  # noqa: BLE001
                    obj = None
                if isinstance(obj, dict):
                    headline = obj.get("headline") or headline
                    intro = obj.get("intro") or ""
                    movers_txt = obj.get("movers") or []
                    outro = obj.get("outro") or outro
                    ai_ok = True

            parts = [intro] if intro else [f"Here's your market recap for {date_str}."]
            for m in movers_txt:
                if isinstance(m, dict) and m.get("line"):
                    parts.append(m["line"].strip())
            parts.append(outro)
            script = " ".join(p for p in parts if p)

            comment_by = {
                (m.get("symbol") or "").upper(): (m.get("line") or "")
                for m in movers_txt if isinstance(m, dict)
            }
            slides = [{**d, "comment": comment_by.get(d["symbol"].upper(), "")} for d in movers]

            return self._json({
                "date": date_str,
                "headline": headline,
                "intro": intro,
                "outro": outro,
                "market": {"avgPct": avg, "mood": mood, "count": len(data)},
                "gainers": gainers,
                "losers": losers,
                "slides": slides,
                "script": script,
                "ai": ai_ok,
            })

        if path.startswith("/api/forecast/"):
            symbol = urllib.parse.unquote(path[len("/api/forecast/"):])
            if _req_tier() < FEATURE_MIN_TIER["forecast"]:
                return self._json(
                    {"error": "Forecasts & advanced charts are on FAAM Pro & up.",
                     "message": "Forecasts & advanced charts are on FAAM Pro & up.",
                     "upgrade": True}, 402)
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            try:
                horizon = int((qs.get("horizon") or ["30"])[0])
            except ValueError:
                horizon = 30
            model = (qs.get("model") or ["apollo"])[0]
            # Per-model tier gate (e.g. Perseverance is Elite-only) — defense in depth.
            _spec = next((m for m in FORECAST_MODELS if m["id"] == model), None)
            if _spec and _req_tier() < _spec.get("minTier", 0):
                model = "apollo"

            # Artemis & Perseverance use the LLM to score headlines, so they cost
            # usage and are metered. Apollo stays free (pure stats).
            news_payload = None
            drift_tilt = 0.0
            if model in ("artemis", "perseverance"):
                if usage_blocked():
                    return self._json(USAGE_LIMIT_MSG, 402)
                headlines = fetch_news(symbol, 8)
                if headlines:
                    sent = score_news_sentiment(symbol, symbol, headlines)
                    if not sent.get("error"):
                        record_cost(chat_cost(sent["api"]))
                        drift_tilt = sent["overall"] * NEWS_TILT_MAX
                        scores = sent.get("scores") or []
                        for i, h in enumerate(headlines):
                            h["sentiment"] = scores[i] if i < len(scores) else 0.0
                        news_payload = {
                            "overall": round(sent["overall"], 3),
                            "summary": sent.get("summary", ""),
                            "tiltPct": round((math.exp(drift_tilt) - 1.0) * 100.0, 3),
                            "headlines": headlines,
                            "count": len(headlines),
                        }
                    else:
                        news_payload = {"error": sent["error"], "headlines": headlines, "count": len(headlines)}
                else:
                    news_payload = {"error": "No recent headlines found for this symbol.", "headlines": [], "count": 0}

            try:
                out = compute_forecast(symbol, horizon, model, drift_tilt=drift_tilt)
                if news_payload is not None and not out.get("error"):
                    out["news"] = news_payload
                return self._json(out, 200 if not out.get("error") else 400)
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 500)

        if path.startswith("/api/stock/"):
            symbol = urllib.parse.unquote(path[len("/api/stock/"):])
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            range_ = (qs.get("range") or ["1mo"])[0]
            interval = (qs.get("interval") or ["1d"])[0]
            try:
                return self._json(yahoo_quote(symbol, range_=range_, interval=interval))
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 500)

        # Static assets (CSS/JS/images) and the root index are real files/dirs;
        # everything else is an unknown route → the styled 404 page.
        fs_path = self.translate_path(self.path)
        if os.path.isfile(fs_path) or os.path.isdir(fs_path):
            return super().do_GET()
        return self._send_404()

    def do_POST(self):
        path = self.path.split("?")[0]
        self._apply_user_context()

        if path == "/api/login":
            body = self._read_json()
            username = (body.get("username") or "").strip().lower()
            pw = body.get("password") or ""
            # Human-verification gate — everyone except the dev/admin account.
            if not is_admin_username(username):
                if not verify_captcha(body.get("captcha_id"), body.get("captcha_answer")):
                    return self._json(
                        {"error": "Verify you're human to continue.", "captcha_required": True}, 400)
            u = load_users().get(username)
            if not u or u.get("provider") == "google" or not verify_password(pw, u.get("pw")):
                return self._json({"error": "Invalid username or password."}, 401)
            token = make_session(username)
            return self._json(
                {"ok": True, "username": username, "tier": int(u.get("tier") or 0), "admin": bool(u.get("admin"))},
                set_cookie=self._session_cookie(token),
            )

        if path == "/api/signup":
            body = self._read_json()
            username = (body.get("username") or "").strip().lower()
            pw = body.get("password") or ""
            email = (body.get("email") or "").strip()
            # Human-verification gate (all new accounts are non-admin).
            if not verify_captcha(body.get("captcha_id"), body.get("captcha_answer")):
                return self._json(
                    {"error": "Verify you're human to continue.", "captcha_required": True}, 400)
            if len(username) < 3 or not username.replace("_", "").replace("-", "").isalnum():
                return self._json({"error": "Username must be 3+ letters or numbers."}, 400)
            if len(pw) < 6:
                return self._json({"error": "Password must be at least 6 characters."}, 400)
            users = load_users()
            if username in users:
                return self._json({"error": "That username is taken."}, 409)
            users[username] = {
                "pw": hash_password(pw), "tier": 0, "plan": "", "admin": False,
                "email": email, "provider": "local", "created": datetime.now().isoformat(),
            }
            save_users(users)
            token = make_session(username)
            return self._json({"ok": True, "username": username, "tier": 0},
                              set_cookie=self._session_cookie(token))

        if path == "/api/logout":
            return self._json({"ok": True}, set_cookie=self._session_cookie(clear=True))

        if path == "/api/game/claim":
            u = self._current_user()
            if not u:
                return self._json({"error": "Log in to play the Game of Stocks."}, 401)
            res = _game_claim(u["username"])
            return self._json(res, 200 if res.get("ok") else 400)

        if path == "/api/transcribe":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length > MAX_AUDIO_BYTES:
                return self._json({"error": "audio too large"}, 413)
            audio = self.rfile.read(length) if length else b""
            if not audio:
                return self._json({"error": "no audio data"}, 400)
            if usage_blocked():
                return self._json(USAGE_LIMIT_MSG, 402)
            ext = (self.headers.get("X-Audio-Ext") or "webm").strip().lstrip(".") or "webm"
            ct = self.headers.get("Content-Type") or "application/octet-stream"
            result = openai_transcribe(audio, ext, ct)
            if "error" in result:
                return self._json(result, 502)
            record_cost(WHISPER_FLAT)
            return self._json({"text": (result.get("text") or "").strip()})

        if path == "/api/speak":
            body = self._read_json()
            text = (body.get("text") or "").strip()
            if not text:
                return self._json({"error": "no text to speak"}, 400)
            if usage_blocked():
                return self._json(USAGE_LIMIT_MSG, 402)
            voice = (body.get("voice") or TTS_VOICE).strip()
            audio, err = openai_tts(text, voice)
            if err:
                return self._json(err, 502)
            record_cost(len(text) / 1e6 * TTS_PER_1M_CHARS)
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(audio)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(audio)
            return

        if path == "/api/broker":
            body = self._read_json()
            broker = (body.get("broker") or "").strip()
            save_broker({"broker": broker})
            return self._json({"ok": True, "broker": broker})

        if path == "/api/stripe/key":
            body = self._read_json()
            key = (body.get("key") or "").strip()
            if not key.startswith("sk_"):
                return self._json({"error": "Enter a Stripe secret key (starts with sk_)."}, 400)
            ok = save_stripe_key(key)
            return self._json({"ok": ok, "configured": bool(stripe_key())})

        if path == "/api/checkout":
            # Billing is paused during the beta — everything's free. (The Stripe
            # code below stays intact for when we launch.)
            if BETA:
                return self._json({"error": "FAAM is free while it's in beta — every feature is already unlocked. 🎉"})
            # Creates a Stripe Checkout subscription session. FAAM never sees the
            # card; Stripe hosts the payment page. We only get back a URL to open.
            body = self._read_json()
            plan = (body.get("plan") or "").strip().lower()
            info = PLANS.get(plan)
            if not info:
                return self._json({"error": "unknown plan"}, 400)
            params = [
                ("mode", "subscription"),
                ("line_items[0][quantity]", "1"),
                ("line_items[0][price_data][currency]", "usd"),
                ("line_items[0][price_data][unit_amount]", str(info["price"])),
                ("line_items[0][price_data][recurring][interval]", "month"),
                ("line_items[0][price_data][product_data][name]", f"FAAM {info['name']}"),
                ("metadata[plan]", plan),
                ("subscription_data[metadata][plan]", plan),
                ("success_url", f"{BASE_URL}/pro/success?session_id={{CHECKOUT_SESSION_ID}}"),
                ("cancel_url", f"{BASE_URL}/dashboard"),
            ]
            res, err = stripe_request("POST", "/v1/checkout/sessions", params)
            if err:
                return self._json(err, 502)
            return self._json({"url": res.get("url")})

        if path == "/api/dashboard/layout":
            # Design a dashboard layout from a plain-English request (GPT-4.1 mini,
            # with a key-free keyword fallback so it always returns something usable).
            body = self._read_json()
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                return self._json({"error": "Describe the dashboard you want."}, 400)
            return self._json(dash_design_layout(prompt))

        if path == "/api/order/parse":
            # Turn plain English ("buy $500 of Apple") into the order-form fields.
            # Heuristic first (no key needed); falls back to the OpenAI key when set.
            # This only FILLS the form — FAAM still never places the trade.
            body = self._read_json()
            text = (body.get("text") or "").strip()
            if not text:
                return self._json({"error": 'Describe your order, e.g. "buy $500 of Apple".'}, 400)
            parsed = parse_order_text(text)
            if not parsed.get("symbol") and OPENAI_API_KEY:
                ai = openai_chat(
                    [{"role": "user", "content":
                      f'Convert this trade request into JSON: "{text}". '
                      'Reply with ONLY compact JSON: '
                      '{"side":"buy|sell","symbol":"US_TICKER","mode":"shares|dollars","qty":number}.'}],
                    system="You convert plain-English trade requests into a JSON order ticket. JSON only, no prose.",
                )
                try:
                    j = json.loads(re.search(r"\{.*\}", extract_text(ai), re.S).group(0))
                    for k in ("side", "symbol", "mode", "qty"):
                        if j.get(k) not in (None, ""):
                            parsed[k] = j[k]
                except Exception:  # noqa: BLE001
                    pass
            sym = (str(parsed.get("symbol") or "")).strip().upper()
            if not sym:
                return self._json({"error": "Couldn't tell which stock — try a ticker like AAPL."}, 400)
            try:
                q = yahoo_quote(sym, range_="1d", interval="5m")
            except Exception:  # noqa: BLE001
                q = {"error": "lookup failed"}
            valid = not bool(q.get("error"))
            return self._json({
                "side": parsed.get("side") or "buy",
                "mode": parsed.get("mode") or "shares",
                "qty": parsed.get("qty"),
                "symbol": sym,
                "name": sym if not valid else q.get("name", sym),
                "price": (q.get("price") if valid else None),
                "valid": valid,
            })

        if path == "/api/order/prepare":
            # IMPORTANT: this ONLY prepares an order ticket for the user to review
            # and place themselves in their broker. FAAM never submits trades or
            # moves money. No brokerage credentials are used here.
            body = self._read_json()
            sym = (body.get("symbol") or "").strip().upper()
            side = (body.get("side") or "buy").strip().lower()
            mode = (body.get("mode") or "shares").strip().lower()
            try:
                qty = float(body.get("qty"))
            except (TypeError, ValueError):
                return self._json({"error": "quantity must be a number"}, 400)
            if not sym or qty <= 0:
                return self._json({"error": "need a symbol and a positive quantity"}, 400)
            if side not in ("buy", "sell"):
                side = "buy"
            try:
                q = yahoo_quote(sym, range_="1d", interval="5m")
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"could not fetch {sym}: {e}"}, 400)
            if q.get("error"):
                return self._json({"error": f"unknown symbol: {sym}"}, 400)
            price = q.get("price") or 0.0
            if mode == "dollars":
                shares = (qty / price) if price else 0.0
                est_cost = qty
            else:
                shares = qty
                est_cost = qty * price
            return self._json({
                "ticket": {
                    "symbol": sym,
                    "name": q.get("name", sym),
                    "side": side,
                    "price": price,
                    "shares": shares,
                    "estCost": est_cost,
                    "currency": q.get("currency", "USD"),
                    "quoteType": q.get("quoteType", ""),
                },
                "placed": False,  # FAAM never places the order
            })

        if path == "/api/screen":
            body = self._read_json()
            criteria = (body.get("criteria") or "").strip()
            if not criteria:
                return self._json({"error": "describe what to screen for"}, 400)
            if usage_blocked():
                return self._json(USAGE_LIMIT_MSG, 402)
            metrics = screen_universe(SCREENER_UNIVERSE)
            if not metrics:
                return self._json({"error": "could not fetch market data"}, 502)
            header = "symbol,price,dayChangePct,week52Low,week52High,pctFromHigh,pctFromLow,type"
            rows = [header] + [
                f"{m['symbol']},{m['price']:.2f},{m['pct']:.2f},"
                f"{m['fiftyTwoWeekLow']:.2f},{m['fiftyTwoWeekHigh']:.2f},"
                f"{m['pctFromHigh']:.1f},{m['pctFromLow']:.1f},{m['quoteType']}"
                for m in metrics
            ]
            prompt = (
                "Universe of tickers with live data (CSV):\n" + "\n".join(rows) +
                f"\n\nUser's screen: \"{criteria}\"\n\n"
                "Pick and rank the best matches (most relevant first, max 8). "
                'Return ONLY a JSON array like '
                '[{"symbol":"AAPL","reason":"short reason citing the numbers"}]. '
                "If nothing fits, return []."
            )
            result = openai_chat(
                [{"role": "user", "content": prompt}],
                system=("You are FAAM's stock screener. Use ONLY the provided data. "
                        "Output strict JSON only — no prose, no code fences."),
            )
            if "error" in result:
                return self._json(result, 502)
            record_cost(chat_cost(result))
            picks = _parse_json_array(extract_text(result))
            by_sym = {m["symbol"]: m for m in metrics}
            results = []
            for p in picks:
                sym = (p.get("symbol") or "").upper() if isinstance(p, dict) else ""
                m = by_sym.get(sym)
                if m:
                    results.append({**m, "reason": (p.get("reason") or "").strip()})
            return self._json({"criteria": criteria, "results": results,
                               "scanned": len(metrics)})

        if path == "/api/learn":
            body = self._read_json()
            question = (body.get("question") or "").strip()
            if not question:
                return self._json({"error": "ask a question"}, 400)
            if usage_blocked():
                return self._json(USAGE_LIMIT_MSG, 402)
            result = openai_chat(
                [{"role": "user", "content": question}],
                system=(
                    "You are FAAM's friendly investing tutor for beginners. Explain clearly "
                    "and simply in 3-6 short sentences or a few bullets. Use a quick concrete "
                    "example. Define any jargon in plain words. Keep it warm and encouraging. "
                    "End with: 'Not financial advice.'"
                ),
            )
            if "error" in result:
                return self._json(result, 502)
            record_cost(chat_cost(result))
            return self._json({"text": extract_text(result)})

        if path == "/api/watchlist/add":
            body = self._read_json()
            sym = (body.get("symbol") or "").strip().upper()
            if not sym:
                return self._json({"error": "missing symbol"}, 400)
            try:
                q = yahoo_quote(sym, range_="1d", interval="5m")
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"could not fetch {sym}: {e}"}, 400)
            if q.get("error"):
                return self._json({"error": f"unknown symbol: {sym}"}, 400)
            wl = load_watchlist()
            if sym not in wl:
                wl.append(sym)
                save_watchlist(wl)
            return self._json({"ok": True, "watchlist": wl, "name": q.get("name", sym)})

        if path == "/api/watchlist/remove":
            body = self._read_json()
            sym = (body.get("symbol") or "").strip().upper()
            wl = [s for s in load_watchlist() if s.upper() != sym]
            save_watchlist(wl)
            return self._json({"ok": True, "watchlist": wl})

        if path == "/api/portfolio/add":
            body = self._read_json()
            sym = (body.get("symbol") or "").strip().upper()
            if not sym:
                return self._json({"error": "need a symbol"}, 400)
            try:
                q = yahoo_quote(sym, range_="1d", interval="5m")
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"could not fetch {sym}: {e}"}, 400)
            if q.get("error"):
                return self._json({"error": f"unknown symbol: {sym}"}, 400)
            price = q.get("price") or 0.0

            amount = body.get("amount")
            if amount not in (None, ""):
                # Dollar-based (fractional) buy at the current price.
                try:
                    amount = float(amount)
                except (TypeError, ValueError):
                    return self._json({"error": "amount must be a number"}, 400)
                if amount <= 0 or price <= 0:
                    return self._json({"error": "need a positive $ amount and a live price"}, 400)
                shares = amount / price
                cost = price
            else:
                try:
                    shares = float(body.get("shares"))
                    cost = float(body.get("cost"))
                except (TypeError, ValueError):
                    return self._json({"error": "shares and cost must be numbers"}, 400)
                if shares <= 0 or cost < 0:
                    return self._json({"error": "need positive shares and non-negative cost"}, 400)

            pf = load_portfolio()
            pf.append({
                "id": uuid.uuid4().hex[:8],
                "symbol": sym,
                "shares": shares,
                "cost": cost,
            })
            save_portfolio(pf)
            return self._json({"ok": True, "shares": shares, "cost": cost, "price": price})

        if path == "/api/portfolio/remove":
            body = self._read_json()
            pid = (body.get("id") or "").strip()
            pf = [p for p in load_portfolio() if p.get("id") != pid]
            save_portfolio(pf)
            return self._json({"ok": True})

        if path == "/api/adviser":
            body = self._read_json()
            text = (body.get("text") or "")
            if len(text) > ADVISER_MAX:
                return self._json({"error": f"too long (max {ADVISER_MAX} chars)"}, 400)
            ok = save_adviser(text)
            return self._json({"ok": ok, "adviser_loaded": bool(text.strip())})

        if path == "/api/chat":
            if usage_blocked():
                return self._json(USAGE_LIMIT_MSG, 402)
            body = self._read_json()
            messages = body.get("messages") or []
            symbol = (body.get("symbol") or "").strip()

            system = effective_system()
            if (body.get("mode") or "") == "voice":
                system += (
                    "\n\nThis reply will be read aloud by text-to-speech. Keep it brief and "
                    "conversational — 2 to 4 sentences. No markdown, no bullet lists, no "
                    "asterisks or other symbols that sound awkward when spoken."
                )
            if symbol:
                try:
                    q = yahoo_quote(symbol)
                    system += (
                        f"\n\nActive ticker context — {q['symbol']} ({q.get('name','')}): "
                        f"price ${q['price']:.2f}, change today {q['change']:+.2f} "
                        f"({q['pct']:+.2f}%), day range "
                        f"${(q.get('low') or 0):.2f}–${(q.get('high') or 0):.2f}, "
                        f"52w range ${(q.get('fiftyTwoWeekLow') or 0):.2f}–"
                        f"${(q.get('fiftyTwoWeekHigh') or 0):.2f}."
                    )
                except Exception:  # noqa: BLE001
                    pass

            result = openai_chat(messages, system=system)
            if "error" in result:
                return self._json(result, 502)
            record_cost(chat_cost(result))
            return self._json({
                "text": extract_text(result),
                "model": result.get("model", OPENAI_MODEL),
            })

        if path == "/api/analyze":
            if usage_blocked():
                return self._json(USAGE_LIMIT_MSG, 402)
            body = self._read_json()
            symbol = (body.get("symbol") or "").strip()
            if not symbol:
                return self._json({"error": "missing symbol"}, 400)
            try:
                q = yahoo_quote(symbol, range_="3mo", interval="1d")
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 500)

            closes = [p["c"] for p in q.get("history", [])]
            recent = closes[-30:] if len(closes) >= 30 else closes
            trend = "up" if recent and recent[-1] > recent[0] else "down"
            lo, hi = (min(recent), max(recent)) if recent else (0, 0)

            prompt = (
                f"Give a tight professional take on {q['symbol']} ({q.get('name','')}).\n"
                f"- Current price: ${q['price']:.2f}\n"
                f"- Change today: {q['pct']:+.2f}% (${q['change']:+.2f})\n"
                f"- 30-day range: ${lo:.2f}–${hi:.2f} (trend {trend})\n"
                f"- 52-week range: ${(q.get('fiftyTwoWeekLow') or 0):.2f}–"
                f"${(q.get('fiftyTwoWeekHigh') or 0):.2f}\n\n"
                "Base your take ONLY on these figures — do not invent prices, P/E ratios, "
                "earnings dates, analyst targets, or news you were not given. "
                "Three short bullets: (1) momentum read, (2) one thing to watch, "
                "(3) primary risk. End with a one-line disclaimer."
            )
            result = openai_chat([{"role": "user", "content": prompt}], system=effective_system())
            if "error" in result:
                return self._json(result, 502)
            record_cost(chat_cost(result))
            return self._json({"text": extract_text(result)})

        return self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        msg = fmt % args
        if "/api/" in msg or msg.startswith('"GET / '):
            sys.stderr.write(f"[{datetime.now():%H:%M:%S}] {msg}\n")


def main() -> None:
    banner = r"""
   ███████╗ █████╗  █████╗ ███╗   ███╗
   ██╔════╝██╔══██╗██╔══██╗████╗ ████║
   █████╗  ███████║███████║██╔████╔██║
   ██╔══╝  ██╔══██║██╔══██║██║╚██╔╝██║
   ██║     ██║  ██║██║  ██║██║ ╚═╝ ██║
   ╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝
   Financial AI Agent Manager
"""
    print(banner)
    if OPENAI_API_KEY:
        masked = OPENAI_API_KEY[:7] + "…" + OPENAI_API_KEY[-4:]
        print(f"   ✓ OpenAI key loaded ({masked})")
    else:
        print("   ⚠ OPENAI_API_KEY not set — AI features disabled.")
        print("     export OPENAI_API_KEY=sk-...")
    print(f"   ✓ Model: {OPENAI_MODEL}")
    seed_users()
    gid, _ = google_creds()
    print(f"   ✓ Accounts on · dev admin seeded (Elite) · Google sign-in: {'on' if gid else 'not configured'}")
    print(f"   → http://localhost:{PORT}\n")

    with ThreadingHTTPServer(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n   Goodbye.")


if __name__ == "__main__":
    main()
