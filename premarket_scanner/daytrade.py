"""Paper day-trade test: previous-day high/low breakout on 15-minute bars (USDJPY by default).

These are the rules that came out best in the 4-year forex day-trade backtest (10 major
pairs, real-account costs). Only USDJPY was clearly positive there, and that may be luck,
so this runs on paper first to see whether it holds on new data:

* trading day = the FX day, which starts at 17:00 New York (MT5 server clock = New York + 7 h)
* window 07:00 London .. 12:00 New York: the first completed M15 bar that closes above
  yesterday's high goes long, below yesterday's low goes short; one trade per day
* entry = that bar's close; stop = 0.5 x daily ATR(14) as of yesterday; T/P = 1R
* exit at the stop or the T/P (checked on M5 bars; the stop counts first when one bar
  touches both), otherwise at 16:00 New York; never held overnight
* result in R after a round-trip cost (spread + commission of a real account)

Paper only: no order reaches MT5 and nothing goes to the groups. Entry and exit alerts go to
the owner chat. Closed trades are appended to ``data/daytrade_paper.jsonl``.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from . import config, mt5_source, trade_fmt

log = logging.getLogger(__name__)

UTC = timezone.utc
NY, LON, BKK = ZoneInfo("America/New_York"), ZoneInfo("Europe/London"), ZoneInfo("Asia/Bangkok")
SERVER_MINUS_NY = timedelta(hours=7)          # FX convention: server midnight = 17:00 New York
WINDOW_START_LON = dtime(7, 0)
WINDOW_END_NY = dtime(12, 0)
FLAT_BY_NY = dtime(16, 0)
STOP_ATR = 0.5
TP_R = 1.0
ATR_PERIOD = 14
TF_MINUTES = {"M5": 5, "M15": 15}
COST = {  # round-trip spread + commission, price units (same table as the backtest)
    "EURUSD": 0.00010, "GBPUSD": 0.00013, "USDJPY": 0.012, "AUDUSD": 0.00012, "USDCAD": 0.00016,
    "USDCHF": 0.00015, "NZDUSD": 0.00016, "EURJPY": 0.016, "EURGBP": 0.00012, "GBPJPY": 0.025,
}


def state_file() -> Path:
    return config.DATA_DIR / "daytrade_state.json"


def results_file() -> Path:
    return config.DATA_DIR / "daytrade_paper.jsonl"


# Time ---------------------------------------------------------------------------------
def _to_server(dt_utc: datetime) -> datetime:
    """A real time as the broker's server clock, encoded as UTC (what copy_rates_* compares)."""
    ny = dt_utc.astimezone(NY).replace(tzinfo=None)
    return (ny + SERVER_MINUS_NY).replace(tzinfo=UTC)


def fx_day(now_utc: datetime) -> date:
    return (now_utc.astimezone(NY).replace(tzinfo=None) + SERVER_MINUS_NY).date()


def window(day: date) -> tuple[datetime, datetime, datetime]:
    """(signal window start, signal window end, flat-by time) of one FX day, in UTC."""
    return (datetime.combine(day, WINDOW_START_LON, LON).astimezone(UTC),
            datetime.combine(day, WINDOW_END_NY, NY).astimezone(UTC),
            datetime.combine(day, FLAT_BY_NY, NY).astimezone(UTC))


def _bkk(dt_utc: datetime) -> str:
    return dt_utc.astimezone(BKK).strftime("%H:%M")


# MT5 data ------------------------------------------------------------------------------
def _bars(symbol: str, tf: str, start_utc: datetime, end_utc: datetime) -> pd.DataFrame:
    """Bars of ``tf`` from MT5 with real UTC start/end times."""
    mt5 = mt5_source._mt5()
    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_range(symbol, getattr(mt5, f"TIMEFRAME_{tf}"), _to_server(start_utc), _to_server(end_utc))
    if rates is None or len(rates) == 0:
        return pd.DataFrame(columns=["start", "end", "open", "high", "low", "close"])
    df = pd.DataFrame(rates)
    server = pd.DatetimeIndex(pd.to_datetime(df["time"], unit="s"))
    start = (server - SERVER_MINUS_NY).tz_localize(NY, ambiguous=False, nonexistent="shift_forward").tz_convert(UTC)
    df["start"] = start
    df["end"] = start + pd.Timedelta(minutes=TF_MINUTES[tf])
    return df[["start", "end", "open", "high", "low", "close"]].reset_index(drop=True)


