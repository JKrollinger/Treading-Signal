#!/usr/bin/env python
"""Set the password for opening the dashboard from outside this PC.

    python dashboard_password.py                    # type it twice (hidden)
    python dashboard_password.py --show             # type it visibly (if hidden input does not work)
    python dashboard_password.py --from-clipboard   # copy the password first, then run this

Only a salted hash is saved (data/dashboard_auth.json). Restart the bot afterwards.
"""
from __future__ import annotations

import argparse
import getpass
import subprocess
import sys

from premarket_scanner import dashboard_auth


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set the dashboard's remote password")
    parser.add_argument("--show", action="store_true", help="show the password while typing")
    parser.add_argument("--from-clipboard", action="store_true", help="take the password from the clipboard")
    args = parser.parse_args(argv)

    if args.from_clipboard:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"], capture_output=True, text=True)
        password = out.stdout.strip()
    else:
        ask = input if args.show else getpass.getpass
        password = ask(f"New dashboard password (at least {dashboard_auth.MIN_LENGTH} characters): ")
        if ask("Again: ") != password:
            print("The two entries differ. Nothing was saved.")
            return 1
    try:
        dashboard_auth.set_password(password)
    except ValueError as exc:
        print(f"Not saved: {exc}")
        return 1
    print("Password saved (as a hash). Restart the bot: the dashboard link will arrive on Telegram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
