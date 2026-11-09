"""Trade journal: every closed MT5 position, why it was opened, why it ended, what to change.

``sync()`` builds one record per closed position from the terminal's deal history and keeps
them in ``data/journal_<login>.json`` (one file per account, so a demo and a real account
never mix). Each record is then analysed against the broker's own bars:

* entry: was the Trend-join rule met at that moment (trend, breakout, high of day), was there
  a stop, which session, was it the bot's signal or a manual trade
* exit: how it ended, how far price went for and against the trade (MFE/MAE), how much of the
  move was kept, and what price did in the 24 hours after the exit
* tags: short labels (counter-trend, no stop, gave it back...) that add up across trades into
  "what costs money, what works"

Read-only towards the broker: nothing here can place, change or close an order. The bot's own
signals (Scanner C and the paper day-trade test) are listed next to the real trades.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import numpy as np

from . import config, ea_bridge, filters
from .mt5_portfolio import DEAL_REASON_TH
from .mt5_source import server_to_utc, utc_to_server  # noqa: F401 - utc_to_server is used by the dashboard

log = logging.getLogger(__name__)

UTC = timezone.utc
NY, BKK = ZoneInfo("America/New_York"), ZoneInfo("Asia/Bangkok")
ANALYSIS_VERSION = 1                        # bump to re-analyse every stored trade
HISTORY_START = datetime(2015, 1, 1, tzinfo=UTC)
EA_MAGIC = 20261022                          # InpMagic of mt5_ea/SignalBotBridge.mq5
AFTER_EXIT_HOURS = 24
REVENGE_MINUTES = 15
MAX_TRADES_PER_DAY = 3
MIN_SAMPLE = 30                              # below this the statistics are only a hint
SESSIONS = ("เอเชีย", "ลอนดอน", "ลอนดอน + นิวยอร์ก", "นิวยอร์ก", "ปลายตลาดนิวยอร์ก")
WEEKDAYS = ("จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์")
ORIGIN_TH = {"manual": "เข้าเอง", "ea_signal": "EA ตามสัญญาณ", "ea_test": "EA ทดสอบ"}

# id: (label, good / bad / info, what to review when it costs money)
TAGS: dict[str, tuple[str, str, str]] = {
    "counter_trend": ("สวนแนวโน้มใหญ่ (SMA200)", "bad", "เข้าเฉพาะทิศเดียวกับแนวโน้มใหญ่ ไม้แบบนี้ระบบบอทจะไม่เข้าเลย"),
    "no_breakout": ("ยังไม่ทะลุระดับเมื่อวาน", "bad", "รอให้ราคาทะลุ High/Low เมื่อวานก่อน แล้วค่อยเข้า"),
    "mid_range": ("เข้ากลางกรอบของวัน", "bad", "เข้าเมื่อราคาอยู่ที่ High/Low ของวันเท่านั้น หรือรอจังหวะใหม่"),
    "no_sl": ("ไม่พบ S/L ตอนเปิดไม้", "bad", "ตั้ง S/L ทันทีที่เปิดไม้ ทุกไม้ ไม่มี S/L ก็วัดความเสี่ยงไม่ได้"),
    "sl_tight": ("S/L แคบเกินไป", "bad", "S/L แคบกว่า 0.3 ATR โดนสะบัดง่าย ลองขยับให้ห่างขึ้นแล้วลดล็อตแทน"),
    "sl_wide": ("S/L กว้างเกินไป", "bad", "S/L กว้างกว่า 2 ATR ถ้าโดนจะเสียหนัก ลองจุดเข้าที่ใกล้จุดตัดขาดทุนกว่านี้"),
    "off_session": ("เข้านอกช่วงเวลาหลัก", "bad", f"ทองคำให้ผลดีในช่วง {(config.SESSION_UTC_START + 7) % 24:02d}:00-"
                    f"{(config.SESSION_UTC_END + 7) % 24:02d}:00 น. ไทย ลองงดเข้านอกช่วงนี้"),
    "friday_late": ("เข้าช่วงปลายวันศุกร์", "bad", "งดไม้ใหม่ช่วงปลายวันศุกร์ เสี่ยงราคาเปิดกระโดดหลังเสาร์-อาทิตย์"),
    "news_window": ("เข้าช่วงข่าวแรง", "bad", "งดเข้าช่วงตัวเลขจ้างงานสหรัฐและการประชุมเฟด"),
    "revenge": ("เข้าซ้ำเร็วหลังขาดทุน", "bad", "พักหลังขาดทุน อย่างน้อย 15 นาทีก่อนเข้าไม้ถัดไปในตัวเดิม"),
    "overtrade": ("เทรดเกินวันละ 3 ไม้", "bad", "กำหนดเพดานจำนวนไม้ต่อวัน ไม้หลัง ๆ มักเข้าเพราะอารมณ์"),
    "never_worked": ("ราคาแทบไม่เคยไปทางกำไร", "bad", "จุดเข้าไม่ดี ลองรอแท่งยืนยันก่อนเข้า"),
    "gave_back": ("เคยกำไรแล้วคืนหมด", "bad", "ถึง +1R ให้เลื่อน S/L มาที่ทุน หรือปิดครึ่งหนึ่งตามแผนบอท"),
    "cut_early": ("ปิดกำไรเร็วไป", "bad", "ราคาไปต่ออีกมากหลังปิด ลองปิดครึ่งเดียวแล้วให้ที่เหลือวิ่งตาม S/L"),
    "reversed_after_sl": ("โดน S/L แล้วราคากลับทาง", "bad", "ทิศถูกแต่ S/L ใกล้ไป ลองวาง S/L ตาม ATR"),
    "quick_loss": ("ขาดทุนภายใน 5 นาที", "bad", "เข้าเร็วไป ลองรอแท่งปิดยืนยันก่อน"),
    "exit_stopout": ("โดน Stop Out", "bad", "ล็อตใหญ่เกินเงินทุน ลดขนาดไม้ลง"),
    "lucky_win": ("กำไรทั้งที่ผิดเงื่อนไข", "bad", "ไม้นี้กำไรแต่ไม่เข้าเกณฑ์ระบบ อย่าถือเป็นแบบแผน"),
    "system_full": ("ครบเงื่อนไขระบบ 3 ข้อ", "good", ""),
    "trend_ok": ("เข้าตามแนวโน้มใหญ่", "good", ""),
    "exit_tp": ("ออกที่ T/P", "good", ""),
    "let_run": ("เก็บกำไรได้เกินครึ่งทาง", "good", ""),
    "exit_sl": ("ออกที่ S/L", "info", ""),
    "exit_manual": ("ปิดเอง", "info", ""),
    "exit_ea": ("ปิดโดย EA", "info", ""),
    "short_side": ("ฝั่งขาย", "info", ""),
    "manual": ("เข้าเอง", "info", ""),
    "bot_signal": ("ตามสัญญาณบอท", "info", ""),
}


# Time ----------------------------------------------------------------------------------
def _srv_dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(float(epoch), UTC)


def session_of(utc: datetime) -> str:
    h = utc.hour
    return SESSIONS[0 if h < 7 else 1 if h < 12 else 2 if h < 16 else 3 if h < 21 else 4]


def _px(v: float | None) -> str:
    if v is None:
        return "n/a"
    v = float(v)
    decimals = 2 if abs(v) >= 1000 else (3 if abs(v) >= 100 else (4 if abs(v) >= 10 else 5))
    return f"{v:,.{decimals}f}"


# Storage -------------------------------------------------------------------------------
def journal_path(login: int) -> Path:
    return config.DATA_DIR / f"journal_{login}.json"


def latest_login() -> int | None:
    """The account last journalled, to show its records while MT5 is closed."""
    files = sorted(config.DATA_DIR.glob("journal_*.json"), key=lambda p: p.stat().st_mtime)
    for path in reversed(files):
        if path.stem[8:].isdigit():
            return int(path.stem[8:])
    return None


def load(login: int) -> dict:
    path = journal_path(login)
    doc = None
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not read %s: %s", path, exc)
            path.replace(path.with_suffix(".corrupt.json"))      # keep it for the user, start clean
    doc = doc or {}
    doc.update(login=login)
    doc.setdefault("trades", {})
    doc.setdefault("open", {})
    doc.setdefault("rev", 0)
    return doc


def save(doc: dict) -> None:
    path = journal_path(doc["login"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def set_note(doc: dict, trade_id: str, note: str) -> bool:
    trade = doc["trades"].get(str(trade_id))
    if trade is None:
        return False
    trade["note"] = note.strip()[:2000]
    doc["rev"] += 1
    save(doc)
    return True


# Open positions: what MT5's history does not keep ---------------------------------------
def track_open(doc: dict, positions: list) -> bool:
    """Remember each open position's first S/L and T/P and every later change."""
    changed = False
    for p in positions:
        key = str(p.ticket)
        sl, tp = float(p.sl) or None, float(p.tp) or None
        meta = doc["open"].get(key)
        if meta is None:
            doc["open"][key] = {"first_sl": sl, "first_tp": tp, "sl_log": []}
            changed = True
        elif sl != meta.get("last_sl", meta["first_sl"]) or tp != meta.get("last_tp", meta["first_tp"]):
            meta.update(last_sl=sl, last_tp=tp)
            if meta["first_sl"] is None:         # set shortly after opening: still the initial stop
                meta["first_sl"] = sl
            if meta["first_tp"] is None:
                meta["first_tp"] = tp
            meta["sl_log"].append({"t": datetime.now(UTC).isoformat(timespec="seconds"), "sl": sl, "tp": tp})
            changed = True
    if changed:
        save(doc)
    return changed


