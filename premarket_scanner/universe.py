"""Build the list of tickers to scan.

Two sources are supported (``UNIVERSE_SOURCE`` in ``.env``):

* ``yahoo`` (default) - page through the Yahoo Finance equity screener for every
  US-listed stock above the configured price and average-volume floor. The screener
  returns full quote objects, so no second request is needed for prices.
* ``file`` - read tickers from ``WATCHLIST_FILE`` (one per line) and quote them.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf
from yfinance import EquityQuery

from . import config, yahoo

log = logging.getLogger(__name__)

PAGE_SIZE = 250            # Yahoo caps a single screener page at 250 rows
PAGE_PAUSE_SECONDS = 0.25
CACHE_MAX_AGE_HOURS = 24 * 7


def _cache_file() -> Path:
    return config.DATA_DIR / "universe_yahoo_symbols.json"


def _dedupe(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in symbols:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def load_watchlist(path: Path | None = None) -> list[str]:
    """Read one ticker per line; ``#`` starts a comment."""
    path = path or config.WATCHLIST_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"Watchlist not found: {path}. Create it with one ticker per line, "
            "or set UNIVERSE_SOURCE=yahoo."
        )
    symbols = []
    for line in path.read_text(encoding="utf-8").splitlines():
        token = line.split("#", 1)[0].strip().upper()
        if token:
            symbols.append(token)
    return _dedupe(symbols)


def _screener_query(min_price: float, min_avg_volume: int) -> EquityQuery:
    return EquityQuery(
        "and",
        [
            EquityQuery("eq", ["region", "us"]),
            EquityQuery("gt", ["intradayprice", min_price]),
            EquityQuery("gt", ["avgdailyvol3m", min_avg_volume]),
        ],
    )


def fetch_yahoo_universe(
    min_price: float = config.MIN_PRICE,
    min_avg_volume: int = config.UNIVERSE_MIN_AVG_VOLUME,
    max_symbols: int = config.MAX_UNIVERSE,
) -> list[dict]:
    """Return raw Yahoo quote dicts for every US stock matching the filters."""
    query = _screener_query(min_price, min_avg_volume)
    quotes: list[dict] = []
    seen: set[str] = set()
    offset = 0
    total: int | None = None

    while offset < max_symbols:
        size = min(PAGE_SIZE, max_symbols - offset)
        page = yf.screen(query, offset=offset, size=size, sortField="avgdailyvol3m", sortAsc=False)
        batch = page.get("quotes") or []
        total = page.get("total", total)
        for q in batch:
            sym = q.get("symbol")
            if sym and sym not in seen and q.get("quoteType", "EQUITY") == "EQUITY":
                seen.add(sym)
                quotes.append(q)
        offset += len(batch)
        log.info("screener page: offset=%d got=%d total=%s", offset, len(batch), total)
        if not batch or len(batch) < size or (total is not None and offset >= total):
            break
        time.sleep(PAGE_PAUSE_SECONDS)

    _write_cache([q["symbol"] for q in quotes])
    return quotes


def _write_cache(symbols: list[str]) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _cache_file().write_text(
            json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "symbols": symbols}),
            encoding="utf-8",
        )
    except OSError as exc:  # cache is best-effort
        log.warning("could not write universe cache: %s", exc)


def _read_cache() -> list[str]:
    path = _cache_file()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(payload["fetched_at"])
        age_h = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600
        if age_h > CACHE_MAX_AGE_HOURS:
            log.warning("universe cache is %.0f hours old; using it anyway", age_h)
        return list(payload.get("symbols") or [])
    except (OSError, ValueError, KeyError) as exc:
        log.warning("could not read universe cache: %s", exc)
        return []


def get_universe_quotes(warnings: list[str] | None = None) -> list[dict]:
    """Return raw Yahoo quote dicts for the whole universe, per ``UNIVERSE_SOURCE``."""
    warnings = warnings if warnings is not None else []

    if config.UNIVERSE_SOURCE == "file":
        symbols = load_watchlist()
        log.info("watchlist universe: %d symbols from %s", len(symbols), config.WATCHLIST_FILE)
        return yahoo.fetch_quotes(symbols)

    try:
        quotes = fetch_yahoo_universe()
        log.info("yahoo screener universe: %d symbols", len(quotes))
        return quotes
    except Exception as exc:  # noqa: BLE001 - any screener failure falls back to the cache
        log.error("Yahoo screener failed: %s", exc)
        cached = _read_cache()
        if not cached:
            raise RuntimeError(
                "Yahoo screener failed and no cached universe is available"
            ) from exc
        warnings.append(f"Yahoo screener failed ({exc}); used cached universe of {len(cached)} symbols")
        return yahoo.fetch_quotes(cached)
