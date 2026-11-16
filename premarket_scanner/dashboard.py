"""Live trading dashboard: a local web page fed by the MT5 terminal.

Runs as its own process (``dashboard_server.py``, started by the scheduler) so it has its own
connection to the terminal and can never disturb the 5-minute signal jobs. One sampler thread
reads the terminal about once a second and every browser tab is served from that snapshot.

Read-only towards the broker: no route here can place, change or close an order. The one switch
it has is the stop button, which only stops the bot from writing new order requests for the EA
(see ``ea_bridge.set_halt``). The page shows account figures, so the server binds to 127.0.0.1 and
refuses any other Host header; it is deliberately not behind the Cloudflare tunnel (that one
exposes the webhook port only).
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

from . import config, ea_bridge, filters, mt5_source, risk_gate
from . import trade_journal as journal
from .special import display_name

log = logging.getLogger(__name__)

UTC = timezone.utc
PAGE = Path(__file__).with_name("dashboard.html")
CLASSIC = Path(__file__).with_name("dashboard_classic.html")
STATIC = Path(__file__).with_name("static")
STATIC_FILES = {                 # the only files served from static/
    "lightweight-charts.js": "application/javascript; charset=utf-8",
    "gold-terminal.css": "text/css; charset=utf-8",
    "gold-bars.webp": "image/webp",
    "golden-bull.webp": "image/webp",
    "golden-bear.webp": "image/webp",
    "bot-avatar.webp": "image/webp",
    "kill-shield.webp": "image/webp",
    "strategy-trend.webp": "image/webp",
    "strategy-daytrade.webp": "image/webp",
    "strategy-stocks.webp": "image/webp",
}
SCHEDULER_LOG = config.PROJECT_ROOT / "logs" / "scheduler.log"
LOG_TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
JOURNAL_RE = re.compile(r"^journal_(\d+)\.json$")
RISK_EVERY_S = 3.0
EA_ONLINE_S = 60                # heartbeat younger than this: the EA is on a chart and running
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
        self.ea_dir: Path | None = None             # <MT5 Common>/Files/signalbot, learned from the terminal
        self.sl_cache: dict[tuple, float | None] = {}
        self.settings = _settings()
        self.risk: dict = {"enabled": config.RISK_GATE_ENABLED}
        self.last_risk = 0.0
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
            if getattr(term, "commondata_path", ""):
                self.ea_dir = Path(term.commondata_path) / "Files" / ea_bridge.SUBDIR
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
            if now - self.last_risk >= RISK_EVERY_S:
                self.last_risk = now
                try:
                    self.risk = risk_gate.tick(acc.login, float(acc.balance), float(acc.equity),
                                               journal.closed_totals(doc)["today"], doc=doc, folder=self._ea_folder())
                    ea_bridge.mirror_halt(self._ea_folder())
                except Exception:  # noqa: BLE001
                    log.exception("risk gate failed")
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
                algo_trading=bool(term.trade_allowed), account_trade_allowed=bool(acc.trade_allowed),
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
        sl_pl = None                                # money at the stop: negative = what hitting it costs
        if p.sl:
            key = (ticket, float(p.sl), float(p.volume))
            if key not in self.sl_cache:
                try:
                    order_type = mt5.ORDER_TYPE_BUY if side > 0 else mt5.ORDER_TYPE_SELL
                    value = mt5.order_calc_profit(order_type, p.symbol, float(p.volume), entry, float(p.sl))
                except Exception:  # noqa: BLE001
                    value = None
                self.sl_cache[key] = float(value) if value is not None else None
            sl_pl = self.sl_cache[key]
        return {
            "ticket": ticket, "symbol": p.symbol, "name": display_name(p.symbol, True), "side": "buy" if side > 0 else "sell",
            "volume": float(p.volume), "entry": entry, "current": current, "sl": float(p.sl) or None, "tp": float(p.tp) or None,
            "profit": float(p.profit), "swap": float(p.swap), "opened": opened.isoformat(timespec="seconds"),
            "held_s": max(0, int((datetime.now(UTC) - opened).total_seconds())),
            "r": side * (current - entry) / risk if risk else None, "risk_basis": basis, "sl_pl": sl_pl,
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

    def set_halt(self, on: bool) -> dict:
        state = ea_bridge.set_halt(on, "dashboard", folder=self._ea_folder())
        if not on:
            with self.lock:
                account = self.state.get("account") or {}
                risk_gate.on_resume(account.get("login"), account.get("equity") if self.state.get("connected") else None)
                self.last_risk = 0.0
        return state

    def set_note(self, trade_id: str, note: str) -> bool:
        with self.lock:
            return bool(self.doc) and journal.set_note(self.doc, trade_id, note)

    def _ea_folder(self) -> Path | None:
        if config.EA_FILES_DIR:
            return Path(config.EA_FILES_DIR)
        if self.ea_dir is not None:
            return self.ea_dir
        appdata = os.environ.get("APPDATA")      # MT5 closed: its default Common folder
        return Path(appdata) / "MetaQuotes" / "Terminal" / "Common" / "Files" / ea_bridge.SUBDIR if appdata else None

    def system(self) -> dict:
        """The EA bridge, the stop switch, the scheduler and the bot's settings."""
        folder = self._ea_folder()
        requests: list[dict] = []
        results: list[dict] = []
        heartbeat = None
        if folder is not None:
            try:
                requests, results = ea_bridge.list_requests(folder), ea_bridge.list_results(folder)
                heartbeat = ea_bridge.heartbeat_age_seconds(folder)
            except OSError as exc:
                log.debug("EA files unreadable: %s", exc)
        by_id = {r["id"]: r for r in requests}
        done = {r["id"] for r in results}
        rows = []
        for r in reversed(results[-15:]):
            req = by_id.get(r["id"], {})
            rows.append({
                "id": r["id"], "time": _mql_utc(r.get("processed_utc")), "status": r.get("status"),
                "ticket": r.get("ticket") if r.get("ticket") not in ("", "0") else None,
                "price": _float(r.get("price")), "lots": _float(r.get("lots")), "message": (r.get("message") or "")[:160],
                "symbol": req.get("symbol"), "side": (req.get("side") or "").lower() or None, "test": req.get("comment") == "test",
            })
        pending = [{"id": r["id"], "time": _mql_utc(r.get("created_utc")), "symbol": r.get("symbol"),
                    "side": (r.get("side") or "").lower(), "lots": _float(r.get("lots")), "test": r.get("comment") == "test"}
                   for r in requests if r["id"] not in done][-10:]
        mode = next((r["status"] for r in reversed(results) if r.get("status") in ("FILLED", "DRYRUN")), None)
        return {
            "ok": True, "halt": ea_bridge.halt_state(), "scheduler": _scheduler_status(), "settings": self.settings,
            "risk": self.risk,
            "ea": {"enabled": config.EA_BRIDGE_ENABLED, "folder_found": bool(folder and folder.exists()),
                   "heartbeat_age_s": heartbeat, "online": heartbeat is not None and heartbeat <= EA_ONLINE_S,
                   "last_mode": mode, "results": rows, "pending": pending},
        }

    def accounts(self) -> dict:
        """Every MT5 login this dashboard has kept a journal for."""
        with self.lock:
            current = self.doc
            connected = bool(self.state.get("connected"))
            live = self.state.get("account") if connected else None
            current_row = _account_row(current, live) if current is not None else None
        out = []
        for path in sorted(config.DATA_DIR.glob("journal_*.json")):
            m = JOURNAL_RE.match(path.name)
            if not m:
                continue
            login = int(m.group(1))
            if current_row is not None and current_row["login"] == login:
                out.append({**current_row, "online": connected})
                continue
            try:
                out.append({**_account_row(journal.load(login), None), "online": False})
            except (OSError, ValueError, KeyError) as exc:
                log.warning("journal %s unreadable: %s", path.name, exc)
        return {"ok": True, "current": current["login"] if current else None, "connected": connected, "accounts": out}


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


