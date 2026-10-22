"""Shared symbols for trade plans in Telegram text.

Every place that prints a plan (Scanner B/C signals, Scanner C alerts, the MT5 account
report and position alerts, EA order requests) uses the same badge and arrows so a
reader can tell at a glance which side the trade is and where the entry and exits sit:

    🟢 BUY / 🔴 SELL   side of the trade
    ➡️  entry point     ⬅️  an exit that has happened (stop hit, expiry, half closed)
    🔻  stop level      🔺  take-profit level   (flipped for a SELL: stop above, T/P below)
"""
from __future__ import annotations

from collections.abc import Callable

BUY_BADGE = "🟢 BUY"
SELL_BADGE = "🔴 SELL"
ENTRY = "➡️"
EXIT = "⬅️"
UP = "🔺"
DOWN = "🔻"

_SELL_WORDS = {"sell", "short", "ขาย", "1"}   # MT5 position type 1 = sell


def is_sell(side) -> bool:
    return str(side).strip().lower() in _SELL_WORDS


def badge(side="buy") -> str:
    return SELL_BADGE if is_sell(side) else BUY_BADGE


def stop_arrow(side="buy") -> str:
    """The stop sits below a long and above a short."""
    return UP if is_sell(side) else DOWN


def tp_arrow(side="buy") -> str:
    return DOWN if is_sell(side) else UP


def plan(entry, stop, tp, fp: Callable, side="buy", thai: bool = True,
         stop_note: str = "", tp_note: str = "", tp_label: str = "T/P") -> str:
    """'➡️ เข้า 33.71 | 🔻 ตัดขาดทุน 33.53 (-0.54%) | 🔺 T/P 33.89 (+1R)'.

    ``fp`` formats a price; ``stop_note``/``tp_note`` are appended verbatim (give them
    their own leading space).
    """
    entry_lbl, stop_lbl = ("เข้า", "ตัดขาดทุน") if thai else ("entry", "stop")
    return (f"{ENTRY} {entry_lbl} {fp(entry)} | {stop_arrow(side)} {stop_lbl} {fp(stop)}{stop_note} | "
            f"{tp_arrow(side)} {tp_label} {fp(tp)}{tp_note}")
