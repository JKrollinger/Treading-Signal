#!/usr/bin/env python
"""Run every scanner on its schedule and deliver the results to Telegram.

Weekdays, US Eastern time (defaults, all configurable in .env):

* Scanner A, pre-market gappers: from SCAN_TIME_ET (08:30) every SCAN_INTERVAL_MIN (30)
  until SCAN_END_ET (14:00).
* Scanner B, Trend-join Long: STRATEGY_OFFSET_MIN (5) minutes after each Scanner A run,
  on the tickers that run produced, continuing until STRATEGY_END_ET (15:00).
* After each A/B run the combined report (sections A, B, C) is sent to Telegram.

Every day, around the clock:

* Scanner C watch: every SPECIAL_WATCH_INTERVAL_MIN (5) minutes the special watchlist
  (currency pairs, gold, bitcoin) is checked and a Telegram alert goes out the moment a
  new Trend-join Long entry appears (with entry, stop and target), and again when that
  signal hits the stop, the target, or weakens.

The schedule is defined in New York time, so it stays correct through US daylight-saving
changes no matter which time zone this machine uses.

Examples:
    python scheduler.py                 # wait for the next job, run it, repeat
    python scheduler.py --run-now       # run Scanner A, B and a C check immediately, then keep the schedule
    python scheduler.py --print-schedule
"""
from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from premarket_scanner import config, report, scanner, special_watch, strategy, tunnel, tv_webhook
from premarket_scanner import telegram_notify as tg

log = logging.getLogger("scheduler")
ET = ZoneInfo(config.MARKET_TZ)


def _parse_hhmm(text: str) -> dtime:
    hour, minute = (int(x) for x in text.split(":"))
    return dtime(hour, minute)


def slots(start: dtime, end: dtime, interval_min: int) -> list[dtime]:
    """Every time from ``start`` to ``end`` inclusive, ``interval_min`` apart."""
    anchor = datetime(2000, 1, 3)
    t = datetime.combine(anchor, start)
    stop = datetime.combine(anchor, end)
    out: list[dtime] = []
    while t <= stop:
        out.append(t.time())
        t += timedelta(minutes=max(1, interval_min))
    return out


def scan_slots() -> list[dtime]:
    return slots(_parse_hhmm(config.SCAN_TIME_ET), _parse_hhmm(config.SCAN_END_ET), config.SCAN_INTERVAL_MIN)


def strategy_slots() -> list[dtime]:
    first = datetime.combine(datetime(2000, 1, 3), _parse_hhmm(config.SCAN_TIME_ET)) + timedelta(
        minutes=config.STRATEGY_OFFSET_MIN
    )
    return slots(first.time(), _parse_hhmm(config.STRATEGY_END_ET), config.SCAN_INTERVAL_MIN)


def watch_slots() -> list[dtime]:
    return slots(dtime(0, 0), dtime(23, 59), config.SPECIAL_WATCH_INTERVAL_MIN)


def next_occurrence(after: datetime, times: list[dtime], weekdays_only: bool = True) -> datetime:
    """Earliest datetime strictly after ``after`` whose wall-clock time is in ``times``."""
    day = after.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(10):
        if not weekdays_only or day.weekday() < 5:
            for t in sorted(times):
                candidate = day.replace(hour=t.hour, minute=t.minute)
                if candidate > after:
                    return candidate
        day += timedelta(days=1)
    raise RuntimeError("no schedule slot found")


@dataclass
class Job:
    name: str
    times: list[dtime]
    run: Callable[[], None]
    weekdays_only: bool = True
    send_report: bool = True


def run_premarket() -> None:
    result, path = scanner.run_and_save()
    log.info("Scanner A finished: %d matches -> %s", len(result.results), path)
    print(report.format_table(result), flush=True)


def run_strategy() -> None:
    try:
        result, path = strategy.run_strategy_scan()
    except FileNotFoundError as exc:
        log.warning("Scanner B skipped: %s", exc)
        return
    log.info(
        "Scanner B finished: %d hit(s), %d new -> %s",
        len(result["signals"]), len(result["new_signals"]), path,
    )
    print(strategy.format_table(result), flush=True)


