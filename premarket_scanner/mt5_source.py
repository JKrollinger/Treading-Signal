"""MetaTrader 5 data source: your broker's own prices and history.

Uses the official ``MetaTrader5`` Python package, which talks to the MT5 desktop
terminal running on this Windows PC (it must be open and logged in; a demo account is
fine). Provides, per broker symbol:

* live bid price, today's high/low, previous day high/low/close
* 200-day SMA and 14-day ATR from the broker's daily bars
* hourly history for the backtest

Phase 1 is read-only: nothing here places orders.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from . import config

log = logging.getLogger(__name__)

ET = ZoneInfo(config.MARKET_TZ)
_lock = threading.Lock()
_connected = False
_server_offset: float | None = None   # broker server clock minus UTC, seconds


def _mt5():
    import MetaTrader5 as mt5  # imported lazily: only needed when the source is "mt5"

    return mt5


def connect() -> bool:
    """Attach to the running terminal (or start it when MT5_PATH is set)."""
    global _connected
    mt5 = _mt5()
    with _lock:
        if _connected:
            return True
        kwargs: dict = {"timeout": config.MT5_TIMEOUT_MS}
        if config.MT5_PATH:
            kwargs["path"] = config.MT5_PATH
        if config.MT5_LOGIN:
            kwargs.update(login=config.MT5_LOGIN, password=config.MT5_PASSWORD, server=config.MT5_SERVER)
        ok = mt5.initialize(**kwargs)
        if not ok:
            log.warning("MT5 initialize failed: %s (is the MT5 terminal open and logged in?)", mt5.last_error())
            return False
        info = mt5.terminal_info()
        acc = mt5.account_info()
        log.info("MT5 connected: %s | account %s on %s (%s)",
                 getattr(info, "name", "?"), getattr(acc, "login", "?"), getattr(acc, "server", "?"),
                 "demo" if getattr(acc, "trade_mode", None) == 0 else "live/contest")
        _connected = True
        return True


def shutdown() -> None:
    global _connected
    with _lock:
        if _connected:
            _mt5().shutdown()
            _connected = False


def account_summary() -> dict:
    if not connect():
        return {}
    acc = _mt5().account_info()
    if acc is None:
        return {}
    return {
        "login": acc.login, "server": acc.server, "name": acc.name, "currency": acc.currency,
        "balance": acc.balance, "equity": acc.equity, "leverage": acc.leverage,
        "trade_mode": {0: "demo", 1: "contest", 2: "real"}.get(acc.trade_mode, str(acc.trade_mode)),
        "trade_allowed": acc.trade_allowed,
    }


def list_symbols(pattern: str | None = None) -> list[str]:
    """Broker symbol names; ``pattern`` is MT5's wildcard filter, e.g. '*USD*'."""
    if not connect():
        return []
    syms = _mt5().symbols_get(pattern) if pattern else _mt5().symbols_get()
    return [s.name for s in (syms or [])]


def _to_utc(server_epoch: int | float) -> datetime:
    """MT5 timestamps are the broker's server clock encoded as if UTC; correct the offset."""
    return datetime.fromtimestamp(float(server_epoch) - (_server_offset or 0.0), timezone.utc)


def _update_server_offset(latest_tick_epoch: float) -> None:
    """Estimate the server-clock offset from the freshest tick (rounded to 15 minutes)."""
    global _server_offset
    raw = latest_tick_epoch - time.time()
    _server_offset = round(raw / 900.0) * 900.0


def _fresh_tick(mt5, symbol: str, tries: int = 6):
    """A symbol just added to Market Watch has no tick for a moment; wait for the first one."""
    tick = None
    for _ in range(tries):
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None and (tick.bid or tick.last):
            return tick
        time.sleep(0.5)
    return tick


def symbol_paths(symbols: list[str]) -> dict[str, str]:
    """Broker folder of each symbol, e.g. 'Forex\\Majors\\EURUSD' or 'Crypto\\BTCUSD'."""
    mt5 = _mt5()
    if not connect():
        return {}
    out = {}
    for s in symbols:
        info = mt5.symbol_info(s)
        if info is not None:
            out[s] = info.path
    return out


