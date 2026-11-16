"""Scalping assistant: what the owner's own trades say, and a nudge while a trade is open.

The owner decides every entry and exit. This module only reads:

* ``review()`` - the manual trades of the journal, cut the ways that matter to a scalper: how
  long they were held, the size of the wins against the size of the losses (and the win rate
  that mix needs to break even), which side, session and hour pay, and how often the plan
  (a stop on every trade, out within ``SCALP_MAX_HOLD_MIN``) was kept.
* ``watch()`` - open positions that have no stop, or have outstayed the plan. The dashboard
  shows them and the owner gets one Telegram line per position and problem.

Nothing here places, changes or closes an order.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo

from . import config

log = logging.getLogger(__name__)

BKK = ZoneInfo("Asia/Bangkok")
HOLD_BUCKETS = [(5 * 60, "ไม่เกิน 5 นาที"), (30 * 60, "5-30 นาที"), (4 * 3600, "30 นาที - 4 ชม."), (float("inf"), "นานกว่า 4 ชม.")]
MIN_FOR_VERDICT = 20            # manual trades before the numbers are worth acting on
_alerted: dict[tuple[int, str], float] = {}


def plan() -> dict:
    return {"max_hold_min": config.SCALP_MAX_HOLD_MIN, "no_sl_grace_s": config.SCALP_NO_SL_GRACE_S,
            "enabled": config.SCALP_COACH_ENABLED}


def _move(t: dict) -> float:
    """Price units kept by the trade (dollars per ounce on gold), before costs."""
    return t["dir"] * (t["exit"] - t["entry"])


def _line(rows: list[dict]) -> dict:
    n = len(rows)
    wins = [t for t in rows if t["net"] > 0]
    return {"n": n, "wins": len(wins), "win_rate": len(wins) / n * 100 if n else None,
            "net": round(sum(t["net"] for t in rows), 2), "avg_move": sum(_move(t) for t in rows) / n if n else None}


def _group(rows: list[dict], key, order: list[str] | None = None) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in rows:
        groups[key(t)].append(t)
    out = [{"key": k, **_line(v)} for k, v in groups.items()]
    if order:
        return sorted(out, key=lambda r: order.index(r["key"]) if r["key"] in order else len(order))
    return sorted(out, key=lambda r: r["net"], reverse=True)


def _bucket(seconds: float) -> str:
    return next(label for limit, label in HOLD_BUCKETS if seconds <= limit)


def _money(v: float) -> str:
    return f"{v:+,.0f}"


def review(rows: list[dict]) -> dict:
    """The owner's manual trades as a scalper would read them. ``rows`` = journal trades."""
    rows = [t for t in rows if t.get("origin") == "manual"]
    out: dict = {"plan": plan(), "n": len(rows), "enough": len(rows) >= MIN_FOR_VERDICT, "min_trades": MIN_FOR_VERDICT}
    if not rows:
        return out
    wins = [t for t in rows if t["net"] > 0]
    losses = [t for t in rows if t["net"] <= 0]
    avg_win = sum(_move(t) for t in wins) / len(wins) if wins else None
    avg_loss = -sum(_move(t) for t in losses) / len(losses) if losses else None
    win_rate = len(wins) / len(rows) * 100
    breakeven = avg_loss / (avg_win + avg_loss) * 100 if avg_win and avg_loss and avg_win + avg_loss > 0 else None
    limit_s = config.SCALP_MAX_HOLD_MIN * 60
    over = [t for t in rows if t["hold_s"] > limit_s]
    bare = [t for t in rows if "no_sl" in (t.get("tags") or [])]
    hold_win = median(t["hold_s"] for t in wins) if wins else None
    hold_loss = median(t["hold_s"] for t in losses) if losses else None
    worst = min(rows, key=lambda t: t["net"])
    out.update({
        "win_rate": win_rate, "breakeven": breakeven, "edge": win_rate - breakeven if breakeven is not None else None,
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": avg_win / avg_loss if avg_win and avg_loss else None,
        "net": round(sum(t["net"] for t in rows), 2),
        "no_sl": len(bare), "over_hold": len(over), "over_hold_net": round(sum(t["net"] for t in over), 2),
        "in_plan": _line([t for t in rows if t["hold_s"] <= limit_s]), "out_plan": _line(over),
        "hold_win_s": hold_win, "hold_loss_s": hold_loss,
        "by_hold": _group(rows, lambda t: _bucket(t["hold_s"]), [b[1] for b in HOLD_BUCKETS]),
        "by_side": _group(rows, lambda t: "ซื้อ" if t["dir"] > 0 else "ขาย"),
        "by_session": _group(rows, lambda t: (t.get("ctx") or {}).get("session") or "ไม่ทราบ"),
        "by_hour": _group(rows, lambda t: f"{datetime.fromisoformat(t['entry_time']).astimezone(BKK).hour:02d}:00"),
    })

    notes: list[dict] = []

    def note(kind: str, title: str, text: str) -> None:
        notes.append({"kind": kind, "title": title, "text": text})

    if bare:
        note("bad", f"ไม่ตั้ง S/L {len(bare)} จาก {len(rows)} ไม้",
             "ไม้ที่ไม่มี S/L คือไม้ที่ขาดทุนได้ไม่จำกัด สแกลป์เป้าเล็กทนไม้แบบนี้ไม่ได้แม้แต่ไม้เดียว ตั้ง S/L พร้อมคำสั่งเปิดทุกครั้ง")
    if breakeven is not None:
        if win_rate < breakeven:
            note("bad", f"ชนะ {win_rate:.0f}% แต่ต้องชนะ {breakeven:.0f}% จึงเท่าทุน",
                 f"ไม้ชนะเฉลี่ยเก็บได้ {avg_win:,.2f} ไม้แพ้เฉลี่ยเสีย {avg_loss:,.2f} ต่อหน่วยราคา ต้องตัดขาดทุนให้เล็กลงหรือปล่อยกำไรให้ไกลขึ้น")
        elif avg_loss > 2 * avg_win:
            note("warn", f"ไม้แพ้ใหญ่กว่าไม้ชนะ {avg_loss / avg_win:.1f} เท่า",
                 f"ตอนนี้ยังบวกเพราะชนะ {win_rate:.0f}% (เท่าทุนที่ {breakeven:.0f}%) แต่แพ้ติดกันไม่กี่ไม้ก็ลบกำไรหลายสิบไม้ได้")
        else:
            note("good", f"ชนะ {win_rate:.0f}% สูงกว่าจุดเท่าทุน {breakeven:.0f}%",
                 f"ไม้ชนะเฉลี่ย {avg_win:,.2f} ไม้แพ้เฉลี่ย {avg_loss:,.2f} ต่อหน่วยราคา สัดส่วนนี้ยืนได้")
    if over:
        inside = out["in_plan"]
        note("bad" if out["over_hold_net"] < 0 else "warn",
             f"ถือเกินแผน {config.SCALP_MAX_HOLD_MIN} นาที {len(over)} จาก {len(rows)} ไม้",
             f"ไม้ที่ถือเกินแผนรวม {_money(out['over_hold_net'])} · ไม้ที่อยู่ในแผน {inside['n']} ไม้ รวม {_money(inside['net'])} "
             "ไม้ที่ถือนานกว่าแผนไม่ใช่สแกลป์แล้ว ควรมีเหตุผลและ S/L ของมันเอง")
    if hold_win and hold_loss and hold_loss > 1.5 * hold_win:
        note("bad", "ถือไม้ขาดทุนนานกว่าไม้กำไร",
             f"ไม้ชนะถือกลาง ๆ {hold_win / 60:,.0f} นาที ไม้แพ้ถือ {hold_loss / 60:,.0f} นาที คือรีบเก็บกำไรแต่ทนขาดทุน")
    if worst["net"] < 0 and out["net"] > 0 and -worst["net"] > 0.5 * sum(t["net"] for t in wins):
        note("warn", "ไม้แพ้ไม้เดียวกินกำไรไปเกินครึ่ง",
             f"ไม้ที่แย่ที่สุดเสีย {_money(worst['net'])} เทียบกับกำไรของไม้ชนะทั้งหมด {_money(sum(t['net'] for t in wins))}")
    for name, groups in (("ช่วงเวลา", out["by_session"]), ("ฝั่ง", out["by_side"])):
        bad = [g for g in groups if g["n"] >= 5 and g["net"] < 0]
        for g in bad[:1]:
            note("warn", f"{name} {g['key']} ขาดทุน", f"{g['n']} ไม้ ชนะ {g['win_rate']:.0f}% รวม {_money(g['net'])} ลองงดช่วงนี้แล้วดูผล")
    if not out["enough"]:
        note("info", f"ข้อมูลยังน้อย ({len(rows)} ไม้)",
             f"ต้องมีอย่างน้อย {MIN_FOR_VERDICT} ไม้ ตัวเลขด้านบนจึงจะน่าเชื่อ ตอนนี้ใช้เป็นแนวทางเท่านั้น")
    out["notes"] = notes
    return out


