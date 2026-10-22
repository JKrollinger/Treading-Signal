#!/usr/bin/env python
"""Run the Trend-join Long strategy scanner once on today's pre-market results.

Examples:
    python strategy_scan.py                      # after 10:00 ET on a trading day
    python strategy_scan.py --ignore-time        # evaluate now regardless of the clock (testing)
    python strategy_scan.py --tickers QS,SAIQ    # evaluate specific tickers instead of the file
    python strategy_scan.py --input output/premarket_scan_2026-10-22.json
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from premarket_scanner import strategy


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("yfinance", "urllib3", "peewee", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trend-join Long strategy scanner")
    parser.add_argument("--date", type=date.fromisoformat, help="trading date (default: today in US Eastern)")
    parser.add_argument("--input", type=Path, help="pre-market scan JSON to read tickers from")
    parser.add_argument("--tickers", help="comma-separated tickers to evaluate instead of the pre-market file")
    parser.add_argument("--output-dir", type=Path, help="folder for the JSON file (default: output/)")
    parser.add_argument("--ignore-time", action="store_true", help="skip the 10:00 ET / regular-session gate")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] if args.tickers else None
    result, path = strategy.run_strategy_scan(
        scan_date=args.date,
        tickers=tickers,
        input_file=args.input,
        output_dir=args.output_dir,
        ignore_time=args.ignore_time,
    )
    print()
    print(strategy.format_table(result))
    print()
    print(result["report_text"])
    print(f"\nSaved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
