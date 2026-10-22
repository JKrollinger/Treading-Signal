"""File bridge between the bot and an Expert Advisor (EA) running in MetaTrader 5.

The bot never sends orders. When the owner presses "confirm" on a signal, the bot
appends an *order request* to ``<MT5 Common>\\Files\\signalbot\\orders.csv``; the EA
(``mt5_ea/SignalBotBridge.mq5``) reads it, validates it and, once you have added the
order line to the EA, places the trade. The EA writes its outcome to ``results.csv``
and a timestamp to ``heartbeat.txt``; the bot reports both back to Telegram.

orders.csv  : id,created_utc,symbol,side,lots,sl,tp,entry_ref,comment
results.csv : id,processed_utc,status,ticket,price,lots,message
created_utc uses MQL5's StringToTime format: YYYY.MM.DD HH:MM:SS (UTC).
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config, mt5_source, trade_fmt

log = logging.getLogger(__name__)

SUBDIR = "signalbot"
ORDER_FIELDS = ["id", "created_utc", "symbol", "side", "lots", "sl", "tp", "entry_ref", "comment"]
RESULT_FIELDS = ["id", "processed_utc", "status", "ticket", "price", "lots", "message"]


# Paths ------------------------------------------------------------------------------
def files_dir() -> Path | None:
    if config.EA_FILES_DIR:
        return Path(config.EA_FILES_DIR)
    try:
        if mt5_source.connect():
            info = mt5_source._mt5().terminal_info()
            if info is not None and info.commondata_path:
                return Path(info.commondata_path) / "Files" / SUBDIR
    except Exception as exc:  # noqa: BLE001
        log.debug("terminal_info unavailable: %s", exc)
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "MetaQuotes" / "Terminal" / "Common" / "Files" / SUBDIR if appdata else None


def orders_file() -> Path | None:
    d = files_dir()
    return d / "orders.csv" if d else None


def results_file() -> Path | None:
    d = files_dir()
    return d / "results.csv" if d else None


def heartbeat_file() -> Path | None:
    d = files_dir()
    return d / "heartbeat.txt" if d else None


def state_file() -> Path:
    return config.DATA_DIR / "ea_bridge_state.json"


# Sizing (read-only) --------------------------------------------------------------------
def compute_lots(symbol: str, entry: float, stop: float, risk_pct: float | None = None) -> dict | None:
    """Lot size so that the distance entry->stop risks ``risk_pct`` of the balance."""
    if not mt5_source.connect():
        return None
    mt5 = mt5_source._mt5()
    if not mt5.symbol_select(symbol, True):
        return None
    info = mt5.symbol_info(symbol)
    acc = mt5.account_info()
    if info is None or acc is None:
        return None
    tick_size = float(info.trade_tick_size or info.point)
    tick_value = float(info.trade_tick_value or 0.0)
    risk = float(entry) - float(stop)
    if risk <= 0 or tick_size <= 0 or tick_value <= 0:
        return None
    risk_per_lot = risk / tick_size * tick_value
    pct = config.EA_RISK_PCT if risk_pct is None else risk_pct
    risk_money = float(acc.balance) * pct / 100.0
    step = float(info.volume_step or 0.01)
    lots = math.floor(risk_money / risk_per_lot / step + 1e-9) * step
    notes: list[str] = []
    if lots < float(info.volume_min):
        lots = float(info.volume_min)
        notes.append(f"ขั้นต่ำ {lots:g} ล็อต ความเสี่ยงจริง {lots * risk_per_lot:,.2f} {acc.currency}")
    if lots > float(info.volume_max):
        lots = float(info.volume_max)
        notes.append(f"เกินสูงสุด ใช้ {lots:g} ล็อต")
    lots = round(lots, 8)
    return {
        "symbol": symbol, "lots": lots, "risk_money": lots * risk_per_lot, "risk_per_lot": risk_per_lot,
        "balance": float(acc.balance), "currency": str(acc.currency), "digits": int(info.digits),
        "stops_level_points": int(info.trade_stops_level or 0), "point": float(info.point),
        "volume_min": float(info.volume_min), "volume_step": step, "notes": notes,
    }


# Requests ---------------------------------------------------------------------------
def write_order_request(symbol: str, side: str, lots: float, sl: float, tp: float, entry_ref: float,
                        comment: str = "signalbot") -> dict:
    path = orders_file()
    if path is None:
        raise RuntimeError("MT5 Common Files folder unknown; set EA_FILES_DIR")
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "id": uuid.uuid4().hex[:12],
        "created_utc": datetime.now(timezone.utc).strftime("%Y.%m.%d %H:%M:%S"),
        "symbol": symbol.upper(), "side": side.upper(), "lots": f"{lots:g}",
        "sl": f"{sl:.5f}", "tp": f"{tp:.5f}" if tp else "0",
        "entry_ref": f"{entry_ref:.5f}", "comment": comment.replace(",", " ")[:30],
    }
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="ascii", errors="replace") as fh:
        w = csv.DictWriter(fh, fieldnames=ORDER_FIELDS, lineterminator="\n")
        if new:
            w.writeheader()
        w.writerow(row)
    log.info("order request written: %s", row)
    return row


def list_requests() -> list[dict]:
    path = orders_file()
    if path is None or not path.exists():
        return []
    with path.open("r", encoding="ascii", errors="replace", newline="") as fh:
        return [r for r in csv.DictReader(fh) if r.get("id")]


def list_results() -> list[dict]:
    path = results_file()
    if path is None or not path.exists():
        return []
    out = []
    with path.open("r", encoding="ascii", errors="replace", newline="") as fh:
        for line in fh:
            parts = line.rstrip("\r\n").split(",")
            if len(parts) < 7 or parts[0] == "id":
                continue
            out.append(dict(zip(RESULT_FIELDS, parts[:7])))
    return out


def pending_requests() -> list[dict]:
    done = {r["id"] for r in list_results()}
    return [r for r in list_requests() if r["id"] not in done]


def read_new_results() -> list[dict]:
    """Results not yet reported to Telegram (tracked in data/ea_bridge_state.json)."""
    state = {}
    try:
        state = json.loads(state_file().read_text(encoding="utf-8")) if state_file().exists() else {}
    except (OSError, ValueError):
        state = {}
    seen = set(state.get("reported") or [])
    fresh = [r for r in list_results() if r["id"] not in seen]
    if fresh:
        state["reported"] = sorted(seen | {r["id"] for r in fresh})[-500:]
        state_file().parent.mkdir(parents=True, exist_ok=True)
        state_file().write_text(json.dumps(state, indent=2), encoding="utf-8")
    return fresh


def heartbeat_age_seconds() -> float | None:
    path = heartbeat_file()
    if path is None or not path.exists():
        return None
    return max(0.0, datetime.now(timezone.utc).timestamp() - path.stat().st_mtime)


def ea_status_th() -> str:
    age = heartbeat_age_seconds()
    if age is None:
        return "EA ยังไม่เคยทำงาน (ไม่พบ heartbeat.txt)"
    if age > 60:
        return f"EA ไม่ตอบสนองมา {int(age // 60)} นาที"
    return "EA ทำงานอยู่"


# Text -------------------------------------------------------------------------------
def fmt_request_th(row: dict, sizing: dict | None, name: str | None = None) -> str:
    label = f"{name} ({row['symbol']})" if name else row["symbol"]
    tp_val = float(row["tp"] or 0)
    tp_txt = f"{tp_val:g}" if tp_val else "ไม่ตั้ง"
    side = row["side"]
    lines = [
        f"📝 ส่งรายการสั่งซื้อให้ EA แล้ว: {label} {trade_fmt.badge(side)} {row['lots']} ล็อต",
        f"{trade_fmt.ENTRY} ราคาอ้างอิง {float(row['entry_ref']):g} | {trade_fmt.stop_arrow(side)} S/L {float(row['sl']):g} | "
        f"{trade_fmt.tp_arrow(side)} T/P {tp_txt}",
    ]
    if sizing:
        lines.append(f"ความเสี่ยงประมาณ {sizing['risk_money']:,.2f} {sizing['currency']} "
                     f"({sizing['risk_money'] / sizing['balance'] * 100:.2f}% ของเงิน {sizing['balance']:,.0f} {sizing['currency']})")
        for n in sizing.get("notes") or []:
            lines.append(f"หมายเหตุ: {n}")
    lines.append(f"สถานะ EA: {ea_status_th()} | รหัสรายการ {row['id']}")
    return "\n".join(lines)


def fmt_result_th(r: dict) -> str:
    status = r.get("status", "")
    icon = {"FILLED": "✅", "DRYRUN": "🧪", "REJECTED": "⛔", "FAILED": "⛔", "EXPIRED": "⏱"}.get(status, "ℹ️")
    label = {"FILLED": "EA เปิดไม้แล้ว", "DRYRUN": "EA ตรวจรายการผ่าน (โหมดทดสอบ ยังไม่ส่งคำสั่ง)",
             "REJECTED": "EA ปฏิเสธรายการ", "FAILED": "EA ส่งคำสั่งไม่สำเร็จ", "EXPIRED": "รายการหมดอายุก่อน EA รับ"}.get(status, status)
    bits = [f"{icon} {label}: รหัส {r.get('id')}"]
    if r.get("ticket") and r.get("ticket") != "0":
        bits.append(f"ticket {r['ticket']}")
    if r.get("price") and r.get("price") != "0":
        bits.append(f"ราคา {r['price']}")
    if r.get("lots"):
        bits.append(f"{r['lots']} ล็อต")
    if r.get("message"):
        bits.append(r["message"])
    return " | ".join(bits)
