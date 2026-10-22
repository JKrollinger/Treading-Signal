"""Backtest of the Trend-join Long rules on hourly bars.

Rules, evaluated bar by bar (one trade per symbol per day, no new entry while a trade
is open):

* yesterday's close above the 200-day SMA (long-term uptrend)
* the bar closes above yesterday's high
* that close is at the high of the day so far (within ``hod_tol_pct``), i.e. a breakout
* entry at that bar's close; stop = entry - ``stop_atr_mult`` x ATR(14, daily, as of
  yesterday); target = entry + ``target_r`` x (entry - stop)
* exit at the stop or target (if a bar touches both, the stop is assumed first), or at the
  close of the last bar after ``max_hold_days`` trading days
* a flat round-trip cost of ``cost_pct`` percent is charged to every trade

"Day" means the calendar day in the data's own time zone (broker server time for MT5,
the exchange zone for Yahoo), so the levels match what the live scanner sees.
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import asdict, dataclass, field
from statistics import mean

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger(__name__)


@dataclass
class Params:
    stop_atr_mult: float = config.STOP_ATR_MULT
    target_r: float = config.TARGET_R_MULT
    hod_tol_pct: float = config.HOD_TOLERANCE_PCT
    max_hold_days: int = 5
    cost_pct: float = 0.03          # round-trip cost in percent of the entry price
    atr_period: int = config.ATR_PERIOD
    sma_period: int = 200


@dataclass
class Trade:
    symbol: str
    day: str
    entry_time: str
    entry: float
    stop: float
    target: float
    exit_time: str
    exit_price: float
    exit_reason: str      # stop | target | time | open
    r_multiple: float     # result in units of initial risk, after costs
    pct_return: float     # percent of entry, after costs
    bars_held: int


@dataclass
class SymbolResult:
    symbol: str
    source: str
    bars: int
    first_bar: str
    last_bar: str
    days_tested: int
    trades: list[Trade] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        d = asdict(self)
        return d


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
    return df[["open", "high", "low", "close"]] if not df.empty else df


# Engine -------------------------------------------------------------------------------
def daily_levels(bars: pd.DataFrame, p: Params) -> pd.DataFrame:
    dates = bars.index.date
    g = bars.groupby(dates)
    daily = pd.DataFrame({"high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last()})
    daily["sma"] = daily["close"].rolling(p.sma_period).mean()
    prev_close = daily["close"].shift(1)
    tr = pd.concat(
        [daily["high"] - daily["low"], (daily["high"] - prev_close).abs(), (daily["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    daily["atr"] = tr.rolling(p.atr_period).mean()
    for col in ("high", "close", "sma", "atr"):
        daily[f"y_{col}"] = daily[col].shift(1)
    return daily


def run_symbol(symbol: str, bars: pd.DataFrame, p: Params, source: str) -> SymbolResult:
    bars = bars.sort_index()
    if len(bars) < 24 * 30:
        return SymbolResult(symbol, source, len(bars), "", "", 0, [], {"trades": 0, "note": "not enough bars"})
    daily = daily_levels(bars, p)
    date_pos = {d: i for i, d in enumerate(daily.index)}
    bar_dates = bars.index.date
    highs, lows, closes = bars["high"].to_numpy(), bars["low"].to_numpy(), bars["close"].to_numpy()
    n = len(bars)

    trades: list[Trade] = []
    cur_day = None
    hod = -math.inf
    y = None
    day_ok = False
    traded_today = False
    open_until = -1
    days_tested = 0

    for i in range(n):
        d = bar_dates[i]
        if d != cur_day:
            cur_day, hod, traded_today = d, -math.inf, False
            row = daily.loc[d] if d in daily.index else None
            y = None
            if row is not None and not any(pd.isna(row[k]) for k in ("y_high", "y_close", "y_sma", "y_atr")):
                y = row
                days_tested += 1
            day_ok = y is not None and float(y["y_close"]) > float(y["y_sma"])
        hod = max(hod, highs[i])
        if i <= open_until or traded_today or not day_ok:
            continue
        c = closes[i]
        if not (c > float(y["y_high"]) and c >= hod * (1.0 - p.hod_tol_pct / 100.0)):
            continue

        entry = float(c)
        stop = entry - p.stop_atr_mult * float(y["y_atr"])
        risk = entry - stop
        if stop <= 0 or risk <= 0:
            continue
        target = entry + p.target_r * risk
        last_day_pos = date_pos[d] + p.max_hold_days
        exit_price, exit_reason, j_exit = None, "open", n - 1
        for j in range(i + 1, n):
            if date_pos.get(bar_dates[j], 10**9) > last_day_pos:
                exit_price, exit_reason, j_exit = float(closes[j - 1]), "time", j - 1
                break
            if lows[j] <= stop:
                exit_price, exit_reason, j_exit = stop, "stop", j
                break
            if highs[j] >= target:
                exit_price, exit_reason, j_exit = target, "target", j
                break
        if exit_price is None:
            exit_price = float(closes[-1])
        cost_r = (p.cost_pct / 100.0) * entry / risk
        r = (exit_price - entry) / risk - cost_r
        pct = (exit_price / entry - 1.0) * 100.0 - p.cost_pct
        trades.append(Trade(
            symbol, str(d), bars.index[i].isoformat(), entry, stop, target,
            bars.index[j_exit].isoformat(), exit_price, exit_reason, float(r), float(pct), int(j_exit - i),
        ))
        traded_today, open_until = True, j_exit

    return SymbolResult(symbol, source, n, bars.index[0].isoformat(), bars.index[-1].isoformat(),
                        days_tested, trades, compute_metrics(trades))


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
        f"TREND-JOIN LONG BACKTEST  source={source}  stop={p.stop_atr_mult}xATR{p.atr_period}  "
        f"target={p.target_r}R  hold<={p.max_hold_days}d  tol={p.hod_tol_pct}%  cost={p.cost_pct}%",
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
        f"📊 ผลทดสอบย้อนหลังกฎ Trend-join Long (ข้อมูล {src} ประมาณ 2 ปี)",
        f"กฎ: ปิดเมื่อวานเหนือเส้นค่าเฉลี่ย 200 วัน + ราคาทะลุจุดสูงสุดเมื่อวานและเป็นจุดสูงสุดของวัน | "
        f"ตัดขาดทุน {p.stop_atr_mult:g} ATR | เป้าหมาย {p.target_r:g} เท่าของความเสี่ยง | ถือไม่เกิน {p.max_hold_days} วัน | "
        f"หักค่าธรรมเนียม {p.cost_pct}% ต่อเทรด",
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
            f"(กำไร/ขาดทุนรวม {m['total_r']:+.1f}R หรือ {m['total_pct']:+.1f}% ของเงินต่อไม้) "
            f"ขาดทุนสะสมลึกสุด {m['max_drawdown_r']:.1f}R"
        )
    o = overall
    if o.get("trades"):
        lines += [
            "",
            f"รวมทุกตัว: {o['trades']} เทรด ชนะ {o['win_rate_pct']:.0f}% เฉลี่ย {o['avg_r']:+.2f}R ต่อเทรด "
            f"profit factor {_f(o['profit_factor'])} ขาดทุนสะสมลึกสุด {o['max_drawdown_r']:.1f}R",
            f"สรุป: {verdict_th(o['avg_r'], o['profit_factor'], o['trades'])}",
            "",
            "R = ระยะเสี่ยงต่อไม้ (ราคาเข้าถึงจุดตัดขาดทุน) เช่น +0.20R ต่อเทรด หมายถึงเสี่ยง 1,000 บาทต่อไม้ ได้กำไรเฉลี่ย 200 บาทต่อเทรด",
            "ข้อจำกัด: ไม่รวมสเปรดจริง/สลิปเพจ เกินกว่าค่าธรรมเนียมที่ตั้งไว้ และผลในอดีตไม่รับประกันอนาคต",
        ]
    return "\n".join(lines)
