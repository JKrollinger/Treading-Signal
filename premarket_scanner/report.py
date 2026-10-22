"""Console table and JSON output for a scan."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

from . import config
from .models import ScanResult

CATALYST_WIDTH = 72


def output_path(scan_date: str, output_dir: Path | None = None) -> Path:
    return (output_dir or config.OUTPUT_DIR) / f"premarket_scan_{scan_date}.json"


def save_json(result: ScanResult, output_dir: Path | None = None) -> Path:
    path = output_path(result.date, output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _fmt_volume(v: int | None) -> str:
    return f"{v:,}" if v is not None else "n/a"


def format_table(result: ScanResult) -> str:
    lines: list[str] = []
    lines.append(f"PRE-MARKET SCAN  {result.date}   market state: {result.market_state or '?'}   generated: {result.generated_at}")
    c = result.criteria
    lines.append(
        f"criteria: gap > {c['min_gap_pct']}%  |  price > ${c['min_price']}  |  "
        f"pre-market volume > {c['min_premarket_volume']:,}  |  universe: {result.universe_size:,} tickers, "
        f"{result.gap_candidates} passed price+gap"
    )
    for w in result.warnings:
        lines.append(f"WARNING: {w}")
    lines.append("")

    if not result.results:
        lines.append("No stocks matched all criteria.")
        return "\n".join(lines)

    header = f"{'TICKER':<8}{'PRICE':>9}{'GAP %':>9}{'PRE-MKT VOL':>14}  CATALYST"
    lines.append(header)
    lines.append("-" * (len(header) + CATALYST_WIDTH - 8))
    indent = " " * 42
    for r in result.results:
        catalyst = r.catalyst or "No catalyst found."
        if r.session != "premarket":
            catalyst = f"[{r.session} session] " + catalyst
        wrapped = textwrap.wrap(catalyst, width=CATALYST_WIDTH) or [""]
        lines.append(
            f"{r.ticker:<8}{r.price:>9.2f}{r.gap_pct:>+8.2f}%{_fmt_volume(r.premarket_volume):>14}  {wrapped[0]}"
        )
        for extra in wrapped[1:]:
            lines.append(indent + extra)
    lines.append("")
    lines.append(f"{len(result.results)} match(es).")
    return "\n".join(lines)
