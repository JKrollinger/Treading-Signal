"""Risk gate: checks that can only block an order request, never create one.

Two layers, both ending in ``ea_bridge.set_halt`` or a refusal text:

* ``tick``     - account limits, checked continuously by the dashboard and again before every
                 request: loss of the day and drawdown from the equity high-water mark. A breach
                 switches the stop on by itself and tells the owner on Telegram.
* ``pretrade`` - the state of the moment a request is about to be written: stop switch, limits,
                 terminal and EA health, spread and free margin.

The day is the Bangkok calendar day, like the dashboard's "today". A daily-loss stop fires once
per day: if the owner switches trading back on, it stays on until tomorrow. A drawdown stop needs
that manual switch too, and measuring then restarts from the equity of that moment.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, ea_bridge

log = logging.getLogger(__name__)

BKK = ZoneInfo("Asia/Bangkok")
EA_ONLINE_S = 60
SAVE_EVERY_S = 30               # a rising high-water mark is written at most this often
_lock = threading.Lock()
_last_save = 0.0


def state_file() -> Path:
    return config.DATA_DIR / "risk_state.json"


def _load() -> dict:
    try:
        data = json.loads(state_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    global _last_save
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    _last_save = time.monotonic()


def _peak_balance(doc: dict | None) -> float:
    """Highest closed balance in the journal: where the high-water mark starts."""
    if not doc:
        return 0.0
    cum = peak = float(doc.get("deposits") or 0.0)
    for t in sorted(doc["trades"].values(), key=lambda t: t["exit_time"]):
        cum += t["net"]
        peak = max(peak, cum)
    return peak


def tick(login: int, balance: float, equity: float, closed_today: float, doc: dict | None = None,
         folder: Path | None = None, notify: bool = True) -> dict:
    """Measure the account against its limits; switch the stop on at a breach. Returns the reading."""
    if not config.RISK_GATE_ENABLED:
        return {"enabled": False}
    today = datetime.now(BKK).date().isoformat()
    tripped: tuple[str, str] | None = None
    with _lock:
        state = _load()
        acct = state.setdefault(str(login), {})
        known = float(acct.get("hwm") or 0.0) or _peak_balance(doc)
        hwm = max(known, equity)
        day_start = balance - closed_today
        daily = max(0.0, day_start - equity) / day_start * 100 if day_start > 0 else 0.0
        drawdown = (hwm - equity) / hwm * 100 if hwm > 0 else 0.0
        rose = hwm != acct.get("hwm")
        acct["hwm"] = hwm
        if config.RISK_DAILY_LOSS_PCT > 0 and daily >= config.RISK_DAILY_LOSS_PCT and acct.get("daily_tripped") != today:
            acct["daily_tripped"] = today
            tripped = ("daily_loss", f"ขาดทุนวันนี้ {daily:.2f}% ถึงเพดาน {config.RISK_DAILY_LOSS_PCT:g}%")
        elif config.RISK_MAX_DRAWDOWN_PCT > 0 and drawdown >= config.RISK_MAX_DRAWDOWN_PCT and not acct.get("dd_tripped"):
            acct["dd_tripped"] = True
            tripped = ("drawdown", f"พอร์ตถอยจากจุดสูงสุด {drawdown:.2f}% ถึงเพดาน {config.RISK_MAX_DRAWDOWN_PCT:g}%")
        if tripped or (rose and time.monotonic() - _last_save > SAVE_EVERY_S):
            _save(state)
        reading = {
            "enabled": True, "daily_loss_pct": daily, "daily_limit_pct": config.RISK_DAILY_LOSS_PCT,
            "drawdown_pct": drawdown, "drawdown_limit_pct": config.RISK_MAX_DRAWDOWN_PCT, "hwm": hwm,
            "day_start": day_start, "daily_tripped": acct.get("daily_tripped") == today, "dd_tripped": bool(acct.get("dd_tripped")),
            "spread_limit_pct": config.RISK_MAX_SPREAD_PCT,
        }
    if tripped:
        kind, reason = tripped
        if not ea_bridge.halt_state().get("halted"):
            ea_bridge.set_halt(True, f"risk:{kind}", reason=reason, folder=folder)
            if notify:
                _tell_owner(reason)
    return reading


def _tell_owner(reason: str) -> None:
    try:
        from . import telegram_notify
        telegram_notify.send_message(
            f"⛔ ด่านตรวจความเสี่ยงหยุดส่งคำสั่งใหม่แล้ว\nสาเหตุ: {reason}\n"
            "ไม้ที่เปิดอยู่ไม่ถูกปิด · เปิดส่งคำสั่งอีกครั้งได้ที่แดชบอร์ด หน้า บริหารความเสี่ยง", admin_only=True)
    except Exception:  # noqa: BLE001 - the stop itself is already on
        log.exception("could not report the risk stop to Telegram")


def on_resume(login: int | None, equity: float | None) -> None:
    """The owner switched trading back on: a drawdown stop starts measuring again from here."""
    if login is None:
        return
    with _lock:
        state = _load()
        acct = state.get(str(login))
        if acct and acct.get("dd_tripped"):
            acct["dd_tripped"] = False
            if equity:
                acct["hwm"] = float(equity)
            _save(state)


def pretrade(symbol: str, lots: float, stop_distance: float) -> str | None:
    """Why this request must not be written right now (Thai), or None when everything is in order."""
    if halted := ea_bridge.halt_message_th():
        return halted
    if not config.RISK_GATE_ENABLED:
        return None
    from . import mt5_source, trade_journal

    if not mt5_source.connect():
        return "⛔ ไม่ได้ส่งรายการ: MT5 ปิดอยู่หรือยังไม่พร้อม"
    mt5 = mt5_source._mt5()
    term, acc, info = mt5.terminal_info(), mt5.account_info(), mt5.symbol_info(symbol)
    if term is None or acc is None or info is None:
        return "⛔ ไม่ได้ส่งรายการ: อ่านสถานะ MT5 ไม่ได้"
    if not term.connected:
        return "⛔ ไม่ได้ส่งรายการ: MT5 ยังไม่ต่อกับโบรกเกอร์"
    if not term.trade_allowed:
        return "⛔ ไม่ได้ส่งรายการ: ปุ่ม Algo Trading ใน MT5 ปิดอยู่ EA จะส่งคำสั่งไม่ได้"
    age = ea_bridge.heartbeat_age_seconds()
    if age is None or age > EA_ONLINE_S:
        return "⛔ ไม่ได้ส่งรายการ: EA ไม่ตอบสนอง (ลาก SignalBotBridge ลงกราฟใน MT5 ก่อน)"
    try:
        doc = trade_journal.load(int(acc.login))
        closed_today = trade_journal.closed_totals(doc)["today"]
    except (OSError, ValueError, KeyError):
        doc, closed_today = None, 0.0
    tick(int(acc.login), float(acc.balance), float(acc.equity), closed_today, doc=doc)
    if halted := ea_bridge.halt_message_th():
        return halted
    spread = float(info.spread or 0) * float(info.point or 0)
    if config.RISK_MAX_SPREAD_PCT > 0 and stop_distance > 0 and spread > stop_distance * config.RISK_MAX_SPREAD_PCT / 100:
        return (f"⛔ ไม่ได้ส่งรายการ: สเปรดกว้างผิดปกติ ({spread:g} = {spread / stop_distance * 100:.1f}% ของระยะ S/L "
                f"เกินเพดาน {config.RISK_MAX_SPREAD_PCT:g}%) ลองกดยืนยันใหม่เมื่อสเปรดกลับมาปกติ")
    tick_now = mt5.symbol_info_tick(symbol)
    if tick_now is not None and tick_now.ask:
        margin = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, symbol, float(lots), float(tick_now.ask))   # a calculation, not an order
        if margin is not None and margin > float(acc.margin_free):
            return f"⛔ ไม่ได้ส่งรายการ: มาร์จิ้นไม่พอ (ต้องใช้ {margin:,.2f} มีว่าง {acc.margin_free:,.2f} {acc.currency})"
    return None
