#!/usr/bin/env python
"""Run one Scanner C watch check by hand.

    python special_watch_once.py            # check, send any new alerts to Telegram
    python special_watch_once.py --no-send  # check, print alerts only
    python special_watch_once.py --status   # show today's tracked signals and current values
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from premarket_scanner import config, special, special_watch
from premarket_scanner import telegram_notify as tg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scanner C watch: one check")
    parser.add_argument("--no-send", action="store_true", help="print alerts instead of sending them")
    parser.add_argument("--status", action="store_true", help="print current values and today's signals, no alerts")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("yfinance", "urllib3", "peewee", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.status:
        thai = tg.is_thai()
        now = datetime.now(ZoneInfo(config.MARKET_TZ))
        state = special_watch.today_records(special_watch.load_state(), now.date())
        for it in special.fetch_special(thai=thai):
            rec = state.get(it.symbol)
            plan = it.plan
            print(f"{it.symbol:10} {it.name:18} price={special.fmt_price(it.price, it.currency):>12} "
                  f"chg={it.change_pct if it.change_pct is None else round(it.change_pct, 2)!s:>6} "
                  f"yHigh={special.fmt_price(it.yesterday_high, it.currency):>12} uptrend={it.uptrend!s:5} "
                  f"atHOD={it.at_high_of_day!s:5} signal={it.signal!s:5} "
                  f"atr={special.fmt_price(it.atr, it.currency) if it.atr else 'n/a':>10} "
                  f"stale={'yes' if it.quote_time is None or (now - it.quote_time).total_seconds() > config.STALE_QUOTE_MIN * 60 else 'no'}"
                  + (f" | plan stop={special.fmt_price(plan.stop, it.currency)} target={special.fmt_price(plan.target, it.currency)}" if plan else "")
                  + (f" | tracked: {rec['status']} since {rec['triggered_at'][11:16]} ET" if rec else ""))
        return 0

    messages = special_watch.run_watch(send=not args.no_send)
    if not messages:
        print("No new alerts.")
    for m in messages:
        print("-" * 60)
        print(m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
