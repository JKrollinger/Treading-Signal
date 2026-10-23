"""Telegram buttons and commands (owner only), via long polling.

* "✅ ยืนยันเข้าเทรด" on a signal -> order request written for the EA (see ea_bridge)
* "🧪 ทดสอบ EA"            -> a fixed minimum-lot BUY that tests the bot -> EA pipeline (not a signal;
                              a real demo position once the EA's InpDryRun is false); the reply lists
                              the signals actually open, with their side, so the two are not confused
* /status                   -> account section      /orders -> requests, results, EA heartbeat
* EA results (results.csv) are reported as they appear.

Only the owner (TELEGRAM_CHAT_ID) can trigger anything; presses from other users are
answered with a refusal. The getUpdates offset is persisted so a restart never replays
old button presses, and updates older than 10 minutes are ignored.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

from . import config, ea_bridge, trade_fmt
from . import telegram_notify as tg

log = logging.getLogger(__name__)

STALE_UPDATE_SECONDS = 600
RESULTS_POLL_SECONDS = 10
FAST_POLL_TIMEOUT = 3            # seconds per poll/result check right after a button press
FAST_POLL_WINDOW_SECONDS = 90


def offset_file():
    return config.DATA_DIR / "telegram_offset.json"


def _load_offset() -> int | None:
    try:
        return int(json.loads(offset_file().read_text(encoding="utf-8"))["offset"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_offset(offset: int) -> None:
    offset_file().parent.mkdir(parents=True, exist_ok=True)
    offset_file().write_text(json.dumps({"offset": offset}), encoding="utf-8")


def signal_keyboard(symbol: str, day: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ ยืนยันเข้าเทรด (ส่งให้ EA)", "callback_data": f"trade:{symbol}:{day}"},
        {"text": "❌ ข้าม", "callback_data": f"skip:{symbol}:{day}"},
    ]]}


def test_keyboard(symbol: str) -> dict:
    return {"inline_keyboard": [[{"text": "🧪 ทดสอบส่งรายการให้ EA (0.01 ล็อต)", "callback_data": f"test:{symbol}"}]]}


# Persistent menu under the message box (owner's private chat only)
MENU = {
    "📊 พอร์ต MT5": "status",
    "🔔 รายงานล่าสุด": "report",
    "📋 รายการ EA": "orders",
    "🧪 ทดสอบ EA": "test",
    "❓ ช่วยเหลือ": "help",
}


def menu_keyboard() -> dict:
    labels = list(MENU)
    return {
        "keyboard": [[{"text": labels[0]}, {"text": labels[1]}], [{"text": labels[2]}, {"text": labels[3]}], [{"text": labels[4]}]],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "เลือกจากเมนูด้านล่าง หรือพิมพ์คำสั่ง",
    }


def send_menu() -> bool:
    text = "เมนูพร้อมใช้งานแล้ว กดปุ่มด้านล่างได้เลย (พิมพ์ /menu เพื่อเรียกเมนูกลับมา)"
    return tg.send_message(text, admin_only=True, reply_markup=menu_keyboard())


# Actions ------------------------------------------------------------------------------
def confirm_trade(symbol: str) -> str:
    from . import special, special_watch

    rec = special_watch.load_state().get(symbol)
    name = special.display_name(symbol, True)
    if not rec or rec.get("status") not in special_watch.TRACKING:
        return f"ไม่มีสัญญาณที่ยังเปิดอยู่สำหรับ {name} ({symbol}) จึงไม่ได้ส่งรายการ"
    sizing = ea_bridge.compute_lots(symbol, float(rec["entry"]), float(rec["stop"]))
    if sizing is None:
        return f"คำนวณขนาดไม้ของ {symbol} ไม่ได้ (MT5 ไม่พร้อม หรือโบรกเกอร์ไม่มีสัญลักษณ์นี้)"
    risk = float(rec["risk"])
    tp = float(rec["entry"]) + config.EA_TP_R * risk if config.EA_TP_R > 0 else 0.0
    row = ea_bridge.write_order_request(symbol, "BUY", sizing["lots"], float(rec["stop"]), tp,
                                        float(rec["entry"]), comment=f"trendjoin {rec['date']}")
    return ea_bridge.fmt_request_th(row, sizing, name)


def test_request(symbol: str) -> str:
    from . import special

    name = special.display_name(symbol, True)
    rows = {}
    try:
        from . import mt5_source
        rows = mt5_source.fetch_rows([symbol])
    except Exception as exc:  # noqa: BLE001
        log.warning("test request: %s", exc)
    r = rows.get(symbol)
    if not r or not r.get("price") or not r.get("atr"):
        return f"ดึงราคา/ATR ของ {symbol} จาก MT5 ไม่ได้"
    entry, atr = float(r["price"]), float(r["atr"])
    sl, tp = entry - atr, entry + atr
    sizing = ea_bridge.compute_lots(symbol, entry, sl) or {}
    row = ea_bridge.write_order_request(symbol, "BUY", sizing.get("volume_min") or 0.01, sl, tp, entry, comment="test")
    lines = [
        "🧪 ทดสอบระบบส่งคำสั่งให้ EA (ไม่ใช่สัญญาณเทรด)",
        "ฝั่ง BUY ล็อตต่ำสุด และ S/L, T/P ห่าง 1 ATR เป็นค่าตายตัวของการทดสอบ ไม่ได้มาจากสัญญาณใด",
        "ถ้า EA ปิดโหมด DryRun อยู่ รายการนี้จะเปิดไม้จริงบนพอร์ต",
        "",
    ]
    signals = open_signals_th()
    lines += (["สัญญาณที่เปิดอยู่ตอนนี้:"] + signals) if signals else ["สัญญาณที่เปิดอยู่ตอนนี้: ไม่มี"]
    lines += ["", ea_bridge.fmt_request_th(row, None, name)]
    return "\n".join(lines)


def open_signals_th() -> list[str]:
    """Every signal the bot is following right now, with its side (one line each)."""
    from . import daytrade, special, special_watch

    out = []
    try:
        state = special_watch.load_state()
    except Exception:  # noqa: BLE001
        state = {}
    for sym, rec in state.items():
        if isinstance(rec, dict) and rec.get("status") in special_watch.TRACKING:
            side = rec.get("side", "buy")                      # Scanner C is long only
            name = special.display_name(sym, True)
            out.append(f"• Scanner C: {name} ({sym}) {trade_fmt.badge(side)} {trade_fmt.ENTRY} "
                       f"เข้า {special_watch._px(rec['entry'])}")
    try:
        paper = daytrade._load(None)
    except Exception:  # noqa: BLE001
        paper = {}
    for sym, rec in paper.items():
        if isinstance(rec, dict) and rec.get("status") == "open":
            side = "buy" if rec["side"] > 0 else "sell"
            out.append(f"• เดย์เทรดทดสอบ (กระดาษ): {sym} {trade_fmt.badge(side)} {trade_fmt.ENTRY} "
                       f"เข้า {daytrade._px(rec['entry'])}")
    return out


def status_text() -> str:
    return "\n".join(tg.section_d(True))


def orders_text() -> str:
    pend = ea_bridge.pending_requests()
    res = ea_bridge.list_results()[-5:]
    reqs = ea_bridge.request_map()
    lines = [f"สถานะ EA: {ea_bridge.ea_status_th()}", f"โฟลเดอร์ไฟล์: {ea_bridge.files_dir()}"]
    lines.append(f"รายการรอ EA: {len(pend)}")
    for r in pend[-5:]:
        lines.append(f"  • {r['id']} {r['symbol']} {trade_fmt.badge(r['side'])} {r['lots']} ล็อต "
                     f"{trade_fmt.stop_arrow(r['side'])} S/L {r['sl']} {trade_fmt.tp_arrow(r['side'])} T/P {r['tp']} "
                     f"({r['created_utc']} UTC)")
    lines.append(f"ผลล่าสุดจาก EA: {len(res)} รายการ")
    for r in res:
        lines.append("  • " + ea_bridge.fmt_result_th(r, reqs.get(r["id"]), compact=True))
    return "\n".join(lines)


HELP_TH = "\n".join([
    "เมนูด้านล่าง (หรือพิมพ์คำสั่ง):",
    "📊 พอร์ต MT5 (/status)  ยอดเงิน ไม้ที่เปิด กำไรขาดทุน",
    "🔔 รายงานล่าสุด (/report)  รายงานสแกน A/B/C และพอร์ต ณ ตอนนี้",
    "📋 รายการ EA (/orders)  รายการที่ส่งให้ EA และผลล่าสุด",
    "🧪 ทดสอบ EA (/test)  ทดสอบระบบส่งคำสั่ง ไม่ใช่สัญญาณ: ส่ง BUY 0.01 ล็อตให้ EA (ถ้า EA ปิดโหมด DryRun จะเปิดไม้จริง)",
    "/menu  เรียกเมนูกลับมา",
    "ปุ่มบนข้อความสัญญาณ: ยืนยันเข้าเทรด = เขียนรายการสั่งซื้อให้ EA | ข้าม = ไม่ทำอะไร",
])


# Update handling ----------------------------------------------------------------------
def _owner_id() -> str:
    return tg.credentials()[1]


def handle_update(upd: dict, send: bool = True) -> list[str]:
    """Process one Telegram update; returns the texts produced (also sent when ``send``)."""
    token = tg.credentials()[0]
    out: list[str] = []

    cq = upd.get("callback_query")
    if cq:
        data = str(cq.get("data") or "")
        from_id = str((cq.get("from") or {}).get("id"))
        msg = cq.get("message") or {}
        if from_id != _owner_id():
            if send:
                _answer(token, cq["id"], "ปุ่มนี้ใช้ได้เฉพาะเจ้าของบอท")
            return out
        action, _, rest = data.partition(":")
        symbol = rest.split(":")[0].upper() if rest else ""
        if action == "trade":
            text = confirm_trade(symbol)
            _answer(token, cq["id"], "รับคำยืนยันแล้ว") if send else None
        elif action == "skip":
            text = f"ข้ามสัญญาณ {symbol} ตามคำสั่ง (บอทยังติดตามสัญญาณให้ตามปกติ)"
            _answer(token, cq["id"], "ข้ามแล้ว") if send else None
        elif action == "test":
            text = test_request(symbol)
            _answer(token, cq["id"], "ส่งรายการทดสอบแล้ว") if send else None
        else:
            return out
        out.append(text)
        if send:
            _remove_buttons(token, msg)
            tg.send_message(text, admin_only=True)
        return out

    m = upd.get("message")
    if m and str((m.get("from") or {}).get("id")) == _owner_id():
        text = str(m.get("text") or "").strip()
        action = MENU.get(text)
        if action is None and text.startswith("/"):
            action = {"/status": "status", "/orders": "orders", "/test": "test", "/help": "help",
                      "/start": "menu", "/menu": "menu", "/report": "report"}.get(text.split("@")[0].split()[0].lower())
        reply = None
        markup = None
        if action == "status":
            reply = status_text()
        elif action == "orders":
            reply = orders_text()
        elif action == "test":
            parts = text.split()
            symbol = parts[1].upper() if text.startswith("/") and len(parts) > 1 else \
                (config.SPECIAL_WATCHLIST[0] if config.SPECIAL_WATCHLIST else "XAUUSD")
            reply = test_request(symbol)
        elif action == "report":
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo as _Z
            reply = tg.build_report_for(_dt.now(_Z(config.MARKET_TZ)).date(), include_portfolio=True)
        elif action == "help":
            reply = HELP_TH
        elif action == "menu":
            reply, markup = "เมนูพร้อมใช้งานแล้ว กดปุ่มด้านล่างได้เลย", menu_keyboard()
        if reply:
            out.append(reply)
            if send:
                tg.send_message(reply, admin_only=True, reply_markup=markup)
    return out


def _answer(token: str, cq_id: str, text: str) -> None:
    try:
        tg.api_call("answerCallbackQuery", token, callback_query_id=cq_id, text=text)
    except Exception as exc:  # noqa: BLE001
        log.debug("answerCallbackQuery failed: %s", tg.redact(exc))


def _remove_buttons(token: str, msg: dict) -> None:
    try:
        if msg.get("message_id") and msg.get("chat"):
            tg.api_call("editMessageReplyMarkup", token, chat_id=msg["chat"]["id"],
                        message_id=msg["message_id"], reply_markup={"inline_keyboard": []})
    except Exception as exc:  # noqa: BLE001
        log.debug("editMessageReplyMarkup failed: %s", tg.redact(exc))


def report_ea_results(send: bool = True) -> list[str]:
    fresh = ea_bridge.read_new_results()
    reqs = ea_bridge.request_map() if fresh else {}
    texts = [ea_bridge.fmt_result_th(r, reqs.get(r["id"])) for r in fresh]
    if send:
        for t in texts:
            tg.send_message(t, admin_only=True)
    return texts


# Polling loop --------------------------------------------------------------------------
def _loop() -> None:
    offset = _load_offset()
    last_results = 0.0
    fast_until = 0.0  # poll quickly for a while after an update so EA results arrive fast
    while True:
        token, chat_id = tg.credentials()
        if not token or not chat_id:
            time.sleep(30)
            continue
        try:
            fast = time.time() < fast_until
            params = {"timeout": FAST_POLL_TIMEOUT if fast else 25, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            updates = tg.api_call("getUpdates", token, **params) or []
            for upd in updates:
                offset = int(upd["update_id"]) + 1
                _save_offset(offset)
                # A button press carries no timestamp of its own (only the date of the message it
                # sits on), so presses are never treated as stale: the persisted offset already
                # prevents replays after a restart. Only old /commands are skipped.
                msg_date = (upd.get("message") or {}).get("date")
                if msg_date and time.time() - float(msg_date) > STALE_UPDATE_SECONDS:
                    log.info("ignoring old message (update %s)", upd["update_id"])
                    continue
                log.info("telegram update %s: %s", upd["update_id"],
                         "button " + str((upd.get("callback_query") or {}).get("data")) if "callback_query" in upd
                         else "message " + str((upd.get("message") or {}).get("text"))[:40])
                fast_until = time.time() + FAST_POLL_WINDOW_SECONDS
                try:
                    handle_update(upd, send=True)
                except Exception:  # noqa: BLE001
                    log.exception("telegram update handling failed")
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram polling error: %s", tg.redact(exc))
            time.sleep(10)
        interval = FAST_POLL_TIMEOUT if time.time() < fast_until else RESULTS_POLL_SECONDS
        if time.time() - last_results >= interval:
            last_results = time.time()
            try:
                report_ea_results(send=True)
            except Exception:  # noqa: BLE001
                log.exception("EA result check failed")


def start_in_background() -> threading.Thread | None:
    if not tg.is_configured():
        log.warning("Telegram not configured; command polling not started")
        return None
    t = threading.Thread(target=_loop, name="telegram-commands", daemon=True)
    t.start()
    log.info("Telegram command/button polling started (owner only)")
    return t
