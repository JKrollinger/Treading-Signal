"""Pre-market volume from Nasdaq.com's public extended-trading endpoint.

Yahoo Finance reports zero volume on its pre-market minute bars, so the volume
screen uses Nasdaq's quote API instead. For each symbol it returns the
consolidated pre-market volume of the current session (04:00-09:30 ET).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import requests

from . import config
from .models import Candidate

log = logging.getLogger(__name__)

NASDAQ_URL = "https://api.nasdaq.com/api/quote/{symbol}/extended-trading"
HEADERS = {
    "User-Agent": config.USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
WORKERS = 4
RETRIES = 2
TIMEOUT_SECONDS = 15      # Nasdaq occasionally hangs; a short timeout keeps a busy morning fast
PAUSE_SECONDS = 0.15
SOURCE = "nasdaq"

_DATE_RE = re.compile(r"([A-Z][a-z]{2} \d{1,2}, \d{4})")
_local = threading.local()


def _session() -> requests.Session:
    sess = getattr(_local, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update(HEADERS)
        _local.session = sess
    return sess


def _nasdaq_symbol(ticker: str) -> str:
    # Yahoo writes share classes as BRK-B; Nasdaq's API expects BRK.B
    return ticker.replace("-", ".")


def _to_int(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _updated_on(data: dict) -> date | None:
    """Parse 'Data last updated Oct 22, 2026 08:15 AM ET.' into a date."""
    for line in data.get("lastUpdateInfo") or []:
        m = _DATE_RE.search(str(line))
        if m:
            try:
                return datetime.strptime(m.group(1), "%b %d, %Y").date()
            except ValueError:
                return None
    return None


def fetch_premarket_volume(ticker: str, scan_date: date) -> int | None:
    """Today's pre-market volume; 0 when there are no pre-market trades yet; None if unknown."""
    url = NASDAQ_URL.format(symbol=_nasdaq_symbol(ticker))
    last_error: Exception | None = None

    for attempt in range(1, RETRIES + 1):
        try:
            resp = _session().get(
                url,
                params={"markettype": "pre", "assetclass": "stocks"},
                timeout=TIMEOUT_SECONDS,
            )
            if resp.status_code in (403, 429, 500, 502, 503):
                raise RuntimeError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(1.5 * attempt)
            continue

        data = payload.get("data") or {}
        message = str(payload.get("message") or "")
        rows = (data.get("infoTable") or {}).get("rows") or []

        if rows and rows[0].get("volume"):
            updated = _updated_on(data)
            if updated and updated != scan_date:
                log.info("%s: Nasdaq pre-market data is from %s, not %s", ticker, updated, scan_date)
                return 0
            return _to_int(rows[0]["volume"])
        if "no trades" in message.lower():
            return 0
        log.debug("%s: unexpected Nasdaq payload: %s", ticker, str(payload)[:200])
        return None

    log.warning("%s: Nasdaq volume lookup failed after %d attempts: %s", ticker, RETRIES, last_error)
    return None


def attach_premarket_volume(candidates: list[Candidate], scan_date: date) -> int:
    """Fill ``premarket_volume`` on every candidate. Returns how many lookups failed."""
    if not candidates:
        return 0

    def work(c: Candidate) -> None:
        vol = fetch_premarket_volume(c.ticker, scan_date)
        c.premarket_volume = vol
        c.volume_source = SOURCE if vol is not None else None
        time.sleep(PAUSE_SECONDS)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(work, candidates))

    failed = sum(1 for c in candidates if c.premarket_volume is None)
    log.info("pre-market volume: %d/%d symbols resolved", len(candidates) - failed, len(candidates))
    return failed
