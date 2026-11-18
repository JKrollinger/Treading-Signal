#!/usr/bin/env python
"""Five published "high win rate" setups on gold, long side, against the live Trend-join rules.

    python backtest_five_logics.py                  # XAUUSD from MT5, about 10 years of hourly bars
    python backtest_five_logics.py --days 730

Signals are read from finished daily bars; fills, stops and targets are walked through the
hourly bars so the order of events inside a day is real. When a bar touches both the stop and
the target the stop counts (the worse case). Costs are the same 0.03% round trip the other
backtests use. One trade at a time per setup.

1. RSI(2) pullback (Connors): close above SMA200, RSI(2) below 10, buy next open, sell on a close above SMA5
2. Holy Grail (Raschke): ADX above 30 and +DI above -DI, a dip to EMA20, buy stop above that bar's high
3. Turtle Soup Plus One (Raschke/Connors): a new 20-day low that closes below the old one, buy the recovery
4. Bollinger reversion: ADX below 20, close under the lower band, buy next open, sell on a close at the mid band
5. NR7 (Crabel): narrowest range of 7 days, buy stop above its high, out after 3 days

The benchmark rows buy with no signal at all: on a market that rose for 10 years any long
setup wins, so a setup only counts when it beats them.

Results: output/backtest_five_logics_<date>.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from backtest_squeeze import base_params
from premarket_scanner import backtest, config

COST_PCT = 0.03


@dataclass
class Setup:
    signal: pd.Series                       # True on the daily bar that sets the trade up
    entry: str                              # "open" (next day's open) or "stop" (buy stop at `level`)
    level: pd.Series | None = None          # buy stop price, per signal day
    stop: pd.Series | None = None           # protective stop price, per signal day (None = no stop)
    stop_atr: float | None = None           # or: stop this many ATRs under the fill
    target: pd.Series | None = None         # target price, per signal day
    target_r: float | None = None           # or: target this many R above the fill
    exit_close: Callable[[pd.Series], bool] | None = None   # checked on each daily close
    max_hold: int = 10                      # trading days, counting the entry day
    valid_days: int = 1                     # how long a buy stop stays in the market
    risk_unit_atr: float = 2.0              # R unit when there is no stop


def indicators(bars: pd.DataFrame) -> pd.DataFrame:
    g = bars.groupby(bars.index.date)
    d = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                      "close": g["close"].last(), "bars": g["close"].count()})
    d = d[d["bars"] >= 6].copy()            # drop stub days (a few Sunday-evening bars)
    c, h, l = d["close"], d["high"], d["low"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()
    d["sma200"], d["sma20"], d["sma5"] = c.rolling(200).mean(), c.rolling(20).mean(), c.rolling(5).mean()
    d["ema20"] = c.ewm(span=20, adjust=False).mean()
    d["bb_low"] = d["sma20"] - 2.0 * c.rolling(20).std(ddof=0)
    w = lambda s, n: s.ewm(alpha=1.0 / n, adjust=False).mean()   # noqa: E731  Wilder smoothing
    delta = c.diff()
    d["rsi2"] = 100 - 100 / (1 + w(delta.clip(lower=0), 2) / w((-delta).clip(lower=0), 2))
    up, down = h.diff(), -l.diff()
    pdm, mdm = up.where((up > down) & (up > 0), 0.0), down.where((down > up) & (down > 0), 0.0)
    atr_w = w(tr, 14)
    d["pdi"], d["mdi"] = 100 * w(pdm, 14) / atr_w, 100 * w(mdm, 14) / atr_w
    d["adx"] = w((100 * (d["pdi"] - d["mdi"]).abs() / (d["pdi"] + d["mdi"])).fillna(0.0), 14)
    rng = h - l
    d["nr7"] = rng <= rng.rolling(7).min()
    d["hh20_prev"] = h.rolling(20).max().shift(1)
    d["ll20_prev"] = l.rolling(20).min().shift(1)
    # how many days ago the previous 20-day low was made (1 = yesterday)
    d["ll20_age"] = l.rolling(20).apply(lambda x: len(x) - int(np.argmin(x)), raw=True).shift(1)
    return d


def setups(d: pd.DataFrame) -> dict[str, Setup]:
    up = d["close"] > d["sma200"]
    rsi = up & (d["rsi2"] < 10)
    grail = (d["adx"] > 30) & (d["pdi"] > d["mdi"]) & (d["low"] <= d["ema20"]) & (d["close"] > d["sma200"])
    grail_any = (d["adx"] > 30) & (d["pdi"] > d["mdi"]) & (d["low"] <= d["ema20"])
    soup = (d["low"] < d["ll20_prev"]) & (d["ll20_age"] >= 3) & (d["close"] <= d["ll20_prev"])
    boll = (d["adx"] < 20) & (d["close"] < d["bb_low"])
    nr7 = d["nr7"].fillna(False)
    above5 = lambda r: r["close"] > r["sma5"]          # noqa: E731
    at_mid = lambda r: r["close"] >= r["sma20"]        # noqa: E731
    everyday = d["atr"].notna() & d["sma200"].notna()
    return {
        "0a ซื้อทุกวัน ไม่มีสัญญาณ (ถือ 5 วัน)": Setup(everyday, "open", stop_atr=2.0, max_hold=5),
        "0b ซื้อทุกวันที่อยู่เหนือ SMA200": Setup(everyday & up, "open", stop_atr=2.0, max_hold=5),
        "1a RSI(2) ต้นฉบับ ไม่มี S/L": Setup(rsi, "open", exit_close=above5),
        "1b RSI(2) + S/L 2 ATR": Setup(rsi, "open", stop_atr=2.0, exit_close=above5),
        "2a Holy Grail ต้นฉบับ": Setup(grail_any, "stop", level=d["high"], stop=d["low"], target=d["hh20_prev"], valid_days=2),
        "2b Holy Grail + เหนือ SMA200": Setup(grail, "stop", level=d["high"], stop=d["low"], target=d["hh20_prev"], valid_days=2),
        "3a Turtle Soup+1 ต้นฉบับ": Setup(soup, "stop", level=d["ll20_prev"], stop=d["low"], target_r=2.0, max_hold=5),
        "3b Turtle Soup+1 + เหนือ SMA200": Setup(soup & up, "stop", level=d["ll20_prev"], stop=d["low"], target_r=2.0, max_hold=5),
        "4a Bollinger ต้นฉบับ": Setup(boll, "open", stop_atr=2.0, exit_close=at_mid),
        "4b Bollinger + เหนือ SMA200": Setup(boll & up, "open", stop_atr=2.0, exit_close=at_mid),
        "5a NR7 ต้นฉบับ": Setup(nr7, "stop", level=d["high"], stop=d["low"], max_hold=3),
        "5b NR7 + เหนือ SMA200": Setup(nr7 & up, "stop", level=d["high"], stop=d["low"], max_hold=3),
    }


def simulate(symbol: str, bars: pd.DataFrame, d: pd.DataFrame, s: Setup) -> list[backtest.Trade]:
    by_day = {k: v for k, v in bars.groupby(bars.index.date)}
    days = list(d.index)
    trades: list[backtest.Trade] = []
    i, n = 0, len(days)
    while i < n - 1:
        row = d.iloc[i]
        if not bool(s.signal.iloc[i]) or pd.isna(row["atr"]):
            i += 1
            continue
        level = float(s.level.iloc[i]) if s.level is not None else None
        if s.entry == "stop" and (level is None or np.isnan(level)):
            i += 1
            continue
        entry = stop = target = None
        entry_time = exit_time = exit_reason = None
        exit_price, held, entry_j = None, 0, None
        j = i + 1
        while j < n:
            in_trade_at_day_start = entry is not None
            if not in_trade_at_day_start and j - i > (s.valid_days if s.entry == "stop" else 1):
                break
            for ts, b in by_day[days[j]].iterrows():
                o, hi, lo = float(b["open"]), float(b["high"]), float(b["low"])
                if entry is None:
                    if s.entry == "open":
                        entry = o
                    elif o >= level:
                        entry = o
                    elif hi >= level:
                        entry = level
                    else:
                        continue
                    entry_time, entry_j = ts, j
                    if s.stop is not None:
                        stop = float(s.stop.iloc[i])
                    elif s.stop_atr is not None:
                        stop = entry - s.stop_atr * float(row["atr"])
                    risk = (entry - stop) if stop is not None else s.risk_unit_atr * float(row["atr"])
                    if not risk > 0:
                        entry = None
                        break
                    if s.target is not None:
                        target = float(s.target.iloc[i])
                        if np.isnan(target) or target <= entry + 0.25 * risk:
                            entry = None       # no room to the old high: not a trade
                            break
                    elif s.target_r is not None:
                        target = entry + s.target_r * risk
                    if stop is not None and lo <= stop:      # stopped in the fill bar (worse case)
                        exit_price, exit_time, exit_reason = stop, ts, "stop"
                        break
                    continue
                if stop is not None and o <= stop:
                    exit_price, exit_time, exit_reason = o, ts, "stop"
                elif stop is not None and lo <= stop:
                    exit_price, exit_time, exit_reason = stop, ts, "stop"
                elif target is not None and o >= target:
                    exit_price, exit_time, exit_reason = o, ts, "target"
                elif target is not None and hi >= target:
                    exit_price, exit_time, exit_reason = target, ts, "target"
                if exit_price is not None:
                    break
            if entry is None and exit_price is None and s.entry == "open":
                break
            if entry is not None and exit_price is None:
                held = j - entry_j + 1
                day_row = d.iloc[j]
                last = by_day[days[j]]
                if s.exit_close is not None and s.exit_close(day_row):
                    exit_price, exit_time, exit_reason = float(last["close"].iloc[-1]), last.index[-1], "rule"
                elif held >= s.max_hold:
                    exit_price, exit_time, exit_reason = float(last["close"].iloc[-1]), last.index[-1], "time"
            if exit_price is not None:
                break
            j += 1
        if entry is None or exit_price is None:
            i = i + 1 if entry is None else n          # an open trade at the end of the data is left out
            continue
        cost_r = (COST_PCT / 100.0) * entry / risk
        trades.append(backtest.Trade(
            symbol=symbol, day=str(days[i]), entry_time=str(entry_time), entry=entry,
            stop=stop if stop is not None else float("nan"), target=target, exit_time=str(exit_time),
            exit_price=exit_price, exit_reason=exit_reason, r_multiple=(exit_price - entry) / risk - cost_r,
            pct_return=(exit_price / entry - 1.0) * 100.0 - COST_PCT, bars_held=max(j - entry_j + 1, 1)))
        i = j                                  # day j's close may set up the next trade
    return trades


def summarise(trades: list[backtest.Trade], years: float) -> dict:
    m = backtest.compute_metrics(trades)
    if not m.get("trades"):
        return m
    ts = sorted(trades, key=lambda t: t.entry_time)
    a, b = ts[:len(ts) // 2], ts[len(ts) // 2:]
    win = lambda xs: 100.0 * sum(t.r_multiple > 0 for t in xs) / len(xs) if xs else float("nan")   # noqa: E731
    avg = lambda xs: float(np.mean([t.r_multiple for t in xs])) if xs else float("nan")           # noqa: E731
    m["halves"] = {"win": [win(a), win(b)], "avg_r": [avg(a), avg(b)]}
    m["trades_per_year"] = len(ts) / years
    m["worst_r"] = min(t.r_multiple for t in ts)
    yearly = pd.Series([t.r_multiple for t in ts], index=[t.exit_time[:4] for t in ts]).groupby(level=0).sum()
    m["losing_years"] = [int((yearly < 0).sum()), int(len(yearly))]
    m["by_year_r"] = {k: round(float(v), 2) for k, v in yearly.items()}
    return m


def fmt(name: str, m: dict) -> str:
    if not m.get("trades"):
        return f"{name:<36}{'no trades':>10}"
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    h = m["halves"]
    return (f"{name:<36}{m['trades']:>6}{m['trades_per_year']:>6.0f}{m['win_rate_pct']:>6.0f}{m['avg_r']:>7.2f}"
            f"{m['avg_win_r']:>7.2f}{m['avg_loss_r']:>7.2f}{m['worst_r']:>7.1f}{m['total_r']:>8.1f}{pf:>6}"
            f"{m['max_drawdown_r']:>8.1f}{m['avg_pct']:>7.2f}   {h['win'][0]:.0f}/{h['win'][1]:.0f}%  "
            f"{h['avg_r'][0]:+.2f}/{h['avg_r'][1]:+.2f}  {m['losing_years'][0]}/{m['losing_years'][1]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Five high-win-rate setups on gold, long side")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--days", type=int, default=3650)
    parser.add_argument("--output-dir", type=Path, default=config.OUTPUT_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    bars = backtest.load_mt5_hourly(args.symbol, args.days)
    if not len(bars):
        print("no data from MT5 (is the terminal running?)")
        return 1
    d = indicators(bars)
    years = (bars.index[-1] - bars.index[0]).days / 365.25
    print(f"{args.symbol}: {len(bars)} hourly bars, {len(d)} days, {bars.index[0]:%Y-%m-%d} to {bars.index[-1]:%Y-%m-%d}"
          f" ({years:.1f} ปี) | ราคา {d['close'].iloc[0]:.0f} -> {d['close'].iloc[-1]:.0f}")
    head = (f"{'':<36}{'N':>6}{'/ปี':>6}{'WIN%':>6}{'AVG R':>7}{'W R':>7}{'L R':>7}{'WORST':>7}{'TOTAL':>8}{'PF':>6}"
            f"{'MAXDD':>8}{'AVG %':>7}   ชนะ% และ R เฉลี่ย ครึ่งแรก/หลัง  ปีขาดทุน")
    print(head)
    report: dict = {}
    for name, s in setups(d).items():
        m = summarise(simulate(args.symbol, bars, d, s), years)
        report[name] = m
        print(fmt(name, m))
    live = replace(base_params(args.symbol), name="live", adx_min=config.ADX_MIN or None)
    m = summarise(backtest.run_symbol(args.symbol, bars, live, "mt5").trades, years)
    report["กฎที่ใช้จริง (Trend-join v3)"] = m
    print(fmt("กฎที่ใช้จริง (Trend-join v3)", m))

    now = datetime.now(ZoneInfo(config.MARKET_TZ))
    out = args.output_dir / f"backtest_five_logics_{now.date().isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"generated_at": now.isoformat(timespec="seconds"), "symbol": args.symbol,
                               "days": args.days, "cost_pct": COST_PCT, "first_bar": str(bars.index[0]),
                               "last_bar": str(bars.index[-1]), "setups": report},
                              indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