def run_watch() -> None:
    messages = special_watch.run_watch(send=True)
    log.info("Scanner C watch: %d alert(s)", len(messages))
    if config.PORTFOLIO_ENABLED:
        try:
            from premarket_scanner import mt5_portfolio
            changes = mt5_portfolio.check_changes(send=True)
            if changes:
                log.info("MT5 portfolio: %d change(s) reported", len(changes))
        except Exception:  # noqa: BLE001
            log.exception("MT5 portfolio check failed")


def notify(job_name: str, error: Exception | None = None) -> None:
    """Send the combined report for today (plus a failure note when a job crashed)."""
    if not tg.is_configured():
        log.warning("Telegram not configured; report not sent. Run telegram_setup.py --save")
        return
    if error is not None:
        tg.send_message(tg.failure_line(job_name, error), admin_only=True)   # owner only
    tg.send_report_for(datetime.now(ET).date())


def run_job(job: Job) -> None:
    error: Exception | None = None
    try:
        job.run()
    except Exception as exc:  # noqa: BLE001 - keep the loop alive
        log.exception("job %s failed", job.name)
        error = exc
    if job.send_report:
        try:
            notify(job.name, error)
        except Exception:  # noqa: BLE001
            log.exception("Telegram notification failed")
        if config.SPECIAL_WATCH_ENABLED:
            # an A/B run occupies the watch slot at the same minute; check once now instead
            try:
                run_watch()
            except Exception:  # noqa: BLE001
                log.exception("Scanner C watch failed")


def setup_logging(log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )
    for noisy in ("yfinance", "urllib3", "peewee", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_jobs() -> list[Job]:
    jobs = [
        Job("Scanner A (pre-market)", scan_slots(), run_premarket),
        Job("Scanner B (Trend-join Long)", strategy_slots(), run_strategy),
    ]
    if config.SPECIAL_WATCH_ENABLED and config.SPECIAL_WATCHLIST:
        jobs.append(Job("Scanner C (watch)", watch_slots(), run_watch, weekdays_only=False, send_report=False))
    return jobs


def describe_schedule(jobs: list[Job]) -> str:
    parts = []
    for j in jobs:
        ts = [t.strftime("%H:%M") for t in j.times]
        if len(ts) > 24:
            parts.append(f"{j.name}: every {config.SPECIAL_WATCH_INTERVAL_MIN} min, all day, every day "
                         f"({len(ts)} checks)")
        else:
            parts.append(f"{j.name}: {ts[0]}-{ts[-1]} ET, {len(ts)} run(s) ({', '.join(ts)})")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scanner scheduler with Telegram delivery")
    parser.add_argument("--run-now", action="store_true", help="run every job once immediately on start")
    parser.add_argument("--print-schedule", action="store_true", help="print the run times and exit")
    parser.add_argument(
        "--log-file",
        type=Path,
        default=config.PROJECT_ROOT / "logs" / "scheduler.log",
        help="append logs here (default: logs/scheduler.log)",
    )
    args = parser.parse_args(argv)

    jobs = build_jobs()
    if args.print_schedule:
        print(describe_schedule(jobs))
        return 0

    setup_logging(args.log_file)
    for line in describe_schedule(jobs).splitlines():
        log.info(line)
    log.info("Telegram delivery: %s", "configured" if tg.is_configured() else "NOT configured (run telegram_setup.py --save)")
    if config.TV_WEBHOOK_ENABLED:
        try:
            server = tv_webhook.start_in_background()
        except OSError as exc:
            server = None
            log.error("TradingView webhook server could not start on port %d: %s", config.TV_WEBHOOK_PORT, exc)
        if server is not None and config.TUNNEL_ENABLED:
            tunnel.start_in_background(config.TV_WEBHOOK_PORT)
    if config.TELEGRAM_COMMANDS_ENABLED:
        from premarket_scanner import telegram_commands
        telegram_commands.start_in_background()

    if args.run_now:
        for job in jobs:
            run_job(job)

    while True:
        now = datetime.now(ET)
        job, due = min(((j, next_occurrence(now, j.times, j.weekdays_only)) for j in jobs), key=lambda x: x[1])
        if job.send_report:
            log.info(
                "next: %s at %s ET (%s local)",
                job.name,
                due.strftime("%a %Y-%m-%d %H:%M"),
                due.astimezone().strftime("%a %Y-%m-%d %H:%M %Z"),
            )
        while True:
            remaining = (due - datetime.now(ET)).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(60, remaining))
        run_job(job)


if __name__ == "__main__":
    main()