# Bars ----------------------------------------------------------------------------------
def _rates(mt5, symbol: str, tf: int, a: float, b: float):
    rates = mt5.copy_rates_range(symbol, tf, _srv_dt(a), _srv_dt(b))
    if rates is None or len(rates) == 0:
        return None
    return rates


def _daily(mt5, symbol: str, cache: dict):
    if symbol not in cache:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 1500)
        if rates is None or len(rates) == 0:                # not in Market Watch yet
            mt5.symbol_select(symbol, True)
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 1500)
        cache[symbol] = rates if rates is not None and len(rates) else None
    return cache[symbol]


def entry_context(mt5, symbol: str, side: int, entry: float, srv_time: float, cache: dict) -> dict | None:
    """The Trend-join conditions as they stood at the moment of entry (long rule mirrored for a sell)."""
    rates = _daily(mt5, symbol, cache)
    if rates is None:
        return None
    day = srv_time - srv_time % 86400
    i = int(np.searchsorted(rates["time"], day, side="right")) - 1
    if i < 1:
        return None
    y = rates[i - 1]
    closes = rates["close"][max(0, i - 200):i]
    sma200 = float(closes.mean()) if i >= 200 else None
    period = config.ATR_PERIOD
    atr = None
    if i > period:
        high, low, close = rates["high"], rates["low"], rates["close"]
        tr = [max(high[k] - low[k], abs(high[k] - close[k - 1]), abs(low[k] - close[k - 1])) for k in range(i - period, i)]
        atr = float(np.mean(tr))
    so_far = _rates(mt5, symbol, mt5.TIMEFRAME_M1, day, srv_time) if srv_time > day else None
    if so_far is None:
        so_far = _rates(mt5, symbol, mt5.TIMEFRAME_M15, day, srv_time)
    day_high = max(float(so_far["high"].max()), entry) if so_far is not None else entry
    day_low = min(float(so_far["low"].min()), entry) if so_far is not None else entry
    tol = config.HOD_TOLERANCE_PCT / 100.0
    y_high, y_low, y_close = float(y["high"]), float(y["low"]), float(y["close"])
    utc = server_to_utc(srv_time)
    return {
        "y_high": y_high, "y_low": y_low, "y_close": y_close, "sma200": sma200, "atr": atr,
        "day_high": day_high, "day_low": day_low,
        "trend_ok": None if sma200 is None else (y_close > sma200 if side > 0 else y_close < sma200),
        "breakout_ok": entry > y_high if side > 0 else entry < y_low,
        "extreme_ok": entry >= day_high * (1 - tol) if side > 0 else entry <= day_low * (1 + tol),
        "session": session_of(utc),
        "in_session": filters.in_session(utc) if filters.applies_to(symbol) else None,
        "friday_late": filters.friday_late(utc) if filters.applies_to(symbol) else False,
        "news": filters.in_recurring_blackout(utc.date(), utc.hour),
    }


