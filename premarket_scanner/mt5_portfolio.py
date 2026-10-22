"""Read-only view of the MetaTrader 5 account for Telegram.

* ``snapshot()``: balance, equity, floating P/L, open positions (with S/L, T/P, profit)
  and today's closed deals, straight from the terminal. Nothing is sent to the broker.
* ``check_changes()``: compares the open positions with the last check and produces
  "position opened" / "position closed" messages (close price, reason, realised profit).
  State: data/mt5_positions_state.json. The first run only records what is open.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, mt5_source, trade_fmt
from .trade_fmt import ENTRY, EXIT

log = logging.getLogger(__name__)

ET = ZoneInfo(config.MARKET_TZ)
BKK = ZoneInfo("Asia/Bangkok")
DEAL_REASON_TH = {
    0: "ปิดเองจากโปรแกรม", 1: "ปิดเองจากมือถือ", 2: "ปิดเองจากเว็บ", 3: "ปิดโดย EA",
    4: "Stop Loss", 5: "Take Profit", 6: "Stop Out (มาร์จิ้นไม่พอ)", 7: "Rollover",
    8: "Variation margin", 9: "Split",
}
DEAL_REASON_EN = {0: "manual (desktop)", 1: "manual (mobile)", 2: "manual (web)", 3: "expert advisor",
                  4: "stop loss", 5: "take profit", 6: "stop out", 7: "rollover", 8: "variation margin", 9: "split"}


def state_file() -> Path:
    return config.DATA_DIR / "mt5_positions_state.json"


def _load() -> dict:
    p = state_file()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    p = state_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _utc(server_epoch) -> datetime:
    return mt5_source._to_utc(server_epoch)


def _position_dict(p) -> dict:
    return {
        "ticket": int(p.ticket), "symbol": p.symbol, "type": "buy" if p.type == 0 else "sell",
        "volume": float(p.volume), "price_open": float(p.price_open), "sl": float(p.sl), "tp": float(p.tp),
        "price_current": float(p.price_current), "profit": float(p.profit), "swap": float(p.swap),
        "magic": int(p.magic), "comment": p.comment,
        "opened_at": _utc(p.time).isoformat(timespec="seconds"),
    }


def snapshot() -> dict | None:
    """Account + open positions + today's closed deals, or None when MT5 is unreachable."""
    if not mt5_source.connect():
        return None
    mt5 = mt5_source._mt5()
    acc = mt5.account_info()
    if acc is None:
        return None
    mt5_source.server_offset_hours()
    positions = [_position_dict(p) for p in (mt5.positions_get() or [])]
    now = datetime.now(timezone.utc)
    deals = mt5.history_deals_get(now - timedelta(hours=36), now + timedelta(hours=6)) or []
    offset = mt5_source._server_offset or 0.0
    today_server = datetime.fromtimestamp(now.timestamp() + offset, timezone.utc).date()
    closed_today = []
    for d in deals:
        if d.entry not in (1, 3):               # closing deals only
            continue
        if datetime.fromtimestamp(d.time, timezone.utc).date() != today_server:
            continue
        closed_today.append({
            "ticket": int(d.position_id), "symbol": d.symbol, "volume": float(d.volume), "price": float(d.price),
            "profit": float(d.profit) + float(d.commission) + float(d.swap), "reason": int(d.reason),
            "time": _utc(d.time).isoformat(timespec="seconds"),
        })
    return {
        "login": acc.login, "server": acc.server, "currency": acc.currency,
        "mode": {0: "demo", 1: "contest", 2: "real"}.get(acc.trade_mode, str(acc.trade_mode)),
        "balance": float(acc.balance), "equity": float(acc.equity), "margin_free": float(acc.margin_free),
        "floating": float(acc.profit), "positions": positions, "closed_today": closed_today,
        "time": datetime.now(ET).isoformat(timespec="seconds"),
    }


def _close_details(mt5, ticket: int) -> dict | None:
    deals = mt5.history_deals_get(position=ticket) or []
    out = [d for d in deals if d.entry in (1, 3)]
    if not out:
        return None
    d = out[-1]
    return {
        "price": float(d.price), "profit": float(d.profit) + float(d.commission) + float(d.swap),
        "reason": int(d.reason), "time": _utc(d.time), "volume": float(d.volume),
    }


# Formatting -----------------------------------------------------------------------
def _money(v: float, cur: str) -> str:
    sign = "+" if v > 0 else ""
    return f"{sign}{v:,.2f} {cur}"