def levels(symbol: str, day: date) -> dict | None:
    """Yesterday's high/low/close and the daily ATR(14) as of yesterday (server-day D1 bars)."""
    mt5 = mt5_source._mt5()
    mt5.symbol_select(symbol, True)
    day_start = datetime.combine(day, dtime(0, 0), UTC)       # server midnight, server-encoded
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_D1, day_start - timedelta(days=500),
                                 day_start - timedelta(seconds=1))
    if rates is None or len(rates) < ATR_PERIOD + 1:
        return None
    d1 = pd.DataFrame(rates)
    prev_close = d1["close"].shift(1)
    tr = pd.concat([d1["high"] - d1["low"], (d1["high"] - prev_close).abs(), (d1["low"] - prev_close).abs()],
                   axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean()
    y = d1.iloc[-1]
    return {"date": datetime.fromtimestamp(int(y["time"]), UTC).date().isoformat(),
            "high": float(y["high"]), "low": float(y["low"]), "close": float(y["close"]), "atr": float(atr.iloc[-1])}


# State and results ---------------------------------------------------------------------
def _load(path: Path | None) -> dict:
    p = path or state_file()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state: dict, path: Path | None) -> None:
    p = path or state_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _append_result(rec: dict, path: Path | None) -> None:
    p = path or results_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def tally(symbol: str | None = None, path: Path | None = None) -> dict:
    """Every closed paper trade so far (optionally one symbol): count, WIN/LOSS and total R."""
    p = path or results_file()
    rows = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if symbol is None or r.get("symbol") == symbol:
                    rows.append(r)
    except (OSError, ValueError):
        pass
    net = [float(r["net_r"]) for r in rows]
    return {"trades": len(net), "win": sum(1 for v in net if v > 0), "loss": sum(1 for v in net if v < 0),
            "total_r": sum(net)}


# Text ----------------------------------------------------------------------------------
def _px(v: float) -> str:
    v = float(v)
    decimals = 2 if abs(v) >= 1000 else (3 if abs(v) >= 100 else (4 if abs(v) >= 10 else 5))
    return f"{v:,.{decimals}f}"


