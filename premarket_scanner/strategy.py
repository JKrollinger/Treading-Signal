"""Trend-join Long strategy scanner.

Input: the tickers that today's pre-market scan produced. After 10:00 ET each ticker
must satisfy:

1. price above yesterday's daily high
2. yesterday's close above the 200-day SMA
3. price above today's pre-market high AND at the high of day (breaking out)

Reference levels (yesterday's high/close, SMA200, pre-market high) come from
TradingView's screener by default so they match the user's charts; the live price and
high of day come from Yahoo Finance unless TradingView is streaming real-time data.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from . import config, tradingview, yahoo

log = logging.getLogger(__name__)

ET = ZoneInfo(config.MARKET_TZ)
STRATEGY_NAME = "Trend-join Long"
CHECK_ORDER = (
    "above_yesterday_high",
    "yesterday_close_above_sma200",
    "above_premarket_high",
    "at_high_of_day",
    "after_start_in_regular_session",
)


def _parse_hhmm(text: str) -> dtime:
    hour, minute = (int(x) for x in text.split(":"))
    return dtime(hour, minute)


# Data ----------------------------------------------------------------------------
@dataclass
class Snapshot:
    ticker: str
    price: float | None = None
    day_high: float | None = None
    premarket_high: float | None = None
    yesterday_high: float | None = None
    yesterday_close: float | None = None
    sma200: float | None = None
    market_state: str = ""
    quote_time: datetime | None = None
    price_source: str = ""
    levels_source: str = ""
    price_delayed: bool = False
    tv_symbol: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        d["quote_time"] = self.quote_time.isoformat() if self.quote_time else None
        for k in ("price", "day_high", "premarket_high", "yesterday_high", "yesterday_close", "sma200"):
            if d[k] is not None:
                d[k] = round(float(d[k]), 4)
        return d


def _sub(df: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    """Pick one ticker's frame out of a yf.download result (single or multi-ticker)."""
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        if ticker not in df.columns.get_level_values(0):
            return None
        return df[ticker]
    return df


def yahoo_levels(tickers: list[str], today: date, quotes: dict[str, dict] | None = None) -> dict[str, dict]:
    """Yesterday's high/close, SMA200 (as of yesterday) and today's pre-market high.

    Yahoo's chart API leaves the latest daily bar's close empty until the next session
    starts, so overnight the last completed bar is rebuilt from the quote (last price and
    day high), and during the session a still-empty close falls back to the quote's
    previous close.
    """
    out: dict[str, dict] = {}
    quotes = quotes or {}
    if not tickers:
        return out
    daily = yf.download(
        tickers, period="1y", interval="1d", group_by="ticker",
        auto_adjust=False, threads=True, progress=False,
    )
    intraday = yf.download(
        tickers, period="1d", interval="1m", prepost=True, group_by="ticker",
        auto_adjust=False, threads=True, progress=False,
    )
    for t in tickers:
        row: dict = {}
        q = quotes.get(t) or {}
        q_time = yahoo._ts_to_et(q.get("regularMarketTime"))
        q_date = q_time.date() if q_time else None

        d = _sub(daily, t)
        if d is not None:
            d = d[pd.Index(d.index.date) < today]
            bars = [
                (idx.date(), _num(h), _num(c))
                for idx, h, c in zip(d.index, d["High"], d["Close"])
                if _num(h) is not None or _num(c) is not None
            ]
            if bars and bars[-1][2] is None:
                # last bar has a high but no close yet
                last_date, last_high, _ = bars[-1]
                fill = None
                if q_date == last_date:
                    fill = _num(q.get("regularMarketPrice"))
                    last_high = last_high or _num(q.get("regularMarketDayHigh"))
                elif q_date == today:
                    fill = _num(q.get("regularMarketPreviousClose"))
                bars[-1] = (last_date, last_high, fill)
            if q_date and q_date < today and (not bars or q_date > bars[-1][0]):
                # the latest session is missing from the history entirely
                bars.append((q_date, _num(q.get("regularMarketDayHigh")), _num(q.get("regularMarketPrice"))))
            bars = [b for b in bars if b[2] is not None]
            if bars:
                row["yesterday_high"] = bars[-1][1]
                row["yesterday_close"] = bars[-1][2]
                closes = [b[2] for b in bars]
                row["sma200"] = sum(closes[-200:]) / 200.0 if len(closes) >= 200 else None
                row["history_days"] = len(closes)
        i = _sub(intraday, t)
        if i is not None:
            i = i.dropna(subset=["High"])
            i = i[pd.Index(i.index.date) == today]
            pre = i.between_time("04:00", "09:29")
            row["premarket_high"] = float(pre["High"].max()) if len(pre) else None
        out[t] = row
    return out


