#!/usr/bin/env python
"""Run the TradingView webhook receiver on its own (the scheduler also starts it).

    python tv_webhook_server.py            # listen on TV_WEBHOOK_BIND:TV_WEBHOOK_PORT until Ctrl-C
    python tv_webhook_server.py --test     # send one sample alert to the running server

Expose it to the internet with a tunnel, e.g.
    cloudflared tunnel --url http://127.0.0.1:8787
and use   https://<tunnel-host>/tv?key=<TV_WEBHOOK_SECRET>   as the TradingView webhook URL.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

import requests

from premarket_scanner import config, tv_webhook


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradingView webhook receiver")
    parser.add_argument("--test", action="store_true", help="POST a sample alert to the local server and exit")
    parser.add_argument("--port", type=int, default=config.TV_WEBHOOK_PORT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    if not config.TV_WEBHOOK_SECRET:
        print("TV_WEBHOOK_SECRET is empty in .env; set it first.")
        return 1

    if args.test:
        sample = {
            "signal": "Trend-join Long (ทดสอบ)", "ticker": "XAUUSD", "exchange": "OANDA",
            "price": "4120.5", "stop": "4047.2", "target": "4267.1", "interval": "5",
        }
        url = f"http://127.0.0.1:{args.port}/tv?key={config.TV_WEBHOOK_SECRET}"
        resp = requests.post(url, data=json.dumps(sample), headers={"Content-Type": "application/json"}, timeout=20)
        print(resp.status_code, resp.text)
        return 0 if resp.ok else 1

    server = tv_webhook.make_server(config.TV_WEBHOOK_BIND, args.port, config.TV_WEBHOOK_SECRET)
    print(f"Listening on http://{config.TV_WEBHOOK_BIND}:{args.port}/tv?key=<TV_WEBHOOK_SECRET>  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
