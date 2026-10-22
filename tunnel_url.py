#!/usr/bin/env python
"""Print the current TradingView webhook URL (the tunnel address plus the secret).

    python tunnel_url.py          # print it
    python tunnel_url.py --send   # also send it to Telegram
"""
from __future__ import annotations

import argparse
import sys

from premarket_scanner import tunnel


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show the TradingView webhook URL")
    parser.add_argument("--send", action="store_true", help="send the URL to Telegram as well")
    args = parser.parse_args(argv)
    public = tunnel.current_url()
    if not public:
        print("No tunnel URL yet. Is the scheduler running with TUNNEL_ENABLED=true and cloudflared installed?")
        return 1
    print("Webhook URL for TradingView:")
    print(tunnel.webhook_url(public))
    if args.send:
        tunnel.notify_url(public)
        print("Sent to Telegram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
