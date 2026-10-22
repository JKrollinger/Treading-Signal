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

from . import config, filters, special, trade_fmt
from .special import SpecialItem, fmt_price
from .trade_fmt import BUY_BADGE, DOWN, EXIT

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


# Signal numbers and results log (compact style) ------------------------------------
def _aux_path(state_path: Path | None, name: str) -> Path:
    return (state_path.parent if state_path else config.DATA_DIR) / name


def next_signal_no(state_path: Path | None = None) -> int:
    """Running signal number (#1, #2, ...) kept in data/signal_seq.json."""
    path = _aux_path(state_path, "signal_seq.json")
    last = 0
    try:
        last = int(json.loads(path.read_text(encoding="utf-8")).get("last", 0))
    except (OSError, ValueError, TypeError):
        last = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last": last + 1}), encoding="utf-8")
    return last + 1


def record_result(rec: dict, state_path: Path | None = None) -> None:
    """Append a finished signal to data/signal_results.jsonl (feeds the WIN/LOSS tally)."""
    row = {k: rec.get(k) for k in ("no", "symbol", "date", "entry", "initial_stop", "status", "result_r", "closed_at")}
    row["side"] = "buy"
    path = _aux_path(state_path, "signal_results.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def outcome(r: float | None) -> str:
    if r is None:
        return "open"
    return "win" if r > 0.05 else ("loss" if r < -0.05 else "flat")


def scoreboard(now: datetime, days: int = 30, state_path: Path | None = None) -> dict:
    """WIN/LOSS count and total R of signals closed in the last ``days`` days."""
    path = _aux_path(state_path, "signal_results.jsonl")
    since = (now - timedelta(days=days)).isoformat()
    out = {"win": 0, "loss": 0, "flat": 0, "total_r": 0.0, "days": days}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (row.get("closed_at") or "") < since or row.get("result_r") is None:
            continue
        out[outcome(float(row["result_r"]))] += 1
        out["total_r"] += float(row["result_r"])
    return out


def _compact() -> bool:
    return config.ALERT_STYLE != "detailed"


def _px(v) -> str:
    """Exact price for entries/exits: 4,057.71 / 157.117 / 1.12136."""
    if v is None:
        return "n/a"
    v = float(v)
    decimals = 2 if abs(v) >= 1000 else (3 if abs(v) >= 100 else (4 if abs(v) >= 10 else 5))
    return f"{v:,.{decimals}f}"


def _tag(rec: dict) -> str:
    return f"#{rec['no']} {rec['symbol']}" if rec.get("no") else rec["symbol"]


def fmt_score(sb: dict, thai: bool) -> str:
    n = sb["win"] + sb["loss"] + sb["flat"]
    if not n:
        return ""
    flat = (f" เสมอ {sb['flat']}" if thai else f" {sb['flat']} flat") if sb["flat"] else ""
    if thai:
        return f"📊 {sb['days']} วันล่าสุด: {sb['win']} WIN {sb['loss']} LOSS{flat} | รวม {sb['total_r']:+.1f}R"
    return f"📊 last {sb['days']} days: {sb['win']} WIN {sb['loss']} LOSS{flat} | total {sb['total_r']:+.1f}R"


def compact_new(rec: dict, thai: bool) -> str:
    expires = date.fromisoformat(rec["expires_on"]).strftime("%d/%m")
    if thai:
        return "\n".join([
            f"🔔 {_tag(rec)} {BUY_BADGE} | {rec['name']}",
            f"{trade_fmt.ENTRY} เข้า {_px(rec['entry'])}",
            f"{trade_fmt.UP} TP1 {_px(rec['partial_level'])} (+{rec['partial_r']:g}R ปิดครึ่ง)",
            f"{DOWN} SL {_px(rec['initial_stop'])} (-1R)",
            f"ที่เหลือเลื่อน SL ตามราคา | ปิดไม่เกิน {expires}",
            "ข้อมูลอัตโนมัติ ไม่ใช่คำแนะนำการลงทุน",
        ])
    return "\n".join([
        f"🔔 {_tag(rec)} {BUY_BADGE} | {rec['name']}",
        f"{trade_fmt.ENTRY} entry {_px(rec['entry'])}",
        f"{trade_fmt.UP} TP1 {_px(rec['partial_level'])} (+{rec['partial_r']:g}R, half off)",
        f"{DOWN} SL {_px(rec['initial_stop'])} (-1R)",
        f"rest trails the price | close by {rec['expires_on']}",
    ])


def compact_partial(rec: dict, price: float, thai: bool) -> str:
    if thai:
        return (f"✅ {_tag(rec)} TP1 +{rec['partial_r']:g}R | ปิดครึ่งที่ {_px(price)} "
                f"ที่เหลือถือต่อ {DOWN} SL {_px(rec['stop'])}")
    return f"✅ {_tag(rec)} TP1 +{rec['partial_r']:g}R | half closed at {_px(price)}, rest runs, {DOWN} SL {_px(rec['stop'])}"


def compact_trail(rec: dict, thai: bool) -> str:
    locked = (rec["stop"] - rec["entry"]) / rec["risk"]
    if thai:
        return f"🔼 {_tag(rec)} เลื่อน {DOWN} SL → {_px(rec['stop'])} ({'ล็อกกำไร' if locked > 0 else 'ความเสี่ยงเหลือ'} {locked:+.2f}R)"
    return f"🔼 {_tag(rec)} {DOWN} SL raised to {_px(rec['stop'])} ({locked:+.2f}R)"


def compact_result(rec: dict, exit_price: float, how: str, thai: bool, score: str = "") -> str:
    r = float(rec["result_r"] or 0.0)
    kind = outcome(r)
    icon, word = {"win": ("✅", "WIN"), "loss": ("❌", "LOSS"),
                  "flat": ("➖", "เสมอตัว" if thai else "FLAT")}[kind]
    start = datetime.fromisoformat(rec["triggered_at"])
    end = datetime.fromisoformat(rec["closed_at"]) if rec.get("closed_at") else start
    held_days = max(0, (end - start).days)
    held = (f"{held_days} วัน" if held_days else "ไม่ถึงวัน") if thai else (f"{held_days}d" if held_days else "<1d")
    lines = [f"{icon} {_tag(rec)} {BUY_BADGE} {word} {r:+.2f}R"]
    if thai:
        lines.append(f"{EXIT} ออก {_px(exit_price)} ({how}) | เข้า {_px(rec['entry'])} | ถือ {held}")
    else:
        lines.append(f"{EXIT} exit {_px(exit_price)} ({how}) | entry {_px(rec['entry'])} | held {held}")
    if score:
        lines.append(score)
    return "\n".join(lines)


def compact_weakened(rec: dict, price: float, thai: bool) -> str:
    if thai:
        return (f"⚠️ {_tag(rec)} ราคา {_px(price)} หลุดจุดทะลุ {_px(rec['breakout_level'])} สัญญาณอ่อนลง "
                f"| {DOWN} SL ยัง {_px(rec['stop'])}")
    return f"⚠️ {_tag(rec)} {_px(price)} back below {_px(rec['breakout_level'])}, weaker | {DOWN} SL {_px(rec['stop'])}"


# Formatting -----------------------------------------------------------------------
def _when(iso_or_dt, thai: bool, with_date: bool = False) -> str:
    """'17:08 น. ไทย' / '06:08 ET'; with_date adds the day ('22/10 17:08 น. ไทย')."""
    dt = datetime.fromisoformat(iso_or_dt) if isinstance(iso_or_dt, str) else iso_or_dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    if thai:
        return dt.astimezone(BKK).strftime("%d/%m %H:%M" if with_date else "%H:%M") + " น. ไทย"
    return dt.astimezone(ET).strftime("%m-%d %H:%M" if with_date else "%H:%M") + " ET"


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
    if _compact():
        return compact_new(rec, thai)
    c = rec.get("currency", "")
    fp = lambda v: fmt_price(v, c)  # noqa: E731
    trail_dist = rec["trail_atr_mult"] * rec["atr"]
    if thai:
        expires = date.fromisoformat(rec["expires_on"]).strftime("%d/%m")
        return "\n".join([
            f"🔔 สัญญาณใหม่ {BUY_BADGE}: {rec['name']} ({rec['symbol']}) {_when(rec['triggered_at'], True)}",
            trade_fmt.plan(rec["entry"], rec["initial_stop"], rec["partial_level"], fp, thai=True,
                           stop_note=f" (-{rec['risk_pct']:.2f}%)", tp_note=f" (+{rec['partial_r']:g}R ปิดครึ่ง)"),
            f"ที่เหลือเลื่อนตัดขาดทุนตามราคา (ห่าง {rec['trail_atr_mult']:g} ATR ≈ {fp(trail_dist)}) "
            f"ถือไม่เกิน {rec['max_hold_days']} วันทำการ (ถึง {expires})",
            f"เหตุผล: ทะลุจุดสูงสุดเมื่อวาน {fp(rec['breakout_level'])} ที่จุดสูงสุดของวัน แนวโน้มระยะยาวขาขึ้น",
            DISCLAIMER_TH,
        ])
    return "\n".join([
        f"NEW {BUY_BADGE} Trend-join Long signal: {rec['name']} ({rec['symbol']})",
        f"Time {_when(rec['triggered_at'], False)}",
        trade_fmt.plan(rec["entry"], rec["initial_stop"], rec["partial_level"], fp, thai=False,
                       stop_note=f" (-{rec['risk_pct']:.2f}%, {config.STOP_ATR_MULT:g} x ATR{config.ATR_PERIOD})",
                       tp_note=f" (+{rec['partial_r']:g}R, half off)"),
        f"Plan: half off at {fp(rec['partial_level'])} (+{rec['partial_r']:g}R), rest trails "
        f"{rec['trail_atr_mult']:g} x ATR (~{fp(trail_dist)}) below the highest price, max {rec['max_hold_days']} "
        f"trading days (until {rec['expires_on']})",
        f"Why: above yesterday's high {fp(rec['breakout_level'])}, at the high of day, "
        f"y-close {fp(rec.get('yesterday_close'))} > SMA200 {fp(rec.get('sma200'))}",
    ])


def fmt_partial(rec: dict, price: float, thai: bool) -> str:
    if _compact():
        return compact_partial(rec, price, thai)
    c = rec.get("currency", "")
    fp = lambda v: fmt_price(v, c)  # noqa: E731
    if thai:
        return (f"✅ {EXIT} ออกครึ่งที่ T/P: {rec['name']} ({rec['symbol']}) {fp(price)} (+{rec['partial_r']:g}R) "
                f"ปิดครึ่งไม้ | {DOWN} ตัดขาดทุนตอนนี้ {fp(rec['stop'])} (เลื่อนตามราคา)")
    return (f"PARTIAL {EXIT} {rec['name']} ({rec['symbol']}): {fp(price)} reached +{rec['partial_r']:g}R; take half, "
            f"trail the rest, {DOWN} current stop {fp(rec['stop'])}")


def fmt_trail(rec: dict, thai: bool) -> str:
    if _compact():
        return compact_trail(rec, thai)
    c = rec.get("currency", "")
    fp = lambda v: fmt_price(v, c)  # noqa: E731
    locked = (rec["stop"] - rec["entry"]) / rec["risk"]
    if thai:
        state = "ล็อกกำไร" if rec["stop"] > rec["entry"] else "ยังต่ำกว่าราคาเข้า"
        return (f"🔼 {rec['name']} ({rec['symbol']}) {DOWN} ตัดขาดทุนเลื่อนขึ้นเป็น {fp(rec['stop'])} "
                f"(สูงสุด {fp(rec['highest'])}) {state} {locked:+.2f}R")
    return (f"TRAIL {rec['name']} ({rec['symbol']}): {DOWN} stop raised to {fp(rec['stop'])} "
            f"(highest {fp(rec['highest'])}, {locked:+.2f}R locked on the remainder)")


def fmt_stopped(rec: dict, price: float, thai: bool, score: str = "") -> str:
    if _compact():
        trailed = rec["stop"] > rec["initial_stop"]
        how = ("SL เลื่อนตาม" if trailed else "SL") if thai else ("trailing SL" if trailed else "SL")
        return compact_result(rec, rec["stop"], how, thai, score)
    c = rec.get("currency", "")
    fp = lambda v: fmt_price(v, c)  # noqa: E731
    r = combined_r(rec, rec["stop"])
    t = _when(rec["triggered_at"], thai, with_date=True)
    if thai:
        kind_th = "จุดตัดขาดทุนที่เลื่อนตาม" if rec["stop"] > rec["initial_stop"] else "จุดตัดขาดทุน"
        return (f"⛔ {EXIT} ออกที่ตัดขาดทุน: {rec['name']} ({rec['symbol']}) {fp(price)} ถึง{kind_th} "
                f"{fp(rec['stop'])} ปิดไม้ที่เหลือ | ผลรวม {r:+.2f}R (สัญญาณ {t})")
    return (f"STOP {EXIT} {rec['name']} ({rec['symbol']}): {fp(price)} <= stop {fp(rec['stop'])}; close the rest, "
            f"combined result {r:+.2f}R (signal {t})")


def fmt_expired(rec: dict, price: float, thai: bool, score: str = "") -> str:
    if _compact():
        how = f"ครบ {rec['max_hold_days']} วันทำการ" if thai else f"{rec['max_hold_days']} trading days"
        return compact_result(rec, price, how, thai, score)
    c = rec.get("currency", "")
    fp = lambda v: fmt_price(v, c)  # noqa: E731
    r = combined_r(rec, price)
    if thai:
        return (f"⏱ {EXIT} ออกครบกำหนด: {rec['name']} ({rec['symbol']}) ครบ {rec['max_hold_days']} วันทำการ "
                f"ปิดไม้ที่เหลือที่ {fp(price)} | ผลรวม {r:+.2f}R")
    return (f"EXPIRED {EXIT} {rec['name']} ({rec['symbol']}): {rec['max_hold_days']} trading days reached; "
            f"close at {fp(price)}, combined result {r:+.2f}R")


def fmt_held(it: SpecialItem, reason: str, thai: bool) -> str:
    c = it.currency
    if _compact():
        return (f"⏸ {it.symbol} {BUY_BADGE} ยังไม่นับ: {reason}" if thai
                else f"⏸ {it.symbol} {BUY_BADGE} held: {reason}")
    if thai:
        return (f"⏸ {it.name} ({it.symbol}) เข้าเงื่อนไข {BUY_BADGE} ที่ {fmt_price(it.price, c)} แต่ยังไม่นับ "
                f"เพราะ{reason} (แจ้งวันละครั้ง นับใหม่เมื่อพ้นช่วงนี้)")
    return f"HELD {BUY_BADGE} {it.name} ({it.symbol}) at {fmt_price(it.price, c)}: {reason}"


def fmt_weakened(rec: dict, price: float, thai: bool) -> str:
    if _compact():
        return compact_weakened(rec, price, thai)
    c = rec.get("currency", "")
    fp = lambda v: fmt_price(v, c)  # noqa: E731
    t = _when(rec["triggered_at"], thai, with_date=True)
    if thai:
        return (f"⚠️ {rec['name']} ({rec['symbol']}) {fp(price)} หลุดใต้จุดสูงสุดเมื่อวาน {fp(rec['breakout_level'])} "
                f"สัญญาณ {t} อ่อนลง | {DOWN} ตัดขาดทุนยัง {fp(rec['stop'])}")
    return (f"WEAKENED {rec['name']} ({rec['symbol']}): {fp(price)} back below breakout level "
            f"{fp(rec['breakout_level'])} (signal {t}); {DOWN} stop still {fp(rec['stop'])}")


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
    kinds: list[str] = []                  # alert kind per message: new / exit / trail / weakened / held
    keyboards: dict[int, dict] = {}        # index in ``messages`` -> inline keyboard (owner only)

    def add(kind: str, text: str) -> None:
        messages.append(text)
        kinds.append(kind)
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
                        add("held", fmt_held(it, reason, thai))
                    log.info("%s: signal held back: %s", it.symbol, reason)
                    continue
                rec = _record(it, now)
                rec["no"] = next_signal_no(state_path)
                state[it.symbol] = rec
                add("new", fmt_new(rec, thai))
                if config.EA_BRIDGE_ENABLED and it.source == "mt5":
                    from . import telegram_commands
                    keyboards[len(messages) - 1] = telegram_commands.signal_keyboard(it.symbol, rec["date"])
                log.info("%s: NEW signal entry=%s stop=%s partial=%s", it.symbol, rec["entry"], rec["stop"], rec["partial_level"])
            continue

        # follow an open signal --------------------------------------------------
        rec["last_price"], rec["updated_at"] = price, stamp
        rec["highest"] = max(float(rec["highest"]), price)
        new_trail = rec["highest"] - rec["trail_atr_mult"] * rec["atr"]
        rec["stop"] = max(float(rec["stop"]), new_trail)

        if price <= rec["stop"]:
            rec.update(status="stopped", closed_at=stamp, result_r=combined_r(rec, rec["stop"]))
            record_result(rec, state_path)
            add("exit", fmt_stopped(rec, price, thai, fmt_score(scoreboard(now, state_path=state_path), thai)))
            continue
        if today > date.fromisoformat(rec["expires_on"]):
            rec.update(status="expired", closed_at=stamp, result_r=combined_r(rec, price))
            record_result(rec, state_path)
            add("exit", fmt_expired(rec, price, thai, fmt_score(scoreboard(now, state_path=state_path), thai)))
            continue
        if not rec.get("partial_taken") and price >= rec["partial_level"]:
            rec.update(partial_taken=True, partial_price=price, partial_at=stamp, status="partial")
            add("exit", fmt_partial(rec, price, thai))
        elif rec["status"] == "active" and price < rec["breakout_level"]:
            rec["status"] = "weakened"
            add("weakened", fmt_weakened(rec, price, thai))
        if rec["stop"] - float(rec.get("last_notified_stop", rec["initial_stop"])) >= config.TRAIL_NOTIFY_ATR * rec["atr"]:
            rec["last_notified_stop"] = rec["stop"]
            add("trail", fmt_trail(rec, thai))

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
        # owner: every alert (with the confirm buttons); groups: only the kinds in GROUP_ALERTS, plain text
        for i, (m, kind) in enumerate(zip(messages, kinds)):
            tg.send_alert(m, kind, reply_markup=keyboards.get(i))
    return messages