def fetch_rows(symbols: list[str]) -> dict[str, dict]:
    """Level data per symbol in the same shape the other sources use."""
    mt5 = _mt5()
    if not connect():
        return {}
    out: dict[str, dict] = {}
    ticks: dict[str, object] = {}
    for s in symbols:
        if not mt5.symbol_select(s, True):
            log.warning("MT5: symbol %s not found at this broker", s)
            continue
        tick = _fresh_tick(mt5, s)
        if tick is not None:
            ticks[s] = tick
    live = [t.time for t in ticks.values() if t.bid or t.last]
    if live:
        _update_server_offset(max(live))

    for s, tick in ticks.items():
        info = mt5.symbol_info(s)
        if not (tick.bid or tick.last):
            # market closed for this symbol or no quote yet: use the last 1-minute bar
            m1 = mt5.copy_rates_from_pos(s, mt5.TIMEFRAME_M1, 0, 1)
            if m1 is not None and len(m1):
                log.info("MT5: %s has no live tick; using last M1 close", s)
                tick = type("T", (), {"bid": float(m1[-1]["close"]), "ask": 0.0, "last": 0.0,
                                      "time": int(m1[-1]["time"])})()
            else:
                log.warning("MT5: %s has no quote", s)
                continue
        rates = mt5.copy_rates_from_pos(s, mt5.TIMEFRAME_D1, 0, 260)
        if rates is None or len(rates) < 2:
            log.warning("MT5: no daily history for %s", s)
            continue
        df = pd.DataFrame(rates)
        today, yday = df.iloc[-1], df.iloc[-2]
        hist = df.iloc[:-1]                           # completed days only
        closes = hist["close"]
        sma200 = float(closes.tail(200).mean()) if len(closes) >= 200 else None
        prev_close = closes.shift(1)
        tr = pd.concat([hist["high"] - hist["low"], (hist["high"] - prev_close).abs(),
                        (hist["low"] - prev_close).abs()], axis=1).max(axis=1)
        atr = float(tr.tail(config.ATR_PERIOD).mean()) if len(tr) > config.ATR_PERIOD else None
        price = float(tick.bid) if tick.bid else float(tick.last)
        out[s] = {
            "price": price,
            "ask": float(tick.ask) if tick.ask else None,
            "day_high": float(today["high"]),
            "day_low": float(today["low"]),
            "yesterday_high": float(yday["high"]),
            "yesterday_low": float(yday["low"]),
            "yesterday_close": float(yday["close"]),
            "sma200": sma200,
            "atr": atr,
            "currency": getattr(info, "currency_profit", "") or "",
            "digits": getattr(info, "digits", None),
            "spread_points": getattr(info, "spread", None),
            "quote_time": _to_utc(tick.time).astimezone(ET),
            "change_pct": (price / float(yday["close"]) - 1.0) * 100.0 if yday["close"] else None,
        }
    return out


def server_offset_hours(symbol: str = "EURUSD") -> float:
    """Broker server clock minus UTC, in hours (estimated from a fresh tick)."""
    mt5 = _mt5()
    if not connect():
        return 0.0
    if _server_offset is None:
        mt5.symbol_select(symbol, True)
        tick = _fresh_tick(mt5, symbol)
        if tick is not None and tick.time:
            _update_server_offset(float(tick.time))
    return (_server_offset or 0.0) / 3600.0


def hourly_history(symbol: str, days: int = 730) -> pd.DataFrame:
    """H1 bars as a DataFrame with open/high/low/close.

    The index is the broker's server time (so a calendar day matches the broker's daily
    bars); ``df.attrs['tz_offset_hours']`` holds server minus UTC for session filters.
    """
    mt5 = _mt5()
    if not connect():
        return pd.DataFrame()
    mt5.symbol_select(symbol, True)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    start = end - timedelta(days=days + 1)
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_H1, start, end)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df.index = pd.to_datetime(df["time"], unit="s")          # naive = server time
    df = df[["open", "high", "low", "close", "tick_volume"]].rename(columns={"tick_volume": "volume"})
    df.attrs["tz_offset_hours"] = server_offset_hours(symbol)
    return df
