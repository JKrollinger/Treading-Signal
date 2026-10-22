"""Scanner C: a fixed watchlist of non-stock markets (currency pairs, spot gold, bitcoin).

These trade around the clock, so there is no pre-market. For each symbol we compute the
latest price, the change versus the previous daily close, the long-term trend (previous
close versus the 200-day average), the Trend-join rules that still apply (price above
yesterday's high and at the high of the current day) and, from the average daily range
(ATR), a trade plan: entry, stop-loss and target.

Data source (``SPECIAL_DATA_SOURCE``):

* ``tradingview`` (default): TradingView's screener API, symbols like ``OANDA:XAUUSD``
  (spot gold), ``FX:EURUSD``, ``BITSTAMP:BTCUSD``. Real-time without a login.
* ``yahoo``: Yahoo Finance, symbols like ``EURUSD=X``, ``GC=F``, ``BTC-USD``.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from . import config, tradingview, yahoo

log = logging.getLogger(__name__)

ET = ZoneInfo(config.MARKET_TZ)
LEVELS_TTL_SECONDS = 60 * 60     # Yahoo daily levels are refreshed at most hourly

# Display names keyed by the "base" symbol (exchange prefix / Yahoo suffix removed)
NAMES_TH = {
    "EURUSD": "ยูโร/ดอลลาร์", "GBPUSD": "ปอนด์/ดอลลาร์", "USDJPY": "ดอลลาร์/เยน", "USDTHB": "ดอลลาร์/บาท",
    "AUDUSD": "ออสเตรเลีย/ดอลลาร์", "USDCHF": "ดอลลาร์/ฟรังก์สวิส", "USDCAD": "ดอลลาร์/แคนาดา",
    "NZDUSD": "นิวซีแลนด์/ดอลลาร์", "EURJPY": "ยูโร/เยน", "GBPJPY": "ปอนด์/เยน",
    "XAUUSD": "ทองคำสปอต", "GOLD": "ทองคำสปอต", "XAGUSD": "โลหะเงินสปอต", "SILVER": "โลหะเงินสปอต",
    "GC=F": "ทองคำ (ราคาอ้างอิงตลาดโลก)", "SI=F": "โลหะเงิน", "CL=F": "น้ำมันดิบ", "USOIL": "น้ำมันดิบ",
    "PAXGUSD": "ทองคำ (โทเคน PAXG)", "XAUTUSD": "ทองคำ (โทเคน XAUT)",
    "BTCUSD": "บิตคอยน์", "BTCUSDT": "บิตคอยน์", "ETHUSD": "อีเธอเรียม", "ETHUSDT": "อีเธอเรียม",
    "SOLUSD": "โซลานา", "SOLUSDT": "โซลานา", "XRPUSD": "XRP", "XRPUSDT": "XRP",
    "^GSPC": "ดัชนี S&P 500", "SPX": "ดัชนี S&P 500", "^IXIC": "ดัชนี Nasdaq", "^DJI": "ดัชนีดาวโจนส์",
}
NAMES_EN = {
    "EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "USDJPY": "USD/JPY", "USDTHB": "USD/THB",
    "AUDUSD": "AUD/USD", "USDCHF": "USD/CHF", "USDCAD": "USD/CAD", "NZDUSD": "NZD/USD",
    "EURJPY": "EUR/JPY", "GBPJPY": "GBP/JPY", "XAUUSD": "Spot gold", "GOLD": "Spot gold",
    "XAGUSD": "Spot silver", "SILVER": "Spot silver", "GC=F": "Gold (global reference)",
    "SI=F": "Silver", "CL=F": "Crude oil", "USOIL": "Crude oil", "PAXGUSD": "Gold (PAXG token)",
    "XAUTUSD": "Gold (XAUT token)", "BTCUSD": "Bitcoin", "BTCUSDT": "Bitcoin", "ETHUSD": "Ethereum",
    "ETHUSDT": "Ethereum", "SOLUSD": "Solana", "SOLUSDT": "Solana", "XRPUSD": "XRP", "XRPUSDT": "XRP",
    "^GSPC": "S&P 500", "SPX": "S&P 500", "^IXIC": "Nasdaq", "^DJI": "Dow Jones",
}
# TradingView equivalents for plain (MT5-style) symbols, used as a fallback
TRADINGVIEW_EQUIVALENT = {
    "XAUUSD": "OANDA:XAUUSD", "GOLD": "OANDA:XAUUSD", "XAGUSD": "OANDA:XAGUSD",
    "BTCUSD": "BITSTAMP:BTCUSD", "ETHUSD": "BITSTAMP:ETHUSD", "USDTHB": "FX_IDC:USDTHB",
}
# Best-effort Yahoo equivalents used when TradingView is down
YAHOO_EQUIVALENT = {
    "XAUUSD": "GC=F", "GOLD": "GC=F", "XAGUSD": "SI=F", "SILVER": "SI=F", "USOIL": "CL=F",
    "BTCUSD": "BTC-USD", "BTCUSDT": "BTC-USD", "ETHUSD": "ETH-USD", "ETHUSDT": "ETH-USD",
    "SOLUSD": "SOL-USD", "XRPUSD": "XRP-USD",
}
TV_COLUMNS = [
    "close", "high", "low", "change", "high[1]", "low[1]", "close[1]", "SMA200[1]", "ATR",
    "currency", "update_mode", "last_bar_update_time", "type",
]


def base_symbol(symbol: str) -> str:
    s = symbol.upper().strip()
    if s in ("GC=F", "SI=F", "CL=F") or s.startswith("^"):
        return s
    if ":" in s:
        s = s.split(":", 1)[1]
    if s.endswith("=X"):
        s = s[:-2]
    if s.endswith("=F"):
        return symbol.upper()
    s = s.replace("-", "")
    # broker suffixes such as XAUUSD.m / EURUSDm / GOLD# are common on MT5
    for suffix in (".M", ".C", ".R", ".I", "M", "#", ".PRO", ".ECN", "_RAW", ".STD"):
        if s.endswith(suffix) and len(s) > len(suffix) + 5:
            s = s[: -len(suffix)]
            break
    return s


def display_name(symbol: str, thai: bool) -> str:
    table = NAMES_TH if thai else NAMES_EN
    return table.get(base_symbol(symbol), base_symbol(symbol))


def yahoo_equivalent(symbol: str) -> str | None:
    """Yahoo symbol for a TradingView- or MT5-style symbol, or None if unknown."""
    if ":" not in symbol and ("=" in symbol or "-" in symbol or symbol.startswith("^")):
        return symbol                            # already a Yahoo symbol
    base = base_symbol(symbol)
    if base in YAHOO_EQUIVALENT:
        return YAHOO_EQUIVALENT[base]
    if len(base) == 6 and base.isalpha():        # a currency pair such as EURUSD
        return f"{base}=X"
    return None


def tradingview_equivalent(symbol: str) -> str | None:
    """TradingView symbol for an MT5-style symbol (XAUUSD -> OANDA:XAUUSD)."""
    if ":" in symbol:
        return symbol
    base = base_symbol(symbol)
    if base in TRADINGVIEW_EQUIVALENT:
        return TRADINGVIEW_EQUIVALENT[base]
    if len(base) == 6 and base.isalpha():
        return f"FX:{base}"
    return None


@dataclass
class TradePlan:
    entry: float
    stop: float            # initial stop = entry - STOP_ATR_MULT x ATR
    target: float          # partial-profit level = entry + PARTIAL_R x risk (half the position)
    atr: float
    risk_pct: float        # distance to the stop, % of entry
    reward_pct: float      # distance to the partial level, % of entry
    breakout_level: float  # yesterday's high; falling back below it weakens the setup
    trail_atr_mult: float = config.TRAIL_ATR_MULT
    partial_r: float = config.PARTIAL_R
    max_hold_days: int = config.MAX_HOLD_DAYS


@dataclass
class SpecialItem:
    symbol: str
    name: str
    price: float | None = None
    change_pct: float | None = None
    previous_close: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    yesterday_high: float | None = None
    yesterday_low: float | None = None
    yesterday_close: float | None = None
    sma200: float | None = None
    atr: float | None = None
    currency: str = ""
    quote_time: datetime | None = None
    source: str = ""
    above_yesterday_high: bool | None = None
    at_high_of_day: bool | None = None
    uptrend: bool | None = None          # yesterday's close above the 200-day average
    signal: bool = False                 # all three of the above
    plan: TradePlan | None = None

    def to_json(self) -> dict:
        d = asdict(self)
        d["quote_time"] = self.quote_time.isoformat() if self.quote_time else None
        return d


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _sub(df: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        if symbol not in df.columns.get_level_values(0):
            return None
        return df[symbol]
    return df


# TradingView path --------------------------------------------------------------------
def _fill_from_tradingview(items: dict[str, SpecialItem]) -> list[str]:
    """Fill items from TradingView; returns the symbols that got no data."""
    rows = tradingview.fetch_rows(list(items), TV_COLUMNS)
    missing: list[str] = []
    for s, it in items.items():
        r = rows.get(s)
        if not r or _num(r.get("close")) is None:
            missing.append(s)
            continue
        it.price = _num(r.get("close"))
        it.day_high = _num(r.get("high"))
        it.day_low = _num(r.get("low"))
        it.change_pct = _num(r.get("change"))
        it.yesterday_high = _num(r.get("high[1]"))
        it.yesterday_low = _num(r.get("low[1]"))
        it.yesterday_close = _num(r.get("close[1]"))
        it.previous_close = it.yesterday_close
        it.sma200 = _num(r.get("SMA200[1]"))
        it.atr = _num(r.get("ATR"))
        it.currency = str(r.get("currency") or "")
        ts = r.get("last_bar_update_time")
        it.quote_time = datetime.fromtimestamp(int(ts), ET) if ts else None
        it.source = "tradingview"
        if it.change_pct is None and it.price and it.previous_close:
            it.change_pct = (it.price / it.previous_close - 1.0) * 100.0
    return missing


# MetaTrader 5 path ---------------------------------------------------------------------
def _fill_from_mt5(items: dict[str, SpecialItem]) -> list[str]:
    """Fill items from the broker through MT5; returns the symbols that got no data."""
    from . import mt5_source

    rows = mt5_source.fetch_rows(list(items))
    missing: list[str] = []
    for s, it in items.items():
        r = rows.get(s)
        if not r or r.get("price") is None:
            missing.append(s)
            continue
        it.price = r["price"]
        it.day_high, it.day_low = r.get("day_high"), r.get("day_low")
        it.change_pct = r.get("change_pct")
        it.yesterday_high, it.yesterday_low = r.get("yesterday_high"), r.get("yesterday_low")
        it.yesterday_close = r.get("yesterday_close")
        it.previous_close = it.yesterday_close
        it.sma200, it.atr = r.get("sma200"), r.get("atr")
        it.currency = r.get("currency") or ""
        it.quote_time = r.get("quote_time")
        it.source = "mt5"
    return missing


# Yahoo path --------------------------------------------------------------------------
_levels_cache: dict[str, tuple[float, dict]] = {}
_levels_lock = threading.Lock()


def _compute_levels(symbols: list[str], today: date) -> dict[str, dict]:
    out: dict[str, dict] = {s: {} for s in symbols}
    daily = yf.download(symbols, period="1y", interval="1d", group_by="ticker",
                        auto_adjust=False, threads=True, progress=False)
    for s in symbols:
        d = _sub(daily, s)
        if d is None:
            continue
        bars = [
            (idx.date(), _num(h), _num(l), _num(c))
            for idx, h, l, c in zip(d.index, d["High"], d["Low"], d["Close"])
        ]
        bars = [b for b in bars if b[1] is not None and b[2] is not None and b[3] is not None]
        if bars and bars[-1][0] >= today:      # the bar still forming today
            bars = bars[:-1]
        if not bars:
            continue
        y = bars[-1]
        row = {"yesterday_high": y[1], "yesterday_low": y[2], "yesterday_close": y[3]}
        closes = [b[3] for b in bars]
        row["sma200"] = sum(closes[-200:]) / 200.0 if len(closes) >= 200 else None
        trs = [
            max(b[1] - b[2], abs(b[1] - p[3]), abs(b[2] - p[3]))
            for p, b in zip(bars[:-1], bars[1:])
        ]
        n = config.ATR_PERIOD
        row["atr"] = sum(trs[-n:]) / n if len(trs) >= n else None
        out[s] = row
    return out


def daily_levels(symbols: list[str], today: date) -> dict[str, dict]:
    now = time.time()
    with _levels_lock:
        need = [s for s in symbols if s not in _levels_cache or now - _levels_cache[s][0] > LEVELS_TTL_SECONDS]
    if need:
        try:
            fresh = _compute_levels(need, today)
        except Exception as exc:  # noqa: BLE001
            log.warning("special watchlist history failed: %s", exc)
            fresh = {}
        with _levels_lock:
            for s in need:
                if s in fresh and fresh[s]:
                    _levels_cache[s] = (now, fresh[s])
                elif s not in _levels_cache:
                    _levels_cache[s] = (now - LEVELS_TTL_SECONDS + 300, {})  # retry in 5 min
    with _levels_lock:
        return {s: dict(_levels_cache.get(s, (0, {}))[1]) for s in symbols}


def _fill_from_yahoo(items: dict[str, SpecialItem], today: date) -> None:
    """Fill items from Yahoo. Keys are the configured symbols; Yahoo symbols are derived."""
    ysym = {s: yahoo_equivalent(s) for s in items}
    lookup = {y: s for s, y in ysym.items() if y}
    if not lookup:
        return
    for q in yahoo.fetch_quotes(list(lookup)):
        it = items.get(lookup.get(str(q.get("symbol") or "").upper(), ""))
        if it is None:
            continue
        it.price = _num(q.get("regularMarketPrice"))
        it.change_pct = _num(q.get("regularMarketChangePercent"))
        it.previous_close = _num(q.get("regularMarketPreviousClose"))
        it.day_high = _num(q.get("regularMarketDayHigh"))
        it.day_low = _num(q.get("regularMarketDayLow"))
        it.currency = str(q.get("currency") or "")
        it.quote_time = yahoo._ts_to_et(q.get("regularMarketTime"))
        it.source = "yahoo"
        if it.change_pct is None and it.price and it.previous_close:
            it.change_pct = (it.price / it.previous_close - 1.0) * 100.0
    levels = daily_levels(list(lookup), today)
    for y, s in lookup.items():
        it, row = items[s], levels.get(y) or {}
        it.yesterday_high = row.get("yesterday_high")
        it.yesterday_low = row.get("yesterday_low")
        it.yesterday_close = row.get("yesterday_close")
        it.sma200 = row.get("sma200")
        it.atr = row.get("atr")


# Snapshot + evaluation --------------------------------------------------------------
def build_plan(price: float, atr: float | None, breakout_level: float | None) -> TradePlan | None:
    if atr is None or atr <= 0 or price <= 0:
        return None
    stop = price - config.STOP_ATR_MULT * atr
    if stop <= 0:
        return None
    target = price + config.PARTIAL_R * (price - stop)
    return TradePlan(
        entry=price, stop=stop, target=target, atr=atr,
        risk_pct=(price - stop) / price * 100.0,
        reward_pct=(target - price) / price * 100.0,
        breakout_level=breakout_level if breakout_level is not None else price,
    )


def fetch_special(symbols: list[str] | None = None, thai: bool = True) -> list[SpecialItem]:
    symbols = [s.upper() for s in (symbols or config.SPECIAL_WATCHLIST)]
    items = {s: SpecialItem(symbol=s, name=display_name(s, thai)) for s in symbols}
    if not symbols:
        return []
    today = datetime.now(ET).date()

    pending = dict(items)
    if config.SPECIAL_DATA_SOURCE == "mt5":
        try:
            missing = _fill_from_mt5(pending)
        except Exception as exc:  # noqa: BLE001
            log.warning("MT5 failed for the special watchlist (%s); trying TradingView", exc)
            missing = list(pending)
        pending = {s: items[s] for s in missing}
    if pending and config.SPECIAL_DATA_SOURCE in ("tradingview", "mt5"):
        # TradingView symbols may differ from the configured (MT5) names; map and map back
        tv_names = {s: (tradingview_equivalent(s) or s) for s in pending}
        tv_items = {tv: pending[s] for s, tv in tv_names.items()}
        try:
            missing_tv = _fill_from_tradingview(tv_items)
        except Exception as exc:  # noqa: BLE001
            log.warning("TradingView failed for the special watchlist (%s); trying Yahoo", exc)
            missing_tv = list(tv_items)
        back = {tv: s for s, tv in tv_names.items()}
        pending = {back[tv]: items[back[tv]] for tv in missing_tv}
    if pending:
        try:
            _fill_from_yahoo(pending, today)
        except Exception as exc:  # noqa: BLE001
            log.warning("Yahoo failed for the special watchlist: %s", exc)

    for it in items.values():
        p = it.price
        if p is not None and it.yesterday_high is not None:
            it.above_yesterday_high = p > it.yesterday_high
        if p is not None and it.day_high:
            it.at_high_of_day = p >= it.day_high * (1.0 - config.HOD_TOLERANCE_PCT / 100.0)
        if it.yesterday_close is not None and it.sma200 is not None:
            it.uptrend = it.yesterday_close > it.sma200
        it.signal = bool(it.above_yesterday_high and it.at_high_of_day and it.uptrend)
        if p is not None:
            it.plan = build_plan(p, it.atr, it.yesterday_high)
    return [items[s] for s in symbols]


def fmt_price(v: float | None, currency: str = "") -> str:
    if v is None:
        return "n/a"
    if abs(v) >= 1000:
        text = f"{v:,.0f}"
    elif abs(v) >= 10:
        text = f"{v:,.2f}"
    else:
        text = f"{v:.4f}"
    return f"${text}" if currency.upper() == "USD" else (f"{text} {currency}" if currency else text)
