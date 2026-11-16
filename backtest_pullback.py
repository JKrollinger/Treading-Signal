#!/usr/bin/env python
"""A second strategy for the uptrend: the pullback to the daily EMA20.

    python backtest_pullback.py                         # MT5 hourly data, about 2 years
    python backtest_pullback.py --source yahoo
    python backtest_pullback.py --ema 10 --slack 0.5

Everything except the entry is the live Trend-join setup (close above the 200-day SMA, stop
1 ATR, half off at 1R, 3 ATR trailing stop, at most 10 days), so only the entry is compared:

* base: the live breakout above yesterday's high
* pullback then breakout: the breakout counts only after a dip to the EMA20
* EMA touch: no breakout; buy the hourly bar that dips to the EMA20 and closes back above it

The last part shows what running base and the EMA touch side by side would have done.

Results: output/backtest_pullback_<source>_<date>.json
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
import pandas as pd

from backtest_squeeze import MT5_SYMBOLS, base_params, line
from premarket_scanner import backtest, config


def variants(slack: float, ema: int) -> dict[str, dict]:
    e = dict(ema_period=ema)
    return {
        "base (ทะลุ High เมื่อวาน)": {},
        f"ย่อแตะ EMA{ema} เมื่อวาน แล้วทะลุ": dict(pullback_atr=slack, pullback_days=1, **e),
        f"ย่อแตะ EMA{ema} ใน 3 วัน แล้วทะลุ": dict(pullback_atr=slack, pullback_days=3, **e),
        f"ซื้อตอนแตะ EMA{ema} (พอดีเส้น)": dict(entry_mode="ema_touch", pullback_atr=0.0, **e),
        f"ซื้อตอนแตะ EMA{ema} (เผื่อ {slack:g} ATR)": dict(entry_mode="ema_touch", pullback_atr=slack, **e),
    }


def monthly(trades: list[backtest.Trade]) -> pd.Series:
    if not trades:
        return pd.Series(dtype=float)
    return pd.Series([t.r_multiple for t in trades], index=[t.exit_time[:7] for t in trades]).groupby(level=0).sum()


def halves(trades: list[backtest.Trade]) -> str:
    if len(trades) < 4:
        return "n/a"
    ts = sorted(trades, key=lambda t: t.entry_time)
    a, b = ts[:len(ts) // 2], ts[len(ts) // 2:]
    return f"{np.mean([t.r_multiple for t in a]):+.2f} / {np.mean([t.r_multiple for t in b]):+.2f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EMA pullback entry compared with the Trend-join breakout")
    parser.add_argument("--source", choices=["yahoo", "mt5"], default="mt5")
    parser.add_argument("--symbols", help="comma-separated symbols, written the way the source names them")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--ema", type=int, default=20)
    parser.add_argument("--slack", type=float, default=0.25, help="how near the EMA counts as a touch, in ATRs")
    parser.add_argument("--output-dir", type=Path, default=config.OUTPUT_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        symbols = MT5_SYMBOLS if args.source == "mt5" else \
            [s.strip().upper() for s in config._DEFAULT_WATCHLIST["yahoo"].split(",")]
    loader = backtest.load_mt5_hourly if args.source == "mt5" else backtest.load_yahoo_hourly
    data = {}
    for s in symbols:
        bars = loader(s, args.days)
        if len(bars):
            data[s] = bars
            print(f"{s}: {len(bars)} hourly bars, {bars.index[0]:%Y-%m-%d} to {bars.index[-1]:%Y-%m-%d}")
        else:
            print(f"{s}: no data, left out")
    symbols = list(data)
    live = [s for s in symbols if s in config.SPECIAL_WATCHLIST]
    names = variants(args.slack, args.ema)

    runs: dict[str, dict[str, backtest.SymbolResult]] = {
        name: {s: backtest.run_symbol(s, data[s], replace(base_params(s), name=name, **extra), args.source)
               for s in symbols} for name, extra in names.items()}
    head = f"{'':<34}{'TRADES':>7}{'WIN%':>7}{'AVG R':>8}{'TOTAL R':>9}{'PF':>7}{'MAXDD R':>9}   ครึ่งแรก / ครึ่งหลัง"
    report: dict = {"variants": {}}
    for scope, group in (("ทุกตัว", symbols), ("เฉพาะที่เฝ้าจริง " + ",".join(live), live)):
        if not group:
            continue
        print(f"\n{scope}\n{head}")
        for name in names:
            results = [runs[name][s] for s in group]
            m = backtest.overall_metrics(results)
            print(line(name, m) + "   " + halves([t for r in results for t in r.trades]))
            report["variants"].setdefault(scope, {})[name] = {
                "overall": m, "by_symbol": {r.symbol: r.metrics for r in results}}

    base_name, touch_name = list(names)[0], list(names)[3]
    print(f"\nรายสัญลักษณ์ (เทรด / ชนะ% / เฉลี่ย R / รวม R)\n{'':<10}{'base':>26}{'ซื้อตอนแตะ EMA':>26}")
    for s in symbols:
        cells = []
        for name in (base_name, touch_name):
            m = runs[name][s].metrics
            cells.append(f"{m['trades']:>4} / {m['win_rate_pct']:>3.0f} / {m['avg_r']:+.2f} / {m['total_r']:+6.1f}"
                         if m.get("trades") else "no trades")
        print(f"{s:<10}{cells[0]:>26}{cells[1]:>26}")

    # running the two side by side --------------------------------------------------------
    report["together"] = {}
    for scope, group in (("ทุกตัว", symbols), ("เฉพาะที่เฝ้าจริง", live)):
        if not group:
            continue
        a = [t for s in group for t in runs[base_name][s].trades]
        b = [t for s in group for t in runs[touch_name][s].trades]
        both = sorted(a + b, key=lambda t: t.exit_time)
        ma, mb = monthly(a), monthly(b)
        idx = sorted(set(ma.index) | set(mb.index))
        ma, mb = ma.reindex(idx, fill_value=0.0), mb.reindex(idx, fill_value=0.0)
        corr = float(ma.corr(mb)) if len(idx) > 2 and ma.std() > 0 and mb.std() > 0 else None
        same_day = len({(t.symbol, t.day) for t in a} & {(t.symbol, t.day) for t in b})
        m = backtest.compute_metrics(both)
        print(f"\nรันสองกลยุทธ์คู่กัน: {scope}")
        print(line("  รวมสองกลยุทธ์", m))
        print(f"  เดือนที่ขาดทุน: base {int((ma < 0).sum())}/{len(idx)}, EMA {int((mb < 0).sum())}/{len(idx)}, "
              f"รวม {int(((ma + mb) < 0).sum())}/{len(idx)} | สหสัมพันธ์ผลรายเดือน "
              f"{'n/a' if corr is None else f'{corr:+.2f}'} | เข้าวันเดียวกัน {same_day} ครั้ง")
        report["together"][scope] = {"combined": m, "monthly_corr": corr, "same_day_entries": same_day,
                                     "months": len(idx), "losing_months": {
                                         "base": int((ma < 0).sum()), "ema_touch": int((mb < 0).sum()),
                                         "combined": int(((ma + mb) < 0).sum())}}

    stamp = datetime.now(ZoneInfo(config.MARKET_TZ)).date().isoformat()
    out = args.output_dir / f"backtest_pullback_{args.source}_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"generated_at": datetime.now(ZoneInfo(config.MARKET_TZ)).isoformat(timespec="seconds"),
                               "source": args.source, "symbols": symbols, "ema": args.ema, "slack_atr": args.slack,
                               **report}, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
