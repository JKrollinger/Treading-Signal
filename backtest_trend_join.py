#!/usr/bin/env python
"""Backtest the Trend-join Long rules on hourly history.

    python backtest_trend_join.py                       # Yahoo data, default symbols, ~2 years
    python backtest_trend_join.py --source mt5          # your broker's data through MetaTrader 5
    python backtest_trend_join.py --symbols GC=F,BTC-USD --hold 3 --stop-atr 1.5 --target-r 2
    python backtest_trend_join.py --send                # also send the Thai summary to Telegram

Results: output/backtest_trend_join_<source>_<date>.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from premarket_scanner import backtest, config, special


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trend-join Long backtest")
    parser.add_argument("--source", choices=["yahoo", "mt5"], default="yahoo")
    parser.add_argument("--symbols", help="comma-separated symbols in the source's own style")
    parser.add_argument("--days", type=int, default=730, help="history length in days (Yahoo hourly max 730)")
    parser.add_argument("--hold", type=int, default=5, help="max holding period in trading days")
    parser.add_argument("--stop-atr", type=float, default=config.STOP_ATR_MULT)
    parser.add_argument("--target-r", type=float, default=config.TARGET_R_MULT)
    parser.add_argument("--tol", type=float, default=config.HOD_TOLERANCE_PCT, help="high-of-day tolerance %")
    parser.add_argument("--cost", type=float, default=0.03, help="round-trip cost, % of entry")
    parser.add_argument("--send", action="store_true", help="send the Thai summary to Telegram")
    parser.add_argument("--output-dir", type=Path, default=config.OUTPUT_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("yfinance", "urllib3", "peewee", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else \
        [s.strip().upper() for s in config._DEFAULT_WATCHLIST[args.source].split(",")]
    params = backtest.Params(stop_atr_mult=args.stop_atr, target_r=args.target_r, hod_tol_pct=args.tol,
                             max_hold_days=args.hold, cost_pct=args.cost)
    loader = backtest.load_mt5_hourly if args.source == "mt5" else backtest.load_yahoo_hourly

    results = []
    for s in symbols:
        bars = loader(s, args.days)
        logging.info("%s: %d hourly bars", s, len(bars))
        results.append(backtest.run_symbol(s, bars, params, args.source))
    overall = backtest.overall_metrics(results)

    names = {s: special.display_name(s, True) for s in symbols}
    table = backtest.format_table(results, overall, params, args.source)
    message = backtest.format_message_th(results, overall, params, args.source, names)
    print()
    print(table)
    print()
    print(message)

    stamp = datetime.now(ZoneInfo(config.MARKET_TZ)).date().isoformat()
    out = args.output_dir / f"backtest_trend_join_{args.source}_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "generated_at": datetime.now(ZoneInfo(config.MARKET_TZ)).isoformat(timespec="seconds"),
        "source": args.source, "params": params.__dict__, "overall": overall,
        "results": [r.to_json() for r in results], "summary_th": message,
    }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nSaved: {out}")

    if args.send:
        from premarket_scanner import telegram_notify as tg
        print("Telegram:", "sent" if tg.send_message(message) else "not sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
