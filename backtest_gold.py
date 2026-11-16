#!/usr/bin/env python
"""Gold only: which changes to the live Trend-join rules raise the win rate, and what they cost.

    python backtest_gold.py                    # XAUUSD from MT5 and GC=F from Yahoo, about 2 years
    python backtest_gold.py --source mt5 --days 3650     # the broker's full hourly history (about 10 years)

A higher win rate is easy to buy (take profit sooner, put the stop further away) and usually
paid for with a smaller average win, so every row shows both: WIN% and the average result in R.
A change is only worth having when it holds on both data sources and in both halves.

Results: output/backtest_gold_<date>.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from backtest_squeeze import base_params
from premarket_scanner import backtest, config

SOURCES = {"mt5": "XAUUSD", "yahoo": "GC=F"}
VARIANTS: dict[str, dict] = {
    "base (กฎที่ใช้จริง)": {},
    # exits: bank profit sooner
    "ปิดครึ่งที่ 0.5R": dict(partial_r=0.5),
    "ปิดทั้งไม้ที่ 1R": dict(target_r=1.0, trail_atr_mult=None, partial_r=None),
    "ปิดทั้งไม้ที่ 1.5R": dict(target_r=1.5, trail_atr_mult=None, partial_r=None),
    "เลื่อน S/L ตาม 2 ATR": dict(trail_atr_mult=2.0),
    # stops: more room
    "S/L 1.5 ATR": dict(stop_atr_mult=1.5),
    "S/L 2 ATR": dict(stop_atr_mult=2.0),
    "S/L 1.5 ATR + ปิดครึ่งที่ 0.5R": dict(stop_atr_mult=1.5, partial_r=0.5),
    "S/L 2 ATR + ปิดทั้งไม้ที่ 1R": dict(stop_atr_mult=2.0, target_r=1.0, trail_atr_mult=None, partial_r=None),
    # entries: fewer, cleaner days
    "ADX >= 20": dict(adx_min=20.0),
    "ADX >= 25": dict(adx_min=25.0),
    "SMA200 ชี้ขึ้น 20 วัน": dict(sma_slope_days=20),
    "ไม่เข้าเมื่อผันผวนเกิน 1.5 เท่า": dict(atr_ratio_max=1.5),
    "S/L 2 ATR + ปิดที่ 1R + ADX >= 20": dict(stop_atr_mult=2.0, target_r=1.0, trail_atr_mult=None, partial_r=None, adx_min=20.0),
    # the other side: the mirror rules below the 200-day SMA
    "ฝั่งขาย (กฎกลับด้าน)": dict(side="short"),
    "ฝั่งขาย S/L 2 ATR + ปิดที่ 1R": dict(side="short", stop_atr_mult=2.0, target_r=1.0, trail_atr_mult=None, partial_r=None),
    "สองฝั่ง (SMA200 เลือกฝั่ง)": dict(side="both"),
}


def row(name: str, trades: list[backtest.Trade]) -> tuple[str, dict]:
    m = backtest.compute_metrics(trades)
    if not m.get("trades"):
        return f"{name:<34}{'no trades':>10}", m
    ts = sorted(trades, key=lambda t: t.entry_time)
    a, b = ts[:len(ts) // 2], ts[len(ts) // 2:]
    win = lambda xs: 100.0 * sum(t.r_multiple > 0 for t in xs) / len(xs) if xs else float("nan")  # noqa: E731
    avg = lambda xs: float(np.mean([t.r_multiple for t in xs])) if xs else float("nan")          # noqa: E731
    m["halves"] = {"win": [win(a), win(b)], "avg_r": [avg(a), avg(b)]}
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    return (f"{name:<34}{m['trades']:>7}{m['win_rate_pct']:>7.0f}{m['avg_r']:>8.2f}{m['total_r']:>9.1f}{pf:>7}"
            f"{m['max_drawdown_r']:>9.1f}   {win(a):.0f}/{win(b):.0f}%  {avg(a):+.2f}/{avg(b):+.2f}"), m


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gold: win rate against average result")
    parser.add_argument("--source", choices=["mt5", "yahoo", "both"], default="both")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--output-dir", type=Path, default=config.OUTPUT_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    report: dict = {}
    head = (f"{'':<34}{'TRADES':>7}{'WIN%':>7}{'AVG R':>8}{'TOTAL R':>9}{'PF':>7}{'MAXDD R':>9}"
            "   ชนะ% และ R เฉลี่ย ครึ่งแรก/ครึ่งหลัง")
    for source in (("mt5", "yahoo") if args.source == "both" else (args.source,)):
        symbol = SOURCES[source]
        loader = backtest.load_mt5_hourly if source == "mt5" else backtest.load_yahoo_hourly
        bars = loader(symbol, args.days)
        if not len(bars):
            print(f"{symbol} ({source}): no data")
            continue
        print(f"\n{symbol} ({source}): {len(bars)} hourly bars, {bars.index[0]:%Y-%m-%d} to {bars.index[-1]:%Y-%m-%d}\n{head}")
        base = replace(base_params("XAUUSD"), cost_pct=0.03)        # the time/news filter gold uses live
        report[source] = {}
        for name, extra in VARIANTS.items():
            result = backtest.run_symbol(symbol, bars, replace(base, name=name, **extra), source)
            text, m = row(name, result.trades)
            print(text)
            report[source][name] = {"params": extra, "metrics": m, "skipped": result.skipped}

    stamp = datetime.now(ZoneInfo(config.MARKET_TZ)).date().isoformat()
    out = args.output_dir / f"backtest_gold_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"generated_at": datetime.now(ZoneInfo(config.MARKET_TZ)).isoformat(timespec="seconds"),
                               "results": report}, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
