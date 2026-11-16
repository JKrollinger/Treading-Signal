"""Why Scanner C did or did not signal, for every check of every symbol.

A check every five minutes would fill a file with the same line, so two things are kept:

* ``data/signal_decisions_latest.json`` - per symbol: the decision of the latest check, since when
  it has been the same, and how many checks of today ended in each decision.
* ``data/signal_decisions.jsonl`` - one line each time a symbol's decision changes, with the
  numbers it was based on (the last ``KEEP`` lines).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

log = logging.getLogger(__name__)

BKK = ZoneInfo("Asia/Bangkok")
KEEP = 1500
LABELS_TH = {                       # short names for the tally
    "signal": "ออกสัญญาณ", "tracking": "ติดตามสัญญาณที่เปิดอยู่", "already_today": "ออกสัญญาณของวันนี้ไปแล้ว",
    "held": "ครบเงื่อนไขแต่ตัวกรองให้รอ", "downtrend": "แนวโน้มใหญ่ไม่ใช่ขาขึ้น", "weak_trend": "แนวโน้มยังไม่แรงพอ (ADX)", "no_breakout": "ยังไม่ทะลุ High เมื่อวาน",
    "not_at_high": "ไม่อยู่ที่ High ของวัน", "no_plan": "คำนวณ S/L ไม่ได้", "stale": "ราคาเก่า (ตลาดปิด)",
    "no_price": "อ่านราคาไม่ได้",
}


def latest_file() -> Path:
    return config.DATA_DIR / "signal_decisions_latest.json"


def log_file() -> Path:
    return config.DATA_DIR / "signal_decisions.jsonl"


def strategy_version() -> str:
    """Name of the rules plus the settings that change their results."""
    adx = f", ADX >= {config.ADX_MIN:g}" if config.ADX_MIN > 0 else ""
    return (f"trend-join-long v3 (stop {config.STOP_ATR_MULT:g} ATR, half at {config.PARTIAL_R:g}R, "
            f"trail {config.TRAIL_ATR_MULT:g} ATR, hold {config.MAX_HOLD_DAYS} d{adx})")


def rule_decision(it) -> tuple[str, str]:
    """The first Trend-join rule a quote fails, in the order a trader would read them."""
    def px(v) -> str:
        if v is None:
            return "-"
        digits = 2 if abs(v) >= 1000 else 3 if abs(v) >= 100 else 4 if abs(v) >= 10 else 5
        return f"{v:,.{digits}f}"

    if it.uptrend is not True:
        if it.sma200 is None or it.yesterday_close is None:
            return "downtrend", "ยังไม่มีค่า SMA200 จึงยืนยันแนวโน้มขาขึ้นไม่ได้"
        gap = (it.yesterday_close / it.sma200 - 1) * 100
        return "downtrend", f"ราคาปิดเมื่อวาน {px(it.yesterday_close)} อยู่ใต้ SMA200 {px(it.sma200)} ({gap:+.1f}%) ระบบซื้อเฉพาะขาขึ้น"
    if it.trend_strong is not True:
        if it.adx is None:
            return "weak_trend", f"แหล่งข้อมูลนี้ไม่มีค่า ADX จึงยืนยันความแรงของแนวโน้มไม่ได้ (ต้องการ ADX ≥ {config.ADX_MIN:g})"
        return "weak_trend", (f"ADX รายวัน {it.adx:.1f} ต่ำกว่า {config.ADX_MIN:g} แนวโน้มยังไม่แรงพอ "
                              "การทะลุในตลาดแกว่งตัวมักหลอก จึงไม่เข้า")
    if it.above_yesterday_high is not True:
        short = (it.yesterday_high - it.price) if it.yesterday_high is not None and it.price is not None else None
        return "no_breakout", f"ราคา {px(it.price)} ยังไม่ทะลุ High เมื่อวาน {px(it.yesterday_high)}" + (f" (ขาดอีก {px(short)})" if short is not None else "")
    if it.at_high_of_day is not True:
        return "not_at_high", f"ทะลุ High เมื่อวานแล้ว แต่ราคา {px(it.price)} ถอยจาก High ของวัน {px(it.day_high)}"
    return "no_plan", "ครบทุกเงื่อนไข แต่ไม่มีค่า ATR สำหรับคำนวณ S/L"


def _snapshot(it) -> dict:
    return {
        "price": it.price, "y_high": it.yesterday_high, "y_close": it.yesterday_close, "day_high": it.day_high,
        "sma200": it.sma200, "atr": it.atr, "uptrend": it.uptrend, "adx": it.adx, "adx_min": config.ADX_MIN, "strong": it.trend_strong,
        "above": it.above_yesterday_high,
        "at_high": it.at_high_of_day, "source": it.source,
    }


def _read_latest() -> dict:
    try:
        data = json.loads(latest_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(decisions: list[tuple], now: datetime) -> None:
    """Store one round of checks: ``[(item, code, reason), ...]``. Never raises into the scanner."""
    if not decisions:
        return
    try:
        stamp = now.astimezone(BKK)
        day, at = stamp.date().isoformat(), stamp.isoformat(timespec="seconds")
        latest = _read_latest()
        changed: list[dict] = []
        version = strategy_version()
        for it, code, reason in decisions:
            prev = latest.get(it.symbol) or {}
            tally = dict(prev.get("today") or {}) if prev.get("day") == day else {}
            tally[code] = tally.get(code, 0) + 1
            row = {"symbol": it.symbol, "code": code, "reason": reason, "at": at, "day": day, "today": tally,
                   "since": prev.get("since") if prev.get("code") == code else at, "version": version, **_snapshot(it)}
            if prev.get("code") != code:
                changed.append({k: v for k, v in row.items() if k not in ("today", "since", "day")})
            latest[it.symbol] = row
        path = latest_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(latest, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        if changed:
            with log_file().open("a", encoding="utf-8") as fh:
                for row in changed:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            _trim()
    except Exception:  # noqa: BLE001 - a diary must never stop the scanner
        log.exception("could not record signal decisions")


def _trim() -> None:
    path = log_file()
    if path.stat().st_size < 600_000:
        return
    lines = path.read_text(encoding="utf-8").splitlines()[-KEEP:]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read(limit: int = 60) -> dict:
    """For the dashboard: the latest decision per symbol and the newest changes."""
    rows: list[dict] = []
    try:
        for line in log_file().read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    watched = set(config.SPECIAL_WATCHLIST)              # a symbol taken off the list is no longer shown
    return {"latest": [r for r in _read_latest().values() if r.get("symbol") in watched],
            "changes": rows[::-1], "labels": LABELS_TH, "version": strategy_version()}