def _price(v: float, symbol: str) -> str:
    from .special import fmt_price

    return fmt_price(v, "USD" if symbol.upper().endswith("USD") else "")


def _bkk(iso_or_dt) -> str:
    dt = datetime.fromisoformat(iso_or_dt) if isinstance(iso_or_dt, str) else iso_or_dt
    return dt.astimezone(BKK).strftime("%d/%m %H:%M")


def _signal_note(symbol: str, thai: bool) -> str:
    """Mention the bot's own open signal for this symbol, if there is one."""
    try:
        from . import special_watch
        rec = special_watch.load_state().get(symbol)
    except Exception:  # noqa: BLE001
        rec = None
    if rec and rec.get("status") in special_watch.TRACKING:
        t = datetime.fromisoformat(rec["triggered_at"]).astimezone(BKK).strftime("%d/%m %H:%M")
        return f" (ตรงกับสัญญาณบอทเมื่อ {t} น. ไทย)" if thai else f" (matches the bot signal of {t} Bangkok)"
    return ""


def format_section(snap: dict | None, thai: bool) -> list[str]:
    head = "[พอร์ตเดโม MT5]" if thai else "[MT5 account]"
    if snap is None:
        return [head, "MT5 ไม่ได้เปิดอยู่ จึงอ่านพอร์ตไม่ได้" if thai else "MT5 terminal not reachable."]
    cur = snap["currency"]
    mode = {"demo": "เดโม", "real": "บัญชีจริง", "contest": "แข่งขัน"}.get(snap["mode"], snap["mode"]) if thai else snap["mode"]
    lines = [head]
    if thai:
        lines.append(f"บัญชี {snap['login']} ({snap['server']}, {mode}) ยอดเงิน {snap['balance']:,.2f} {cur} | "
                     f"อิควิตี้ {snap['equity']:,.2f} {cur} | กำไรขาดทุนลอยตัว {_money(snap['floating'], cur)}")
    else:
        lines.append(f"Account {snap['login']} ({snap['server']}, {mode}) balance {snap['balance']:,.2f} {cur} | "
                     f"equity {snap['equity']:,.2f} {cur} | floating {_money(snap['floating'], cur)}")
    positions = snap["positions"]
    if not positions:
        lines.append("ไม่มีไม้ที่เปิดอยู่" if thai else "No open positions.")
    else:
        lines.append(f"ไม้ที่เปิดอยู่ {len(positions)} ไม้:" if thai else f"Open positions ({len(positions)}):")
        for i, p in enumerate(positions, 1):
            side = trade_fmt.badge(p["type"])
            sl_arrow, tp_arrow = trade_fmt.stop_arrow(p["type"]), trade_fmt.tp_arrow(p["type"])
            sl = _price(p["sl"], p["symbol"]) if p["sl"] else ("ไม่ได้ตั้ง!" if thai else "none!")
            tp = _price(p["tp"], p["symbol"]) if p["tp"] else ("ไม่ได้ตั้ง" if thai else "none")
            if thai:
                lines.append(f"{i}. {p['symbol']} {side} {p['volume']:g} ล็อต {ENTRY} เข้า {_price(p['price_open'], p['symbol'])} "
                             f"ตอนนี้ {_price(p['price_current'], p['symbol'])} | {sl_arrow} S/L {sl} | {tp_arrow} T/P {tp} | "
                             f"กำไร {_money(p['profit'], cur)} (เปิด {_bkk(p['opened_at'])} น. ไทย){_signal_note(p['symbol'], True)}")
            else:
                lines.append(f"{i}. {p['symbol']} {side} {p['volume']:g} lots {ENTRY} @ {_price(p['price_open'], p['symbol'])} "
                             f"now {_price(p['price_current'], p['symbol'])} | {sl_arrow} SL {sl} | {tp_arrow} TP {tp} | "
                             f"P/L {_money(p['profit'], cur)} (opened {_bkk(p['opened_at'])}){_signal_note(p['symbol'], False)}")
    closed = snap["closed_today"]
    total = sum(c["profit"] for c in closed)
    if thai:
        lines.append(f"ปิดวันนี้ {len(closed)} ไม้ รวม {_money(total, cur)}" if closed else "ปิดวันนี้: ยังไม่มี")
    else:
        lines.append(f"Closed today: {len(closed)} trade(s), {_money(total, cur)}" if closed else "Closed today: none")
    return lines


