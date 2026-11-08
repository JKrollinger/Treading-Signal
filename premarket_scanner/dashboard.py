"""Live trading dashboard: a local web page fed by the MT5 terminal.

Runs as its own process (``dashboard_server.py``, started by the scheduler) so it has its own
connection to the terminal and can never disturb the 5-minute signal jobs. One sampler thread
reads the terminal about once a second and every browser tab is served from that snapshot.

Read-only towards the broker: no route here can place, change or close an order. The page shows
account figures, so the server binds to 127.0.0.1 and refuses any other Host header; it is
deliberately not behind the Cloudflare tunnel (that one exposes the webhook port only).
"""
from __future__ import annotations

import bisect
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, filters, mt5_source
from . import trade_journal as journal
from .special import display_name

log = logging.getLogger(__name__)

UTC = timezone.utc
PAGE = Path(__file__).with_name("dashboard.html")
STATIC = Path(__file__).with_name("static")
LIVE_POLL_S = 1.0
CLOSED_POLL_S = 10.0            # MT5 closed: look for it now and then (it is never started from here)
SYNC_EVERY_S = 20.0
WATCH_EVERY_S = 5.0
BARS_CACHE_S = 0.5
STALE_TICK_S = 180              # no tick for this long: the market is treated as closed
MAX_BODY = 16 * 1024
RESTART_DELAY_S = 30
TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")
SYMBOL_RE = re.compile(r"^[A-Za-z0-9._#-]{1,32}$")
ACCOUNT_KEPT = ("login", "server", "currency", "mode", "balance", "equity", "floating", "margin_free", "margin_level", "leverage")