def fmt_entry(rec: dict, now: datetime) -> str:
    side = "buy" if rec["side"] > 0 else "sell"
    trig = datetime.fromisoformat(rec["entry_at"])
    flat = datetime.fromisoformat(rec["flat_by"])
    late = int((now - trig).total_seconds() // 60)
    lines = [
        f"🧪 เดย์เทรดทดสอบ (กระดาษ ไม่ส่งคำสั่งจริง) | {rec['symbol']}",
        f"{trade_fmt.badge(side)} {trade_fmt.ENTRY} เข้า {_px(rec['entry'])}",
        f"{trade_fmt.tp_arrow(side)} TP {_px(rec['tp'])} (+{TP_R:g}R)",
        f"{trade_fmt.stop_arrow(side)} SL {_px(rec['stop'])} (-1R)",
        f"สัญญาณ {_bkk(trig)} น. ไทย | ปิดไม่เกิน {_bkk(flat)} น. ไทย (ไม่ถือข้ามคืน)",
        "เหตุผล: แท่ง 15 นาทีปิด" + ("เหนือจุดสูงสุด" if rec["side"] > 0 else "ใต้จุดต่ำสุด")
        + f"เมื่อวาน {_px(rec['level'])}",
    ]
    if late > 20:
        lines.append(f"(บอทตรวจพบช้ากว่าสัญญาณ {late} นาที ราคาจริงตอนนี้อาจต่างจากราคาเข้า)")
    return "\n".join(lines)


def fmt_exit(rec: dict, score: dict) -> str:
    side = "buy" if rec["side"] > 0 else "sell"
    r = float(rec["net_r"])
    icon, word = ("✅", "WIN") if r > 0 else (("❌", "LOSS") if r < 0 else ("➖", "เสมอตัว"))
    how = {"tp": "ถึง TP", "stop": "ชน SL", "eod": "ปิดสิ้นวัน"}[rec["reason"]]
    lines = [
        f"🧪 {icon} เดย์เทรดทดสอบ {rec['symbol']} {trade_fmt.badge(side)} {word} {r:+.2f}R",
        f"{trade_fmt.EXIT} ออก {_px(rec['exit'])} ({how}) | เข้า {_px(rec['entry'])} | หักต้นทุนแล้ว",
    ]
    if score["trades"]:
        lines.append(f"📊 ทดสอบมาแล้ว {score['trades']} ไม้: {score['win']} WIN {score['loss']} LOSS "
                     f"| รวม {score['total_r']:+.2f}R")
    return "\n".join(lines)


# Core ----------------------------------------------------------------------------------
def _close(rec: dict, price: float, reason: str, at: datetime, results_path: Path | None) -> None:
    risk = abs(rec["entry"] - rec["stop"])
    gross = rec["side"] * (price - rec["entry"]) / risk
    cost_r = COST.get(rec["symbol"], 0.0) / risk
    rec.update(status="closed", exit=float(price), reason=reason, closed_at=at.isoformat(timespec="seconds"),
               gross_r=round(gross, 4), cost_r=round(cost_r, 4), net_r=round(gross - cost_r, 4))
    _append_result({k: rec[k] for k in ("symbol", "day", "side", "entry", "stop", "tp", "entry_at", "exit",
                                        "reason", "closed_at", "gross_r", "cost_r", "net_r")}, results_path)


def _follow(rec: dict, now: datetime, results_path: Path | None) -> bool:
    """Check an open trade for its stop/T/P on completed M5 bars, or close it at the flat-by
    time. Returns True when the trade closed."""
    flat_by = datetime.fromisoformat(rec["flat_by"])
    since = datetime.fromisoformat(rec["entry_at"])
    upto = min(now, flat_by)
    bars = _bars(rec["symbol"], "M5", since, upto + timedelta(minutes=5))
    bars = bars[(bars["start"] >= since) & (bars["end"] <= upto)] if not bars.empty else bars
    side, stop, tp = rec["side"], rec["stop"], rec["tp"]
    for b in bars.itertuples():
        hit_stop = b.low <= stop if side > 0 else b.high >= stop
        hit_tp = b.high >= tp if side > 0 else b.low <= tp
        if hit_stop or hit_tp:
            _close(rec, stop if hit_stop else tp, "stop" if hit_stop else "tp", b.end.to_pydatetime(), results_path)
            return True
    if now >= flat_by:
        if not bars.empty:
            last = bars.iloc[-1]
            price, at = float(last["close"]), last["end"].to_pydatetime()
        else:
            price, at = rec["entry"], flat_by
            log.warning("%s: no M5 bars up to the flat-by time; paper trade closed at entry", rec["symbol"])
        _close(rec, price, "eod", at, results_path)
        return True
    return False


def _check_symbol(symbol: str, now: datetime, state: dict, results_path: Path | None) -> list[str]:
    out: list[str] = []
    rec = state.get(symbol)
    if rec and rec.get("status") == "open" and _follow(rec, now, results_path):
        out.append(fmt_exit(rec, tally(symbol, results_path)))

    day = fx_day(now)
    if day.weekday() >= 5:
        return out
    if rec and rec.get("day") == day.isoformat():
        return out                                   # one trade, or one look, per day
    start, end, flat_by = window(day)
    if now < start + timedelta(minutes=15):
        return out
    lv = levels(symbol, day)
    if lv is None:
        log.warning("%s: no daily levels for %s", symbol, day)
        return out
    bars = _bars(symbol, "M15", start, min(now, end) + timedelta(minutes=15))
    if not bars.empty:
        bars = bars[(bars["start"] >= start) & (bars["start"] < end) & (bars["end"] <= now)]
    trigger = None
    for b in bars.itertuples():
        if b.close > lv["high"]:
            trigger = (1, b)
            break
        if b.close < lv["low"]:
            trigger = (-1, b)
            break
    if trigger is None:
        if now >= end:
            state[symbol] = {"symbol": symbol, "day": day.isoformat(), "status": "no_signal"}
        return out
    side, b = trigger
    entry = float(b.close)
    risk = STOP_ATR * lv["atr"]
    rec = {
        "symbol": symbol, "day": day.isoformat(), "status": "open", "side": side,
        "entry": entry, "stop": entry - side * risk, "tp": entry + side * TP_R * risk,
        "level": lv["high"] if side > 0 else lv["low"], "atr": lv["atr"],
        "entry_at": b.end.to_pydatetime().isoformat(timespec="seconds"),
        "flat_by": flat_by.isoformat(timespec="seconds"),
    }
    state[symbol] = rec
    out.append(fmt_entry(rec, now))
    log.info("%s: paper day-trade %s entry=%s stop=%s tp=%s", symbol, "BUY" if side > 0 else "SELL",
             rec["entry"], rec["stop"], rec["tp"])
    if _follow(rec, now, results_path):              # the bot was down: catch up at once
        out.append(fmt_exit(rec, tally(symbol, results_path)))
    return out


def check(now: datetime | None = None, send: bool = True, symbols: list[str] | None = None,
          state_path: Path | None = None, results_path: Path | None = None) -> list[str]:
    """One pass over the paper day-trade symbols. Returns the messages (sent to the owner only)."""
    from . import telegram_notify as tg

    now = (now or datetime.now(UTC)).astimezone(UTC)
    if not mt5_source.connect():
        log.warning("day-trade paper test: MT5 not reachable")
        return []
    state = _load(state_path)
    messages: list[str] = []
    for symbol in symbols or config.DAYTRADE_SYMBOLS:
        try:
            messages += _check_symbol(symbol, now, state, results_path)
        except Exception:  # noqa: BLE001 - one symbol must not stop the others
            log.exception("day-trade paper test failed for %s", symbol)
    _save(state, state_path)
    if send:
        for m in messages:
            tg.send_message(m, admin_only=True)      # owner chat only, never the groups
    return messages
