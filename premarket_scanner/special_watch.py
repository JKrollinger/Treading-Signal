"""Scanner C watch mode with the v2-trail exit.

Checks the special watchlist (currency pairs, gold, bitcoin) every few minutes. When a
symbol newly satisfies the Trend-join Long rules it sends an alert with a trade plan and
then follows that signal, across days, with the exit that won the backtest comparison:

* initial stop = entry - STOP_ATR_MULT x ATR
* half the position taken at +PARTIAL_R R ("partial" alert)
* the rest trails: stop = highest price since entry - TRAIL_ATR_MULT x ATR, raised only,
  with an alert every time it rises by TRAIL_NOTIFY_ATR x ATR
* price at or below the current stop -> "stopped" alert with the combined result
* MAX_HOLD_DAYS trading days after entry -> "expired" alert (close at market)
* price back below yesterday's high while still before the partial -> "weakened", once

One new signal per symbol per US trading day, and none while that symbol is still being
followed. State lives in data/special_watch_state.json; a copy of the records touched
today in output/special_signals_<date>.json.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, filters, special
from .special import SpecialItem, fmt_price

log = logging.getLogger(__name__)

ET = ZoneInfo(config.MARKET_TZ)
BKK = ZoneInfo("Asia/Bangkok")
DISCLAIMER_TH = "ข้อมูลอัตโนมัติเพื่อประกอบการตัดสินใจ ไม่ใช่คำแนะนำการลงทุน"
TRACKING = ("active", "weakened", "partial")


def state_file() -> Path:
    return config.DATA_DIR / "special_watch_state.json"


def day_file(day: date) -> Path:
    return config.OUTPUT_DIR / f"special_signals_{day.isoformat()}.json"


def _upgrade_record(rec: dict) -> dict:
    """Bring a record written by the earlier fixed-target watcher up to the v2-trail shape."""
    if "entry" not in rec or all(k in rec for k in ("initial_stop", "highest", "expires_on", "risk", "atr")):
        return rec
    entry = float(rec.get("entry") or 0.0)
    stop = float(rec.get("stop") or 0.0)
    risk = max(entry - stop, 1e-9)
    rec.setdefault("initial_stop", stop)
    rec.setdefault("last_notified_stop", stop)
    rec.setdefault("risk", risk)
    rec["atr"] = float(rec.get("atr") or risk / max(config.STOP_ATR_MULT, 1e-9))
    rec.setdefault("partial_r", config.PARTIAL_R)
    rec.setdefault("partial_level", entry + config.PARTIAL_R * risk)
    rec.setdefault("partial_taken", False)
    rec.setdefault("partial_price", None)
    rec.setdefault("partial_at", None)
    rec.setdefault("trail_atr_mult", config.TRAIL_ATR_MULT)
    rec.setdefault("highest", max(entry, float(rec.get("last_price") or entry)))
    rec.setdefault("max_hold_days", config.MAX_HOLD_DAYS)
    start = date.fromisoformat(rec.get("date") or datetime.now(ET).date().isoformat())
    rec.setdefault("expires_on", add_trading_days(start, config.MAX_HOLD_DAYS).isoformat())
    rec.setdefault("result_r", None)
    if rec.get("status") == "target":
        rec["status"] = "stopped"
    return rec


def load_state(path: Path | None = None) -> dict[str, dict]:
    path = path or state_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return {}
    return {k: _upgrade_record(v) for k, v in data.items() if isinstance(v, dict)}


def save_state(state: dict[str, dict], path: Path | None = None) -> None:
    path = path or state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def visible_records(state: dict[str, dict], day: date) -> dict[str, dict]:
    """Signal records worth showing: still being followed, or finished today."""
    out = {}
    for s, r in state.items():
        if s.startswith("hold:") or "entry" not in r:
            continue
        if r.get("status") in TRACKING or r.get("date") == day.isoformat() or \
                (r.get("closed_at") or "").startswith(day.isoformat()):
            out[s] = r
    return out


def holds_today(state: dict[str, dict], day: date) -> dict[str, dict]:
    """Symbols whose qualifying setup was held back today by the time/news filter."""
    return {k[5:]: v for k, v in state.items() if k.startswith("hold:") and v.get("date") == day.isoformat()}


def today_records(state: dict[str, dict], day: date) -> dict[str, dict]:   # backwards compatible name
    return visible_records(state, day)


def add_trading_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


# Formatting -----------------------------------------------------------------------
def _when(iso_or_dt, thai: bool) -> str:
    dt = datetime.fromisoformat(iso_or_dt) if isinstance(iso_or_dt, str) else iso_or_dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    et, bkk = dt.astimezone(ET).strftime("%H:%M"), dt.astimezone(BKK).strftime("%H:%M")
    return f"{bkk} น. ไทย ({et} น. เวลาตลาดสหรัฐ)" if thai else f"{et} ET ({bkk} Bangkok)"


def _record(it: SpecialItem, now: datetime) -> dict:
    p = it.plan
    assert p is not None
    risk = p.entry - p.stop
    return {
        "symbol": it.symbol,
        "name": it.name,
        "currency": it.currency,
        "date": now.date().isoformat(),
        "triggered_at": now.isoformat(timespec="seconds"),
        "entry": p.entry,
        "initial_stop": p.stop,
        "stop": p.stop,                    # current effective stop (rises with the trail)
        "last_notified_stop": p.stop,
        "partial_level": p.target,
        "partial_r": p.partial_r,
        "partial_taken": False,
        "partial_price": None,
        "partial_at": None,
        "trail_atr_mult": p.trail_atr_mult,
        "highest": p.entry,
        "atr": p.atr,
        "risk": risk,
        "risk_pct": p.risk_pct,
        "breakout_level": p.breakout_level,
        "yesterday_close": it.yesterday_close,
        "sma200": it.sma200,
        "max_hold_days": p.max_hold_days,
        "expires_on": add_trading_days(now.date(), p.max_hold_days).isoformat(),
        "status": "active",
        "last_price": it.price,
        "updated_at": now.isoformat(timespec="seconds"),
        "closed_at": None,
        "result_r": None,
    }


def combined_r(rec: dict, exit_price: float) -> float:
    """Result in R: half at the partial level (if taken) plus half (or all) at ``exit_price``."""
    risk = rec["risk"]
    rest = (exit_price - rec["entry"]) / risk
    if rec.get("partial_taken"):
        return 0.5 * rec["partial_r"] + 0.5 * rest
    return rest


def fmt_new(rec: dict, thai: bool) -> str:
    c = rec.get("currency", "")
    fp = lambda v: fmt_price(v, c)  # noqa: E731
    trail_dist = rec["trail_atr_mult"] * rec["atr"]
    if thai:
        return "\n".join([
            f"🔔 สัญญาณซื้อใหม่ (Trend-join Long): {rec['name']} ({rec['symbol']})",
            f"เวลา {_when(rec['triggered_at'], True)}",
            f"ราคาเข้าโดยประมาณ: {fp(rec['entry'])}",
            f"จุดตัดขาดทุนเริ่มต้น: {fp(rec['initial_stop'])} (ต่ำกว่าราคาเข้า {rec['risk_pct']:.2f}% "
            f"เท่ากับระยะที่ราคาแกว่งเฉลี่ยใน 1 วัน คิดจาก {config.ATR_PERIOD} วันล่าสุด)",
            f"แผนทำกำไร: ปิดครึ่งไม้เมื่อราคาถึง {fp(rec['partial_level'])} (+{rec['partial_r']:g}R) "
            f"ส่วนที่เหลือถือตามแนวโน้ม โดยเลื่อนจุดตัดขาดทุนตามราคาสูงสุดห่าง {rec['trail_atr_mult']:g} ATR "
            f"(ประมาณ {fp(trail_dist)}) จะแจ้งทุกครั้งที่จุดตัดขาดทุนขยับขึ้น ถือไม่เกิน {rec['max_hold_days']} วันทำการ "
            f"(ถึง {rec['expires_on']})",
            f"เหตุผล: ราคาทะลุจุดสูงสุดเมื่อวาน {fp(rec['breakout_level'])} และกำลังอยู่ที่จุดสูงสุดของวัน "
            f"แนวโน้มระยะยาวเป็นขาขึ้น (ปิดเมื่อวาน {fp(rec.get('yesterday_close'))} "
            f"เหนือเส้นค่าเฉลี่ย 200 วัน {fp(rec.get('sma200'))})",
            DISCLAIMER_TH,
        ])
    return "\n".join([
        f"NEW Trend-join Long signal: {rec['name']} ({rec['symbol']})",
        f"Time {_when(rec['triggered_at'], False)}",
        f"Entry ~{fp(rec['entry'])} | initial stop {fp(rec['initial_stop'])} (-{rec['risk_pct']:.2f}%, "
        f"{config.STOP_ATR_MULT:g} x ATR{config.ATR_PERIOD})",
        f"Plan: half off at {fp(rec['partial_level'])} (+{rec['partial_r']:g}R), rest trails "
        f"{rec['trail_atr_mult']:g} x ATR (~{fp(trail_dist)}) below the highest price, max {rec['max_hold_days']} "
        f"trading days (until {rec['expires_on']})",
        f"Why: above yesterday's high {fp(rec['breakout_level'])}, at the high of day, "
        f"y-close {fp(rec.get('yesterday_close'))} > SMA200 {fp(rec.get('sma200'))}",
    ])


def fmt_partial(rec: dict, price: float, thai: bool) -> str:
    c = rec.get("currency", "")
    fp = lambda v: fmt_price(v, c)  # noqa: E731
    if thai:
        return (f"✅ {rec['name']} ({rec['symbol']}): ราคา {fp(price)} ถึงระดับ +{rec['partial_r']:g}R แล้ว "
                f"แนะนำปิดครึ่งไม้ที่ราคานี้ (ล็อกกำไรไว้ก่อน) ส่วนที่เหลือถือต่อ "
                f"จุดตัดขาดทุนตอนนี้ {fp(rec['stop'])} และจะเลื่อนขึ้นตามราคา")
    return (f"PARTIAL {rec['name']} ({rec['symbol']}): {fp(price)} reached +{rec['partial_r']:g}R; take half, "
            f"trail the rest, current stop {fp(rec['stop'])}")


def fmt_trail(rec: dict, thai: bool) -> str:
    c = rec.get("currency", "")
    fp = lambda v: fmt_price(v, c)  # noqa: E731
    locked = (rec["stop"] - rec["entry"]) / rec["risk"]
    if thai:
        state = ("กำไรล็อกไว้แล้ว" if rec["stop"] > rec["entry"] else "ยังต่ำกว่าราคาเข้า")
        return (f"🔼 {rec['name']} ({rec['symbol']}): เลื่อนจุดตัดขาดทุนขึ้นเป็น {fp(rec['stop'])} "
                f"(ราคาสูงสุดตั้งแต่เข้า {fp(rec['highest'])}) {state} {locked:+.2f}R ของส่วนที่ถืออยู่")
    return (f"TRAIL {rec['name']} ({rec['symbol']}): stop raised to {fp(rec['stop'])} "
            f"(highest {fp(rec['highest'])}, {locked:+.2f}R locked on the remainder)")


def fmt_stopped(rec: dict, price: float, thai: bool) -> str:
    c = rec.get("currency", "")
    fp = lambda v: fmt_price(v, c)  # noqa: E731
    r = combined_r(rec, rec["stop"])
    t = _when(rec["triggered_at"], thai)
    kind_th = "จุดตัดขาดทุนที่เลื่อนตามแนวโน้ม" if rec["stop"] > rec["initial_stop"] else "จุดตัดขาดทุนเริ่มต้น"
    if thai:
        verb = "กำไร" if r > 0 else "ขาดทุน"
        return (f"⛔ {rec['name']} ({rec['symbol']}): ราคา {fp(price)} ถึง{kind_th} {fp(rec['stop'])} แล้ว "
                f"ควรปิดไม้ที่เหลือ ผลรวมของสัญญาณนี้ประมาณ {r:+.2f}R ({verb}) จากสัญญาณเมื่อ {t}"
                + (" รวมครึ่งไม้ที่ปิดไปแล้ว" if rec.get("partial_taken") else ""))
    return (f"STOP {rec['name']} ({rec['symbol']}): {fp(price)} <= stop {fp(rec['stop'])}; close the rest, "
            f"combined result {r:+.2f}R (signal {t})")


def fmt_expired(rec: dict, price: float, thai: bool) -> str:
    c = rec.get("currency", "")
    fp = lambda v: fmt_price(v, c)  # noqa: E731
    r = combined_r(rec, price)
    if thai:
        return (f"⏱ {rec['name']} ({rec['symbol']}): ครบ {rec['max_hold_days']} วันทำการแล้ว "
                f"แนะนำปิดไม้ที่เหลือที่ราคาปัจจุบัน {fp(price)} ผลรวมประมาณ {r:+.2f}R")
    return (f"EXPIRED {rec['name']} ({rec['symbol']}): {rec['max_hold_days']} trading days reached; "
            f"close at {fp(price)}, combined result {r:+.2f}R")


def fmt_held(it: SpecialItem, reason: str, thai: bool) -> str:
    c = it.currency
    if thai:
        return (f"⏸ {it.name} ({it.symbol}): เข้าเงื่อนไข Trend-join ที่ราคา {fmt_price(it.price, c)} "
                f"แต่ยังไม่นับเป็นสัญญาณ เพราะ{reason} (แจ้งครั้งเดียวต่อวัน จะนับใหม่เมื่อพ้นช่วงนี้และราคายังเข้าเงื่อนไข)")
    return f"HELD {it.name} ({it.symbol}) at {fmt_price(it.price, c)}: {reason}"


def fmt_weakened(rec: dict, price: float, thai: bool) -> str:
    c = rec.get("currency", "")
    fp = lambda v: fmt_price(v, c)  # noqa: E731
    t = _when(rec["triggered_at"], thai)
    if thai:
        return (f"⚠️ {rec['name']} ({rec['symbol']}): ราคา {fp(price)} กลับลงต่ำกว่าจุดสูงสุดเมื่อวาน "
                f"{fp(rec['breakout_level'])} แล้ว สัญญาณซื้อเมื่อ {t} อ่อนลง ยังไม่ถึงจุดตัดขาดทุน "
                f"{fp(rec['stop'])} แต่ควรเฝ้าใกล้ชิด")
    return (f"WEAKENED {rec['name']} ({rec['symbol']}): {fp(price)} back below breakout level "
            f"{fp(rec['breakout_level'])} (signal {t}); stop still {fp(rec['stop'])}")


# Main loop body ------------------------------------------------------------------
def run_watch(
    now: datetime | None = None,
    send: bool = True,
    items: list[SpecialItem] | None = None,
    state_path: Path | None = None,
    thai: bool | None = None,
) -> list[str]:
    """One check of the watchlist. Returns the alert messages produced (and sends them)."""
    from . import telegram_notify as tg  # local import: telegram_notify imports this module lazily too

    thai = tg.is_thai() if thai is None else thai
    now = now or datetime.now(ET)
    today = now.date()
    state = load_state(state_path)
    if items is None:
        items = special.fetch_special(thai=thai)
    stale_after = timedelta(minutes=config.STALE_QUOTE_MIN)
    messages: list[str] = []
    stamp = now.isoformat(timespec="seconds")

    for it in items:
        if it.price is None:
            continue
        if it.quote_time is None or now - it.quote_time > stale_after:
            log.debug("%s: quote is stale (market closed?), skipped", it.symbol)
            continue
        price = float(it.price)
        rec = state.get(it.symbol)

        if rec is None or rec.get("status") not in TRACKING:
            # nothing being followed: allow one new signal per symbol per day
            if rec is not None and rec.get("date") == today.isoformat():
                continue
            if it.signal and it.plan:
                reason = filters.check_new_signal(it.symbol, now.astimezone(timezone.utc))
                if reason:
                    key = f"hold:{it.symbol}"
                    if state.get(key, {}).get("date") != today.isoformat():
                        state[key] = {"date": today.isoformat(), "reason": reason, "at": stamp, "symbol": it.symbol}
                        messages.append(fmt_held(it, reason, thai))
                    log.info("%s: signal held back: %s", it.symbol, reason)
                    continue
                rec = _record(it, now)
                state[it.symbol] = rec
                messages.append(fmt_new(rec, thai))
                log.info("%s: NEW signal entry=%s stop=%s partial=%s", it.symbol, rec["entry"], rec["stop"], rec["partial_level"])
            continue

        # follow an open signal --------------------------------------------------
        rec["last_price"], rec["updated_at"] = price, stamp
        rec["highest"] = max(float(rec["highest"]), price)
        new_trail = rec["highest"] - rec["trail_atr_mult"] * rec["atr"]
        rec["stop"] = max(float(rec["stop"]), new_trail)

        if price <= rec["stop"]:
            rec.update(status="stopped", closed_at=stamp, result_r=combined_r(rec, rec["stop"]))
            messages.append(fmt_stopped(rec, price, thai))
            continue
        if today > date.fromisoformat(rec["expires_on"]):
            rec.update(status="expired", closed_at=stamp, result_r=combined_r(rec, price))
            messages.append(fmt_expired(rec, price, thai))
            continue
        if not rec.get("partial_taken") and price >= rec["partial_level"]:
            rec.update(partial_taken=True, partial_price=price, partial_at=stamp, status="partial")
            messages.append(fmt_partial(rec, price, thai))
        elif rec["status"] == "active" and price < rec["breakout_level"]:
            rec["status"] = "weakened"
            messages.append(fmt_weakened(rec, price, thai))
        if rec["stop"] - float(rec.get("last_notified_stop", rec["initial_stop"])) >= config.TRAIL_NOTIFY_ATR * rec["atr"]:
            rec["last_notified_stop"] = rec["stop"]
            messages.append(fmt_trail(rec, thai))

    save_state(state, state_path)
    if state_path is None:
        try:
            path = day_file(today)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(list(visible_records(state, today).values()), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("could not write %s: %s", day_file(today), exc)

    if send:
        for m in messages:
            tg.send_message(m)
    return messages