class Hub:
    """The terminal's latest state. ``lock`` keeps MT5 calls to one thread at a time."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.doc: dict | None = None
        self.state: dict = {"connected": False, "account": None, "positions": [], "watch": [], "symbols": []}
        self.daily_cache: dict = {}
        self.entry_cache: dict[int, dict] = {}
        self.last_sync = self.last_watch = 0.0
        self.position_ids: set[int] = set()
        self.bars_cache: dict[tuple, tuple[float, list]] = {}
        self.clock_warned = False
        self.last_tick_time: int | None = None
        self.stop = threading.Event()
        login = journal.latest_login()
        if login is not None:                   # records stay readable while MT5 is closed
            self.doc = journal.load(login)
            self.state.update(account=self.doc.get("account"), closed=journal.closed_totals(self.doc), rev=self.doc["rev"])

    # Sampler ---------------------------------------------------------------------------
    def run(self) -> None:
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                connected = self.sample()
            except Exception:  # noqa: BLE001 - the page must outlive a bad read
                log.exception("dashboard sample failed")
                connected = False
            self.stop.wait(max(0.05, (LIVE_POLL_S if connected else CLOSED_POLL_S) - (time.monotonic() - started)))

    def sample(self) -> bool:
        now = time.monotonic()
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        with self.lock:
            if not mt5_source.connect():
                self.state.update(connected=False, terminal_online=False, updated=stamp, autostart=config.MT5_AUTOSTART)
                return False
            mt5 = mt5_source._mt5()
            acc, term = mt5.account_info(), mt5.terminal_info()
            if acc is None or term is None:
                self.state.update(connected=False, terminal_online=False, updated=stamp)
                return False
            if self.doc is None or self.doc["login"] != acc.login:
                self.doc = journal.load(acc.login)
                self.entry_cache.clear()
                self.last_sync = 0.0
            doc = self.doc
            positions = list(mt5.positions_get() or [])
            journal.track_open(doc, positions)
            ids = {int(p.ticket) for p in positions}
            if ids != self.position_ids or now - self.last_sync >= SYNC_EVERY_S:
                self.daily_cache.clear()
                journal.sync(doc, mt5)
                self.last_sync, self.position_ids = now, ids
            margin_level = float(acc.margin_level) if acc.margin else None
            account = {
                "login": acc.login, "server": acc.server, "currency": acc.currency,
                "mode": {0: "demo", 1: "contest", 2: "real"}.get(acc.trade_mode, str(acc.trade_mode)),
                "balance": float(acc.balance), "equity": float(acc.equity), "floating": float(acc.profit),
                "margin": float(acc.margin), "margin_free": float(acc.margin_free), "margin_level": margin_level,
                "leverage": int(acc.leverage),
            }
            if doc.get("account") != {k: account[k] for k in ACCOUNT_KEPT}:     # for when MT5 is closed
                doc["account"] = {k: account[k] for k in ACCOUNT_KEPT}
                journal.save(doc)
            rows = [self._position(mt5, doc, p) for p in positions]
            if now - self.last_watch >= WATCH_EVERY_S:
                self.state["watch"] = self._watch(mt5)
                self.last_watch = now
            counts: dict[str, int] = defaultdict(int)
            for t in doc["trades"].values():
                counts[t["symbol"]] += 1
            busiest = sorted(counts, key=counts.get, reverse=True)      # the chart opens on the most traded one
            symbols = list(dict.fromkeys([p["symbol"] for p in rows] + busiest + config.SPECIAL_WATCHLIST))
            self._check_clock(mt5, symbols)
            self.state.update(
                connected=True, terminal_online=bool(term.connected), updated=stamp, account=account, positions=rows,
                closed=journal.closed_totals(doc), rev=doc["rev"], symbols=symbols, autostart=config.MT5_AUTOSTART,
                market_open=any(w.get("fresh") for w in self.state["watch"]),
            )
        return True

    def _check_clock(self, mt5, symbols: list[str]) -> None:
        """Warn once when the broker's clock is not MT5_SERVER_TZ (every time shown would be off)."""
        if self.clock_warned:
            return
        tick = mt5.symbol_info_tick(symbols[0]) if symbols else None
        if tick is None:
            return
        seen, self.last_tick_time = self.last_tick_time, int(tick.time)
        if seen is None or seen == int(tick.time):      # only a tick arriving now says what time it is
            return
        skew = int(tick.time) - journal.utc_to_server(datetime.now(UTC)).timestamp()
        if abs(skew) > 1800:
            self.clock_warned = True
            log.warning("broker clock differs from MT5_SERVER_TZ=%s by %.1f h; set MT5_SERVER_TZ in .env",
                        config.MT5_SERVER_TZ, skew / 3600)

    def _position(self, mt5, doc: dict, p) -> dict:
        side = 1 if p.type == 0 else -1
        entry, current = float(p.price_open), float(p.price_current)
        ticket = int(p.ticket)
        ctx = self.entry_cache.get(ticket)
        if ctx is None:
            try:
                ctx = journal.entry_context(mt5, p.symbol, side, entry, int(p.time), self.daily_cache) or {}
            except Exception:  # noqa: BLE001
                log.exception("entry context failed for %s", ticket)
                ctx = {}
            self.entry_cache[ticket] = ctx
        meta = doc["open"].get(str(ticket)) or {}
        first_sl = float(p.sl) if meta.get("first_sl") is None and p.sl else meta.get("first_sl")
        draft = {"dir": side, "entry": entry, "exit": current, "sl": first_sl, "sl_final": None, "ctx": ctx,
                 "symbol": p.symbol, "origin": "manual", "net": float(p.profit), "reason": -1, "hold_s": 0}
        risk, basis = journal._risk(draft, ctx)
        opened = journal.server_to_utc(p.time)
        return {
            "ticket": ticket, "symbol": p.symbol, "name": display_name(p.symbol, True), "side": "buy" if side > 0 else "sell",
            "volume": float(p.volume), "entry": entry, "current": current, "sl": float(p.sl) or None, "tp": float(p.tp) or None,
            "profit": float(p.profit), "swap": float(p.swap), "opened": opened.isoformat(timespec="seconds"),
            "held_s": max(0, int((datetime.now(UTC) - opened).total_seconds())),
            "r": side * (current - entry) / risk if risk else None, "risk_basis": basis,
            "checks": journal._checks({**draft, "sl": float(p.sl) or first_sl}) if ctx else [],
            "comment": (p.comment or "")[:40],
        }

    def _watch(self, mt5) -> list[dict]:
        """The Trend-join conditions of each watched symbol right now."""
        rows = mt5_source.fetch_rows(list(config.SPECIAL_WATCHLIST))
        out = []
        now_srv = journal.utc_to_server(datetime.now(UTC)).timestamp()
        tol = config.HOD_TOLERANCE_PCT / 100.0
        for symbol in config.SPECIAL_WATCHLIST:
            r = rows.get(symbol)
            if not r:
                out.append({"symbol": symbol, "name": display_name(symbol, True), "price": None})
                continue
            tick = mt5.symbol_info_tick(symbol)
            fresh = tick is not None and now_srv - int(tick.time) < STALE_TICK_S
            price, y_high, day_high = r["price"], r["yesterday_high"], r["day_high"]
            uptrend = None if r["sma200"] is None else r["yesterday_close"] > r["sma200"]
            above = price > y_high
            at_high = price >= day_high * (1 - tol)
            ready = bool(uptrend and above and at_high)
            held = filters.check_new_signal(symbol, datetime.now(UTC)) if ready and fresh else None
            out.append({
                "symbol": symbol, "name": display_name(symbol, True), "price": price, "digits": r.get("digits"), "fresh": fresh,
                "change_pct": r.get("change_pct"), "uptrend": uptrend, "above": above, "at_high": at_high, "ready": ready,
                "held": held, "y_high": y_high, "y_close": r["yesterday_close"], "sma200": r["sma200"], "day_high": day_high,
                "to_breakout": max(y_high - price, 0.0), "sma_gap_pct": (r["yesterday_close"] / r["sma200"] - 1) * 100 if r["sma200"] else None,
                "last_tick": journal.server_to_utc(tick.time).isoformat(timespec="seconds") if tick is not None else None,
            })
        return out

    # Requests --------------------------------------------------------------------------
    def _bars(self, symbol: str, tf: str, count: int) -> list | None:
        with self.lock:
            if not self.state.get("connected"):
                return None
            mt5 = mt5_source._mt5()
            rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, f"TIMEFRAME_{tf}"), 0, count)
            if rates is None or len(rates) == 0:
                mt5.symbol_select(symbol, True)
                rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, f"TIMEFRAME_{tf}"), 0, count)
            if rates is None or len(rates) == 0:
                return None
        return [{"t": int(journal.server_to_utc(int(r["time"])).timestamp()), "o": float(r["open"]), "h": float(r["high"]),
                 "l": float(r["low"]), "c": float(r["close"]), "v": int(r["tick_volume"])} for r in rates]

    def live(self, symbol: str | None, tf: str | None) -> dict:
        with self.lock:
            out = dict(self.state)
            if symbol and tf and out.get("connected"):
                key = (symbol, tf)
                cached = self.bars_cache.get(key)
                if cached is None or time.monotonic() - cached[0] > BARS_CACHE_S:
                    cached = (time.monotonic(), self._bars(symbol, tf, 2) or [])
                    self.bars_cache[key] = cached
                out["chart"] = cached[1]
                tick = mt5_source._mt5().symbol_info_tick(symbol)
                if tick is not None:
                    out["tick"] = {"bid": float(tick.bid), "ask": float(tick.ask)}
        return out

    def chart(self, symbol: str, tf: str, count: int) -> dict:
        bars = self._bars(symbol, tf, count)
        if bars is None:
            return {"ok": False, "error": "mt5_closed" if not self.state.get("connected") else "no_data", "symbol": symbol, "tf": tf}
        with self.lock:
            mt5 = mt5_source._mt5()
            info = mt5.symbol_info(symbol)
            digits = int(info.digits) if info is not None else 2
            levels = self._levels(mt5, symbol)
            sma = self._sma_line(mt5, symbol) if tf == "D1" else []
            trades = [t for t in (self.doc or {"trades": {}})["trades"].values() if t["symbol"] == symbol]
            positions = [p for p in self.state.get("positions", []) if p["symbol"] == symbol]
        return {"ok": True, "symbol": symbol, "tf": tf, "digits": digits, "name": display_name(symbol, True), "bars": bars,
                "levels": levels, "sma": sma, "markers": _markers(bars, trades, positions, journal.bot_signals(), symbol)}

    def _levels(self, mt5, symbol: str) -> dict:
        rates = journal._daily(mt5, symbol, self.daily_cache)
        if rates is None or len(rates) < 2:
            return {}
        y = rates[-2]
        closes = rates["close"][:-1]
        return {"y_high": float(y["high"]), "y_low": float(y["low"]), "y_close": float(y["close"]),
                "sma200": float(closes[-200:].mean()) if len(closes) >= 200 else None}

    def _sma_line(self, mt5, symbol: str) -> list[dict]:
        rates = journal._daily(mt5, symbol, self.daily_cache)
        if rates is None or len(rates) <= 200:
            return []
        closes = rates["close"]
        return [{"t": int(journal.server_to_utc(int(rates["time"][i])).timestamp()), "v": float(closes[i - 200:i].mean())}
                for i in range(200, len(rates))]

    def journal(self, period: str, symbol: str, origin: str) -> dict:
        with self.lock:
            doc = self.doc
            if doc is None:
                return {"ok": False, "error": "no_account"}
            rows = journal.query(doc, period, symbol, origin)
            account = self.state.get("account") or {}
            every = sorted({t["symbol"] for t in doc["trades"].values()})
            payload = {
                "ok": True, "currency": account.get("currency", ""), "rev": doc["rev"], "symbols": every, "trades": rows,
                "summary": journal.summarize(rows), "analysis": journal.analysis(rows), "deposits": doc.get("deposits"),
                "signals": journal.bot_signals(),
            }
        return payload

    def set_note(self, trade_id: str, note: str) -> bool:
        with self.lock:
            return bool(self.doc) and journal.set_note(self.doc, trade_id, note)


