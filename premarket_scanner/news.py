"""Catalyst headlines.

Primary source is the Benzinga News API (set ``BENZINGA_API_KEY``). When no key is
configured, or Benzinga rejects it, Yahoo Finance headlines are used instead so the
catalyst column is never silently empty.
"""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests
from yfinance.data import YfData

from . import config
from .models import Candidate

log = logging.getLogger(__name__)

BENZINGA_URL = "https://api.benzinga.com/api/v2/news"
YAHOO_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
# Publishers whose items are usually the company's own announcement (the real catalyst)
PRESS_WIRES = ("benzinga", "pr newswire", "globenewswire", "business wire", "accesswire", "newsfile")
MAX_SENTENCE_CHARS = 220
PAUSE_SECONDS = 0.2
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


class BenzingaAuthError(RuntimeError):
    """Benzinga rejected the API key."""


# Text helpers ------------------------------------------------------------------
def clean_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", str(raw)))
    return _WS_RE.sub(" ", text).strip()


def one_sentence(*parts: str | None) -> str | None:
    """First sentence of the first non-empty text, trimmed to a readable length."""
    for part in parts:
        text = clean_text(part)
        if not text:
            continue
        sentence = _SENTENCE_SPLIT_RE.split(text, maxsplit=1)[0].strip()
        if len(sentence) > MAX_SENTENCE_CHARS:
            sentence = sentence[:MAX_SENTENCE_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "..."
        if sentence and sentence[-1] not in ".!?":
            sentence += "."
        return sentence
    return None


def _cutoff(lookback_hours: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=lookback_hours)


_NAME_STOPWORDS = {
    "the", "inc", "corp", "corporation", "co", "ltd", "plc", "holdings", "holding", "group",
    "company", "limited", "sa", "nv", "ag", "llc", "lp", "trust", "fund", "class", "common",
    "stock", "shares", "ordinary", "adr", "ads", "american", "depositary", "and", "of",
}


def _mentions_company(title: str | None, company_name: str | None) -> int:
    """1 when the headline contains a distinctive word of the company name, else 0."""
    if not title or not company_name:
        return 0
    words = [w.strip(",.()&-'\"") for w in str(company_name).split()]
    keywords = [w for w in words if len(w) >= 3 and w.lower() not in _NAME_STOPWORDS]
    if not keywords:
        return 0
    text = str(title).lower()
    return 1 if any(k.lower() in text for k in keywords[:2]) else 0


# Benzinga ----------------------------------------------------------------------
def _parse_benzinga_time(value) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        dt = parsedate_to_datetime(text)          # "Thu, 22 Oct 2026 08:01:12 -0400"
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(text)      # "2026-10-22T08:01:12-04:00"
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_benzinga_catalyst(
    ticker: str,
    company_name: str = "",
    lookback_hours: int = config.NEWS_LOOKBACK_HOURS,
    api_key: str = config.BENZINGA_API_KEY,
) -> dict | None:
    """Best recent Benzinga story for ``ticker`` within the lookback window.

    Ranking: "Why Is It Moving" (WIIM) briefs first, then headlines naming the company,
    then stories tagged with this ticker alone, then the newest.
    """
    cutoff = _cutoff(lookback_hours)
    symbol = ticker.upper()
    params = {
        "token": api_key,
        "tickers": ticker,
        "pageSize": 15,
        "displayOutput": "abstract",       # headline + teaser (first sentence of the story)
        "dateFrom": cutoff.strftime("%Y-%m-%d"),
        "sort": "created:desc",
    }
    resp = requests.get(
        BENZINGA_URL,
        params=params,
        headers={"Accept": "application/json", "User-Agent": config.USER_AGENT},
        timeout=config.HTTP_TIMEOUT,
    )
    if resp.status_code in (401, 403):
        raise BenzingaAuthError(f"Benzinga rejected the API key (HTTP {resp.status_code})")
    if resp.status_code == 404:  # documented: no stories match the filters
        return None
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("news") or payload.get("items") or []

    scored: list[tuple[tuple[int, int, int, datetime], str, str | None, datetime | None]] = []
    for item in payload or []:
        created = _parse_benzinga_time(item.get("created") or item.get("updated"))
        if created and created < cutoff:
            continue
        sentence = one_sentence(item.get("teaser"), item.get("title"))
        if not sentence:
            continue
        names = {str(s.get("name") or "").upper() for s in (item.get("stocks") or [])}
        channels = {str(ch.get("name") or "").upper() for ch in (item.get("channels") or [])}
        wiim = 1 if "WIIM" in channels else 0
        named = _mentions_company(item.get("title"), company_name)
        sole = 1 if names == {symbol} else 0
        scored.append(((wiim, named, sole, created or _EPOCH), sentence, item.get("url"), created))

    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    _, sentence, url, created = scored[0]
    return {
        "catalyst": sentence,
        "source": "benzinga",
        "url": url,
        "time": created.isoformat() if created else None,
    }


# Yahoo fallback ----------------------------------------------------------------
def _yahoo_news_items(ticker: str, count: int = 12) -> list[dict]:
    """Raw Yahoo search results; unlike ``yfinance.Search`` these keep ``relatedTickers``."""
    resp = YfData().get(
        YAHOO_SEARCH_URL,
        params={
            "q": ticker,
            "newsCount": count,
            "quotesCount": 0,
            "enableFuzzyQuery": "false",
            "enableNavLinks": "false",
        },
        timeout=config.HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("news") or []


def fetch_yahoo_catalyst(
    ticker: str,
    company_name: str = "",
    lookback_hours: int = config.NEWS_LOOKBACK_HOURS,
) -> dict | None:
    """Best recent Yahoo Finance headline tagged with ``ticker``.

    Ranking: headlines naming the company first, then stories tagged with this ticker
    alone (not market round-ups), then press-wire announcements, then the newest.
    """
    cutoff = _cutoff(lookback_hours)
    symbol = ticker.upper()
    try:
        items = _yahoo_news_items(ticker)
    except Exception as exc:  # noqa: BLE001
        log.warning("%s: Yahoo news lookup failed: %s", ticker, exc)
        return None

    tagged_any = any(item.get("relatedTickers") for item in items)
    scored: list[tuple[tuple[int, int, int, datetime], dict]] = []
    for item in items:
        related = {str(t).upper() for t in (item.get("relatedTickers") or [])}
        if tagged_any and symbol not in related:
            continue
        ts = item.get("providerPublishTime")
        published = datetime.fromtimestamp(int(ts), timezone.utc) if ts else None
        if published and published < cutoff:
            continue
        named = _mentions_company(item.get("title"), company_name)
        sole = 1 if related == {symbol} else 0
        wire = 1 if str(item.get("publisher") or "").lower().startswith(PRESS_WIRES) else 0
        scored.append(((named, sole, wire, published or _EPOCH), item))
    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    (_, _, _, published), item = scored[0]
    sentence = one_sentence(item.get("summary"), item.get("title"))
    if not sentence:
        return None
    publisher = item.get("publisher") or "Yahoo Finance"
    return {
        "catalyst": sentence,
        "source": f"yahoo/{publisher}",
        "url": item.get("link"),
        "time": published.isoformat() if published != _EPOCH else None,
    }


# Orchestration -----------------------------------------------------------------
def attach_catalysts(candidates: list[Candidate], warnings: list[str]) -> None:
    """Fill the catalyst fields of every candidate in place."""
    if not candidates:
        return
    use_benzinga = bool(config.BENZINGA_API_KEY)
    if not use_benzinga:
        warnings.append("BENZINGA_API_KEY not set: catalysts come from Yahoo Finance headlines instead of Benzinga")

    for c in candidates:
        info = None
        if use_benzinga:
            try:
                info = fetch_benzinga_catalyst(c.ticker, c.name)
            except BenzingaAuthError as exc:
                use_benzinga = False
                warnings.append(f"{exc}; falling back to Yahoo Finance headlines")
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: Benzinga lookup failed: %s", c.ticker, exc)
        if info is None:
            info = fetch_yahoo_catalyst(c.ticker, c.name)
        if info:
            c.catalyst = info["catalyst"]
            c.catalyst_source = info["source"]
            c.catalyst_url = info.get("url")
            c.catalyst_time = info.get("time")
        time.sleep(PAUSE_SECONDS)

    found = sum(1 for c in candidates if c.catalyst)
    log.info("catalysts: %d/%d symbols have a headline", found, len(candidates))