def watch(positions: list[dict], notify: bool = True) -> list[dict]:
    """Open positions that break the scalp plan. ``positions`` = the dashboard's rows."""
    if not config.SCALP_COACH_ENABLED:
        return []
    out: list[dict] = []
    live = {int(p["ticket"]) for p in positions}
    for key in [k for k in _alerted if k[0] not in live]:
        del _alerted[key]
    for p in positions:
        if p.get("ea"):
            continue
        held, side = p.get("held_s") or 0, "ซื้อ" if p["side"] == "buy" else "ขาย"
        label = f"{p['symbol']} {side} {p['volume']:g} ล็อต"
        found = []
        if not p.get("sl") and held >= config.SCALP_NO_SL_GRACE_S:
            found.append(("no_sl", f"⚠ {label} ยังไม่มี S/L (เปิดมา {held // 60} นาที) ขาดทุนไม่จำกัดจนกว่าจะตั้ง"))
        if held > config.SCALP_MAX_HOLD_MIN * 60:
            pl = p.get("profit") or 0.0
            found.append(("over_hold", f"⏱ {label} ถือมา {held // 60} นาที เกินแผน {config.SCALP_MAX_HOLD_MIN} นาที "
                                       f"(ตอนนี้ {pl:+,.2f}) ไม้นี้ไม่ใช่สแกลป์แล้ว ตัดสินใจว่าจะออกหรือถือต่อด้วยเหตุผลอะไร"))
        for kind, text in found:
            out.append({"ticket": p["ticket"], "symbol": p["symbol"], "kind": kind, "text": text})
            key = (int(p["ticket"]), kind)
            if notify and key not in _alerted:
                _alerted[key] = time.monotonic()
                _tell_owner(text)
    return out


