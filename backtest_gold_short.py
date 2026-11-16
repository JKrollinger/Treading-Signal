#!/usr/bin/env python
"""Gold, short side: is there any rule set worth trading when gold is falling?

    python backtest_gold_short.py                 # XAUUSD hourly from MT5, about 10 years
    python backtest_gold_short.py --split 2022-01-01 --min-trades 30

The plain mirror of Trend-join loses on gold, so this tries short rules built for how gold falls
(shorter, sharper legs with hard bounces), each as a combination of:

* regime: close below the 200-, 100- or 50-day average; optionally that average must be falling;
  optionally daily ADX >= 20
* entry: breakdown below yesterday's low at the low of the day, or "sell the rally" (price pops up
  to the daily EMA20 and the hourly bar closes back below it)
* exit: the long side's trailing exits, or quicker ones (fixed 1R / 1.5R target, shorter holds)

To keep this honest with so many combinations, the history is split in two. Rules are ranked on
the first part only; the second part is the test they never saw. A rule set is worth a demo run
only if it is positive on both, and the count of such sets among all tried is printed too: a
handful out of a hundred is what luck alone produces.

Results: output/backtest_gold_short_<date>.json
"""
from __future__ import annotations

import argparse
import itertools
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

REGIMES = {"SMA200": dict(sma_period=200), "SMA100": dict(sma_period=100), "SMA50": dict(sma_period=50)}
SLOPES = {"": {}, "+เส้นชี้ลง": dict(sma_slope_days=20)}
STRENGTH = {"": {}, "+ADX20": dict(adx_min=20.0)}
ENTRIES = {"หลุด Low": {}, "ขายตอนเด้ง EMA20": dict(entry_mode="ema_touch", pullback_atr=0.0)}
FULL = dict(trail_atr_mult=None, partial_r=None)
EXITS = {
    "trail 3 ATR": dict(trail_atr_mult=3.0),
    "trail 2 ATR": dict(trail_atr_mult=2.0),
    "SL2 TP1R": dict(stop_atr_mult=2.0, target_r=1.0, **FULL),
    "SL1.5 TP1R 5d": dict(stop_atr_mult=1.5, target_r=1.0, max_hold_days=5, **FULL),
    "SL1 TP1.5R 5d": dict(stop_atr_mult=1.0, target_r=1.5, max_hold_days=5, **FULL),
}


def stats(trades: list[backtest.Trade]) -> dict:
    if not trades:
        return {"trades": 0, "win": None, "avg_r": None, "total_r": 0.0, "pf": None, "dd": 0.0}
    rs = np.array([t.r_multiple for t in trades])
    cum = np.cumsum(rs)
    loss = -rs[rs <= 0].sum()
    return {"trades": len(rs), "win": float((rs > 0).mean() * 100), "avg_r": float(rs.mean()), "total_r": float(rs.sum()),
            "pf": float(rs[rs > 0].sum() / loss) if loss > 0 else None, "dd": float((cum - np.maximum.accumulate(cum)).min())}


