"""Yahoo Finance quotes: batch fetch and normalisation into :class:`Candidate`."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from yfinance.data import YfData

from . import config
from .models import Candidate

log = logging.getLogger(__name__)

ET = ZoneInfo(config.MARKET_TZ)
QUOTE_URL = "https://query2.finance.yahoo.com/v7/finance/quote"
QUOTE_BATCH = 200
QUOTE_FIELDS = [
    "symbol", "shortName", "longName", "quoteType", "exchange", "fullExchangeName",
    "currency", "marketState", "hasPrePostMarketData",
    "regularMarketPrice", "regularMarketPreviousClose", "regularMarketChangePercent",
    "regularMarketChange", "regularMarketTime", "regularMarketVolume", "regularMarketOpen",
    "regularMarketDayHigh", "regularMarketDayLow",
    "preMarketPrice", "preMarketChange", "preMarketChangePercent", "preMarketTime",
    "postMarketPrice", "postMarketChangePercent", "postMarketTime",
]


def now_et() -> datetime:
    return datetime.now(ET)


def fetch_quotes(symbols: list[str], batch_size: int = QUOTE_BATCH) -> list[dict]:
    """Quote many symbols with Yahoo's v7 endpoint (yfinance handles cookie/crumb auth)."""
    data = YfData()
    out: list[dict] = []
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        try:
            resp = data.get(
                QUOTE_URL,
                params={"symbols": ",".join(batch), "fields": ",".join(QUOTE_FIELDS)},
                timeout=config.HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            result = resp.json().get("quoteResponse", {}).get("result") or []
            out.extend(result)
            log.info("quotes: %d/%d symbols", min(i + batch_size, len(symbols)), len(symbols))
        except Exception as exc:  # noqa: BLE001
            log.warning("quote batch starting at %s failed: %s", batch[0], exc)
        if i + batch_size < len(symbols):
            time.sleep(0.2)
    return out


def _ts_to_et(ts: int | float | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), ET)
    except (OverflowError, OSError, ValueError):
        return None


def normalize_quote(q: dict, scan_date: date) -> Candidate | None:
    """Turn a raw Yahoo quote into a Candidate using today's pre-market data.

    Pre-market fields are used when Yahoo has a pre-market trade for ``scan_date``.
    If the script runs after the open, today's regular-session change is used instead
    and ``session`` is set to ``"regular"`` so the output is never silently stale.
    Quotes whose latest trade is not from ``scan_date`` are dropped.
    """
    symbol = q.get("symbol")
    if not symbol or q.get("quoteType", "EQUITY") != "EQUITY":
        return None

    pre_time = _ts_to_et(q.get("preMarketTime"))
    pre_price = q.get("preMarketPrice")
    reg_time = _ts_to_et(q.get("regularMarketTime"))
    reg_price = q.get("regularMarketPrice")
    pre_fresh = bool(pre_time and pre_time.date() == scan_date and pre_price)
    reg_fresh = bool(reg_time and reg_time.date() == scan_date and reg_price)

    # Yahoo keeps this morning's pre-market fields on the quote all day, so once the
    # regular session has traded (its timestamp is newer) the live regular price wins.
    if pre_fresh and not (reg_fresh and reg_time > pre_time):
        price = float(pre_price)
        pct = q.get("preMarketChangePercent")
        change = q.get("preMarketChange")
        session, quote_time = "premarket", pre_time
    elif reg_fresh:
        price = float(reg_price)
        pct = q.get("regularMarketChangePercent")
        change = q.get("regularMarketChange")
        session, quote_time = "regular", reg_time
    else:
        return None

    # Yahoo measures the pre-market change against the last regular close, but during
    # pre-market `regularMarketPreviousClose` can still point one session further back
    # for some symbols. Derive the base from the change itself so that price, previous
    # close and gap always agree.
    if change is not None:
        prev_close = price - float(change)
    elif pct is not None:
        prev_close = price / (1.0 + float(pct) / 100.0)
    else:
        prev_close = q.get("regularMarketPreviousClose")
    if not prev_close or float(prev_close) <= 0:
        return None
    if pct is None:
        pct = (price / float(prev_close) - 1.0) * 100.0

    return Candidate(
        ticker=symbol,
        name=q.get("longName") or q.get("shortName") or symbol,
        price=price,
        previous_close=float(prev_close),
        gap_pct=float(pct),
        session=session,
        market_state=str(q.get("marketState") or ""),
        quote_time=quote_time,
        exchange=str(q.get("fullExchangeName") or q.get("exchange") or ""),
    )