def m5_signals(bars: list[dict], sl_atr: float = 2.0, tp_r: float = 1.0) -> tuple[list[dict], dict | None]:
    """Experimental scalp entries on 5-minute bars: fade a close outside the 2-sigma band in a flat market.

    The best of the M5 rule sets in backtest_gold_scalp.py, and still only about +0.03R a trade after
    costs, so it is drawn on the chart as a study and never offered as an order. Only completed bars
    count (the last bar of ``bars`` is still forming). Returns (chart markers, the latest entry).
    """
    import numpy as np
    import pandas as pd

    if len(bars) < 80:
        return [], None
    c = pd.Series([b["c"] for b in bars], dtype=float)
    h = pd.Series([b["h"] for b in bars], dtype=float)
    l = pd.Series([b["l"] for b in bars], dtype=float)
    prev = c.shift(1)
    atr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1).ewm(alpha=1 / 14, adjust=False).mean()
    mid, sd = c.rolling(20).mean(), c.rolling(20).std()
    e50 = c.ewm(span=50, adjust=False).mean()
    flat = (e50 - e50.shift(12)).abs() < 0.5 * atr
    hour = np.array([(b["t"] // 3600) % 24 for b in bars])
    session = (hour >= 7) & (hour < 20)                       # London to late New York, UTC
    side = np.where(flat & session & (c < mid - 2 * sd), 1, np.where(flat & session & (c > mid + 2 * sd), -1, 0))
    last_done = len(bars) - 2
    markers: list[dict] = []
    latest, last_i, last_side = None, -99, 0
    for i in range(60, last_done + 1):
        s = int(side[i])
        if s == 0 or (s == last_side and i - last_i <= 3):
            continue
        last_i, last_side = i, s
        markers.append({"t": bars[i]["t"], "kind": "scalp", "side": "buy" if s > 0 else "sell", "text": "S"})
        risk = sl_atr * float(atr.iloc[i])
        entry = float(c.iloc[i])
        latest = {"t": bars[i]["t"], "side": "buy" if s > 0 else "sell", "entry": entry, "sl": entry - s * risk,
                  "tp": entry + s * tp_r * risk, "age_bars": last_done - i, "recent": last_done - i <= 1}
    return markers, latest


def _tell_owner(text: str) -> None:
    try:
        from . import telegram_notify as tg

        tg.send_message("ผู้ช่วยสแกลป์\n" + text, admin_only=True)
    except Exception:  # noqa: BLE001 - a reminder must never stop the dashboard
        log.exception("could not send the scalp reminder")