def _settings() -> dict:
    """What the bot runs with (from .env); fixed for the life of the process."""
    c = config
    return {
        "watchlist": list(c.SPECIAL_WATCHLIST), "watch_enabled": bool(c.SPECIAL_ENABLED and c.SPECIAL_WATCH_ENABLED),
        "watch_every_min": c.SPECIAL_WATCH_INTERVAL_MIN, "source": c.SPECIAL_DATA_SOURCE,
        "stop_atr": c.STOP_ATR_MULT, "partial_r": c.PARTIAL_R, "trail_atr": c.TRAIL_ATR_MULT, "max_hold_days": c.MAX_HOLD_DAYS,
        "stale_min": c.STALE_QUOTE_MIN, "filter_symbols": list(c.TIME_FILTER_SYMBOLS),
        "session_utc": [c.SESSION_UTC_START, c.SESSION_UTC_END], "friday_cutoff_utc": c.FRIDAY_CUTOFF_UTC,
        "news_blackout_min": c.NEWS_BLACKOUT_MIN, "daytrade_enabled": c.DAYTRADE_ENABLED,
        "daytrade_symbols": list(c.DAYTRADE_SYMBOLS), "stocks_enabled": c.STOCKS_ENABLED,
        "spread_limit_pct": c.RISK_MAX_SPREAD_PCT if c.RISK_GATE_ENABLED else 0, "ea_enabled": c.EA_BRIDGE_ENABLED, "ea_risk_pct": c.EA_RISK_PCT, "ea_tp_r": c.EA_TP_R, "autostart": c.MT5_AUTOSTART,
    }


