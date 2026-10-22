"""TradingView screener API client (the same data behind tradingview.com/screener).

Fetches, per ticker, the current daily bar (close = last price, high = high of day),
the previous daily bar (``high[1]``, ``close[1]``), the 200-day SMA now and as of the
previous bar, and today's pre-market high.

Note on bar timing: before the 09:30 ET open TradingView's "current" daily bar is still
yesterday's completed bar, so ``[1]`` then means the day before yesterday. The caller
decides which pair to treat as "yesterday" (see strategy.build_snapshots).

Anonymous requests return 15-minute delayed prices (``update_mode`` =
``delayed_streaming_900``). Set ``TRADINGVIEW_SESSIONID`` to the ``sessionid`` cookie of a
logged-in account with real-time US data to get streaming prices.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from . import config

log = logging.getLogger(__name__)

SCAN_URL = "https://scanner.tradingview.com/america/scan"
GLOBAL_SCAN_URL = "https://scanner.tradingview.com/global/scan"
HEADERS = {
    "User-Agent": config.USER_AGENT,
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}
EXCHANGES = ["NASDAQ", "NYSE", "AMEX"]
COLUMNS = [
    "name", "exchange", "close", "high", "high[1]", "close[1]",
    "SMA200", "SMA200[1]", "premarket_high", "update_mode",
]
BATCH = 50
STREAMING_MODES = {"streaming", "realtime", "rt_streaming"}


@dataclass
class TvRow:
    symbol: str                   # e.g. "NASDAQ:QS"
    close: float | None           # current daily bar: last price
    high: float | None            # current daily bar: high of day
    high_prev: float | None       # previous daily bar high
    close_prev: float | None      # previous daily bar close
    sma200: float | None          # 200-day SMA on the current bar
    sma200_prev: float | None     # 200-day SMA on the previous bar
    premarket_high: float | None
    update_mode: str

    @property
    def streaming(self) -> bool:
        return self.update_mode in STREAMING_MODES


def _tv_name(ticker: str) -> str:
    # Yahoo writes share classes as BRK-B; TradingView uses BRK.B
    return ticker.upper().replace("-", ".")


def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _post(names: list[str]) -> list[dict]:
    body = {
        "markets": ["america"],
        "symbols": {"query": {"types": ["stock", "dr"]}},
        "filter": [
            {"left": "name", "operation": "in_range", "right": names},
            {"left": "exchange", "operation": "in_range", "right": EXCHANGES},
        ],
        "columns": COLUMNS,
        "range": [0, max(100, len(names) * 3)],
    }
    cookies = {"sessionid": config.TRADINGVIEW_SESSIONID} if config.TRADINGVIEW_SESSIONID else None
    resp = requests.post(SCAN_URL, json=body, headers=HEADERS, cookies=cookies, timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("data") or []


def fetch_rows(tickers: list[str], columns: list[str]) -> dict[str, dict]:
    """Any TradingView symbols (``EXCHANGE:SYMBOL``: forex, CFD/spot gold, crypto, ...).

    Returns ``{symbol: {column: value}}``. Forex, commodity and crypto quotes stream in
    real time even without a session cookie.
    """
    out: dict[str, dict] = {}
    tickers = [t.upper() for t in tickers]
    cookies = {"sessionid": config.TRADINGVIEW_SESSIONID} if config.TRADINGVIEW_SESSIONID else None
    for i in range(0, len(tickers), BATCH):
        batch = tickers[i : i + BATCH]
        resp = requests.post(
            GLOBAL_SCAN_URL,
            json={"symbols": {"tickers": batch}, "columns": columns},
            headers=HEADERS, cookies=cookies, timeout=config.HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        for item in resp.json().get("data") or []:
            out[str(item.get("s") or "").upper()] = dict(zip(columns, item.get("d") or []))
    missing = [t for t in tickers if t not in out]
    if missing:
        log.warning("tradingview: no data for %s", ", ".join(missing))
    return out


def fetch_levels(tickers: list[str]) -> dict[str, TvRow]:
    """Return a TvRow per ticker (keyed by the original Yahoo-style ticker)."""
    out: dict[str, TvRow] = {}
    by_name = {_tv_name(t): t for t in tickers}
    names = list(by_name)
    for i in range(0, len(names), BATCH):
        batch = names[i : i + BATCH]
        for item in _post(batch):
            values = dict(zip(COLUMNS, item.get("d") or []))
            ticker = by_name.get(str(values.get("name") or "").upper())
            if ticker is None or ticker in out:
                continue
            out[ticker] = TvRow(
                symbol=str(item.get("s") or f"{values.get('exchange')}:{values.get('name')}"),
                close=_num(values.get("close")),
                high=_num(values.get("high")),
                high_prev=_num(values.get("high[1]")),
                close_prev=_num(values.get("close[1]")),
                sma200=_num(values.get("SMA200")),
                sma200_prev=_num(values.get("SMA200[1]")),
                premarket_high=_num(values.get("premarket_high")),
                update_mode=str(values.get("update_mode") or ""),
            )
    missing = [t for t in tickers if t not in out]
    log.info("tradingview: %d/%d symbols found%s", len(out), len(tickers),
             f" (missing: {', '.join(missing)})" if missing else "")
    return out
