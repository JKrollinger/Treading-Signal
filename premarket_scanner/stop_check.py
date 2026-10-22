"""Was a stop-loss hit by the market, or only by this broker's prices?

When MT5 closes a position by Stop Loss, ``check(position, close)`` compares the broker's
prices around the close with an independent feed (TradingView, e.g. OANDA:XAUUSD or
FX:EURUSD):

* TradingView's scanner gives the low/high of the current and the previous bar for 5, 15,
  60 and 240 minute bars. The smallest bar size whose two bars surely cover the stop time
  is used, so the window is a few minutes wide when the close is noticed quickly (the
  portfolio check runs every 5 minutes) and at most 8 hours otherwise.
* A long's stop triggers on the broker's bid falling to the S/L; the market "reached" it
  when the independent low is at or below the S/L. A short's stop triggers on the ask, so
  the independent high plus the broker's normal spread is compared with the S/L.
* A gap of up to ``TOLERANCE_SPREADS`` normal spreads, and never less than
  ``TOLERANCE_ATR`` of the daily ATR (some symbols show a zero spread), counts as normal
  because feeds differ by their own spreads; beyond that the stop was hit by this broker's
  prices only.

Read-only: nothing is sent to the broker.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import config, mt5_source

log = logging.getLogger(__name__)

INTERVALS_MIN = (5, 15, 60, 240)
TOLERANCE_SPREADS = 3.0
TOLERANCE_ATR = 0.02        # floor: 2% of the daily ATR (gold ~1.4 USD, EURUSD ~1.4 pips)
DEAL_REASON_SL = 4


def _daily_atr(mt5, symbol: str, period: int = 14) -> float | None:
    try:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 1, period + 1)
    except Exception:  # noqa: BLE001
        return None
    if rates is None or len(rates) < period + 1:
        return None
    trs = [max(rates[i]["high"] - rates[i]["low"], abs(rates[i]["high"] - rates[i - 1]["close"]),
               abs(rates[i]["low"] - rates[i - 1]["close"])) for i in range(1, len(rates))]
    return float(sum(trs) / len(trs))


@dataclass
class StopCheck:
    symbol: str
    side: str                    # buy | sell
    stop: float
    fill: float
    digits: int
    source: str | None = None    # TradingView symbol used
    window_min: int | None = None
    market_extreme: float | None = None   # independent low (buy) / high (sell) in the window
    broker_extreme: float | None = None   # broker bid low (buy) / ask high (sell) near the stop
    spread: float | None = None           # broker's normal spread in price units
    tolerance: float | None = None        # allowed feed difference before "broker only"
    gap: float | None = None              # how far the market stayed from the stop (> 0 = did not reach)
    verdict: str = "unknown"              # reached | near | broker_only | unknown
    note: str = ""

    def fmt(self, v: float | None) -> str:
        return "n/a" if v is None else f"{v:,.{self.digits}f}"


def _broker_extreme(mt5, symbol: str, side: str, ts_utc: datetime, point: float) -> float | None:
    """Broker's bid low (buy) or ask high (sell) in the minutes around the stop."""
    try:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 600)
    except Exception as exc:  # noqa: BLE001
        log.debug("M1 rates unavailable for %s: %s", symbol, exc)
        return None
    if rates is None or len(rates) == 0:
        return None
    lo, hi = ts_utc - timedelta(minutes=2), ts_utc + timedelta(minutes=1)
    picked = [r for r in rates if lo <= mt5_source._to_utc(r["time"]) <= hi]
    if not picked:
        return None
    if side == "buy":
        return float(min(r["low"] for r in picked))
    return float(max(r["high"] + r["spread"] * point for r in picked))


def check(position: dict, close: dict, now: datetime | None = None) -> StopCheck | None:
    """Compare the broker with TradingView for a position closed by Stop Loss (else None)."""
    if not close or int(close.get("reason", -1)) != DEAL_REASON_SL:
        return None
    from . import special, tradingview

    symbol = position["symbol"]
    side = "sell" if str(position.get("type")).lower() == "sell" else "buy"
    fill = float(close["price"])
    stop = float(position.get("sl") or 0.0) or fill
    ts = close["time"] if isinstance(close["time"], datetime) else datetime.fromisoformat(str(close["time"]))
    now = now or datetime.now(timezone.utc)

    mt5 = mt5_source._mt5()
    mt5_source.server_offset_hours(symbol)          # make sure server-time conversion is set
    info = mt5.symbol_info(symbol)
    digits = int(info.digits) if info else 5
    point = float(info.point) if info else 0.0
    res = StopCheck(symbol, side, stop, fill, digits)
    res.spread = float(info.spread) * point if info and info.spread else None
    res.broker_extreme = _broker_extreme(mt5, symbol, side, ts, point)

    tv_symbol = special.tradingview_equivalent(symbol)
    if not tv_symbol:
        res.note = "no_tv_symbol"
        return res
    res.source = tv_symbol
    age_min = max(0.0, (now - ts).total_seconds() / 60.0)
    interval = next((i for i in INTERVALS_MIN if age_min <= i - 1), None)   # 1 min margin for TV lag
    if interval is None:
        res.note = "too_late"
        return res
    field = "low" if side == "buy" else "high"
    cols = [f"{field}|{interval}", f"{field}[1]|{interval}", "update_mode"]
    try:
        row = tradingview.fetch_rows([tv_symbol], cols).get(tv_symbol.upper())
    except Exception as exc:  # noqa: BLE001
        log.warning("stop check: TradingView request failed: %s", exc)
        row = None
    vals = [float(v) for v in ((row or {}).get(c) for c in cols[:2]) if isinstance(v, (int, float))]
    if not vals:
        res.note = "no_tv_data"
        return res
    if row and row.get("update_mode") and "delayed" in str(row.get("update_mode")):
        res.note = "tv_delayed"
    res.window_min = interval
    spread = res.spread or 0.0
    if side == "buy":
        res.market_extreme = min(vals)
        res.gap = res.market_extreme - stop                    # > 0: the market stayed above the stop
    else:
        res.market_extreme = max(vals)
        res.gap = stop - (res.market_extreme + spread)         # > 0: market ask stayed below the stop
    atr = _daily_atr(mt5, symbol)
    res.tolerance = max(TOLERANCE_SPREADS * spread, TOLERANCE_ATR * atr if atr else 0.0)
    if res.gap <= 0:
        res.verdict = "reached"
    elif res.gap <= res.tolerance:
        res.verdict = "near"
    else:
        res.verdict = "broker_only"
    log.info("stop check %s %s: stop %s fill %s market %s (%s, %d min) broker %s spread %s gap %s -> %s",
             symbol, side, stop, fill, res.market_extreme, tv_symbol, interval, res.broker_extreme,
             res.spread, res.gap, res.verdict)
    return res


