#!/usr/bin/env python
"""Does a squeeze (NR7 / Inside Day) or room below resistance improve Trend-join Long?

    python backtest_squeeze.py                          # MT5 hourly data, about 2 years
    python backtest_squeeze.py --source yahoo --symbols GC=F,USDJPY=X
    python backtest_squeeze.py --room-atr 1.5 --room-days 60

Base = the live rules (v2-trail exit; the time/news filter on the symbols that use it live).
Two views are printed:

1. the base trades split by what the chart looked like at entry (same trades, so the groups
   compare like with like), with a permutation test of the difference in average R;
2. each filter run as a rule of its own (the trade list changes, because a skipped entry
   frees the symbol for a later one).

Results: output/backtest_squeeze_<source>_<date>.json
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

from premarket_scanner import backtest, config

MT5_SYMBOLS = ["XAUUSD", "USDJPY", "EURUSD", "GBPUSD", "USDTHB", "BTCUSD"]


def base_params(symbol: str) -> backtest.Params:
    p = backtest.Params(name="base", stop_atr_mult=config.STOP_ATR_MULT, target_r=None,
                        trail_atr_mult=config.TRAIL_ATR_MULT, partial_r=config.PARTIAL_R,
                        max_hold_days=config.MAX_HOLD_DAYS, hod_tol_pct=config.HOD_TOLERANCE_PCT, cost_pct=0.03)
    if symbol in config.TIME_FILTER_SYMBOLS:
        p = replace(p, session_utc=(config.SESSION_UTC_START, config.SESSION_UTC_END),
                    friday_cutoff_utc=config.FRIDAY_CUTOFF_UTC, news_blackout=True)
    return p


def variants(room_atr: float, room_days: int) -> dict[str, dict]:
    room = dict(room_atr=room_atr, room_lookback=room_days)
    return {
        "base (กฎที่ใช้จริง)": {},
        "NR7": dict(squeeze="nr7"),
        "Inside Day": dict(squeeze="inside"),
        "NR7 หรือ Inside": dict(squeeze="either"),
        f"ไม่มีแนวต้านใน {room_atr:g} ATR": room,
        "บีบตัว + ไม่มีแนวต้าน": dict(squeeze="either", **room),
    }


def tag_trades(symbol: str, bars: pd.DataFrame, trades: list[backtest.Trade], p: backtest.Params) -> list[dict]:
    """What the chart looked like when each base trade was opened (values known before the entry)."""
    daily = backtest.daily_levels(bars.sort_index(), p)
    keys = {str(d): d for d in daily.index}
    rows = []
    for t in trades:
        y = daily.loc[keys[t.day]]
        gap = (float(y["y_hh"]) - t.entry) / float(y["y_atr"]) if not pd.isna(y["y_hh"]) else np.nan
        rows.append({"symbol": symbol, "day": t.day, "r": t.r_multiple,
                     "nr7": bool(y["y_nr7"] == 1.0), "inside": bool(y["y_inside"] == 1.0), "gap_atr": gap})
    return rows


def stats(rs) -> dict:
    rs = np.asarray(list(rs), dtype=float)
    if not len(rs):
        return {"trades": 0}
    wins, losses = rs[rs > 0].sum(), -rs[rs <= 0].sum()
    return {"trades": int(len(rs)), "win_rate_pct": float((rs > 0).mean() * 100), "avg_r": float(rs.mean()),
            "total_r": float(rs.sum()), "profit_factor": float(wins / losses) if losses > 0 else float("inf")}


def perm_p(a, b, rounds: int = 20_000, seed: int = 7) -> float | None:
    """Two-sided chance that a difference in average R this large comes from luck alone."""
    a, b = np.asarray(list(a), float), np.asarray(list(b), float)
    if len(a) < 5 or len(b) < 5:
        return None
    rng = np.random.default_rng(seed)
    pool, seen = np.concatenate([a, b]), abs(a.mean() - b.mean())
    hits = 0
    for _ in range(rounds):
        rng.shuffle(pool)
        hits += abs(pool[:len(a)].mean() - pool[len(a):].mean()) >= seen
    return hits / rounds


def line(label: str, m: dict) -> str:
    if not m.get("trades"):
        return f"{label:<34}{'no trades':>10}"
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    extra = f"{m['max_drawdown_r']:>9.1f}" if "max_drawdown_r" in m else ""
    return f"{label:<34}{m['trades']:>7}{m['win_rate_pct']:>7.0f}{m['avg_r']:>8.2f}{m['total_r']:>9.1f}{pf:>7}{extra}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trend-join Long: squeeze and resistance filters")
    parser.add_argument("--source", choices=["yahoo", "mt5"], default="mt5")
    parser.add_argument("--symbols", help="comma-separated symbols, written the way the source names them")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--room-atr", type=float, default=1.0, help="resistance closer than this many ATRs blocks the entry")
    parser.add_argument("--room-days", type=int, default=20, help="resistance = highest high of this many days")
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

    # 1) the base trades, split by the chart at entry -----------------------------------
    tags: list[dict] = []
    for s in symbols:
        p = replace(base_params(s), room_lookback=args.room_days)
        tags += tag_trades(s, data[s], backtest.run_symbol(s, data[s], p, args.source).trades, p)
    df = pd.DataFrame(tags)
    df["squeeze"] = df["nr7"] | df["inside"]
    df["near"] = (df["gap_atr"] > 0) & (df["gap_atr"] < args.room_atr)
    half = df["day"] >= df["day"].sort_values().iloc[len(df) // 2]
    splits = {"NR7": df["nr7"], "Inside Day": df["inside"], "NR7 หรือ Inside": df["squeeze"],
              f"แนวต้านใกล้กว่า {args.room_atr:g} ATR": df["near"]}
    head = f"{'':<34}{'TRADES':>7}{'WIN%':>7}{'AVG R':>8}{'TOTAL R':>9}{'PF':>7}"
    print(f"\n1) เทรดของกฎที่ใช้จริง แยกตามหน้าตากราฟตอนเข้า ({', '.join(symbols)})\n{head}")
    print(line("ทั้งหมด", stats(df["r"])))
    report = {"all": stats(df["r"]), "splits": {}}
    for name, mask in splits.items():
        yes, no = df.loc[mask, "r"], df.loc[~mask, "r"]
        pv = perm_p(yes, no)
        print(line(f"  {name}: ใช่", stats(yes)))
        print(line(f"  {name}: ไม่ใช่", stats(no)) + (f"   p={pv:.2f}" if pv is not None else "   p=n/a"))
        halves = {k: {"yes": stats(df.loc[mask & h, "r"]), "no": stats(df.loc[~mask & h, "r"])}
                  for k, h in (("first_half", ~half), ("second_half", half))}
        for k, v in halves.items():
            ya, na = v["yes"].get("avg_r"), v["no"].get("avg_r")
            print(f"      {k}: ใช่ {v['yes']['trades']} เทรด avg {ya if ya is None else round(ya, 2)} | "
                  f"ไม่ใช่ {v['no']['trades']} เทรด avg {na if na is None else round(na, 2)}")
        by_symbol = {s: {"yes": stats(df.loc[mask & (df["symbol"] == s), "r"]),
                         "no": stats(df.loc[~mask & (df["symbol"] == s), "r"])} for s in symbols}
        report["splits"][name] = {"yes": stats(yes), "no": stats(no), "p_value": pv, "halves": halves,
                                  "by_symbol": by_symbol}

    # 2) each filter as a rule of its own -------------------------------------------------
    report["variants"] = {}
    for scope, group in (("ทุกตัว", symbols), ("เฉพาะที่เฝ้าจริง " + ",".join(live), live)):
        if not group:
            continue
        print(f"\n2) ใช้ตัวกรองเป็นกฎจริง: {scope}\n{head}{'MAXDD R':>9}")
        for name, extra in variants(args.room_atr, args.room_days).items():
            results = [backtest.run_symbol(s, data[s], replace(base_params(s), name=name, **extra), args.source)
                       for s in group]
            m = backtest.overall_metrics(results)
            print(line(name, m))
            report["variants"].setdefault(scope, {})[name] = {
                "overall": m, "by_symbol": {r.symbol: r.metrics for r in results},
                "skipped": {r.symbol: r.skipped for r in results}}

    stamp = datetime.now(ZoneInfo(config.MARKET_TZ)).date().isoformat()
    out = args.output_dir / f"backtest_squeeze_{args.source}_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"generated_at": datetime.now(ZoneInfo(config.MARKET_TZ)).isoformat(timespec="seconds"),
                               "source": args.source, "symbols": symbols, "room_atr": args.room_atr,
                               "room_days": args.room_days, **report}, indent=2, ensure_ascii=False, default=str),
                   encoding="utf-8")
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
