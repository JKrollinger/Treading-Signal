"""Time and news filters shared by the live watch and the backtest.

* session: new signals only between SESSION_UTC_START and SESSION_UTC_END (UTC hours)
* Friday: none from FRIDAY_CUTOFF_UTC on, to avoid holding a fresh breakout into the
  weekend gap
* news: none within NEWS_BLACKOUT_MIN minutes of a high-impact US release. The backtest
  uses recurring events only (non-farm payrolls, FOMC); live mode also reads this week's
  calendar from ForexFactory when available.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests

from . import config

log = logging.getLogger(__name__)

# FOMC decision days (second day of each scheduled meeting)
FOMC_DATES = {
    date(2024, 1, 31), date(2024, 3, 20), date(2024, 5, 1), date(2024, 6, 12),
    date(2024, 7, 31), date(2024, 9, 18), date(2024, 11, 7), date(2024, 12, 18),
    date(2025, 1, 29), date(2025, 3, 19), date(2025, 5, 7), date(2025, 6, 18),
    date(2025, 7, 30), date(2025, 9, 17), date(2025, 10, 29), date(2025, 12, 10),
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
    date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9),
}
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_TTL = 60 * 60
_cal_lock = threading.Lock()
_cal_cache: tuple[float, list[dict]] | None = None


def applies_to(symbol: str) -> bool:
    from .special import base_symbol

    base = base_symbol(symbol)
    return base in config.TIME_FILTER_SYMBOLS or symbol.upper() in config.TIME_FILTER_SYMBOLS


def in_session(now_utc: datetime, start: int = config.SESSION_UTC_START, end: int = config.SESSION_UTC_END) -> bool:
    return start <= now_utc.hour < end


def friday_late(now_utc: datetime, cutoff: int = config.FRIDAY_CUTOFF_UTC) -> bool:
    return now_utc.weekday() == 4 and now_utc.hour >= cutoff


def _first_friday(d: date) -> bool:
    return d.weekday() == 4 and d.day <= 7


def in_recurring_blackout(d: date, utc_hour: int) -> bool:
    """Backtest-grade rule: payrolls (first Friday 12:00-14:00 UTC) and FOMC (17:30-20:00 UTC)."""
    if _first_friday(d) and 12 <= utc_hour < 14:
        return True
    if d in FOMC_DATES and 17 <= utc_hour < 20:
        return True
    return False


# Live calendar ---------------------------------------------------------------------
def weekly_events() -> list[dict]:
    """This week's high-impact events from ForexFactory: [{time(utc datetime), currency, title}]."""
    global _cal_cache
    if not config.NEWS_CALENDAR_ENABLED:
        return []
    with _cal_lock:
        if _cal_cache and time.time() - _cal_cache[0] < CALENDAR_TTL:
            return _cal_cache[1]
    events: list[dict] = []
    try:
        resp = requests.get(CALENDAR_URL, headers={"User-Agent": config.USER_AGENT}, timeout=20)
        resp.raise_for_status()
        for e in resp.json():
            if str(e.get("impact", "")).lower() != "high":
                continue
            try:
                when = datetime.fromisoformat(str(e.get("date")).replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                continue
            events.append({"time": when, "currency": str(e.get("country") or "").upper(), "title": str(e.get("title") or "")})
    except Exception as exc:  # noqa: BLE001
        log.warning("news calendar unavailable (%s); using recurring NFP/FOMC rule only", exc)
        with _cal_lock:
            if _cal_cache:
                return _cal_cache[1]
        return []
    with _cal_lock:
        _cal_cache = (time.time(), events)
    log.info("news calendar: %d high-impact events this week", len(events))
    return events


def upcoming_blackout(now_utc: datetime, currencies: tuple[str, ...] = ("USD",),
                      minutes: int = config.NEWS_BLACKOUT_MIN) -> dict | None:
    """The high-impact event (if any) whose +-minutes window contains ``now_utc``."""
    window = timedelta(minutes=minutes)
    for e in weekly_events():
        if e["currency"] in currencies and e["time"] - window <= now_utc <= e["time"] + window:
            return e
    return None


def check_new_signal(symbol: str, now_utc: datetime) -> str | None:
    """None when a new signal is allowed; otherwise a short reason (Thai) why it is held back."""
    if not applies_to(symbol):
        return None
    if not in_session(now_utc):
        return (f"นอกช่วงเวลาซื้อขายหลัก (รับสัญญาณ {config.SESSION_UTC_START:02d}:00-{config.SESSION_UTC_END:02d}:00 UTC "
                f"= {(config.SESSION_UTC_START + 7) % 24:02d}:00-{(config.SESSION_UTC_END + 7) % 24:02d}:00 น. ไทย)")
    if friday_late(now_utc):
        return f"วันศุกร์หลัง {config.FRIDAY_CUTOFF_UTC:02d}:00 UTC งดสัญญาณใหม่ก่อนปิดสัปดาห์"
    if in_recurring_blackout(now_utc.date(), now_utc.hour):
        return "อยู่ในช่วงข่าวแรงของสหรัฐ (ตัวเลขจ้างงานหรือการประชุมเฟด)"
    ev = upcoming_blackout(now_utc)
    if ev:
        return f"อยู่ในช่วง {config.NEWS_BLACKOUT_MIN} นาทีรอบข่าวแรง: {ev['title']} ({ev['currency']})"
    return None
