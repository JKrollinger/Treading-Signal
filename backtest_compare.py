#!/usr/bin/env python
"""Compare the version 1 rules with the version 2 layers on the same hourly history.

    python backtest_compare.py --source mt5 --send
    python backtest_compare.py --source yahoo --symbols GC=F,EURUSD=X

Variants: v1 (current live rules), v2-regime (ADX/ATR/SMA-slope filter), v2-trail
(Chandelier trailing stop + half off at 1R), v2-time (London/NY session, no late Friday,
NFP/FOMC blackout) and v2-all. Results: output/backtest_compare_<source>_<date>.json
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
    parser = argparse.ArgumentParser(description="Trend-join Long: v1 vs v2 comparison")
    parser.add_argument("--source", choices=["yahoo", "mt5"], default="mt5")
    parser.add_argument("--symbols", help="comma-separated symbols in the source's own style")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--send", action="store_true", help="send the Thai comparison to Telegram")
    parser.add_argument("--output-dir", type=Path, default=config.OUTPUT_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("yfinance", "urllib3", "peewee", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    elif args.source == "mt5":
        symbols = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "USDTHB"]
    else:
        symbols = [s.strip().upper() for s in config._DEFAULT_WATCHLIST["yahoo"].split(",")]
    loader = backtest.load_mt5_hourly if args.source == "mt5" else backtest.load_yahoo_hourly

    data = {}
    for s in symbols:
        bars = loader(s, args.days)
        logging.info("%s: %d hourly bars (tz offset %s h)", s, len(bars), bars.attrs.get("tz_offset_hours", "n/a"))
        data[s] = bars

    by_variant: dict[str, tuple[list[backtest.SymbolResult], dict]] = {}
    for p in backtest.variants():
        results = [backtest.run_symbol(s, data[s], p, args.source) for s in symbols]
        by_variant[p.name] = (results, backtest.overall_metrics(results))
        logging.info("%s: %d trades overall", p.name, by_variant[p.name][1].get("trades", 0))

    names = {s: special.display_name(s, True) for s in symbols}
    print()
    print(backtest.format_comparison(by_variant, args.source))
    print()
    for p in backtest.variants():
        results, overall = by_variant[p.name]
        print(backtest.format_table(results, overall, p, args.source))
        skipped = {r.symbol: r.skipped for r in results if r.skipped}
        if skipped:
            print("skipped candidate days:", skipped)
        print()
    message = backtest.format_comparison_th(by_variant, args.source, names)
    print(message)

    stamp = datetime.now(ZoneInfo(config.MARKET_TZ)).date().isoformat()
    out = args.output_dir / f"backtest_compare_{args.source}_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "generated_at": datetime.now(ZoneInfo(config.MARKET_TZ)).isoformat(timespec="seconds"),
        "source": args.source,
        "variants": {n: {"params": next(p for p in backtest.variants() if p.name == n).__dict__,
                         "overall": o, "results": [r.to_json() for r in rs]}
                     for n, (rs, o) in by_variant.items()},
        "summary_th": message,
    }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nSaved: {out}")

    if args.send:
        from premarket_scanner import telegram_notify as tg
        print("Telegram:", "sent" if tg.send_message(message) else "not sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
