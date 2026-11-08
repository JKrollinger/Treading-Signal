#!/usr/bin/env python
"""Run the live trading dashboard on its own (the scheduler also starts it).

    python dashboard_server.py         # http://127.0.0.1:8790 until Ctrl-C
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys

from premarket_scanner import config, dashboard


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live trading dashboard")
    parser.add_argument("--port", type=int, default=config.DASHBOARD_PORT)
    parser.add_argument("--parent-pid", type=int, help="exit when this process ends (used by the scheduler)")
    args = parser.parse_args(argv)

    log_file = config.PROJECT_ROOT / "logs" / "dashboard.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        handlers=[logging.handlers.RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8")])
    if args.parent_pid:
        dashboard.exit_with_parent(args.parent_pid)
    try:
        dashboard.serve(config.DASHBOARD_BIND, args.port)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        logging.error("dashboard could not listen on port %d: %s", args.port, exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
