"""TradingView webhook receiver: TradingView alert -> this bot -> Telegram.

TradingView (paid plans) can POST an alert's message to a URL. Point it at this server
(through a tunnel such as Cloudflare or ngrok, because TradingView must reach it from the
internet) and every alert is forwarded to Telegram in plain Thai and logged to
``output/tradingview_alerts_<date>.json``.

Security: every request must carry the shared secret, either as ``?key=SECRET`` in the
URL or as a ``"secret"`` field in the JSON body. The server binds to 127.0.0.1 by default.

Alert message format (JSON is preferred; plain text is forwarded as-is):

    {"secret": "...", "signal": "Trend-join Long", "ticker": "{{ticker}}",
     "exchange": "{{exchange}}", "price": "{{close}}", "stop": "{{plot(\"Stop\")}}",
     "target": "{{plot(\"Target\")}}", "interval": "{{interval}}", "time": "{{timenow}}"}
"""
from __future__ import annotations

import hmac
import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from . import config

log = logging.getLogger(__name__)

ET = ZoneInfo(config.MARKET_TZ)
BKK = ZoneInfo("Asia/Bangkok")
MAX_BODY = 64 * 1024
DISCLAIMER_TH = "ข้อมูลอัตโนมัติเพื่อประกอบการตัดสินใจ ไม่ใช่คำแนะนำการลงทุน"


def alerts_file(day) -> Path:
    return config.OUTPUT_DIR / f"tradingview_alerts_{day.isoformat()}.json"


def _num(v) -> float | None:
    try:
        f = float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _fmt(v) -> str:
    f = _num(v)
    if f is None:
        return str(v) if v not in (None, "") else "n/a"
    if abs(f) >= 1000:
        return f"{f:,.0f}"
    if abs(f) >= 10:
        return f"{f:,.2f}"
    return f"{f:.4f}"


def parse_body(raw: bytes) -> dict:
    """JSON body -> dict; anything else -> {"text": ...}."""
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        return {"text": text}
    except ValueError:
        return {"text": text}


def format_alert(data: dict, thai: bool) -> str:
    now = datetime.now(ET)
    when = (f"{now.astimezone(BKK).strftime('%H:%M')} น. ไทย ({now.strftime('%H:%M')} น. เวลาตลาดสหรัฐ)"
            if thai else f"{now.strftime('%H:%M')} ET ({now.astimezone(BKK).strftime('%H:%M')} Bangkok)")
    ticker = str(data.get("ticker") or data.get("symbol") or "")
    exchange = str(data.get("exchange") or "")
    label = f"{ticker} ({exchange})" if exchange else ticker
    signal = str(data.get("signal") or data.get("name") or "")
    interval = str(data.get("interval") or "")
    price, stop, target = data.get("price") or data.get("close"), data.get("stop"), data.get("target")
    text = str(data.get("text") or data.get("message") or "").strip()

    lines = ["📣 แจ้งเตือนจาก TradingView" if thai else "TradingView alert"]
    if signal or label:
        if thai:
            lines.append(f"สัญญาณ: {signal or 'ไม่ระบุ'}" + (f" | {label}" if label else "")
                         + (f" | กรอบเวลา {interval}" if interval else ""))
        else:
            lines.append(f"{signal or 'alert'}" + (f" | {label}" if label else "") + (f" | {interval}" if interval else ""))
    lines.append(("เวลา " if thai else "Time ") + when)
    if price not in (None, ""):
        parts = [("ราคา: " if thai else "Price: ") + _fmt(price)]
        if _num(stop) is not None:
            parts.append(("จุดตัดขาดทุน: " if thai else "Stop: ") + _fmt(stop))
        if _num(target) is not None:
            parts.append(("เป้าหมาย: " if thai else "Target: ") + _fmt(target))
        lines.append(" | ".join(parts))
    if text:
        lines.append(text)
    if thai:
        lines.append(DISCLAIMER_TH)
    return "\n".join(lines)


def _authorized(secret: str, query: dict, data: dict) -> bool:
    if not secret:
        return False
    supplied = (query.get("key") or [""])[0] or str(data.get("secret") or "")
    return hmac.compare_digest(supplied, secret)


def _append_log(entry: dict) -> None:
    path = alerts_file(datetime.now(ET).date())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        existing.append(entry)
        path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError) as exc:
        log.warning("could not write %s: %s", path, exc)


def _deliver(message: str, data: dict) -> None:
    from . import telegram_notify as tg

    sent = tg.send_message(message)
    log.info("TradingView alert: %s %s -> telegram %s", data.get("signal"), data.get("ticker"),
             "sent" if sent else "NOT sent")


def handle_alert(raw: bytes, query: dict, secret: str, send: bool = True) -> tuple[int, dict]:
    """Validate one webhook call and queue the Telegram delivery.

    TradingView gives a webhook only a few seconds to answer, so the HTTP reply goes out
    immediately and the Telegram call runs in a background thread.
    """
    from . import telegram_notify as tg

    data = parse_body(raw)
    if not _authorized(secret, query, data):
        log.warning("TradingView webhook rejected: bad or missing secret")
        return 403, {"ok": False, "error": "forbidden"}
    data = {k: v for k, v in data.items() if k != "secret"}
    message = format_alert(data, tg.is_thai())
    _append_log({"received_at": datetime.now(ET).isoformat(timespec="seconds"), "queued": send, "alert": data})
    if send:
        threading.Thread(target=_deliver, args=(message, data), name="tv-alert-send", daemon=True).start()
    return 200, {"ok": True, "queued": send}


class _Handler(BaseHTTPRequestHandler):
    secret = ""

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path in ("/", "/health"):
            self._reply(200, {"ok": True, "service": "signal-bot tradingview webhook"})
        else:
            self._reply(404, {"ok": False})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._reply(413, {"ok": False, "error": "too large"})
            return
        raw = self.rfile.read(length) if length else b""
        query = parse_qs(urlparse(self.path).query)
        try:
            status, payload = handle_alert(raw, query, self.secret)
        except Exception as exc:  # noqa: BLE001
            log.exception("webhook handler failed")
            status, payload = 500, {"ok": False, "error": str(exc)}
        self._reply(status, payload)

    def log_message(self, fmt, *args) -> None:  # route http.server logs to logging
        log.debug("http: " + fmt, *args)


def make_server(bind: str = config.TV_WEBHOOK_BIND, port: int = config.TV_WEBHOOK_PORT,
                secret: str = config.TV_WEBHOOK_SECRET) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"secret": secret})
    return ThreadingHTTPServer((bind, port), handler)


def start_in_background(bind: str = config.TV_WEBHOOK_BIND, port: int = config.TV_WEBHOOK_PORT,
                        secret: str = config.TV_WEBHOOK_SECRET) -> ThreadingHTTPServer | None:
    if not secret:
        log.warning("TV_WEBHOOK_SECRET is empty; webhook server not started (set it in .env)")
        return None
    server = make_server(bind, port, secret)
    thread = threading.Thread(target=server.serve_forever, name="tv-webhook", daemon=True)
    thread.start()
    log.info("TradingView webhook listening on http://%s:%d/tv?key=... (expose it with a tunnel)", bind, port)
    return server