def _risk(t: dict, ctx: dict | None) -> tuple[float | None, str | None]:
    """Distance to the stop: the real one when known, else the system's stop (1 ATR) as a yardstick."""
    side, entry = t["dir"], t["entry"]
    for key, basis in (("sl", "sl"), ("sl_final", "sl_final")):
        level = t.get(key)
        if level and side * (entry - level) > 0:
            return abs(entry - level), basis
    atr = (ctx or {}).get("atr")
    return (config.STOP_ATR_MULT * atr, "atr") if atr else (None, None)


def _path(mt5, t: dict, risk: float | None) -> dict | None:
    """How far price went for and against the trade, and what it did after the exit."""
    side, entry, exit_price = t["dir"], t["entry"], t["exit"]
    a, b = t["entry_srv"], t["exit_srv"]
    first, last = a - a % 60, b - b % 60
    bars = _rates(mt5, t["symbol"], mt5.TIMEFRAME_M1, first, last + 60)
    resolution = "M1"
    if bars is None:                                    # older than the terminal's M1 history
        bars = _rates(mt5, t["symbol"], mt5.TIMEFRAME_M5, first - first % 300, last + 300)
        resolution = "M5"
    done = side * (exit_price - entry)
    fav, adv = max(done, 0.0), max(-done, 0.0)
    if bars is not None:
        inner = bars[(bars["time"] > first) & (bars["time"] < last)]    # whole bars inside the trade
        if len(inner):
            hi, lo = float(inner["high"].max()), float(inner["low"].min())
            fav = max(fav, hi - entry if side > 0 else entry - lo)
            adv = max(adv, entry - lo if side > 0 else hi - entry)
    else:
        resolution = None
    out = {"mfe": fav, "mae": adv, "resolution": resolution,
           "mfe_r": fav / risk if risk else None, "mae_r": adv / risk if risk else None,
           "kept": done / fav if fav > 0 and done > 0 else None}
    after = _rates(mt5, t["symbol"], mt5.TIMEFRAME_M5, b, b + AFTER_EXIT_HOURS * 3600)
    if after is not None:
        further = float(after["high"].max()) - exit_price if side > 0 else exit_price - float(after["low"].min())
        out["after_further"] = max(further, 0.0)
        if risk:
            level = entry + side * risk
            hit = after[after["high"] >= level] if side > 0 else after[after["low"] <= level]
            out["after_reached_1r"] = bool(len(hit))
            if len(hit):
                out["after_reached_min"] = max(0, int((int(hit["time"][0]) - b) // 60))
    return out


# Tags and reasons ----------------------------------------------------------------------
def _base_tags(t: dict) -> dict[str, str]:
    """Tag -> the concrete numbers behind it, from the single trade alone."""
    tags: dict[str, str] = {}
    ctx, path = t.get("ctx") or {}, t.get("path") or {}
    side, entry, net = t["dir"], t["entry"], t["net"]
    t_r = t.get("r")
    if ctx.get("trend_ok") is True:
        tags["trend_ok"] = f"ปิดเมื่อวาน {_px(ctx['y_close'])} อยู่ฝั่งเดียวกับไม้นี้เทียบ SMA200 {_px(ctx['sma200'])}"
    elif ctx.get("trend_ok") is False:
        gap = (ctx["y_close"] / ctx["sma200"] - 1) * 100
        tags["counter_trend"] = f"ปิดเมื่อวาน {_px(ctx['y_close'])} ห่าง SMA200 {_px(ctx['sma200'])} อยู่ {gap:+.1f}%"
    level = ctx.get("y_high") if side > 0 else ctx.get("y_low")
    if ctx.get("breakout_ok") is False:
        word = "High" if side > 0 else "Low"
        tags["no_breakout"] = f"เข้า {_px(entry)} ยังไม่ผ่าน {word} เมื่อวาน {_px(level)}"
    if ctx.get("extreme_ok") is False:
        tags["mid_range"] = f"High วันนั้น {_px(ctx['day_high'])} / Low {_px(ctx['day_low'])} ขณะเข้า {_px(entry)}"
    if ctx and all(ctx.get(k) is True for k in ("trend_ok", "breakout_ok", "extreme_ok")):
        tags["system_full"] = "แนวโน้ม ทะลุระดับเมื่อวาน และอยู่ที่จุดสุดของวัน ครบทั้งหมด" +             ("" if side > 0 else " (กฎเดียวกันแบบกลับด้านสำหรับฝั่งขาย)")
    atr = ctx.get("atr")
    if not t.get("sl"):
        tags["no_sl"] = "ประวัติ MT5 ไม่มี S/L ในคำสั่งเปิดไม้ (ถ้าตั้งทีหลัง ระบบจะรู้เฉพาะไม้ที่เปิดหลังจากแดชบอร์ดเริ่มทำงาน)"
    elif atr:
        multiple = abs(entry - t["sl"]) / atr
        if multiple < 0.3:
            tags["sl_tight"] = f"S/L ห่างเพียง {multiple:.2f} ATR"
        elif multiple > 2:
            tags["sl_wide"] = f"S/L ห่างถึง {multiple:.2f} ATR"
    if ctx.get("in_session") is False:
        tags["off_session"] = f"เข้าช่วง{ctx['session']}"
    if ctx.get("friday_late"):
        tags["friday_late"] = f"หลัง {(config.FRIDAY_CUTOFF_UTC + 7) % 24:02d}:00 น. ไทยของวันศุกร์"
    if ctx.get("news"):
        tags["news_window"] = "ตรงกับช่วงตัวเลขจ้างงานสหรัฐหรือการประชุมเฟด"
    if side < 0:
        tags["short_side"] = "ระบบบอทไม่ออกสัญญาณขาย เพราะแบ็กเทสต์ฝั่งขายไม่ผ่าน"
    if t["origin"] == "manual":
        if t.get("signal_no"):
            tags["bot_signal"] = f"ตรงกับสัญญาณบอท #{t['signal_no']}"
        else:
            tags["manual"] = "ไม่มีสัญญาณบอทรองรับ"
    reason = t["reason"]
    if reason == 5:
        tags["exit_tp"] = f"ถึง T/P ที่ {_px(t['exit'])}"
    elif reason == 4:
        tags["exit_sl"] = f"ชน S/L ที่ {_px(t['exit'])}"
    elif reason == 3:
        tags["exit_ea"] = f"EA ปิดที่ {_px(t['exit'])}"
    elif reason == 6:
        tags["exit_stopout"] = "มาร์จิ้นไม่พอ โบรกเกอร์สั่งปิด"
    elif reason in (0, 1, 2):
        tags["exit_manual"] = f"{DEAL_REASON_TH[reason]} ที่ {_px(t['exit'])}"
    mfe_r, kept = path.get("mfe_r"), path.get("kept")
    if net < 0 and mfe_r is not None and mfe_r < 0.2:
        tags["never_worked"] = f"ไปทางกำไรสูงสุดเพียง {mfe_r:.2f}R"
    if mfe_r is not None and mfe_r >= 1 and (net <= 0 or (kept is not None and kept < 0.3)):
        tags["gave_back"] = f"เคยไปถึง +{mfe_r:.2f}R แต่จบที่ {t_r:+.2f}R" if t_r is not None else f"เคยไปถึง +{mfe_r:.2f}R"
    if net > 0 and kept is not None and kept >= 0.6:
        tags["let_run"] = f"เก็บได้ {kept * 100:.0f}% ของระยะที่ราคาไปได้"
    further = path.get("after_further")
    moved, risk = abs(t["exit"] - entry), t.get("risk") or 0.0
    if net > 0 and reason in (0, 1, 2) and further is not None and moved > 0 and further >= max(2 * moved, 0.5 * risk):
        tags["cut_early"] = f"หลังปิด ราคาไปต่ออีก {_px(further)} ภายใน {AFTER_EXIT_HOURS} ชม."
    if reason == 4 and net < 0 and path.get("after_reached_1r"):
        tags["reversed_after_sl"] = f"หลังโดน S/L {path.get('after_reached_min', 0)} นาที ราคาไปถึง +1R ตามทิศที่เข้า"
    if net < 0 and t["hold_s"] < 300:
        tags["quick_loss"] = f"ถือแค่ {t['hold_s'] // 60} นาที {t['hold_s'] % 60} วินาที"
    if net > 0 and (ctx.get("trend_ok") is False or ctx.get("breakout_ok") is False):
        tags["lucky_win"] = "กำไรทั้งที่ไม่เข้าเกณฑ์ของระบบ"
    return tags


def _checks(t: dict) -> list[dict]:
    """The entry conditions as a pass/fail list for the trade detail panel."""
    ctx = t.get("ctx") or {}
    if not ctx:
        return []
    side = t["dir"]
    word, level = ("High", ctx["y_high"]) if side > 0 else ("Low", ctx["y_low"])
    extreme = ctx["day_high"] if side > 0 else ctx["day_low"]
    trend = "ขาขึ้น" if side > 0 else "ขาลง"
    atr = ctx.get("atr")
    out = [
        {"label": f"แนวโน้มใหญ่{trend}", "ok": ctx.get("trend_ok"),
         "detail": f"ปิดเมื่อวาน {_px(ctx['y_close'])} / SMA200 {_px(ctx.get('sma200'))}"},
        {"label": f"ทะลุ {word} เมื่อวาน", "ok": ctx.get("breakout_ok"),
         "detail": f"เข้า {_px(t['entry'])} / {word} เมื่อวาน {_px(level)}"},
        {"label": f"อยู่ที่ {word} ของวัน", "ok": ctx.get("extreme_ok"),
         "detail": f"เข้า {_px(t['entry'])} / {word} ถึงตอนนั้น {_px(extreme)}"},
        {"label": "มี S/L ตอนเปิดไม้", "ok": bool(t.get("sl")),
         "detail": (f"S/L {_px(t['sl'])}" + (f" = {abs(t['entry'] - t['sl']) / atr:.2f} ATR" if atr else ""))
         if t.get("sl") else "ไม่มี S/L"},
    ]
    if ctx.get("in_session") is not None:
        out.append({"label": "อยู่ในช่วงเวลาหลัก", "ok": ctx["in_session"], "detail": f"ช่วง{ctx['session']}"})
    out.append({"label": "ไม่ตรงช่วงข่าวแรง", "ok": not ctx.get("news"), "detail": "NFP / FOMC"})
    return out


def _explain(t: dict) -> None:
    """Fill the tags, the reasons shown for this outcome and the one-line verdict."""
    outcome = "win" if t["net"] > 0 else ("loss" if t["net"] < 0 else "flat")
    tags = dict(t.get("base_tags") or {})
    tags.update(t.get("cross_tags") or {})
    order = list(TAGS)
    ordered = sorted(tags, key=lambda k: order.index(k) if k in order else len(order))
    why = []
    for key in ordered:
        label, kind, tip = TAGS.get(key, (key, "info", ""))
        why.append({"tag": key, "label": label, "kind": kind, "detail": tags[key], "tip": tip})
    bad = [w["label"] for w in why if w["kind"] == "bad"]
    good = [w["label"] for w in why if w["kind"] == "good"]
    if t["origin"] == "ea_test":
        verdict = "ไม้ทดสอบระบบ EA ไม่ใช่การตัดสินใจเทรด"
    elif outcome == "loss":
        verdict = "ขาดทุนจาก: " + " · ".join(bad[:3]) if bad else "ขาดทุนตามแผน: เข้าตามระบบ แล้วยอมตัดขาดทุน"
    elif outcome == "win":
        verdict = "กำไรจาก: " + " · ".join(good[:3]) if good else "กำไร"
        if "lucky_win" in tags:
            verdict = "กำไร แต่ไม่เข้าเกณฑ์ระบบ"
    else:
        verdict = "เท่าทุน"
    t.update(outcome=outcome, tags=ordered, why=why, verdict=verdict, checks=_checks(t))


def _cross_tags(trades: list[dict]) -> None:
    """Tags that need the trades around this one: revenge entries and too many trades a day."""
    by_entry = sorted(trades, key=lambda t: t["entry_srv"])
    per_day: dict[str, int] = defaultdict(int)
    for t in by_entry:
        cross: dict[str, str] = {}
        entry_utc = datetime.fromisoformat(t["entry_time"])
        day = entry_utc.astimezone(BKK).date().isoformat()
        per_day[day] += 1
        if per_day[day] > MAX_TRADES_PER_DAY:
            cross["overtrade"] = f"ไม้ที่ {per_day[day]} ของวัน"
        for prev in by_entry:
            if prev is t or prev["symbol"] != t["symbol"] or prev["net"] >= 0:
                continue
            gap = (entry_utc - datetime.fromisoformat(prev["exit_time"])).total_seconds() / 60
            if 0 <= gap <= REVENGE_MINUTES:
                cross["revenge"] = f"เข้าหลังไม้ขาดทุนเพียง {gap:.0f} นาที"
                break
        t["cross_tags"] = cross
        _explain(t)


# Sync ----------------------------------------------------------------------------------
def _ea_requests() -> dict[str, dict]:
    try:
        return {r["id"]: r for r in ea_bridge.list_requests()}
    except Exception as exc:  # noqa: BLE001 - only used to tell EA tests from EA signals
        log.debug("EA order requests unavailable: %s", exc)
        return {}


def _build(pid: int, deals: list, orders: dict, meta: dict | None, requests: dict) -> dict | None:
    entries = sorted((d for d in deals if d.entry == 0), key=lambda d: d.time_msc)
    exits = sorted((d for d in deals if d.entry in (1, 2, 3)), key=lambda d: d.time_msc)
    if not entries or not exits:
        return None
    first, last = entries[0], exits[-1]
    in_vol, out_vol = sum(d.volume for d in entries), sum(d.volume for d in exits)
    if in_vol <= 0 or out_vol <= 0:
        return None
    entry = sum(d.price * d.volume for d in entries) / in_vol
    exit_price = sum(d.price * d.volume for d in exits) / out_vol
    side = 1 if first.type == 0 else -1
    gross = sum(float(d.profit) for d in exits)
    commission = sum(float(d.commission) for d in deals)
    swap = sum(float(d.swap) for d in deals)
    fee = sum(float(d.fee) for d in deals)
    order = orders.get(first.order)
    sl = (float(order.sl) if order is not None else 0.0) or (meta or {}).get("first_sl")
    tp = (float(order.tp) if order is not None else 0.0) or (meta or {}).get("first_tp")
    final = re.search(r"\[sl ([\d.]+)\]", last.comment or "")
    comment = first.comment or ""
    origin = "manual"
    if first.magic == EA_MAGIC or comment.startswith("signalbot"):
        request = requests.get(comment.split()[-1], {}) if comment else {}
        origin = "ea_test" if str(request.get("comment", "")).lower().startswith("test") else "ea_signal"
    entry_utc, exit_utc = server_to_utc(first.time), server_to_utc(last.time)
    return {
        "id": str(pid), "symbol": first.symbol, "side": "buy" if side > 0 else "sell", "dir": side,
        "volume": round(float(in_vol), 4), "entry": float(entry), "exit": float(exit_price),
        "entry_srv": int(first.time), "exit_srv": int(last.time),
        "entry_time": entry_utc.isoformat(timespec="seconds"), "exit_time": exit_utc.isoformat(timespec="seconds"),
        "hold_s": max(0, int(last.time) - int(first.time)),
        "gross": round(gross, 2), "commission": round(commission, 2), "swap": round(swap, 2), "fee": round(fee, 2),
        "net": round(gross + commission + swap + fee, 2),
        "reason": int(last.reason), "reason_th": DEAL_REASON_TH.get(int(last.reason), str(last.reason)),
        "closes": len(exits), "deals": len(deals), "origin": origin, "origin_th": ORIGIN_TH[origin],
        "comment": comment[:40], "sl": float(sl) if sl else None, "tp": float(tp) if tp else None,
        "sl_final": float(final.group(1)) if final else None,
        "sl_log": (meta or {}).get("sl_log") or [],
    }


def _link_signal(t: dict, signals: list[dict]) -> None:
    """A manual trade taken while the bot's signal for that symbol and side was live."""
    for s in signals:
        if s["symbol"] == t["symbol"] and s["side"] == t["side"] and s.get("no") and \
                s["entry_time"] <= t["entry_time"] <= (s.get("exit_time") or "9999"):
            t["signal_no"] = s["no"]
            return


def sync(doc: dict, mt5) -> bool:
    """Bring the journal up to date with the terminal's history. True when anything changed."""
    now = datetime.now(UTC) + timedelta(days=2)
    deals = mt5.history_deals_get(HISTORY_START, now)
    if deals is None:
        log.warning("MT5 deal history unavailable: %s", mt5.last_error())
        return False
    orders = {o.ticket: o for o in (mt5.history_orders_get(HISTORY_START, now) or [])}
    still_open = {int(p.ticket) for p in (mt5.positions_get() or [])}
    grouped: dict[int, list] = defaultdict(list)
    deposits = 0.0
    for d in deals:
        if d.type == 2:
            deposits += float(d.profit)
        elif d.type in (0, 1) and d.position_id:
            grouped[int(d.position_id)].append(d)

    changed = doc.get("deposits") != deposits
    doc["deposits"] = deposits
    requests, signals, daily = None, None, {}
    for pid, ds in grouped.items():
        key = str(pid)
        if pid in still_open:
            continue
        old = doc["trades"].get(key)
        if old and old.get("deals") == len(ds) and old.get("analysis_v") == ANALYSIS_VERSION:
            continue
        if requests is None:
            requests, signals = _ea_requests(), bot_signals()
        t = _build(pid, ds, orders, doc["open"].get(key), requests)
        if t is None:
            continue
        if old:                                # a re-analysis keeps the user's notes and what MT5 forgot
            t["note"] = old.get("note", "")
            t["sl"] = t["sl"] or old.get("sl")
            t["tp"] = t["tp"] or old.get("tp")
            t["sl_log"] = t["sl_log"] or old.get("sl_log") or []
        _link_signal(t, signals)
        try:
            ctx = entry_context(mt5, t["symbol"], t["dir"], t["entry"], t["entry_srv"], daily)
            risk, basis = _risk(t, ctx)
            t.update(ctx=ctx, risk=risk, risk_basis=basis,
                     r=t["dir"] * (t["exit"] - t["entry"]) / risk if risk else None)
            t["path"] = _path(mt5, t, risk)
        except Exception:  # noqa: BLE001 - one odd trade must not stop the journal
            log.exception("analysis failed for position %s", pid)
            t.setdefault("ctx", None)
            t.setdefault("path", None)
        t["analysis_v"] = ANALYSIS_VERSION
        t["base_tags"] = _base_tags(t)
        doc["trades"][key] = t
        doc["open"].pop(key, None)
        changed = True
    if changed:
        _cross_tags(list(doc["trades"].values()))
        doc["rev"] += 1
        save(doc)
    return changed


# Statistics ----------------------------------------------------------------------------
def _closed_key(t: dict) -> str:
    return t["exit_time"]


def _line(rows: list[dict]) -> dict:
    wins = [t for t in rows if t["net"] > 0]
    rs = [t["r"] for t in rows if t.get("r") is not None]
    net = round(sum(t["net"] for t in rows), 2)
    return {"n": len(rows), "wins": len(wins), "win_rate": len(wins) / len(rows) * 100 if rows else None,
            "net": net, "avg_r": sum(rs) / len(rs) if rs else None}


def _group(rows: list[dict], key) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in rows:
        groups[key(t)].append(t)
    out = [{"key": k, **_line(v)} for k, v in groups.items()]
    return sorted(out, key=lambda r: r["net"], reverse=True)


def _hold_text(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} นาที"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ชม. {minutes} นาที" if hours < 24 else f"{hours // 24} วัน {hours % 24} ชม."


def summarize(rows: list[dict]) -> dict:
    """Totals, curves and breakdowns of the given trades (the money counts every trade)."""
    rows = sorted(rows, key=_closed_key)
    wins = [t for t in rows if t["net"] > 0]
    losses = [t for t in rows if t["net"] < 0]
    gross_win = sum(t["net"] for t in wins)
    gross_loss = -sum(t["net"] for t in losses)
    curve, peak, drawdown, cum = [], 0.0, 0.0, 0.0
    streak = worst_streak = 0
    for t in rows:
        cum += t["net"]
        peak = max(peak, cum)
        drawdown = max(drawdown, peak - cum)
        streak = streak + 1 if t["net"] < 0 else 0
        worst_streak = max(worst_streak, streak)
        curve.append({"t": int(datetime.fromisoformat(t["exit_time"]).timestamp()), "v": round(cum, 2), "id": t["id"]})
    days: dict[str, list[dict]] = defaultdict(list)
    for t in rows:
        days[datetime.fromisoformat(t["exit_time"]).astimezone(BKK).date().isoformat()].append(t)
    rs = [t["r"] for t in rows if t.get("r") is not None]
    avg_win = gross_win / len(wins) if wins else None
    avg_loss = gross_loss / len(losses) if losses else None
    hold_win = median(t["hold_s"] for t in wins) if wins else None
    hold_loss = median(t["hold_s"] for t in losses) if losses else None
    return {
        **_line(rows), "losses": len(losses),
        "gross_win": round(gross_win, 2), "gross_loss": round(gross_loss, 2),
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": avg_win / avg_loss if avg_win and avg_loss else None,
        "expectancy": sum(t["net"] for t in rows) / len(rows) if rows else None,
        "total_r": sum(rs) if rs else None, "max_drawdown": round(drawdown, 2), "max_loss_streak": worst_streak,
        "best": max((t["net"] for t in rows), default=None), "worst": min((t["net"] for t in rows), default=None),
        "hold_win_s": hold_win, "hold_loss_s": hold_loss,
        "hold_win": _hold_text(hold_win), "hold_loss": _hold_text(hold_loss),
        "curve": curve,
        "daily": [{"day": d, **_line(v)} for d, v in sorted(days.items())],
        "by_symbol": _group(rows, lambda t: t["symbol"]),
        "by_side": _group(rows, lambda t: "ซื้อ" if t["dir"] > 0 else "ขาย"),
        "by_exit": _group(rows, lambda t: t["reason_th"]),
        "by_origin": _group(rows, lambda t: t["origin_th"]),
        "by_session": _group(rows, lambda t: (t.get("ctx") or {}).get("session") or "ไม่ทราบ"),
        "by_weekday": _group(rows, lambda t: WEEKDAYS[datetime.fromisoformat(t["entry_time"]).astimezone(BKK).weekday()]),
    }


def analysis(rows: list[dict]) -> dict:
    """What costs money and what works. EA system tests are left out: they say nothing about trading."""
    rows = [t for t in rows if t["origin"] != "ea_test"]
    tagged: dict[str, list[dict]] = defaultdict(list)
    for t in rows:
        for tag in t.get("tags") or []:
            tagged[tag].append(t)
    table = []
    for tag, trades in tagged.items():
        label, kind, tip = TAGS.get(tag, (tag, "info", ""))
        table.append({"tag": tag, "label": label, "kind": kind, "tip": tip, **_line(trades)})
    table.sort(key=lambda r: r["net"])

    review, habits = [], []                  # what cost money / risky habits that have not cost money yet
    for r in table:
        if r["kind"] != "bad":
            continue
        item = {"title": r["label"], "tag": r["tag"], "n": r["n"], "net": r["net"], "win_rate": r["win_rate"], "text": r["tip"]}
        (review if r["net"] < 0 else habits).append(item)
    habits.sort(key=lambda r: r["n"], reverse=True)
    s = summarize(rows) if rows else None
    if s and s["hold_win_s"] and s["hold_loss_s"] and s["hold_loss_s"] > 1.5 * s["hold_win_s"]:
        review.append({"title": "ถือไม้ขาดทุนนานกว่าไม้กำไร", "tag": "", "n": s["losses"], "net": None, "win_rate": None,
                       "text": f"ไม้กำไรถือเฉลี่ย {s['hold_win']} แต่ไม้ขาดทุนถือ {s['hold_loss']} ลองตัดขาดทุนให้เร็วขึ้น"})
    if s and s["payoff"] and s["payoff"] < 1:
        review.append({"title": "กำไรต่อไม้เล็กกว่าขาดทุนต่อไม้", "tag": "", "n": s["n"], "net": None, "win_rate": None,
                       "text": f"กำไรเฉลี่ย {s['avg_win']:,.2f} เทียบขาดทุนเฉลี่ย {s['avg_loss']:,.2f} "
                               f"ต้องชนะเกิน {100 / (1 + s['payoff']):.0f}% จึงจะคุ้มทุน"})
    volumes = sorted(t["volume"] for t in rows)
    if len(volumes) >= 4 and volumes[-1] >= 5 * median(volumes):
        review.append({"title": "ขนาดไม้ไม่สม่ำเสมอ", "tag": "", "n": len(volumes), "net": None, "win_rate": None,
                       "text": f"ใช้ตั้งแต่ {volumes[0]:g} ถึง {volumes[-1]:g} ล็อต (กลาง ๆ {median(volumes):g}) ผลรวมจึงขึ้นกับไม้ใหญ่ไม่กี่ไม้ "
                               "ลองคำนวณล็อตจากความเสี่ยงต่อไม้ให้เท่ากันทุกไม้"})
    works = [{"title": r["label"], "tag": r["tag"], "n": r["n"], "net": r["net"], "win_rate": r["win_rate"]}
             for r in reversed(table) if r["kind"] == "good" and r["net"] > 0]
    return {"tags": table, "review": review, "habits": habits, "works": works, "n": len(rows),
            "small_sample": len(rows) < MIN_SAMPLE, "min_sample": MIN_SAMPLE}


def query(doc: dict, period: str = "all", symbol: str = "", origin: str = "") -> list[dict]:
    """Closed trades matching the filters, newest first."""
    rows = list(doc["trades"].values())
    now = datetime.now(BKK)
    since = None
    if period in ("7", "30", "90"):
        since = now - timedelta(days=int(period))
    elif period == "month":
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if since is not None:
        rows = [t for t in rows if datetime.fromisoformat(t["exit_time"]) >= since]
    if symbol:
        rows = [t for t in rows if t["symbol"] == symbol]
    if origin:
        rows = [t for t in rows if t["origin"] == origin]
    return sorted(rows, key=_closed_key, reverse=True)


def closed_totals(doc: dict) -> dict:
    """Realised profit today and this month, Bangkok calendar."""
    today = datetime.now(BKK).date()
    out = {"today": 0.0, "today_n": 0, "month": 0.0, "month_n": 0}
    for t in doc["trades"].values():
        day: date = datetime.fromisoformat(t["exit_time"]).astimezone(BKK).date()
        if (day.year, day.month) == (today.year, today.month):
            out["month"] += t["net"]
            out["month_n"] += 1
            if day == today:
                out["today"] += t["net"]
                out["today_n"] += 1
    return out


# The bot's own signals -----------------------------------------------------------------
def _iso_utc(text: str | None) -> str | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _trend_join_row(rec: dict, live_keys: set) -> dict:
    status = rec.get("status") or ""
    key = (rec.get("symbol"), rec.get("triggered_at"))
    tracking = status in ("active", "weakened", "partial")
    if tracking and key not in live_keys:
        status = "dropped"
    trailed = float(rec.get("stop") or 0) > float(rec.get("initial_stop") or 0)
    exit_th = {
        "active": "ยังติดตามอยู่", "weakened": "ยังติดตามอยู่ (สัญญาณอ่อนลง)", "partial": "ปิดครึ่งแล้ว ที่เหลือวิ่งตาม S/L",
        "stopped": "ชน S/L ที่เลื่อนตาม" if trailed else "ชน S/L", "expired": f"ครบ {rec.get('max_hold_days', 10)} วันทำการ",
        "dropped": "หยุดติดตาม (นำออกจากรายการเฝ้าดู)",
    }.get(status, status)
    return {
        "kind": "สัญญาณ Trend-join", "no": rec.get("no"), "symbol": rec.get("symbol"), "side": "buy",
        "entry_time": _iso_utc(rec.get("triggered_at")), "entry": rec.get("entry"),
        "stop": rec.get("initial_stop") or rec.get("stop"), "tp": rec.get("partial_level"),
        "exit": rec.get("exit_price"), "exit_time": _iso_utc(rec.get("closed_at")), "r": rec.get("result_r"),
        "status": status, "open": status in ("active", "weakened", "partial"),
        "why_entry": f"ทะลุ High เมื่อวาน {_px(rec.get('breakout_level'))} ที่จุดสูงสุดของวัน "
                     f"และปิดเมื่อวาน {_px(rec.get('yesterday_close'))} เหนือ SMA200 {_px(rec.get('sma200'))}",
        "why_exit": exit_th,
    }


def bot_signals() -> list[dict]:
    """Scanner C signals and paper day-trades, newest first, each with why it was issued and how it ended."""
    records: dict[tuple, dict] = {}
    for path in sorted(config.OUTPUT_DIR.glob("special_signals_*.json")):
        for rec in _read_json(path) or []:
            if isinstance(rec, dict) and rec.get("triggered_at"):
                records[(rec.get("symbol"), rec["triggered_at"])] = rec
    results = config.DATA_DIR / "signal_results.jsonl"
    if results.exists():
        for line in results.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("triggered_at"):
                records[(rec.get("symbol"), rec["triggered_at"])] = {**records.get((rec.get("symbol"), rec["triggered_at"]), {}), **rec}
    state = _read_json(config.DATA_DIR / "special_watch_state.json") or {}
    live_keys = set()
    for sym, rec in state.items():
        if isinstance(rec, dict) and rec.get("triggered_at") and not sym.startswith("hold:"):
            key = (rec.get("symbol"), rec["triggered_at"])
            records[key] = rec
            live_keys.add(key)
    rows = [_trend_join_row(rec, live_keys) for rec in records.values()]

    paper = config.DATA_DIR / "daytrade_paper.jsonl"
    if paper.exists():
        how = {"tp": "ถึง T/P", "stop": "ชน S/L", "eod": "ปิดสิ้นวัน (ไม่ถือข้ามคืน)"}
        for line in paper.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            up = int(rec.get("side") or 1) > 0
            rows.append({
                "kind": "เดย์เทรดทดสอบ (กระดาษ)", "no": None, "symbol": rec.get("symbol"), "side": "buy" if up else "sell",
                "entry_time": rec.get("entry_at"), "entry": rec.get("entry"), "stop": rec.get("stop"), "tp": rec.get("tp"),
                "exit": rec.get("exit"), "exit_time": rec.get("closed_at"), "r": rec.get("net_r"),
                "status": "closed", "open": False,
                "why_entry": "แท่ง 15 นาทีปิด" + ("เหนือ High เมื่อวาน" if up else "ใต้ Low เมื่อวาน") + " ในช่วงลอนดอนถึงนิวยอร์ก",
                "why_exit": how.get(rec.get("reason"), str(rec.get("reason"))),
            })
    rows = [r for r in rows if r["entry_time"]]
    rows.sort(key=lambda r: r["entry_time"], reverse=True)
    return rows