def _account_row(doc: dict, live: dict | None) -> dict:
    trades = sorted(doc["trades"].values(), key=lambda t: t["exit_time"])
    account = live or doc.get("account") or {}
    cum, curve = 0.0, []
    for t in trades:
        cum += t["net"]
        curve.append(round(cum, 2))
    if len(curve) > 80:                      # enough points for a sparkline
        step = len(curve) / 80
        curve = [curve[int(i * step)] for i in range(80)] + [curve[-1]]
    line = journal._line(trades)
    return {
        "login": doc["login"], "server": account.get("server"), "currency": account.get("currency"), "mode": account.get("mode"),
        "balance": account.get("balance"), "equity": account.get("equity"), "floating": account.get("floating"),
        "leverage": account.get("leverage"), "deposits": doc.get("deposits"), "trades": line["n"], "wins": line["wins"],
        "win_rate": line["win_rate"], "net": line["net"], "curve": curve, "last_trade": trades[-1]["exit_time"] if trades else None,
    }


def _scheduler_status() -> dict:
    """When the scheduler last wrote to its log, and when Scanner C last checked the market."""
    try:
        with SCHEDULER_LOG.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 16384))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return {"last": None, "last_watch": None}

    def stamp(line: str) -> str | None:
        m = LOG_TIME_RE.match(line)          # the log is in this PC's local time
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").astimezone(UTC).isoformat(timespec="seconds") if m else None

    last = next((t for t in map(stamp, reversed(lines)) if t), None)
    watch = next((stamp(x) for x in reversed(lines) if "Scanner C watch" in x and stamp(x)), None)
    return {"last": last, "last_watch": watch}


def _mql_utc(text: str | None) -> str | None:
    """'2026.10.28 02:36:04' (UTC, as the EA writes it) -> ISO."""
    try:
        return datetime.strptime((text or "").strip().replace("-", "."), "%Y.%m.%d %H:%M:%S").replace(tzinfo=UTC).isoformat()
    except ValueError:
        return None


def _float(text) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


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
            elif url.path == "/classic":
                self._send(200, CLASSIC.read_bytes(), "text/html; charset=utf-8")
            elif url.path.startswith("/static/") and url.path[8:] in STATIC_FILES:
                name = url.path[8:]
                self._send(200, (STATIC / name).read_bytes(), STATIC_FILES[name], cache=name.endswith((".js", ".webp")))
            elif url.path == "/api/system":
                self._json(200, self.hub.system())
            elif url.path == "/api/accounts":
                self._json(200, self.hub.accounts())
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
        path = urlparse(self.path).path
        if path not in ("/api/note", "/api/halt") or length > MAX_BODY:
            self._json(400, {"ok": False})
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if path == "/api/halt":
                if not isinstance(data.get("halt"), bool):
                    raise ValueError("halt must be true or false")
                self._json(200, {"ok": True, "halt": self.hub.set_halt(data["halt"])})
            else:
                self._json(200, {"ok": self.hub.set_note(str(data["id"]), str(data.get("note") or ""))})
        except (ValueError, KeyError, AttributeError):
            self._json(400, {"ok": False})
        except OSError as exc:
            log.error("dashboard could not save %s: %s", path, exc)
            self._json(500, {"ok": False})

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