def cell(m: dict) -> str:
    if not m["trades"]:
        return f"{'-':>27}"
    pf = "inf" if m["pf"] is None else f"{m['pf']:.2f}"
    return f"{m['trades']:>4}{m['win']:>5.0f}%{m['avg_r']:>+7.2f}{m['total_r']:>+7.1f}{pf:>6}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gold short-side rule search with an out-of-sample test")
    parser.add_argument("--days", type=int, default=3650)
    parser.add_argument("--split", default="2022-01-01", help="entries before this date pick the rules, later ones test them")
    parser.add_argument("--min-trades", type=int, default=30, help="fewer trades than this in the first part: not ranked")
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--output-dir", type=Path, default=config.OUTPUT_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    bars = backtest.load_mt5_hourly("XAUUSD", args.days)
    if not len(bars):
        print("XAUUSD: no data from MT5 (is the terminal open?)")
        return 1
    print(f"XAUUSD: {len(bars)} hourly bars, {bars.index[0]:%Y-%m-%d} to {bars.index[-1]:%Y-%m-%d}; "
          f"rules chosen on entries before {args.split}, tested on the rest")
    base = replace(base_params("XAUUSD"), side="short")

    rows = []
    combos = list(itertools.product(REGIMES, SLOPES, STRENGTH, ENTRIES, EXITS))
    for n, (rg, sl, st, en, ex) in enumerate(combos, 1):
        name = f"{rg}{sl}{st} | {en} | {ex}"
        p = replace(base, name=name, **REGIMES[rg], **SLOPES[sl], **STRENGTH[st], **ENTRIES[en], **EXITS[ex])
        trades = backtest.run_symbol("XAUUSD", bars, p, "mt5").trades
        first = [t for t in trades if t.entry_time[:10] < args.split]
        second = [t for t in trades if t.entry_time[:10] >= args.split]
        by_year: dict[str, float] = {}
        for t in trades:
            by_year[t.entry_time[:4]] = by_year.get(t.entry_time[:4], 0.0) + t.r_multiple
        rows.append({"name": name, "all": stats(trades), "first": stats(first), "second": stats(second),
                     "by_year": {k: round(v, 1) for k, v in sorted(by_year.items())}})
        if n % 20 == 0:
            print(f"  {n}/{len(combos)} rule sets done", flush=True)

    ranked = [r for r in rows if r["first"]["trades"] >= args.min_trades]
    ranked.sort(key=lambda r: r["first"]["avg_r"], reverse=True)
    good_first = [r for r in ranked if r["first"]["avg_r"] > 0]
    held = [r for r in good_first if r["second"]["trades"] >= 10 and r["second"]["avg_r"] > 0]
    solid = [r for r in held if r["first"]["avg_r"] >= 0.10 and r["second"]["avg_r"] >= 0.10]

    head = f"{'':<52}{'ช่วงเลือกกฎ':>27}{'ช่วงทดสอบ (ไม่เคยเห็น)':>30}"
    sub = f"{'กฎ (สภาพตลาด | จุดเข้า | จุดออก)':<52}" + f"{'n  win   avgR   totR    PF':>27}" * 2
    print(f"\n{args.top} ชุดที่ดีที่สุดในช่วงเลือกกฎ และผลในช่วงทดสอบ\n{head}\n{sub}")
    for r in ranked[:args.top]:
        print(f"{r['name'][:51]:<52}{cell(r['first'])}{cell(r['second'])}")
    mirror = next(r for r in rows if r["name"] == "SMA200 | หลุด Low | trail 3 ATR")
    print(f"{'(กฎกลับด้านเดิม) ' + mirror['name']:<52}{cell(mirror['first'])}{cell(mirror['second'])}")

    print(f"\nทดสอบทั้งหมด {len(rows)} ชุด · มีเทรดพอจัดอันดับ {len(ranked)} · กำไรในช่วงเลือกกฎ {len(good_first)} · "
          f"ยังกำไรในช่วงทดสอบ {len(held)} · กำไรอย่างน้อย +0.10R ทั้งสองช่วง {len(solid)}")
    if good_first:
        oos = [r["second"]["avg_r"] for r in good_first if r["second"]["trades"] >= 10]
        if oos:
            print(f"ชุดที่กำไรในช่วงเลือกกฎ ได้เฉลี่ยในช่วงทดสอบ {np.mean(oos):+.2f}R ต่อเทรด (มัธยฐาน {np.median(oos):+.2f}R)")
    for r in solid[:6]:
        print(f"\n✓ {r['name']}\n   ทั้ง 10 ปี: {cell(r['all'])}  ขาดทุนสะสมลึกสุด {r['all']['dd']:.1f}R\n   รายปี (R): "
              + "  ".join(f"{y}: {v:+.1f}" for y, v in r["by_year"].items()))

    stamp = datetime.now(ZoneInfo(config.MARKET_TZ)).date().isoformat()
    out = args.output_dir / f"backtest_gold_short_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"generated_at": datetime.now(ZoneInfo(config.MARKET_TZ)).isoformat(timespec="seconds"),
                               "split": args.split, "min_trades": args.min_trades, "tested": len(rows),
                               "positive_first": len(good_first), "positive_both": len(held), "solid_both": len(solid),
                               "rows": rows}, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
