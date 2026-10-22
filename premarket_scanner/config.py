"""Scanner settings.

Everything is read from environment variables (a ``.env`` file in the project
root is loaded automatically). See ``.env.example`` for the full list.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").split("#")[0].strip()
    return float(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").split("#")[0].strip()
    return int(float(raw)) if raw else default


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name, "").split("#")[0].strip()
    return raw or default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").split("#")[0].strip().lower()
    return raw in ("1", "true", "yes", "on") if raw else default


# Scan criteria -----------------------------------------------------------------
MIN_GAP_PCT: float = _env_float("MIN_GAP_PCT", 5.0)
MIN_PRICE: float = _env_float("MIN_PRICE", 3.0)
MIN_PREMARKET_VOLUME: int = _env_int("MIN_PREMARKET_VOLUME", 50_000)

# Universe ----------------------------------------------------------------------
UNIVERSE_SOURCE: str = _env_str("UNIVERSE_SOURCE", "yahoo").lower()  # "yahoo" | "file"
UNIVERSE_MIN_AVG_VOLUME: int = _env_int("UNIVERSE_MIN_AVG_VOLUME", 50_000)
MAX_UNIVERSE: int = _env_int("MAX_UNIVERSE", 8_000)
WATCHLIST_FILE: Path = PROJECT_ROOT / _env_str("WATCHLIST_FILE", "watchlist.txt")

# Schedule (all times HH:MM, US Eastern, weekdays) ---------------------------------
MARKET_TZ = "America/New_York"
SCAN_TIME_ET: str = _env_str("SCAN_TIME_ET", "08:30")        # first pre-market scan
SCAN_END_ET: str = _env_str("SCAN_END_ET", "14:00")          # last pre-market scan
SCAN_INTERVAL_MIN: int = _env_int("SCAN_INTERVAL_MIN", 30)   # minutes between scans
STRATEGY_OFFSET_MIN: int = _env_int("STRATEGY_OFFSET_MIN", 5)  # strategy runs this long after each scan
STRATEGY_END_ET: str = _env_str("STRATEGY_END_ET", "15:00")  # last strategy run
PREMARKET_OPEN = "04:00"
REGULAR_OPEN = "09:30"

# Strategy scanner (Trend-join Long) ---------------------------------------------
STRATEGY_DATA_SOURCE: str = _env_str("STRATEGY_DATA_SOURCE", "tradingview").lower()  # "tradingview" | "yahoo"
TRADINGVIEW_SESSIONID: str = _env_str("TRADINGVIEW_SESSIONID", "")
STRATEGY_START_ET: str = _env_str("STRATEGY_START_ET", "10:00")  # earliest time a signal is valid
HOD_TOLERANCE_PCT: float = _env_float("HOD_TOLERANCE_PCT", 0.25)

# Telegram ----------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: str = _env_str("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = _env_str("TELEGRAM_CHAT_ID", "")              # owner: gets everything
TELEGRAM_EXTRA_CHAT_IDS: str = _env_str("TELEGRAM_EXTRA_CHAT_IDS", "")  # groups/channels: reports + signals only
REPORT_LANGUAGE: str = _env_str("REPORT_LANGUAGE", "th").lower()   # "th" plain Thai | "en"
TRANSLATE_NEWS: bool = _env_bool("TRANSLATE_NEWS", True)           # translate headlines to Thai

# Scanner C: special watchlist (currency pairs, gold, bitcoin) ---------------------
SPECIAL_ENABLED: bool = _env_bool("SPECIAL_ENABLED", True)
# tradingview = real-time quotes from TradingView (symbols like OANDA:XAUUSD, FX:EURUSD, BITSTAMP:BTCUSD)
# yahoo       = Yahoo Finance (symbols like EURUSD=X, GC=F, BTC-USD)
# mt5         = your broker's prices through the MetaTrader 5 terminal running on this PC
#               (broker symbol names such as XAUUSD, EURUSD, BTCUSD)
SPECIAL_DATA_SOURCE: str = _env_str("SPECIAL_DATA_SOURCE", "tradingview").lower()
_DEFAULT_WATCHLIST = {   # GBPUSD dropped: it lost money under every rule variant in the backtests
    "tradingview": "OANDA:XAUUSD,FX:EURUSD,FX:USDJPY,FX_IDC:USDTHB,BITSTAMP:BTCUSD",
    "yahoo": "GC=F,EURUSD=X,USDJPY=X,USDTHB=X,BTC-USD",
    "mt5": "XAUUSD,EURUSD,USDJPY,USDTHB,BTCUSD",
}

# MetaTrader 5 (optional; leave login fields empty to attach to the already logged-in terminal)
MT5_PATH: str = _env_str("MT5_PATH", "")            # full path to terminal64.exe, empty = auto
MT5_LOGIN: int = _env_int("MT5_LOGIN", 0)
MT5_PASSWORD: str = _env_str("MT5_PASSWORD", "")
MT5_SERVER: str = _env_str("MT5_SERVER", "")
MT5_TIMEOUT_MS: int = _env_int("MT5_TIMEOUT_MS", 10_000)
SPECIAL_WATCHLIST: list[str] = [
    s.strip().upper()
    for s in _env_str("SPECIAL_WATCHLIST", _DEFAULT_WATCHLIST.get(SPECIAL_DATA_SOURCE, "")).split(",")
    if s.strip()
]

# TradingView webhook receiver (TradingView alert -> this bot -> Telegram) ---------
TV_WEBHOOK_ENABLED: bool = _env_bool("TV_WEBHOOK_ENABLED", True)
TV_WEBHOOK_BIND: str = _env_str("TV_WEBHOOK_BIND", "127.0.0.1")   # local only; expose via a tunnel
TV_WEBHOOK_PORT: int = _env_int("TV_WEBHOOK_PORT", 8787)
TV_WEBHOOK_SECRET: str = _env_str("TV_WEBHOOK_SECRET", "")        # required in the URL (?key=) or JSON "secret"
# Cloudflare quick tunnel started by the scheduler so TradingView can reach the webhook
TUNNEL_ENABLED: bool = _env_bool("TUNNEL_ENABLED", True)
CLOUDFLARED_PATH: str = _env_str("CLOUDFLARED_PATH", "")          # empty = auto-detect
TUNNEL_NOTIFY: bool = _env_bool("TUNNEL_NOTIFY", True)            # Telegram message when the URL changes
# Watch mode: check every few minutes, alert immediately on a new entry signal
SPECIAL_WATCH_ENABLED: bool = _env_bool("SPECIAL_WATCH_ENABLED", True)
SPECIAL_WATCH_INTERVAL_MIN: int = _env_int("SPECIAL_WATCH_INTERVAL_MIN", 5)
ATR_PERIOD: int = _env_int("ATR_PERIOD", 14)              # days used for the average daily range
STOP_ATR_MULT: float = _env_float("STOP_ATR_MULT", 1.0)   # initial stop = entry - this x ATR
TARGET_R_MULT: float = _env_float("TARGET_R_MULT", 2.0)   # fixed target used by the v1 backtest only
# Live exit (v2-trail): half off at PARTIAL_R, the rest trails TRAIL_ATR_MULT x ATR below the
# highest price since entry (the stop only ever rises), closed after MAX_HOLD_DAYS trading days
PARTIAL_R: float = _env_float("PARTIAL_R", 1.0)
TRAIL_ATR_MULT: float = _env_float("TRAIL_ATR_MULT", 3.0)
MAX_HOLD_DAYS: int = _env_int("MAX_HOLD_DAYS", 10)
TRAIL_NOTIFY_ATR: float = _env_float("TRAIL_NOTIFY_ATR", 0.5)   # alert when the stop rises this many ATR
STALE_QUOTE_MIN: int = _env_int("STALE_QUOTE_MIN", 30)    # ignore quotes older than this (market closed)
# Time / news filter for new signals (backtest: helps gold, hurts the currency pairs)
TIME_FILTER_SYMBOLS: list[str] = [
    s.strip().upper() for s in _env_str("TIME_FILTER_SYMBOLS", "XAUUSD,GOLD,GC=F").split(",") if s.strip()
]
SESSION_UTC_START: int = _env_int("SESSION_UTC_START", 7)    # London open
SESSION_UTC_END: int = _env_int("SESSION_UTC_END", 20)       # late New York
FRIDAY_CUTOFF_UTC: int = _env_int("FRIDAY_CUTOFF_UTC", 15)   # no new signals on Friday from this hour
NEWS_BLACKOUT_MIN: int = _env_int("NEWS_BLACKOUT_MIN", 30)   # minutes before/after a high-impact release
NEWS_CALENDAR_ENABLED: bool = _env_bool("NEWS_CALENDAR_ENABLED", True)   # ForexFactory weekly calendar

# News --------------------------------------------------------------------------
BENZINGA_API_KEY: str = _env_str("BENZINGA_API_KEY", "")
NEWS_LOOKBACK_HOURS: int = _env_int("NEWS_LOOKBACK_HOURS", 48)

# Paths -------------------------------------------------------------------------
OUTPUT_DIR: Path = PROJECT_ROOT / _env_str("OUTPUT_DIR", "output")
DATA_DIR: Path = PROJECT_ROOT / "data"

# Networking --------------------------------------------------------------------
HTTP_TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
