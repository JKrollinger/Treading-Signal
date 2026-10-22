"""Backtest of the Trend-join Long rules on hourly bars, version 1 and version 2.

Version 1 (the live rules):

* yesterday's close above the 200-day SMA
* a bar closes above yesterday's high while at the high of the day (within ``hod_tol_pct``)
* entry at that close; stop = entry - ``stop_atr_mult`` x ATR(14, daily, as of yesterday);
  target = entry + ``target_r`` x risk; exit at stop/target or after ``max_hold_days``

Version 2 adds switchable layers (each ``None``/``False`` = off):

* regime filter: daily ADX(14) >= ``adx_min``; yesterday's ATR(14) not above
  ``atr_ratio_max`` x its 100-day median; 200-day SMA rising over ``sma_slope_days``
* trend exit: Chandelier trailing stop ``trail_atr_mult`` x ATR below the highest high
  since entry (stop only ever rises); optional half position taken at ``partial_r`` R;
  with a trailing stop the fixed target is off unless ``target_r`` is kept
* time filter: entries only between ``session_utc`` hours (UTC) and never on Friday at or
  after ``friday_cutoff_utc``
* news filter: no entries in a window around US non-farm payrolls (first Friday of the
  month, 12:00-14:00 UTC) and FOMC decisions (17:30-20:00 UTC on the scheduled dates)

One trade per symbol per day, no new entry while a trade is open, a flat round-trip cost
of ``cost_pct`` percent per trade. "Day" is the calendar day of the data's own clock
(broker server time for MT5, the exchange zone for Yahoo).
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from statistics import mean

import numpy as np
import pandas as pd

from . import config
from .filters import FOMC_DATES, in_recurring_blackout  # noqa: F401 - shared with the live watch

log = logging.getLogger(__name__)


@dataclass
class Params:
    name: str = "v1"
    stop_atr_mult: float = config.STOP_ATR_MULT
    target_r: float | None = config.TARGET_R_MULT
    hod_tol_pct: float = config.HOD_TOLERANCE_PCT
    max_hold_days: int = 5
    cost_pct: float = 0.03          # round-trip cost in percent of the entry price
    atr_period: int = config.ATR_PERIOD
    sma_period: int = 200
    # version 2 layers
    adx_min: float | None = None
    atr_ratio_max: float | None = None
    sma_slope_days: int | None = None
    trail_atr_mult: float | None = None
    partial_r: float | None = None
    session_utc: tuple[int, int] | None = None     # entry hours [start, end) in UTC
    friday_cutoff_utc: int | None = None
    news_blackout: bool = False
    # which side may trade: "long" (Trend-join Long), "short" (the mirror, Trend-join Short)
    # or "both" (the 200-day SMA picks the side each day; still one trade at a time)
    side: str = "long"
    # intraday versions: the "day" becomes a bar of this many minutes (None = calendar day),
    # so SMA200, ATR14, "yesterday's high" and "high of day" are taken on that bar size and
    # max_hold_days counts those bars
    level_minutes: int | None = None
    cost_abs: float | None = None      # round-trip cost in price units (spread + commission); overrides cost_pct
    hod_tol_atr: float | None = None   # "at the high of the period" tolerance in ATRs; overrides hod_tol_pct


@dataclass
class Trade:
    symbol: str
    day: str
    entry_time: str
    entry: float
    stop: float
    target: float | None
    exit_time: str
    exit_price: float
    exit_reason: str      # stop | target | trail | time | open
    r_multiple: float     # result in units of initial risk, after costs
    pct_return: float     # percent of entry, after costs
    bars_held: int
    partial_taken: bool = False
    side: str = "long"


@dataclass
class SymbolResult:
    symbol: str
    source: str
    variant: str
    bars: int
    first_bar: str
    last_bar: str
    days_tested: int
    trades: list[Trade] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    skipped: dict = field(default_factory=dict)   # why candidate days produced no entry

    def to_json(self) -> dict:
        return asdict(self)


# Variants -------------------------------------------------------------------------------
def variants() -> list[Params]:
    base = dict(stop_atr_mult=config.STOP_ATR_MULT, target_r=config.TARGET_R_MULT,
                hod_tol_pct=config.HOD_TOLERANCE_PCT, max_hold_days=5, cost_pct=0.03)
    regime = dict(adx_min=20.0, atr_ratio_max=2.0, sma_slope_days=20)
    trail = dict(trail_atr_mult=3.0, partial_r=1.0, target_r=None, max_hold_days=10)
    timef = dict(session_utc=(7, 20), friday_cutoff_utc=15, news_blackout=True)
    return [
        Params(name="v1 (กฎเดิม)", **base),
        Params(name="v2-regime (กรองสภาพตลาด)", **{**base, **regime}),
        Params(name="v2-trail (exit ตามแนวโน้ม)", **{**base, **trail}),
        Params(name="v2-time (กรองเวลา/ข่าว)", **{**base, **timef}),
        Params(name="v2-all (ทั้งหมด)", **{**base, **regime, **trail, **timef}),
    ]


# Data -----------------------------------------------------------------------------
def load_yahoo_hourly(symbol: str, days: int = 730) -> pd.DataFrame:
    import yfinance as yf

    df = yf.download(symbol, period=f"{min(days, 730)}d", interval="1h", auto_adjust=False,
                     progress=False, multi_level_index=False)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns=str.lower)[["open", "high", "low", "close"]].dropna()
    return df[(df["high"] > 0) & (df["low"] > 0)]


def load_mt5_hourly(symbol: str, days: int = 730) -> pd.DataFrame:
    from . import mt5_source

    df = mt5_source.hourly_history(symbol, days)
    if df.empty:
        return df
    out = df[["open", "high", "low", "close"]].copy()
    out.attrs["tz_offset_hours"] = df.attrs.get("tz_offset_hours", 0.0)
    return out


def load_mt5_bars(symbol: str, timeframe: str = "M5", count: int = 99_999) -> pd.DataFrame:
    """The latest ``count`` bars of any MT5 timeframe (the terminal keeps at most its
    "max bars in chart", 100,000 by default, so M5 reaches back about 16 months)."""
    from . import mt5_source

    if not mt5_source.connect():
        return pd.DataFrame()
    mt5 = mt5_source._mt5()
    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, f"TIMEFRAME_{timeframe}"), 0, count)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df.index = pd.to_datetime(df["time"], unit="s")          # naive = server time
    out = df[["open", "high", "low", "close"]].copy()
    out.attrs["tz_offset_hours"] = mt5_source.server_offset_hours(symbol)
    return out


# Indicators --------------------------------------------------------------------------
def _wilder(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(alpha=1.0 / n, adjust=False).mean()


def period_keys(bars: pd.DataFrame, p: Params):
    """The 'day' each bar belongs to: its calendar date, or the start of its level bar."""
    if p.level_minutes:
        return bars.index.floor(f"{int(p.level_minutes)}min")
    return bars.index.date


def daily_levels(bars: pd.DataFrame, p: Params) -> pd.DataFrame:
    dates = period_keys(bars, p)
    g = bars.groupby(dates)
    daily = pd.DataFrame({"high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last()})
    daily["sma"] = daily["close"].rolling(p.sma_period).mean()
    prev_close = daily["close"].shift(1)
    tr = pd.concat(
        [daily["high"] - daily["low"], (daily["high"] - prev_close).abs(), (daily["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    daily["atr"] = tr.rolling(p.atr_period).mean()
    daily["atr_med100"] = daily["atr"].rolling(100).median()
    # ADX (Wilder)
    up = daily["high"].diff()
    down = -daily["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    atr_w = _wilder(tr, 14)
    plus_di = 100 * _wilder(plus_dm, 14) / atr_w
    minus_di = 100 * _wilder(minus_dm, 14) / atr_w
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    daily["adx"] = _wilder(dx.fillna(0.0), 14)
    if p.sma_slope_days:
        daily["sma_slope"] = daily["sma"] - daily["sma"].shift(p.sma_slope_days)
    for col in ("high", "low", "close", "sma", "atr", "adx", "atr_med100", "sma_slope"):
        if col in daily:
            daily[f"y_{col}"] = daily[col].shift(1)
    return daily


def in_news_blackout(d: date, utc_hour: int) -> bool:
    return in_recurring_blackout(d, utc_hour)


# Engine -------------------------------------------------------------------------------
def run_symbol(symbol: str, bars: pd.DataFrame, p: Params, source: str) -> SymbolResult:
    bars = bars.sort_index()
    if len(bars) < 24 * 30:
        return SymbolResult(symbol, source, p.name, len(bars), "", "", 0, [], {"trades": 0, "note": "not enough bars"})
    daily = daily_levels(bars, p)
    date_pos = {d: i for i, d in enumerate(daily.index)}
    bar_dates = period_keys(bars, p)
    if p.level_minutes:
        bar_dates = list(bar_dates)                 # Timestamps, matching daily.index
    if bars.index.tz is not None:
        utc_hours = bars.index.tz_convert("UTC").hour.to_numpy()
    else:
        offset = float(bars.attrs.get("tz_offset_hours", 0.0))
        utc_hours = ((bars.index.hour - offset) % 24).astype(int).to_numpy()
    weekdays = bars.index.weekday.to_numpy()
    highs, lows, closes = bars["high"].to_numpy(), bars["low"].to_numpy(), bars["close"].to_numpy()
    n = len(bars)

    trades: list[Trade] = []
    skipped: Counter = Counter()
    cur_day = None
    hod, lod = -math.inf, math.inf
    y = None
    day_side: str | None = None      # "long" / "short" when today may trade, else None
    traded_today = False
    open_until = -1
    days_tested = 0
    allowed = {"long": ("long",), "short": ("short",), "both": ("long", "short")}[p.side]

    for i in range(n):
        d = bar_dates[i]
        if d != cur_day:
            cur_day, hod, lod, traded_today = d, -math.inf, math.inf, False
            row = daily.loc[d] if d in daily.index else None
            y = None
            if row is not None and not any(pd.isna(row[k]) for k in ("y_high", "y_low", "y_close", "y_sma", "y_atr")):
                y = row
                days_tested += 1
            day_side = None
            if y is not None:
                # the 200-day SMA decides the side: above -> only longs, below -> only shorts
                trend = "long" if float(y["y_close"]) > float(y["y_sma"]) else "short"
                slope = float(y["y_sma_slope"]) if p.sma_slope_days and not pd.isna(y.get("y_sma_slope", np.nan)) else np.nan
                if trend not in allowed:
                    skipped["below_sma200" if trend == "short" else "above_sma200"] += 1
                elif p.adx_min is not None and (pd.isna(y["y_adx"]) or float(y["y_adx"]) < p.adx_min):
                    skipped["adx_low"] += 1
                elif p.atr_ratio_max is not None and not pd.isna(y["y_atr_med100"]) and \
                        float(y["y_atr"]) > p.atr_ratio_max * float(y["y_atr_med100"]):
                    skipped["volatility_extreme"] += 1
                elif p.sma_slope_days and (np.isnan(slope) or (slope <= 0 if trend == "long" else slope >= 0)):
                    skipped["sma_falling" if trend == "long" else "sma_rising"] += 1
                else:
                    day_side = trend
        hod = max(hod, highs[i])
        lod = min(lod, lows[i])
        if i <= open_until or traded_today or day_side is None:
            continue
        c = closes[i]
        is_long = day_side == "long"
        if p.hod_tol_atr is not None:
            tol_abs = p.hod_tol_atr * float(y["y_atr"])
            at_extreme = (c >= hod - tol_abs) if is_long else (c <= lod + tol_abs)
        else:
            at_extreme = (c >= hod * (1.0 - p.hod_tol_pct / 100.0)) if is_long else \
                (c <= lod * (1.0 + p.hod_tol_pct / 100.0))
        if is_long:
            # breakout above yesterday's high while at the high of the day
            if not (c > float(y["y_high"]) and at_extreme):
                continue
        elif not (c < float(y["y_low"]) and at_extreme):
            # mirror: breakdown below yesterday's low while at the low of the day
            continue
        h = int(utc_hours[i])
        if p.session_utc and not (p.session_utc[0] <= h < p.session_utc[1]):
            skipped["outside_session"] += 1
            continue
        if p.friday_cutoff_utc is not None and weekdays[i] == 4 and h >= p.friday_cutoff_utc:
            skipped["friday_late"] += 1
            continue
        if p.news_blackout and in_news_blackout(bars.index[i].date(), h):
            skipped["news_blackout"] += 1
            continue

        entry = float(c)
        atr = float(y["y_atr"])
        sign = 1.0 if is_long else -1.0          # +1 long, -1 short: profit = sign x (exit - entry)
        stop = entry - sign * p.stop_atr_mult * atr
        risk = abs(entry - stop)
        initial_stop = stop
        if stop <= 0 or risk <= 0:
            continue
        target = entry + sign * p.target_r * risk if p.target_r else None
        partial_level = entry + sign * p.partial_r * risk if p.partial_r else None
        last_day_pos = date_pos[d] + p.max_hold_days
        extreme = entry                           # highest high (long) / lowest low (short) since entry
        partial_price = None
        exit_price, exit_reason, j_exit = None, "open", n - 1
        for j in range(i + 1, n):
            if date_pos.get(bar_dates[j], 10**9) > last_day_pos:
                exit_price, exit_reason, j_exit = float(closes[j - 1]), "time", j - 1
                break
            # adverse extreme of the bar (low for a long, high for a short) checked first
            adverse, favourable = (lows[j], highs[j]) if is_long else (highs[j], lows[j])
            if sign * (adverse - stop) <= 0:
                moved = sign * (stop - initial_stop) > 0
                exit_price, exit_reason, j_exit = stop, "trail" if (p.trail_atr_mult and moved) else "stop", j
                break
            if target is not None and sign * (favourable - target) >= 0:
                exit_price, exit_reason, j_exit = target, "target", j
                break
            if partial_level is not None and partial_price is None and sign * (favourable - partial_level) >= 0:
                partial_price = partial_level
            if p.trail_atr_mult:
                if is_long:
                    extreme = max(extreme, highs[j])
                    stop = max(stop, extreme - p.trail_atr_mult * atr)
                else:
                    extreme = min(extreme, lows[j])
                    stop = min(stop, extreme + p.trail_atr_mult * atr)
        if exit_price is None:
            exit_price = float(closes[-1])

        cost_pct = p.cost_abs / entry * 100.0 if p.cost_abs is not None else p.cost_pct
        cost_r = (cost_pct / 100.0) * entry / risk
        if partial_price is not None:
            r = 0.5 * sign * (partial_price - entry) / risk + 0.5 * sign * (exit_price - entry) / risk - cost_r
            pct = (0.5 * sign * (partial_price / entry - 1.0) * 100.0
                   + 0.5 * sign * (exit_price / entry - 1.0) * 100.0 - cost_pct)
        else:
            r = sign * (exit_price - entry) / risk - cost_r
            pct = sign * (exit_price / entry - 1.0) * 100.0 - cost_pct
        trades.append(Trade(
            symbol, str(d), bars.index[i].isoformat(), entry, initial_stop, target,
            bars.index[j_exit].isoformat(), exit_price, exit_reason, float(r), float(pct), int(j_exit - i),
            partial_price is not None, day_side,
        ))
        traded_today, open_until = True, j_exit

    return SymbolResult(symbol, source, p.name, n, bars.index[0].isoformat(), bars.index[-1].isoformat(),
                        days_tested, trades, compute_metrics(trades), dict(skipped))


def compute_metrics(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0}
    rs = [t.r_multiple for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    cum = np.cumsum(rs)
    drawdown = float((cum - np.maximum.accumulate(cum)).min())
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    pf = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
    return {
        "trades": len(trades),
        "win_rate_pct": len(wins) / len(rs) * 100.0,
        "avg_r": mean(rs),
        "total_r": sum(rs),
        "profit_factor": pf,
        "max_drawdown_r": drawdown,
        "avg_win_r": mean(wins) if wins else 0.0,
        "avg_loss_r": mean(losses) if losses else 0.0,
        "avg_pct": mean(t.pct_return for t in trades),
        "total_pct": sum(t.pct_return for t in trades),
        "exits": dict(Counter(t.exit_reason for t in trades)),
        "avg_bars_held": mean(t.bars_held for t in trades),
    }


def overall_metrics(results: list[SymbolResult]) -> dict:
    trades = [t for r in results for t in r.trades]
    m = compute_metrics(trades)
    m["symbols"] = len(results)
    m["days_tested"] = sum(r.days_tested for r in results)
    return m


# Reporting ----------------------------------------------------------------------------
def _f(v, nd=2) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float) and math.isinf(v):
        return "inf"
    return f"{v:.{nd}f}"


def format_table(results: list[SymbolResult], overall: dict, p: Params, source: str) -> str:
    lines = [
        f"TREND-JOIN LONG BACKTEST [{p.name}]  source={source}  stop={p.stop_atr_mult}xATR{p.atr_period}  "
        f"target={p.target_r}R  trail={p.trail_atr_mult}  hold<={p.max_hold_days}d  cost={p.cost_pct}%",
        f"{'SYMBOL':<16}{'DAYS':>6}{'TRADES':>8}{'WIN%':>7}{'AVG R':>8}{'TOTAL R':>9}{'PF':>7}{'MAXDD R':>9}  EXITS",
    ]
    lines.append("-" * 96)
    for r in results:
        m = r.metrics
        if not m.get("trades"):
            lines.append(f"{r.symbol:<16}{r.days_tested:>6}{0:>8}  {m.get('note', 'no trades')}")
            continue
        lines.append(
            f"{r.symbol:<16}{r.days_tested:>6}{m['trades']:>8}{m['win_rate_pct']:>7.1f}{m['avg_r']:>8.2f}"
            f"{m['total_r']:>9.1f}{_f(m['profit_factor']):>7}{m['max_drawdown_r']:>9.1f}  {m['exits']}"
        )
    lines.append("-" * 96)
    o = overall
    if o.get("trades"):
        lines.append(
            f"{'ALL':<16}{o['days_tested']:>6}{o['trades']:>8}{o['win_rate_pct']:>7.1f}{o['avg_r']:>8.2f}"
            f"{o['total_r']:>9.1f}{_f(o['profit_factor']):>7}{o['max_drawdown_r']:>9.1f}  {o['exits']}"
        )
    return "\n".join(lines)


def format_comparison(by_variant: dict[str, tuple[list[SymbolResult], dict]], source: str) -> str:
    names = list(by_variant)
    symbols = [r.symbol for r in by_variant[names[0]][0]]
    lines = [f"COMPARISON  source={source}   (trades | win% | avg R | total R | PF | maxDD R)"]
    header = f"{'SYMBOL':<10}" + "".join(f"{n[:22]:>24}" for n in names)
    lines.append(header)
    lines.append("-" * len(header))
    for i, s in enumerate(symbols + ["ALL"]):
        cells = []
        for n in names:
            results, overall = by_variant[n]
            m = overall if s == "ALL" else results[i].metrics
            if not m.get("trades"):
                cells.append(f"{'no trades':>24}")
            else:
                cells.append(f"{m['trades']:>4}|{m['win_rate_pct']:>3.0f}|{m['avg_r']:>5.2f}|{m['total_r']:>6.1f}|"
                             f"{_f(m['profit_factor'], 2):>4}|{m['max_drawdown_r']:>5.1f}")
        lines.append(f"{s:<10}" + "".join(f"{c:>24}" for c in cells))
    return "\n".join(lines)


def verdict_th(avg_r: float, pf: float, trades: int) -> str:
    if trades < 20:
        return "จำนวนเทรดน้อยเกินไปที่จะสรุป"
    if avg_r >= 0.15 and pf >= 1.3:
        return "กฎนี้มีขอบได้เปรียบชัดเจนในช่วงที่ทดสอบ"
    if avg_r > 0.05:
        return "มีขอบได้เปรียบเล็กน้อย ยังไม่ควรเปิดอัตโนมัติด้วยเงินจริงจนกว่าจะทดสอบเดโม"
    if avg_r > -0.05:
        return "เกือบเท่าทุน ค่าธรรมเนียมและสลิปเพจจะกินกำไรหมด"
    return "ไม่มีขอบได้เปรียบ ขาดทุนโดยเฉลี่ย ไม่ควรเปิดอัตโนมัติ"


def format_message_th(results: list[SymbolResult], overall: dict, p: Params, source: str, names: dict[str, str]) -> str:
    src = {"yahoo": "Yahoo Finance รายชั่วโมง", "mt5": "MT5 ของโบรกเกอร์ รายชั่วโมง"}.get(source, source)
    lines = [
        f"📊 ผลทดสอบย้อนหลังกฎ Trend-join Long [{p.name}] (ข้อมูล {src} ประมาณ 2 ปี)",
        f"ตัดขาดทุน {p.stop_atr_mult:g} ATR | " + (f"เลื่อนจุดตัดขาดทุนตาม {p.trail_atr_mult:g} ATR" if p.trail_atr_mult else f"เป้าหมาย {p.target_r:g}R")
        + f" | ถือไม่เกิน {p.max_hold_days} วัน | ค่าธรรมเนียม {p.cost_pct}% ต่อเทรด",
        "",
    ]
    for r in results:
        m = r.metrics
        label = names.get(r.symbol, r.symbol)
        if not m.get("trades"):
            lines.append(f"• {label}: ไม่มีสัญญาณในช่วงที่ทดสอบ")
            continue
        lines.append(
            f"• {label}: {m['trades']} เทรด ชนะ {m['win_rate_pct']:.0f}% เฉลี่ย {m['avg_r']:+.2f}R ต่อเทรด "
            f"(รวม {m['total_r']:+.1f}R) ขาดทุนสะสมลึกสุด {m['max_drawdown_r']:.1f}R"
        )
    o = overall
    if o.get("trades"):
        lines += [
            "",
            f"รวมทุกตัว: {o['trades']} เทรด ชนะ {o['win_rate_pct']:.0f}% เฉลี่ย {o['avg_r']:+.2f}R ต่อเทรด "
            f"profit factor {_f(o['profit_factor'])} ขาดทุนสะสมลึกสุด {o['max_drawdown_r']:.1f}R",
            f"สรุป: {verdict_th(o['avg_r'], o['profit_factor'], o['trades'])}",
        ]
    return "\n".join(lines)


def format_comparison_th(by_variant: dict[str, tuple[list[SymbolResult], dict]], source: str,
                         names: dict[str, str]) -> str:
    src = {"yahoo": "Yahoo", "mt5": "MT5 ของโบรกเกอร์"}.get(source, source)
    variant_names = list(by_variant)
    base_name = variant_names[0]
    lines = [f"📊 เทียบกฎเดิม (v1) กับกฎเวอร์ชัน 2 (ข้อมูล {src} รายชั่วโมง ประมาณ 2 ปี)",
             "ตัวเลข = เทรด / ชนะ% / กำไรเฉลี่ยต่อเทรด (R) / กำไรรวม (R) / ขาดทุนสะสมลึกสุด (R)", ""]
    for i, sym in enumerate([r.symbol for r in by_variant[base_name][0]]):
        lines.append(f"• {names.get(sym, sym)}")
        for n in variant_names:
            m = by_variant[n][0][i].metrics
            if not m.get("trades"):
                lines.append(f"   {n}: ไม่มีเทรด")
            else:
                lines.append(f"   {n}: {m['trades']} / {m['win_rate_pct']:.0f}% / {m['avg_r']:+.2f}R / "
                             f"{m['total_r']:+.1f}R / {m['max_drawdown_r']:.1f}R")
    lines.append("")
    lines.append("รวมทุกตัว")
    best_name, best_avg = None, -math.inf
    for n in variant_names:
        o = by_variant[n][1]
        if not o.get("trades"):
            lines.append(f"   {n}: ไม่มีเทรด")
            continue
        lines.append(f"   {n}: {o['trades']} / {o['win_rate_pct']:.0f}% / {o['avg_r']:+.2f}R / {o['total_r']:+.1f}R / "
                     f"{o['max_drawdown_r']:.1f}R / PF {_f(o['profit_factor'])}")
        if o["trades"] >= 20 and o["avg_r"] > best_avg:
            best_name, best_avg = n, o["avg_r"]
    v1 = by_variant[base_name][1]
    lines.append("")
    if best_name and best_name != base_name and v1.get("trades"):
        lines.append(f"สรุป: {best_name} ให้กำไรเฉลี่ยต่อเทรดดีที่สุด ({best_avg:+.2f}R เทียบกับกฎเดิม {v1['avg_r']:+.2f}R)")
    elif v1.get("trades"):
        lines.append(f"สรุป: กฎเดิมยังดีที่สุด ({v1['avg_r']:+.2f}R ต่อเทรด) ส่วนเพิ่มของ v2 ไม่ช่วยในช่วงที่ทดสอบ")
    lines.append("ข้อจำกัด: ตัวกรองข่าวครอบคลุมเฉพาะ NFP และ FOMC, ไม่รวมสเปรดจริง/สลิปเพจ, และผลในอดีตไม่รับประกันอนาคต")
    return "\n".join(lines)