def _markers(bars: list[dict], trades: list[dict], positions: list[dict], signals: list[dict], symbol: str) -> list[dict]:
    """Chart marks, each moved onto the bar it happened in."""
    times = [b["t"] for b in bars]

    def bar_of(iso: str) -> int | None:
        t = int(datetime.fromisoformat(iso).timestamp())
        i = bisect.bisect_right(times, t) - 1
        return times[i] if i >= 0 else None

    entries: dict[tuple, list[dict]] = defaultdict(list)    # trades on one bar share one mark
    exits: dict[tuple, list[dict]] = defaultdict(list)
    for tr in trades:
        at = bar_of(tr["entry_time"])
        if at is not None:
            entries[(at, tr["side"])].append(tr)
        at = bar_of(tr["exit_time"])
        if at is not None:
            exits[(at, tr["side"])].append(tr)
    out: list[dict] = []
    for (at, side), group in entries.items():
        out.append({"t": at, "kind": "entry", "side": side, "trade": group[0]["id"], "text": ""})
    for (at, side), group in exits.items():
        net = sum(tr["net"] for tr in group)
        count = f" ({len(group)} ไม้)" if len(group) > 1 else ""
        out.append({"t": at, "kind": "exit", "side": side, "trade": group[0]["id"], "win": net >= 0, "text": f"{net:+,.0f}{count}"})
    for p in positions:
        at = bar_of(p["opened"])
        if at is not None:
            out.append({"t": at, "kind": "entry", "side": p["side"], "ticket": p["ticket"], "text": ""})
    for s in signals:
        if s["symbol"] == symbol and s.get("entry_time"):
            at = bar_of(s["entry_time"])
            if at is not None:
                out.append({"t": at, "kind": "signal", "side": s["side"], "text": f"#{s['no']}" if s.get("no") else "T"})
    return sorted(out, key=lambda m: m["t"])


