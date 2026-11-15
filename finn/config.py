"""Central configuration. Everything tunable lives here or in env vars."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Minimal .env loader (no extra dependency); real env vars win.
_env = ROOT / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

DATA = ROOT / "data"
CACHE = DATA / "cache"
# PostgreSQL is the system of record (local: docker compose; production: managed Postgres, e.g. Neon).
# Without DATABASE_URL FINN falls back to a SQLite file so a fresh clone still runs with zero setup,
# but that mode has no row-level security and is single-writer: dev convenience only.
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{(DATA / 'finn.sqlite3').as_posix()}"
if DATABASE_URL.startswith("postgres://"):  # some hosts hand out the short scheme
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgresql://"):]
WEB = ROOT / "web"

CACHE.mkdir(parents=True, exist_ok=True)

# Honest user agent: we identify ourselves and rate-limit, we don't impersonate a browser.
USER_AGENT = os.getenv("FINN_USER_AGENT", "FINN-prototype/0.1 (investor research demo)")

# Polite fetching: minimum seconds between requests to the same host.
HOST_MIN_INTERVAL = float(os.getenv("FINN_HOST_MIN_INTERVAL", "1.2"))

# Cache TTLs (seconds)
TTL_SCREENER = int(os.getenv("FINN_TTL_SCREENER", str(12 * 3600)))
TTL_RSS = int(os.getenv("FINN_TTL_RSS", str(5 * 60)))
TTL_PRICES = int(os.getenv("FINN_TTL_PRICES", str(6 * 3600)))

# --- LLM routing -----------------------------------------------------------
# Cheap model for bulk extraction / classification / one-liners; strong model
# only where reasoning over numbers + quotes is needed. Both overridable.
def _secret(*names: str) -> str:
    return next((v for v in (os.getenv(n, "").strip().strip('"').strip("'") for n in names) if v), "")   # tolerate pasted quotes


ANTHROPIC_API_KEY = _secret("ANTHROPIC_API_KEY")
GEMINI_API_KEY = _secret("GEMINI_API_KEY", "GOOGLE_API_KEY")
# The server's own key is optional (visitors can bring theirs, see finn/intel/providers.py). Set either
# ANTHROPIC_API_KEY or GEMINI_API_KEY, or any provider with FINN_LLM_PROVIDER + FINN_LLM_API_KEY
# (+ FINN_LLM_BASE_URL for "custom"; FINN_MODEL_FAST / FINN_MODEL_DEEP to pick models).
_GENERIC = _secret("FINN_LLM_API_KEY")
LLM_PROVIDER = os.getenv("FINN_LLM_PROVIDER", "").lower() or ("gemini" if GEMINI_API_KEY and not ANTHROPIC_API_KEY else "anthropic")
_G = LLM_PROVIDER == "gemini"
LLM_API_KEY = _GENERIC or (GEMINI_API_KEY if _G else ANTHROPIC_API_KEY)
LLM_BASE_URL = os.getenv("FINN_LLM_BASE_URL", "").strip()
MODEL_FAST = os.getenv("FINN_MODEL_FAST", "gemini-2.5-flash" if _G else "claude-haiku-5-5")
MODEL_DEEP = os.getenv("FINN_MODEL_DEEP", "gemini-2.5-pro" if _G else "claude-opus-5-5")
LLM_ENABLED = bool(LLM_API_KEY) and os.getenv("FINN_LLM", "on") != "off"
# Agent roles. The answer-writer gets the strongest model (1M context, adaptive reasoning; repairs at
# low effort); the planner reuses it at low effort, but only for questions the rule planner can't plan
# confidently (comparisons, multi-part, no explicit topic); web search runs on Sonnet (server-side
# search with dynamic filtering); routing, memory and summaries run on Haiku.
MODEL_AGENT = os.getenv("FINN_MODEL_AGENT", MODEL_DEEP)
MODEL_WEB = os.getenv("FINN_MODEL_WEB", "gemini-2.5-flash" if _G else "claude-sonnet-5-5")
# Both providers' models by role, so a key a visitor brings (either provider) gets the right ones.
MODELS = {
    "anthropic": {"fast": os.getenv("FINN_CLAUDE_FAST", "claude-haiku-5-5"), "deep": os.getenv("FINN_CLAUDE_DEEP", "claude-opus-5-5"),
                  "web": os.getenv("FINN_CLAUDE_WEB", "claude-sonnet-5-5")},
    "gemini": {"fast": os.getenv("FINN_GEMINI_FAST", "gemini-2.5-flash"), "deep": os.getenv("FINN_GEMINI_DEEP", "gemini-2.5-pro"),
               "web": os.getenv("FINN_GEMINI_WEB", "gemini-2.5-flash")},
}
if LLM_PROVIDER in MODELS or (os.getenv("FINN_MODEL_FAST") and os.getenv("FINN_MODEL_DEEP")):
    MODELS[LLM_PROVIDER] = {"fast": MODEL_FAST, "deep": MODEL_DEEP, "web": MODEL_WEB if LLM_PROVIDER in ("anthropic", "gemini") else MODEL_FAST}
# A visitor may bring their own key (sent with each request, never stored). Off: only the server's key is used.
LLM_BYOK = os.getenv("FINN_LLM_BYOK", "on") != "off"

# Demo: include simulated scenario events (clearly badged in the UI).
INCLUDE_SCENARIOS = os.getenv("FINN_SCENARIOS", "on") != "off"

# "Now" for the demo. Defaults to real now; can be pinned so a recorded demo is reproducible.
PINNED_NOW = os.getenv("FINN_NOW")  # ISO timestamp, optional
