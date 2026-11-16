"""Has the demo run earned a look at a real account? A checklist, measured from the records.

The numbers come from what the bot already keeps: the closed Trend-join signals (their result in
R, after the same exit rules the signals announce), the journal of the MT5 account, and the risk
gate. Nothing here trades, switches accounts or changes a setting: it reports "collecting",
"review" (the results are bad enough to stop and look) or "passed". Passing is a reason to
consider the step, not an instruction to take it.

The limits sit below the two-year backtest of the live rules (average +0.17R, profit factor
1.40, deepest drawdown 8.6R over 107 trades) and above break-even, so an ordinary unlucky run
still passes while a strategy that stopped working does not.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

from . import config
from . import trade_journal as journal

UTC = timezone.utc
TREND_JOIN = "สัญญาณ Trend-join"
REVIEW_MIN_TRADES = 15          # enough closed signals to call a bad run a bad run
REVIEW_AVG_R = -0.10
_cache: dict = {"at": 0.0, "signals": []}


def _signals() -> list[dict]:
    if time.monotonic() - _cache["at"] > 30:
        _cache.update(at=time.monotonic(), signals=journal.bot_signals())
    return _cache["signals"]


def _num(v: float, digits: int = 2, sign: bool = False) -> str:
    return f"{v:+.{digits}f}" if sign else f"{v:.{digits}f}"


def evaluate(doc: dict | None, risk: dict | None, now: datetime | None = None) -> dict:
    """The checklist. ``ok`` of a check: True = met, False = not met, None = cannot be judged yet."""
    now = now or datetime.now(UTC)
    rows = [s for s in _signals() if s.get("kind") == TREND_JOIN and s.get("entry_time")]
    closed = sorted((s for s in rows if not s.get("open") and s.get("r") is not None),
                    key=lambda s: s.get("exit_time") or s["entry_time"])
    rs = [float(s["r"]) for s in closed]
    n = len(rs)
    first = min((s["entry_time"] for s in rows), default=None)
    weeks = (now - datetime.fromisoformat(first)).days / 7 if first else 0.0

    avg = sum(rs) / n if n else None
    wins, losses = sum(r for r in rs if r > 0), -sum(r for r in rs if r <= 0)
    pf = wins / losses if losses > 0 else (math.inf if wins > 0 else None)
    cum = peak = dd = 0.0
    streak = worst = 0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    spread = None
    if n >= 5:
        sd = math.sqrt(sum((r - avg) ** 2 for r in rs) / (n - 1))
        spread = 1.645 * sd / math.sqrt(n)              # half of a 90% interval for the average

    trades = list((doc or {}).get("trades", {}).values())
    ea = [t for t in trades if t.get("origin") == "ea_signal"]
    since = (now - timedelta(days=30)).isoformat()
    bare = [t for t in trades if t.get("origin") != "ea_test" and "no_sl" in (t.get("tags") or [])
            and (t.get("exit_time") or "") >= since]
    dd_pct = (risk or {}).get("drawdown_pct")
    dd_cap = config.RISK_MAX_DRAWDOWN_PCT / 2 if config.RISK_MAX_DRAWDOWN_PCT > 0 else None

    enough = n >= config.GO_LIVE_MIN_TRADES
    c = config
    checks = [
        {"key": "sample", "label": "จำนวนสัญญาณที่ปิดแล้ว", "value": f"{n} สัญญาณ", "target": f"อย่างน้อย {c.GO_LIVE_MIN_TRADES}",
         "ok": enough, "progress": min(1.0, n / c.GO_LIVE_MIN_TRADES) if c.GO_LIVE_MIN_TRADES else 1.0,
         "why": "ต่ำกว่านี้ผลยังเป็นเรื่องของดวงมากกว่าระบบ"},
        {"key": "weeks", "label": "ระยะเวลาที่เดินระบบ", "value": f"{weeks:.1f} สัปดาห์", "target": f"อย่างน้อย {c.GO_LIVE_MIN_WEEKS}",
         "ok": weeks >= c.GO_LIVE_MIN_WEEKS, "progress": min(1.0, weeks / c.GO_LIVE_MIN_WEEKS) if c.GO_LIVE_MIN_WEEKS else 1.0,
         "why": "ต้องผ่านทั้งช่วงตลาดมีแนวโน้มและช่วงแกว่งตัว"},
        {"key": "avg_r", "label": "กำไรเฉลี่ยต่อสัญญาณ",
         "value": "-" if avg is None else _num(avg, 2, True) + "R" + (f" (ช่วง 90%: {_num(avg - spread, 2, True)} ถึง {_num(avg + spread, 2, True)})" if spread is not None else ""),
         "target": f"อย่างน้อย {_num(c.GO_LIVE_MIN_AVG_R, 2, True)}R", "ok": (avg >= c.GO_LIVE_MIN_AVG_R) if enough else None,
         "why": "ทดสอบย้อนหลังได้ +0.17R ต้องเหลือพอจ่ายสเปรดและสลิปเพจจริง"},
        {"key": "pf", "label": "Profit factor", "value": "-" if pf is None else "∞" if pf == math.inf else _num(pf),
         "target": f"อย่างน้อย {_num(c.GO_LIVE_MIN_PF)}", "ok": (pf is not None and pf >= c.GO_LIVE_MIN_PF) if enough else None,
         "why": "กำไรรวมต้องมากกว่าขาดทุนรวมอย่างมีระยะ ทดสอบย้อนหลังได้ 1.40"},
        {"key": "dd", "label": "ขาดทุนสะสมลึกสุด", "value": f"{_num(dd, 1)}R", "target": f"ไม่เกิน {_num(c.GO_LIVE_MAX_DD_R, 0)}R",
         "ok": (dd <= c.GO_LIVE_MAX_DD_R) if (enough or dd > c.GO_LIVE_MAX_DD_R) else None,
         "why": "ทดสอบย้อนหลังลึกสุด 8.6R ถ้าลึกกว่านี้แปลว่าตลาดไม่เหมือนช่วงที่ทดสอบ"},
        {"key": "streak", "label": "แพ้ติดกันมากที่สุด", "value": f"{worst} ครั้ง", "target": f"ไม่เกิน {c.GO_LIVE_MAX_LOSS_STREAK}",
         "ok": (worst <= c.GO_LIVE_MAX_LOSS_STREAK) if (enough or worst > c.GO_LIVE_MAX_LOSS_STREAK) else None,
         "why": "ระบบที่ชนะราวครึ่งหนึ่งแพ้ติดกัน 6-7 ครั้งได้ตามปกติ มากกว่านั้นผิดปกติ"},
        {"key": "ea", "label": "ไม้ที่ส่งผ่าน EA จากสัญญาณ", "value": f"{len(ea)} ไม้", "target": f"อย่างน้อย {c.GO_LIVE_MIN_EA_TRADES}",
         "ok": len(ea) >= c.GO_LIVE_MIN_EA_TRADES, "progress": min(1.0, len(ea) / c.GO_LIVE_MIN_EA_TRADES) if c.GO_LIVE_MIN_EA_TRADES else 1.0,
         "why": "พิสูจน์ว่าสายส่งคำสั่ง (ยืนยัน → EA → โบรกเกอร์ → S/L) ทำงานครบทุกครั้ง"},
        {"key": "no_sl", "label": "ไม้ที่ไม่ตั้ง S/L ใน 30 วันล่าสุด", "value": f"{len(bare)} ไม้", "target": "0",
         "ok": (len(bare) == 0) if doc else None,
         "why": "ไม้เดียวที่ไม่มี S/L ลบกำไรของทั้งระบบได้ รวมไม้ที่เปิดเองด้วย"},
        {"key": "account_dd", "label": "พอร์ตถอยจากจุดสูงสุดตอนนี้", "value": "-" if dd_pct is None else f"{_num(dd_pct)}%",
         "target": "-" if dd_cap is None else f"ต่ำกว่า {_num(dd_cap, 1)}%", "ok": None if dd_pct is None or dd_cap is None else dd_pct < dd_cap,
         "why": "ไม่ควรเริ่มเงินจริงขณะที่พอร์ตทดสอบยังอยู่ในช่วงขาดทุน"},
    ]

    met = sum(1 for k in checks if k["ok"] is True)
    review = None
    if dd > c.GO_LIVE_MAX_DD_R:
        review = f"ขาดทุนสะสม {_num(dd, 1)}R เกินเพดาน {_num(c.GO_LIVE_MAX_DD_R, 0)}R"
    elif worst > c.GO_LIVE_MAX_LOSS_STREAK:
        review = f"แพ้ติดกัน {worst} ครั้ง เกินเพดาน {c.GO_LIVE_MAX_LOSS_STREAK}"
    elif n >= REVIEW_MIN_TRADES and avg is not None and avg < REVIEW_AVG_R:
        review = f"เฉลี่ย {_num(avg, 2, True)}R หลัง {n} สัญญาณ ต่ำกว่า {_num(REVIEW_AVG_R, 2, True)}R"
    if review:
        status, text = "review", f"ควรหยุดและทบทวนกฎก่อนเดินต่อ: {review}"
    elif met == len(checks):
        status, text = "passed", "ผ่านทุกข้อ ระบบพร้อมให้คุณพิจารณาขั้นต่อไป การตัดสินใจเป็นของคุณ"
    else:
        left = max(0, c.GO_LIVE_MIN_TRADES - n)
        status = "collecting"
        text = f"ยังเก็บข้อมูลอยู่ ผ่าน {met} จาก {len(checks)} ข้อ" + (f" · ต้องการสัญญาณที่ปิดแล้วอีก {left}" if left else "")
    mode = ((doc or {}).get("account") or {}).get("mode")
    return {"status": status, "text": text, "met": met, "total": len(checks), "checks": checks, "closed": n,
            "open": sum(1 for s in rows if s.get("open")), "total_r": sum(rs) if rs else None,
            "first_signal": first, "account_mode": mode, "strategy": "Trend-join Long"}