def format_th(r: StopCheck) -> str:
    f = r.fmt
    word = "ต่ำสุด" if r.side == "buy" else "สูงสุด"
    lines = []
    slip = r.fill - r.stop if r.side == "sell" else r.stop - r.fill     # > 0 = filled worse than the S/L
    if abs(slip) >= 10 ** -r.digits:
        lines.append(f"ปิดที่ {f(r.fill)} {'แย่กว่า' if slip > 0 else 'ดีกว่า'} S/L {f(r.stop)} อยู่ {f(abs(slip))} (สลิปเพจ)")
    if r.verdict == "unknown":
        why = {"no_tv_symbol": "ไม่มีสัญลักษณ์เทียบบน TradingView", "too_late": "ตรวจพบช้าเกิน 4 ชั่วโมง",
               "no_tv_data": "ดึงราคา TradingView ไม่ได้"}.get(r.note, r.note or "ข้อมูลไม่พอ")
        lines.append(f"🔎 เทียบราคาตลาดตอนโดน S/L ไม่ได้: {why}")
        return "\n".join(lines)
    head = (f"🔎 เทียบราคาตลาด ({r.source} ช่วง {r.window_min}-{2 * r.window_min} นาทีล่าสุด ซึ่งครอบเวลาที่โดน S/L): "
            f"ราคา{word} {f(r.market_extreme)}")
    broker = f" | ราคาโบรกเกอร์{word} {f(r.broker_extreme)}" if r.broker_extreme is not None else ""
    ask_note = "\n(ไม้ขายโดน S/L จากราคา Ask จึงบวกสเปรดปกติให้ราคาตลาดก่อนเทียบ)" if r.side == "sell" else ""
    if r.verdict == "reached":
        tail = "✅ ราคาตลาดทั่วไปแตะ S/L จริง ไม่ใช่ราคาเฉพาะของโบรกเกอร์"
    elif r.verdict == "near":
        tail = (f"✅ ราคาตลาดมาใกล้ S/L ห่าง {f(r.gap)} อยู่ในส่วนต่างปกติระหว่างแหล่งราคา "
                f"(ไม่เกิน {f(r.tolerance)}) ถือว่าตลาดแตะ S/L")
    else:
        times = f" หรือ {r.gap / r.spread:.0f} เท่าของสเปรดปกติ" if r.spread else ""
        tail = (f"⚠️ ราคาตลาดทั่วไปไม่ถึง S/L ห่าง {f(r.gap)}{times} "
                f"แปลว่า S/L โดนจากราคาของโบรกเกอร์นี้เท่านั้น ถ้าเกิดซ้ำบ่อยควรพิจารณาเปลี่ยนโบรกเกอร์")
    lines.append(head + broker + ask_note)
    lines.append(tail)
    if r.note == "tv_delayed":
        lines.append("หมายเหตุ: ราคา TradingView ของสัญลักษณ์นี้ล่าช้า ผลเทียบอาจคลาดเคลื่อน")
    return "\n".join(lines)


def format_en(r: StopCheck) -> str:
    f = r.fmt
    word = "low" if r.side == "buy" else "high"
    if r.verdict == "unknown":
        return f"Stop check vs market: unavailable ({r.note or 'no data'})"
    verdict = {"reached": "the market really reached the S/L",
               "near": f"the market came within {f(r.gap)} of the S/L (normal spread difference)",
               "broker_only": f"WARNING: the market stayed {f(r.gap)} away; only this broker's price hit the S/L"}[r.verdict]
    broker = f", broker {word} {f(r.broker_extreme)}" if r.broker_extreme is not None else ""
    return (f"Stop check vs {r.source} ({r.window_min}-{2 * r.window_min} min window): market {word} "
            f"{f(r.market_extreme)}{broker}, S/L {f(r.stop)}, fill {f(r.fill)} -> {verdict}")
