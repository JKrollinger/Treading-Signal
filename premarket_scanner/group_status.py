"""Daily status for the groups/channels on days without an entry signal.

The groups only get entry and exit alerts, so while no symbol qualifies they can stay
silent for days and the bot looks broken. On weekdays at GROUP_STATUS_TIME_BKK (Bangkok
time) this sends one short note: each watchlist symbol, its price, and how far yesterday's
close sits from the 200-day average (the uptrend rule every entry signal needs). It is
skipped when an entry signal reached the groups in the last 24 hours, and sent at most
once per Bangkok day. State lives in data/group_status.json.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, special, special_watch
from .trade_fmt import DOWN

log = logging.getLogger(__name__)

BKK = ZoneInfo("Asia/Bangkok")
QUIET_HOURS = 24        # no status when an entry signal reached the groups this recently
NEAR_LINE_PCT = 1.0     # "close to the line" note within this distance of the 200-day average
_lock = threading.Lock()


def state_file() -> Path:
    return config.DATA_DIR / "group_status.json"


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _update(path: Path, **fields) -> None:
    with _lock:
        data = _load(path)
        data.update(fields)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def note_signal(when: datetime | None = None, path: Path | None = None) -> None:
    """Remember that an entry signal reached the groups (that day needs no status note)."""
    try:
        _update(path or state_file(), last_signal_at=(when or datetime.now(BKK)).isoformat(timespec="seconds"))
    except OSError as exc:
        log.warning("could not record the group signal time: %s", exc)


def _gap_pct(it: special.SpecialItem) -> float | None:
    """Yesterday's close vs the 200-day average, in % (the same inputs as the uptrend rule)."""
    if it.yesterday_close is None or not it.sma200:
        return None
    return (it.yesterday_close / it.sma200 - 1.0) * 100.0


def _line(it: special.SpecialItem, thai: bool) -> str:
    name = f"{it.name} ({it.symbol})"
    if it.price is None:
        return f"• {name} " + ("ไม่มีข้อมูลราคา" if thai else "no price")
    price = special.fmt_price(it.price, it.currency)
    gap = _gap_pct(it)
    if gap is None or it.uptrend is None:
        return f"• {name} {price} — " + ("ยังคำนวณเส้นเฉลี่ย 200 วันไม่ได้" if thai else "200-day average unavailable")
    near = abs(gap) < NEAR_LINE_PCT
    if it.uptrend:
        if thai:
            return f"• {name} {price} — เหนือเส้นเฉลี่ย 200 วัน {gap:.1f}% ✅ รอราคาทำจุดสูงสุดใหม่"
        return f"• {name} {price} — {gap:.1f}% above the 200-day average ✅ waiting for a new high"
    if thai:
        return f"• {name} {price} — ต่ำกว่าเส้นเฉลี่ย 200 วัน {abs(gap):.1f}% {DOWN}" + (" ใกล้เส้นแล้ว" if near else "")
    return f"• {name} {price} — {abs(gap):.1f}% below the 200-day average {DOWN}" + (" (close)" if near else "")


def build_message(items: list[special.SpecialItem], open_recs: list[dict], now: datetime, thai: bool) -> str:
    day = now.astimezone(BKK).strftime("%d/%m")
    every = config.SPECIAL_WATCH_INTERVAL_MIN
    if thai:
        lines = [f"📊 สรุปประจำวัน {day} — ยังไม่มีสัญญาณซื้อใหม่"]
    else:
        lines = [f"📊 Daily status {day} — no new buy signal"]
    lines += [_line(it, thai) for it in items]
    if open_recs:
        tags = ", ".join(special_watch._tag(r) for r in open_recs)
        lines.append(f"📌 สัญญาณที่ยังถืออยู่: {tags}" if thai else f"📌 Still open: {tags}")
    if thai:
        lines.append("สัญญาณซื้อจะมาเมื่อราคาปิดเหนือเส้นเฉลี่ย 200 วัน แล้วขึ้นทำจุดสูงสุดใหม่เหนือของเมื่อวาน")
        lines.append(f"บอทยังเฝ้าตลาดทุก {every} นาที มีสัญญาณจะแจ้งทันที")
    else:
        lines.append("A buy signal needs a close above the 200-day average, then a new high above yesterday's.")
        lines.append(f"The bot still checks every {every} minutes and alerts the moment one appears.")
    return "\n".join(lines)


def send_daily(now: datetime | None = None, path: Path | None = None) -> str | None:
    """Send today's status to the groups if it is due; returns the text sent (None = skipped)."""
    from . import telegram_notify as tg   # local import: telegram_notify calls note_signal()

    now = (now or datetime.now(BKK)).astimezone(BKK)
    path = path or state_file()
    state = _load(path)
    today = now.date().isoformat()
    if state.get("last_status_day") == today:
        log.info("group status: already sent today")
        return None
    if not tg.extra_chat_ids():
        log.info("group status: no groups configured (TELEGRAM_EXTRA_CHAT_IDS)")
        return None
    last = state.get("last_signal_at")
    if last and now - datetime.fromisoformat(last) < timedelta(hours=QUIET_HOURS):
        log.info("group status: skipped, an entry signal reached the groups at %s", last)
        return None
    thai = tg.is_thai()
    items = special.fetch_special(thai=thai)
    if not items or all(it.price is None for it in items):
        log.warning("group status: no prices for the watchlist; nothing sent")
        return None
    watch = special_watch.load_state()
    open_recs = [r for r in special_watch.visible_records(watch, now.date()).values()
                 if r.get("status") in special_watch.TRACKING]
    text = build_message(items, open_recs, now, thai)
    if not tg.send_message(text, extras_only=True):
        return None
    _update(path, last_status_day=today)
    log.info("group status sent (%d symbol(s))", len(items))
    return text