def fmt_opened(p: dict, cur: str, thai: bool) -> str:
    side = trade_fmt.badge(p["type"])
    sl_arrow, tp_arrow = trade_fmt.stop_arrow(p["type"]), trade_fmt.tp_arrow(p["type"])
    sl = _price(p["sl"], p["symbol"]) if p["sl"] else None
    tp = _price(p["tp"], p["symbol"]) if p["tp"] else None
    if thai:
        text = (f"📗 {ENTRY} เปิดไม้ใหม่ใน MT5: {p['symbol']} {side} {p['volume']:g} ล็อต เข้า {_price(p['price_open'], p['symbol'])}"
                f" | {sl_arrow} S/L {sl or 'ยังไม่ได้ตั้ง ⚠️ ควรตั้งทันที'} | {tp_arrow} T/P {tp or 'ยังไม่ได้ตั้ง'}"
                f"{_signal_note(p['symbol'], True)}")
    else:
        text = (f"OPENED {ENTRY} in MT5: {p['symbol']} {side} {p['volume']:g} lots @ {_price(p['price_open'], p['symbol'])}"
                f" | {sl_arrow} SL {sl or 'NONE - set one'} | {tp_arrow} TP {tp or 'none'}{_signal_note(p['symbol'], False)}")
    return text


def fmt_closed(p: dict, close: dict | None, cur: str, balance: float | None, thai: bool) -> str:
    side = trade_fmt.badge(p["type"])
    if close is None:
        return (f"📕 {EXIT} ไม้ {p['symbol']} {side} {p['volume']:g} ล็อต (เข้า {_price(p['price_open'], p['symbol'])}) ปิดแล้ว "
                f"แต่อ่านรายละเอียดการปิดไม่ได้" if thai else
                f"CLOSED {EXIT} {p['symbol']} {side} {p['volume']:g} lots (entry {_price(p['price_open'], p['symbol'])}); details unavailable")
    held = close["time"] - datetime.fromisoformat(p["opened_at"])
    hours, minutes = divmod(int(held.total_seconds() // 60), 60)
    reason = (DEAL_REASON_TH if thai else DEAL_REASON_EN).get(close["reason"], str(close["reason"]))
    pnl = _money(close["profit"], cur)
    if thai:
        verdict = "กำไร" if close["profit"] > 0 else ("ขาดทุน" if close["profit"] < 0 else "เท่าทุน")
        text = (f"📕 {EXIT} ไม้ปิดแล้ว: {p['symbol']} {side} {p['volume']:g} ล็อต {ENTRY} เข้า {_price(p['price_open'], p['symbol'])} "
                f"{EXIT} ปิด {_price(close['price'], p['symbol'])} โดย {reason} {verdict} {pnl} (ถือ {hours} ชม. {minutes} นาที)")
        if balance is not None:
            text += f"\nยอดเงินตอนนี้ {balance:,.2f} {cur}"
    else:
        text = (f"CLOSED {EXIT} {p['symbol']} {side} {p['volume']:g} lots: {ENTRY} entry {_price(p['price_open'], p['symbol'])} "
                f"{EXIT} exit {_price(close['price'], p['symbol'])} by {reason}, {pnl} (held {hours}h {minutes}m)")
        if balance is not None:
            text += f"\nBalance now {balance:,.2f} {cur}"
    return text


# Change detection ----------------------------------------------------------------
def check_changes(send: bool = True, thai: bool | None = None) -> list[str]:
    """Detect positions opened or closed since the last check; return (and send) messages."""
    from . import telegram_notify as tg

    thai = tg.is_thai() if thai is None else thai
    snap = snapshot()
    if snap is None:
        return []
    state = _load()
    known: dict[str, dict] = state.get("known") or {}
    current = {str(p["ticket"]): p for p in snap["positions"]}
    messages: list[str] = []
    cur = snap["currency"]

    if "known" not in state:                      # first run: just remember what is open
        state = {"known": current, "updated_at": snap["time"]}
        _save(state)
        log.info("portfolio watch initialised with %d open position(s)", len(current))
        return []

    mt5 = mt5_source._mt5()
    for ticket, p in current.items():
        if ticket not in known:
            messages.append(fmt_opened(p, cur, thai))
    for ticket, p in known.items():
        if ticket not in current:
            close = _close_details(mt5, int(ticket))
            messages.append(fmt_closed(p, close, cur, snap["balance"], thai))
    state = {"known": current, "updated_at": snap["time"]}
    _save(state)

    if send:
        for m in messages:
            tg.send_message(m, admin_only=not config.PORTFOLIO_TO_GROUP)
    return messages
