"""Data classes shared by the scanner modules."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


@dataclass
class Candidate:
    """One stock that passed the price/gap screen (and possibly the volume screen)."""

    ticker: str
    name: str
    price: float                 # current pre-market price (regular price when in fallback)
    previous_close: float
    gap_pct: float               # % change vs previous close
    session: str                 # "premarket" | "regular"
    market_state: str            # Yahoo marketState: PRE, REGULAR, POST, CLOSED, ...
    quote_time: datetime         # time of the quote (US Eastern)
    exchange: str = ""
    premarket_volume: int | None = None
    volume_source: str | None = None
    catalyst: str | None = None
    catalyst_source: str | None = None
    catalyst_url: str | None = None
    catalyst_time: str | None = None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["price"] = round(self.price, 4)
        d["previous_close"] = round(self.previous_close, 4)
        d["gap_pct"] = round(self.gap_pct, 2)
        d["quote_time"] = self.quote_time.isoformat()
        return d


@dataclass
class ScanResult:
    date: str                     # YYYY-MM-DD (US Eastern trading date)
    generated_at: str             # ISO timestamp with offset
    market_state: str
    criteria: dict[str, Any]
    universe_size: int
    gap_candidates: int           # passed price + gap screen
    results: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "generated_at": self.generated_at,
            "market_state": self.market_state,
            "criteria": self.criteria,
            "universe_size": self.universe_size,
            "gap_candidates": self.gap_candidates,
            "count": len(self.results),
            "warnings": self.warnings,
            "results": [c.to_json() for c in self.results],
        }
