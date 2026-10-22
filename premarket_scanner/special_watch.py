"""Scanner C watch mode.

Checks the special watchlist (currency pairs, gold, bitcoin) every few minutes. When a
symbol newly satisfies the Trend-join Long rules it sends an alert with a trade plan
(entry, stop-loss, target) and then follows that signal for the rest of the day:

* price at or below the stop      -> "stopped" alert
* price at or above the target    -> "target" alert
* price back below yesterday's high (the breakout level) -> "weakened" alert, once

One signal per symbol per US trading day. State lives in data/special_watch_state.json
and a copy of the day's signals in output/special_signals_<date>.json.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, special
from .special import SpecialItem, fmt_price

log = logging.getLogger(__name__)

ET = ZoneInfo(config.MARKET_TZ)
BKK = ZoneInfo("Asia/Bangkok")
DISCLAIMER_TH = "ข้อมูลอัตโนมัติเพื่อประกอบการตัดสินใจ ไม่ใช่คำแนะนำการลงทุน"
TRACKING = ("active", "weakened")


def state_file() -> Path:
    return config.DATA_DIR / "special_watch_state.json"


def day_file(day: date) -> Path:
    return config.OUTPUT_DIR / f"special_signals_{day.isoformat()}.json"


def load_state(path: Path | None = None) -> dict[str, dict]:
    path = path or state_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return {}


def save_state(state: dict[str, dict], path: Path | None = None) -> None:
    path = path or state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def today_records(state: dict[str, dict], day: date) -> dict[str, dict]:
    return {s: r for s, r in state.items() if r.get("date") == day.isoformat()}


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
    return {
        "symbol": it.symbol,
        "name": it.name,
        "currency": it.currency,
        "date": now.date().isoformat(),
        "triggered_at": now.isoformat(timespec="seconds"),
        "entry": p.entry,
        "stop": p.stop,
        "target": p.target,
        "atr": p.atr,
        "risk_pct": p.risk_pct,
        "reward_pct": p.reward_pct,
        "breakout_level": p.breakout_level,
        "yesterday_close": it.yesterday_close,
        "sma200": it.sma200,
        "status": "active",
        "last_price": it.price,
        "updated_at": now.isoformat(timespec="seconds"),
        "closed_at": None,
    }


def fmt_new(rec: dict, thai: bool) -> str:
    c = rec.get("currency", "")
    fp = lambda v: fmt_price(v, c)  # noqa: E731
    if thai:
        return "\n".join([
            f"🔔 สัญญาณซื้อใหม่ (Trend-join Long): {rec['name']} ({rec['symbol']})",
            f"เวลา {_when(rec['triggered_at'], True)}",
            f"ราคาเข้าโดยประมาณ: {fp(rec['entry'])}",
            f"จุดตัดขาดทุน: {fp(rec['stop'])} (ต่ำกว่าราคาเข้า {rec['risk_pct']:.2f}% "
            f"เท่ากับระยะที่ราคาแกว่งเฉลี่ยใน 1 วัน คิดจาก {config.ATR_PERIOD} วันล่าสุด)",
            f"เป้าหมายทำกำไร: {fp(rec['target'])} (สูงกว่าราคาเข้า {rec['reward_pct']:.2f}% "
            f"คือ {config.TARGET_R_MULT:g} เท่าของระยะเสี่ยง)",
            f"เหตุผล: ราคาทะลุจุดสูงสุดเมื่อวาน {fp(rec['breakout_level'])} และกำลังอยู่ที่จุดสูงสุดของวัน "
            f"แนวโน้มระยะยาวเป็นขาขึ้น (ปิดเมื่อวาน {fp(rec.get('yesterday_close'))} "
            f"เหนือเส้นค่าเฉลี่ย 200 วัน {fp(rec.get('sma200'))})",
            f"ถ้าราคากลับลงต่ำกว่า {fp(rec['breakout_level'])} ก่อนถึงเป้าหมาย ถือว่าสัญญาณอ่อนลง จะแจ้งให้ทราบ",
            DISCLAIMER_TH,
        ])
    return "\n".join([
        f"NEW Trend-join Long signal: {rec['name']} ({rec['symbol']})",
        f"Time {_when(rec['triggered_at'], False)}",
        f"Entry ~{fp(rec['entry'])} | Stop {fp(rec['stop'])} (-{rec['risk_pct']:.2f}%, 1 x {config.ATR_PERIOD}-day ATR) | "
        f"Target {fp(rec['target'])} (+{rec['reward_pct']:.2f}%, {config.TARGET_R_MULT:g}R)",
        f"Why: above yesterday's high {fp(rec['breakout_level'])}, at the high of day, "
        f"y-close {fp(rec.get('yesterday_close'))} > SMA200 {fp(rec.get('sma200'))}",
        f"Setup weakens if price falls back below {fp(rec['breakout_level'])}.",
    ])


def fmt_stopped(rec: dict, price: float, thai: bool) -> str:
    c = rec.get("currency", "")
    pct = (rec["entry"] - price) / rec["entry"] * 100.0
    t = _when(rec["triggered_at"], thai)
    if thai:
        return (f"⛔ {rec['name']} ({rec['symbol']}): ราคา {fmt_price(price, c)} ลงมาถึงจุดตัดขาดทุน "
                f"{fmt_price(rec['stop'], c)} แล้ว (สัญญาณเมื่อ {t}) ถ้าเข้าที่ {fmt_price(rec['entry'], c)} "
                f"จะขาดทุนประมาณ {pct:.2f}% ควรออกตามแผนเพื่อจำกัดความเสียหาย")
    return (f"STOP HIT {rec['name']} ({rec['symbol']}): {fmt_price(price, c)} <= stop {fmt_price(rec['stop'], c)} "
            f"(signal {t}); about -{pct:.2f}% from entry {fmt_price(rec['entry'], c)}")


def fmt_target(rec: dict, price: float, thai: bool) -> str:
    c = rec.get("currency", "")
    pct = (price - rec["entry"]) / rec["entry"] * 100.0
    t = _when(rec["triggered_at"], thai)
    if thai:
        return (f"✅ {rec['name']} ({rec['symbol']}): ราคา {fmt_price(price, c)} ถึงเป้าหมาย "
                f"{fmt_price(rec['target'], c)} แล้ว (สัญญาณเมื่อ {t}) กำไรประมาณ +{pct:.2f}% จากราคาเข้า "
                f"{fmt_price(rec['entry'], c)} พิจารณาทำกำไร หรือเลื่อนจุดตัดขาดทุนขึ้นมาที่ราคาเข้าเพื่อถือต่อแบบไม่เสี่ยง")
    return (f"TARGET HIT {rec['name']} ({rec['symbol']}): {fmt_price(price, c)} >= target {fmt_price(rec['target'], c)} "
            f"(signal {t}); about +{pct:.2f}% from entry {fmt_price(rec['entry'], c)}")


def fmt_weakened(rec: dict, price: float, thai: bool) -> str:
    c = rec.get("currency", "")
    t = _when(rec["triggered_at"], thai)
    if thai:
        return (f"⚠️ {rec['name']} ({rec['symbol']}): ราคา {fmt_price(price, c)} กลับลงต่ำกว่าจุดสูงสุดเมื่อวาน "
                f"{fmt_price(rec['breakout_level'], c)} แล้ว สัญญาณซื้อเมื่อ {t} อ่อนลง ยังไม่ถึงจุดตัดขาดทุน "
                f"{fmt_price(rec['stop'], c)} แต่ควรเฝ้าใกล้ชิด")
    return (f"WEAKENED {rec['name']} ({rec['symbol']}): {fmt_price(price, c)} back below breakout level "
            f"{fmt_price(rec['breakout_level'], c)} (signal {t}); stop still {fmt_price(rec['stop'], c)}")


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

    for it in items:
        if it.price is None:
            continue
        if it.quote_time is None or now - it.quote_time > stale_after:
            log.debug("%s: quote is stale (market closed?), skipped", it.symbol)
            continue
        rec = state.get(it.symbol)
        if rec and rec.get("date") != today.isoformat():
            rec = None                                   # a new day starts fresh
        if rec is None:
            if it.signal and it.plan:
                rec = _record(it, now)
                state[it.symbol] = rec
                messages.append(fmt_new(rec, thai))
                log.info("%s: NEW signal entry=%s stop=%s target=%s", it.symbol, rec["entry"], rec["stop"], rec["target"])
            continue

        rec["last_price"] = it.price
        rec["updated_at"] = now.isoformat(timespec="seconds")
        if rec["status"] not in TRACKING:
            continue
        if it.price <= rec["stop"]:
            rec["status"], rec["closed_at"] = "stopped", now.isoformat(timespec="seconds")
            messages.append(fmt_stopped(rec, it.price, thai))
        elif it.price >= rec["target"]:
            rec["status"], rec["closed_at"] = "target", now.isoformat(timespec="seconds")
            messages.append(fmt_target(rec, it.price, thai))
        elif rec["status"] == "active" and it.price < rec["breakout_level"]:
            rec["status"] = "weakened"
            messages.append(fmt_weakened(rec, it.price, thai))

    save_state(state, state_path)
    if state_path is None:
        try:
            path = day_file(today)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(list(today_records(state, today).values()), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("could not write %s: %s", day_file(today), exc)

    if send:
        for m in messages:
            tg.send_message(m)
    return messages