def build_snapshots(tickers: list[str], today: date, warnings: list[str]) -> list[Snapshot]:
    snaps = {t: Snapshot(ticker=t) for t in tickers}
    if not tickers:
        return []
    now = datetime.now(ET)

    # 0. Yahoo quotes: live price, high of day, and whether today's session has traded --
    quotes: dict[str, dict] = {}
    try:
        quotes = {q["symbol"]: q for q in yahoo.fetch_quotes(tickers) if q.get("symbol")}
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Yahoo quotes failed ({exc})")
    clock_says_open = now.weekday() < 5 and now.time() >= dtime(9, 30)

    def today_bar_started(t: str) -> bool:
        q = quotes.get(t)
        qt = yahoo._ts_to_et(q.get("regularMarketTime")) if q else None
        return qt.date() == today if qt else clock_says_open

    # 1. Reference levels (and, when streaming, the live price) from TradingView ----
    if config.STRATEGY_DATA_SOURCE == "tradingview":
        try:
            rows = tradingview.fetch_levels(tickers)
        except Exception as exc:  # noqa: BLE001
            rows = {}
            warnings.append(f"TradingView scanner failed ({exc}); using Yahoo Finance for reference levels")
        for t, row in rows.items():
            s = snaps[t]
            s.tv_symbol = row.symbol
            s.premarket_high = row.premarket_high
            s.levels_source = "tradingview"
            if today_bar_started(t):
                # current bar = today, so [1] is yesterday
                s.yesterday_high, s.yesterday_close, s.sma200 = row.high_prev, row.close_prev, row.sma200_prev
                if row.streaming and row.close is not None:
                    s.price, s.day_high = row.close, row.high
                    s.price_source, s.price_delayed = "tradingview", False
            else:
                # before the open the current bar is still yesterday's completed bar
                s.yesterday_high, s.yesterday_close, s.sma200 = row.high, row.close, row.sma200
            if s.sma200 is None:
                s.notes.append("200 SMA unavailable on TradingView (less than 200 days of history?)")
        missing = [t for t in tickers if snaps[t].levels_source != "tradingview"]
        if missing and rows:
            warnings.append(f"TradingView had no data for: {', '.join(missing)}; using Yahoo Finance for them")
    else:
        missing = list(tickers)

    # 2. Yahoo levels for whatever is still missing ----------------------------------
    if missing:
        try:
            levels = yahoo_levels(missing, today, quotes)
        except Exception as exc:  # noqa: BLE001
            levels = {}
            warnings.append(f"Yahoo Finance history failed ({exc})")
        for t in missing:
            s = snaps[t]
            row = levels.get(t) or {}
            s.yesterday_high = row.get("yesterday_high")
            s.yesterday_close = row.get("yesterday_close")
            s.sma200 = row.get("sma200")
            s.premarket_high = row.get("premarket_high")
            s.levels_source = "yahoo"
            days = row.get("history_days")
            if days is not None and days < 200:
                s.notes.append(f"only {days} daily bars of history; 200 SMA unavailable")

    # 3. Live price + high of day from Yahoo for everything without a streaming price -
    for t in tickers:
        s, q = snaps[t], quotes.get(t)
        if q is None:
            if s.price is None:
                s.notes.append("no Yahoo quote")
            continue
        s.market_state = str(q.get("marketState") or "")
        s.quote_time = yahoo._ts_to_et(q.get("regularMarketTime"))
        if s.price is None:
            s.price = _num(q.get("regularMarketPrice"))
            s.day_high = _num(q.get("regularMarketDayHigh"))
            s.price_source = "yahoo"
            if s.quote_time and s.quote_time.date() != today:
                s.notes.append("Yahoo quote is not from today's session (market not open yet?)")
    return [snaps[t] for t in tickers]