class _Handler(BaseHTTPRequestHandler):
    hub: Hub

    def _send(self, status: int, body: bytes, kind: str, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False, default=float).encode("utf-8"), "application/json; charset=utf-8")

    def _local_host(self) -> bool:
        """Refuse other host names, so a web page elsewhere cannot reach this server through DNS tricks."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def do_GET(self) -> None:  # noqa: N802
        if not self._local_host():
            self._json(403, {"ok": False, "error": "forbidden"})
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/static/lightweight-charts.js":
                self._send(200, (STATIC / "lightweight-charts.js").read_bytes(), "application/javascript; charset=utf-8", cache=True)
            elif url.path == "/api/live":
                self._json(200, self.hub.live(*_chart_args(q)))
            elif url.path == "/api/chart":
                symbol, tf = _chart_args(q)
                if not symbol or not tf:
                    self._json(400, {"ok": False, "error": "bad symbol or timeframe"})
                    return
                self._json(200, self.hub.chart(symbol, tf, max(50, min(int(q.get("count") or 800), 5000))))
            elif url.path == "/api/journal":
                self._json(200, self.hub.journal(q.get("period", "all"), q.get("symbol", ""), q.get("origin", "")))
            else:
                self._json(404, {"ok": False})
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass                                # the browser went away mid-reply
        except Exception as exc:  # noqa: BLE001
            log.exception("dashboard request failed: %s", self.path)
            self._json(500, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        origin = self.headers.get("Origin")
        same_origin = origin is None or urlparse(origin).netloc == self.headers.get("Host")
        if not self._local_host() or not same_origin:
            self._json(403, {"ok": False, "error": "forbidden"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if urlparse(self.path).path != "/api/note" or length > MAX_BODY:
            self._json(400, {"ok": False})
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            self._json(200, {"ok": self.hub.set_note(str(data["id"]), str(data.get("note") or ""))})
        except (ValueError, KeyError):
            self._json(400, {"ok": False})

    def log_message(self, fmt, *args) -> None:
        log.debug("http: " + fmt, *args)


def _chart_args(q: dict) -> tuple[str | None, str | None]:
    symbol, tf = q.get("symbol") or "", q.get("tf") or ""
    return (symbol, tf) if SYMBOL_RE.match(symbol) and tf in TIMEFRAMES else (None, None)


def serve(bind: str = config.DASHBOARD_BIND, port: int = config.DASHBOARD_PORT) -> None:
    """Run until the process is stopped."""
    hub = Hub()
    handler = type("Handler", (_Handler,), {"hub": hub})
    server = ThreadingHTTPServer((bind, port), handler)
    threading.Thread(target=hub.run, name="dashboard-sampler", daemon=True).start()
    log.info("dashboard: http://%s:%d", bind, port)
    server.serve_forever()


def exit_with_parent(pid: int) -> None:
    """Stop this process when ``pid`` (the scheduler) ends. Task Scheduler kills the scheduler
    without any cleanup, so the child has to notice by itself."""
    if os.name != "nt":
        return
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    handle = k32.OpenProcess(0x00100000, False, pid)        # SYNCHRONIZE
    if not handle:
        os._exit(0)                                         # the parent is already gone

    def wait() -> None:
        k32.WaitForSingleObject(handle, 0xFFFFFFFF)
        os._exit(0)

    threading.Thread(target=wait, name="dashboard-parent-watch", daemon=True).start()


def start_child() -> None:
    """Keep ``dashboard_server.py`` running as a child of the scheduler."""
    script = config.PROJECT_ROOT / "dashboard_server.py"
    command = [sys.executable, str(script), "--parent-pid", str(os.getpid())]

    def keep_alive() -> None:
        while True:
            started = time.monotonic()
            try:
                proc = subprocess.Popen(command, cwd=config.PROJECT_ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                proc.wait()
                log.warning("dashboard exited with code %s", proc.returncode)
            except OSError as exc:
                log.error("dashboard could not start: %s", exc)
            time.sleep(max(RESTART_DELAY_S, 5) if time.monotonic() - started < 60 else 5)

    threading.Thread(target=keep_alive, name="dashboard-child", daemon=True).start()
    log.info("dashboard: http://%s:%d", config.DASHBOARD_BIND, config.DASHBOARD_PORT)
