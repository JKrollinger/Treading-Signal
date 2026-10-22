#!/usr/bin/env python
"""Run the pre-market scanner once, print the table, and save the daily JSON file.

Examples:
    python scan.py                 # scan now, save output/premarket_scan_<today>.json
    python scan.py --no-news       # skip catalyst lookups (faster)
    python scan.py -v              # verbose logging
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from premarket_scanner import report, scanner


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("yfinance", "urllib3", "peewee", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-market gap scanner")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        help="trading date to label the report with (YYYY-MM-DD). Default: today in US Eastern. "
        "Quotes are always live, so a past date yields an empty report.",
    )
    parser.add_argument("--output-dir", type=Path, help="folder for the JSON file (default: output/)")
    parser.add_argument("--no-news", action="store_true", help="skip the catalyst lookup")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    result, path = scanner.run_and_save(
        scan_date=args.date, output_dir=args.output_dir, skip_news=args.no_news
    )
    print()
    print(report.format_table(result))
    print(f"\nSaved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