def _num(v) -> float | None:
    """float(v), with None/NaN/unparseable -> None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN is the only float that is not equal to itself


# Evaluation ------------------------------------------------------------------------
@dataclass
class Evaluation:
    ticker: str
    valid: bool
    checks: dict[str, bool | None]
    reasons: list[str]
    snapshot: Snapshot
    evaluated_at: datetime

    def to_json(self) -> dict:
        return {
            "ticker": self.ticker,
            "valid": self.valid,
            "checks": self.checks,
            "reasons": self.reasons,
            "evaluated_at": self.evaluated_at.isoformat(timespec="seconds"),
            "snapshot": self.snapshot.to_json(),
        }


def evaluate(
    s: Snapshot,
    now: datetime,
    ignore_time: bool = False,
    hod_tolerance_pct: float = config.HOD_TOLERANCE_PCT,
    start: dtime | None = None,
) -> Evaluation:
    checks: dict[str, bool | None] = {}
    reasons: list[str] = []
    p = s.price

    def put(name: str, ok: bool | None, fail_reason: str, missing: str) -> None:
        checks[name] = ok
        if ok is None:
            reasons.append(f"{name}: no data ({missing})")
        elif not ok:
            reasons.append(fail_reason)

    # 1. price above yesterday's high
    if p is None or s.yesterday_high is None:
        put("above_yesterday_high", None, "", "price or yesterday's high missing")
    else:
        put("above_yesterday_high", p > s.yesterday_high,
            f"price {p:.2f} not above yesterday's high {s.yesterday_high:.2f}", "")

    # 2. yesterday's close above the 200 SMA
    if s.yesterday_close is None or s.sma200 is None:
        put("yesterday_close_above_sma200", None, "", "yesterday's close or 200 SMA missing")
    else:
        put("yesterday_close_above_sma200", s.yesterday_close > s.sma200,
            f"yesterday's close {s.yesterday_close:.2f} not above 200 SMA {s.sma200:.2f}", "")

    # 3a. price above today's pre-market high
    if p is None or s.premarket_high is None:
        put("above_premarket_high", None, "", "price or pre-market high missing")
    else:
        put("above_premarket_high", p > s.premarket_high,
            f"price {p:.2f} not above pre-market high {s.premarket_high:.2f}", "")

    # 3b. price at the high of day (within tolerance): the stock is breaking out now
    if p is None or s.day_high is None:
        put("at_high_of_day", None, "", "price or high of day missing")
    else:
        floor = s.day_high * (1.0 - hod_tolerance_pct / 100.0)
        put("at_high_of_day", p >= floor,
            f"price {p:.2f} below high of day {s.day_high:.2f} (tolerance {hod_tolerance_pct}%)", "")

    # Time gate: weekday, regular session, at/after the start time
    start = start or _parse_hhmm(config.STRATEGY_START_ET)
    in_window = now.weekday() < 5 and start <= now.time() < dtime(16, 0)
    in_session = (s.market_state or "REGULAR") == "REGULAR"
    if ignore_time:
        checks["after_start_in_regular_session"] = None
    else:
        ok = in_window and in_session
        checks["after_start_in_regular_session"] = ok
        if not ok:
            reasons.append(
                f"outside the window: now {now.strftime('%a %H:%M')} ET, market state {s.market_state or '?'}, "
                f"window starts {start.strftime('%H:%M')} ET on weekdays"
            )

    required = [checks[k] for k in CHECK_ORDER if not (ignore_time and k == "after_start_in_regular_session")]
    valid = all(v is True for v in required)
    return Evaluation(s.ticker, valid, checks, reasons, s, now)


# Persistence -----------------------------------------------------------------------
def premarket_file(scan_date: date, output_dir: Path | None = None) -> Path:
    return (output_dir or config.OUTPUT_DIR) / f"premarket_scan_{scan_date.isoformat()}.json"


def strategy_file(scan_date: date, output_dir: Path | None = None) -> Path:
    return (output_dir or config.OUTPUT_DIR) / f"trend_join_{scan_date.isoformat()}.json"


def load_premarket_results(scan_date: date, path: Path | None = None) -> tuple[list[dict], Path]:
    path = path or premarket_file(scan_date)
    if not path.exists():
        raise FileNotFoundError(
            f"No pre-market scan for {scan_date} at {path}. Run scan.py first or pass --tickers."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("results") or []), path


def _load_existing_signals(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {s["ticker"]: s for s in payload.get("signals") or []}
    except (OSError, ValueError, KeyError) as exc:
        log.warning("could not read existing %s: %s", path, exc)
        return {}


def _signal_record(ev: Evaluation, pre: dict, now_iso: str) -> dict:
    s = ev.snapshot
    return {
        "ticker": ev.ticker,
        "strategy": STRATEGY_NAME,
        "first_triggered_at": now_iso,
        "last_valid_at": now_iso,
        "entry_price": round(float(s.price), 4),
        "price_source": s.price_source,
        "price_delayed": s.price_delayed,
        "yesterday_high": round(float(s.yesterday_high), 4),
        "yesterday_close": round(float(s.yesterday_close), 4),
        "sma200": round(float(s.sma200), 4),
        "premarket_high": round(float(s.premarket_high), 4),
        "high_of_day": round(float(s.day_high), 4),
        "levels_source": s.levels_source,
        "tv_symbol": s.tv_symbol,
        "gap_pct": pre.get("gap_pct"),
        "premarket_volume": pre.get("premarket_volume"),
        "catalyst": pre.get("catalyst"),
        "catalyst_url": pre.get("catalyst_url"),
    }


# Orchestration ---------------------------------------------------------------------
def run_strategy_scan(
    scan_date: date | None = None,
    tickers: list[str] | None = None,
    input_file: Path | None = None,
    output_dir: Path | None = None,
    ignore_time: bool = False,
) -> tuple[dict, Path]:
    """Evaluate today's pre-market tickers, merge new signals into the day's file, save."""
    now = datetime.now(ET)
    scan_date = scan_date or now.date()
    warnings: list[str] = []

    if tickers:
        pre_by_ticker = {t.upper(): {"ticker": t.upper()} for t in tickers}
        source = "command line"
    else:
        results, src = load_premarket_results(scan_date, input_file)
        pre_by_ticker = {r["ticker"]: r for r in results}
        source = str(src)
    symbols = list(pre_by_ticker)
    log.info("%s: evaluating %d ticker(s) from %s", STRATEGY_NAME, len(symbols), source)

    snapshots = build_snapshots(symbols, scan_date, warnings)
    delayed = [s.ticker for s in snapshots if s.price_delayed]
    if delayed:
        warnings.append(f"prices for {', '.join(delayed)} are delayed 15 minutes")
    evaluations = [evaluate(s, now, ignore_time=ignore_time) for s in snapshots]
    if ignore_time:
        warnings.append("time gate ignored (--ignore-time): signals may be outside the 10:00 ET window")

    out_path = strategy_file(scan_date, output_dir)
    signals = _load_existing_signals(out_path)
    now_iso = now.isoformat(timespec="seconds")
    new_signals: list[str] = []
    for ev in evaluations:
        if not ev.valid:
            continue
        if ev.ticker in signals:
            signals[ev.ticker]["last_valid_at"] = now_iso
            signals[ev.ticker]["last_price"] = round(float(ev.snapshot.price), 4)
        else:
            signals[ev.ticker] = _signal_record(ev, pre_by_ticker.get(ev.ticker, {}), now_iso)
            new_signals.append(ev.ticker)

    result = {
        "strategy": STRATEGY_NAME,
        "date": scan_date.isoformat(),
        "generated_at": now_iso,
        "source": source,
        "data_source": {
            "levels": config.STRATEGY_DATA_SOURCE,
            "price": "tradingview (streaming)" if any(s.price_source == "tradingview" for s in snapshots) else "yahoo",
        },
        "criteria": {
            "above_yesterday_high": "price > yesterday's daily high",
            "yesterday_close_above_sma200": "yesterday's close > 200-day SMA",
            "above_premarket_high": "price > today's pre-market high",
            "at_high_of_day": f"price within {config.HOD_TOLERANCE_PCT}% of the high of day",
            "window": f"weekdays {config.STRATEGY_START_ET}-16:00 ET, regular session",
        },
        "warnings": warnings,
        "new_signals": new_signals,
        "signals": sorted(signals.values(), key=lambda s: s["first_triggered_at"]),
        "scanned": [ev.to_json() for ev in evaluations],
    }
    result["report_text"] = format_message(result)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("%d valid signal(s), %d new -> %s", len(signals), len(new_signals), out_path)
    return result, out_path


# Formatting ------------------------------------------------------------------------
def _f(v, nd: int = 2) -> str:
    return "n/a" if v is None else f"{float(v):.{nd}f}"


def format_table(result: dict) -> str:
    lines = [
        f"{STRATEGY_NAME.upper()}  {result['date']}   generated: {result['generated_at']}   "
        f"levels: {result['data_source']['levels']}   price: {result['data_source']['price']}"
    ]
    for w in result.get("warnings") or []:
        lines.append(f"WARNING: {w}")
    lines.append("")
    header = (
        f"{'TICKER':<8}{'PRICE':>9}{'Y-HIGH':>9}{'Y-CLOSE':>9}{'SMA200':>9}"
        f"{'PM-HIGH':>9}{'HOD':>9}  {'1':>1} {'2':>1} {'3a':>2} {'3b':>2}  RESULT"
    )
    lines.append(header)
    lines.append("-" * len(header))
    mark = {True: "Y", False: "-", None: "?"}
    for ev in result.get("scanned") or []:
        s, c = ev["snapshot"], ev["checks"]
        verdict = "ENTRY SIGNAL" if ev["valid"] else (ev["reasons"][0] if ev["reasons"] else "no")
        lines.append(
            f"{ev['ticker']:<8}{_f(s['price']):>9}{_f(s['yesterday_high']):>9}{_f(s['yesterday_close']):>9}"
            f"{_f(s['sma200']):>9}{_f(s['premarket_high']):>9}{_f(s['day_high']):>9}  "
            f"{mark[c['above_yesterday_high']]:>1} {mark[c['yesterday_close_above_sma200']]:>1} "
            f"{mark[c['above_premarket_high']]:>2} {mark[c['at_high_of_day']]:>2}  {verdict}"
        )
    lines.append("")
    lines.append(
        f"{len(result.get('signals') or [])} signal(s) today, {len(result.get('new_signals') or [])} new this run."
    )
    return "\n".join(lines)


def format_message(result: dict) -> str:
    """Plain-text report for automated delivery (Telegram, email, ...)."""
    when = datetime.fromisoformat(result["generated_at"]).strftime("%a %Y-%m-%d %H:%M ET")
    lines = [f"{STRATEGY_NAME} - {when}"]
    signals = result.get("signals") or []
    new = set(result.get("new_signals") or [])
    if not signals:
        lines.append("No valid entry signals yet.")
    else:
        lines.append(f"{len(signals)} entry signal(s) today ({len(new)} new):")
        for s in signals:
            tag = "NEW " if s["ticker"] in new else ""
            lines.append(
                f"- {tag}{s['ticker']} entry {_f(s['entry_price'])} "
                f"(y-high {_f(s['yesterday_high'])}, PM high {_f(s['premarket_high'])}, HOD {_f(s['high_of_day'])}, "
                f"y-close {_f(s['yesterday_close'])} > SMA200 {_f(s['sma200'])})"
            )
            if s.get("catalyst"):
                lines.append(f"  catalyst: {s['catalyst']}")
    scanned = result.get("scanned") or []
    failed = [ev["ticker"] for ev in scanned if not ev["valid"]]
    lines.append(f"Checked {len(scanned)} pre-market ticker(s); not qualifying: {', '.join(failed) if failed else 'none'}.")
    for w in result.get("warnings") or []:
        lines.append(f"Note: {w}")
    return "\n".join(lines)
