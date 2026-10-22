"""Scan orchestration: universe -> price/gap screen -> volume screen -> catalysts -> JSON."""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date
from pathlib import Path

from . import config, news, report, universe, volume, yahoo
from .models import Candidate, ScanResult

log = logging.getLogger(__name__)


def criteria() -> dict:
    return {
        "min_gap_pct": config.MIN_GAP_PCT,
        "min_price": config.MIN_PRICE,
        "min_premarket_volume": config.MIN_PREMARKET_VOLUME,
        "universe_source": config.UNIVERSE_SOURCE,
        "universe_min_avg_volume": config.UNIVERSE_MIN_AVG_VOLUME,
    }


def _dominant_market_state(quotes: list[dict]) -> str:
    states = Counter(str(q.get("marketState") or "") for q in quotes)
    states.pop("", None)
    return states.most_common(1)[0][0] if states else ""


def run_scan(scan_date: date | None = None, skip_news: bool = False) -> ScanResult:
    started = yahoo.now_et()
    scan_date = scan_date or started.date()
    warnings: list[str] = []
    log.info("scan for %s started at %s ET", scan_date, started.strftime("%H:%M:%S"))

    # 1. Universe + quotes -------------------------------------------------------
    quotes = universe.get_universe_quotes(warnings)
    market_state = _dominant_market_state(quotes)
    if (
        config.UNIVERSE_SOURCE == "yahoo"
        and market_state == "PRE"
        and quotes
        and not any(q.get("preMarketPrice") for q in quotes)
    ):
        # Screener rows normally carry pre-market fields; re-quote if they do not.
        log.info("screener rows lack pre-market fields; re-quoting %d symbols", len(quotes))
        quotes = yahoo.fetch_quotes([q["symbol"] for q in quotes if q.get("symbol")])

    # 2. Price + gap screen -------------------------------------------------------
    candidates: list[Candidate] = []
    fresh = 0
    for q in quotes:
        c = yahoo.normalize_quote(q, scan_date)
        if c is None:
            continue
        fresh += 1
        if c.gap_pct > config.MIN_GAP_PCT and c.price > config.MIN_PRICE:
            candidates.append(c)
    log.info(
        "universe=%d fresh=%d gap>%s%% & price>$%s -> %d candidates (market state %s)",
        len(quotes), fresh, config.MIN_GAP_PCT, config.MIN_PRICE, len(candidates), market_state or "?",
    )
    if quotes and fresh == 0:
        warnings.append(
            f"No Yahoo quotes dated {scan_date}: the market is closed or it is before 04:00 ET, so there is nothing to scan"
        )
    regular = sum(1 for c in candidates if c.session == "regular")
    if regular:
        warnings.append(
            f"{regular} candidate(s) reflect today's regular session (change vs previous close) "
            "because the scan ran after the 09:30 ET open"
        )

    # 3. Pre-market volume screen -------------------------------------------------
    failed = volume.attach_premarket_volume(candidates, scan_date)
    if candidates and failed == len(candidates):
        warnings.append(
            "Pre-market volume source (Nasdaq) is unavailable; volume filter NOT applied, "
            "results below passed only the price and gap checks"
        )
        passed = list(candidates)
    else:
        if failed:
            missing = ", ".join(c.ticker for c in candidates if c.premarket_volume is None)
            warnings.append(f"Pre-market volume unavailable for {failed} candidate(s), excluded: {missing}")
        passed = [
            c for c in candidates
            if c.premarket_volume is not None and c.premarket_volume > config.MIN_PREMARKET_VOLUME
        ]
    passed.sort(key=lambda c: c.gap_pct, reverse=True)
    log.info("volume>%s -> %d matches", f"{config.MIN_PREMARKET_VOLUME:,}", len(passed))

    # 4. Catalysts ----------------------------------------------------------------
    if not skip_news:
        news.attach_catalysts(passed, warnings)

    return ScanResult(
        date=scan_date.isoformat(),
        generated_at=started.isoformat(timespec="seconds"),
        market_state=market_state,
        criteria=criteria(),
        universe_size=len(quotes),
        gap_candidates=len(candidates),
        results=passed,
        warnings=warnings,
    )


def run_and_save(
    scan_date: date | None = None,
    output_dir: Path | None = None,
    skip_news: bool = False,
) -> tuple[ScanResult, Path]:
    result = run_scan(scan_date=scan_date, skip_news=skip_news)
    path = report.save_json(result, output_dir)
    log.info("saved %s (%d matches)", path, len(result.results))
    return result, path
