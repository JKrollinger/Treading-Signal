#!/usr/bin/env python
"""Check the MetaTrader 5 connection: account, broker symbols, and live levels.

    python mt5_probe.py                 # account + find gold / FX / bitcoin symbols + levels
    python mt5_probe.py --symbols XAUUSD,EURUSD
    python mt5_probe.py --list "*USD*"  # list broker symbols matching a pattern

The MT5 terminal must be open and logged in on this PC.
"""
from __future__ import annotations

import argparse
import logging
import sys

from premarket_scanner import config, mt5_source, special


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MetaTrader 5 connection check")
    parser.add_argument("--symbols", help="comma-separated broker symbols to fetch")
    parser.add_argument("--list", metavar="PATTERN", help="list symbols matching an MT5 wildcard pattern")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    acc = mt5_source.account_summary()
    if not acc:
        print("Could not connect to MetaTrader 5. Open the MT5 terminal, log in (demo is fine), then run again.")
        print("If MT5 is installed in an unusual place, set MT5_PATH in .env to terminal64.exe.")
        return 1
    print(f"Account: {acc['login']} on {acc['server']} ({acc['trade_mode']}), balance {acc['balance']:,.2f} {acc['currency']}, "
          f"leverage 1:{acc['leverage']}, algo trading allowed: {acc['trade_allowed']}")

    if args.list:
        names = mt5_source.list_symbols(args.list)
        print(f"{len(names)} symbol(s) matching {args.list}: {', '.join(names[:80])}")
        return 0

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        wanted = ("XAU", "GOLD", "EURUSD", "GBPUSD", "USDJPY", "USDTHB", "BTC")
        names = mt5_source.list_symbols()
        symbols = [n for n in names if any(w in n.upper() for w in wanted)][:12]
        print(f"Broker symbols that look like gold / FX / bitcoin: {', '.join(symbols) or 'none found'}")
    rows = mt5_source.fetch_rows(symbols)
    for s in symbols:
        r = rows.get(s)
        if not r:
            print(f"{s:10} no data"); continue
        fp = lambda v: special.fmt_price(v, r["currency"])  # noqa: E731
        print(f"{s:10} bid {fp(r['price'])} ask {fp(r['ask'])} | today H {fp(r['day_high'])} | "
              f"yesterday H {fp(r['yesterday_high'])} C {fp(r['yesterday_close'])} | SMA200 {fp(r['sma200'])} | "
              f"ATR14 {fp(r['atr'])} | spread {r['spread_points']} pts | quote {r['quote_time'].strftime('%H:%M:%S')} ET")
    mt5_source.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
