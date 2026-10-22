#!/usr/bin/env python
"""Trend-join Long vs its mirror (Trend-join Short) vs both sides, per symbol.

    python backtest_sides.py --send                 # MT5 data, ~10 years, Thai summary to the owner chat
    python backtest_sides.py --days 730 --symbols XAUUSD,EURUSD

Rules are the live ones: v2-trail exit (stop 1 ATR, half off at 1R, Chandelier trail
3 ATR, hold <= 10 days) and the session/news filter only for TIME_FILTER_SYMBOLS (gold).
The short side mirrors every rule: yesterday's close below the 200-day SMA, a bar closing
below yesterday's low at the low of the day, stop 1 ATR above, trail 3 ATR above the
lowest low. "Both" lets the SMA pick the side each day, one trade at a time per symbol,
like the live watch. Results: output/backtest_sides_<source>_<date>.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from premarket_scanner import backtest, config, special

SIDES = [("long", "ซื้ออย่างเดียว"), ("short", "ขายอย่างเดียว"), ("both", "ทั้งสองฝั่ง")]
RECENT_DAYS = 730


def live_params(symbol: str, side: str) -> backtest.Params:
    p = backtest.Params(
        name=side, stop_atr_mult=config.STOP_ATR_MULT, target_r=None, hod_tol_pct=config.HOD_TOLERANCE_PCT,
        max_hold_days=config.MAX_HOLD_DAYS, cost_pct=0.03, trail_atr_mult=config.TRAIL_ATR_MULT,
        partial_r=config.PARTIAL_R, side=side,
    )
    if special.base_symbol(symbol) in {special.base_symbol(s) for s in config.TIME_FILTER_SYMBOLS}:
        p = replace(p, session_utc=(config.SESSION_UTC_START, config.SESSION_UTC_END),
                    friday_cutoff_utc=config.FRIDAY_CUTOFF_UTC, news_blackout=True)
    return p


def recent(trades: list[backtest.Trade], since: str) -> dict:
    return backtest.compute_metrics([t for t in trades if t.entry_time >= since])


def cell(m: dict) -> str:
    if not m.get("trades"):
        return "ไม่มีเทรด"
    return (f"{m['trades']} เทรด / ชนะ {m['win_rate_pct']:.0f}% / {m['avg_r']:+.2f}R / รวม {m['total_r']:+.1f}R / "
            f"ลึกสุด {m['max_drawdown_r']:.1f}R")


def short_verdict(m: dict) -> str:
    if m.get("trades", 0) < 20:
        return "สัญญาณขายน้อยเกินไปที่จะสรุป"
    pf = m["profit_factor"]
    if m["avg_r"] >= 0.10 and pf >= 1.25:
        return "ฝั่งขายมีขอบได้เปรียบ น่าเปิดทดลองบนเดโม"
    if m["avg_r"] > 0.0:
        return "ฝั่งขายกำไรเล็กน้อย ยังไม่ชัดพอ"
    return "ฝั่งขายขาดทุน ไม่ควรเปิด"


def summary_th(results: dict, names: dict, period: str, since: str) -> str:
    lines = [
        "📊 ทดสอบย้อนหลัง: ซื้ออย่างเดียว / ขายอย่างเดียว / ทั้งสองฝั่ง",
        f"ข้อมูล MT5 รายชั่วโมง {period} | กฎที่ใช้จริง: ตัดขาดทุน 1 ATR, ปิดครึ่งไม้ที่ 1R, "
        f"เลื่อนจุดตัดขาดทุน {config.TRAIL_ATR_MULT:g} ATR, ถือไม่เกิน {config.MAX_HOLD_DAYS} วัน, ค่าธรรมเนียม 0.03%",
        "ตัวเลข = จำนวนเทรด / ชนะ% / กำไรเฉลี่ยต่อเทรด / กำไรรวม / ขาดทุนสะสมลึกสุด (หน่วย R = เท่าของเงินที่เสี่ยงต่อไม้)",
        "",
    ]
    picks = []
    for sym, by_side in results.items():
        lines.append(f"• {names[sym]} ({sym})")
        for side, label in SIDES:
            lines.append(f"   {label}: {cell(by_side[side].metrics)}")
        sm = by_side["short"].metrics
        verdict = short_verdict(sm)
        lines.append(f"   ➜ {verdict}")
        if verdict.startswith("ฝั่งขายมีขอบ"):
            picks.append(sym)
        lines.append("")
    lines.append("รวมทุกสินค้า (ทั้งช่วง | 2 ปีล่าสุด)")
    for side, label in SIDES:
        all_trades = [t for by_side in results.values() for t in by_side[side].trades]
        o = backtest.compute_metrics(all_trades)
        r = recent(all_trades, since)
        pf = backtest._f(o.get("profit_factor")) if o.get("trades") else "n/a"
        lines.append(f"   {label}: {cell(o)} / PF {pf}")
        lines.append(f"      2 ปีล่าสุด: {cell(r)}")
    lines.append("")
    if picks:
        lines.append("สรุป: ฝั่งขายน่าทดลองต่อเฉพาะ " + ", ".join(f"{names[s]} ({s})" for s in picks)
                     + " ที่เหลือคงซื้ออย่างเดียว")
    else:
        lines.append("สรุป: ไม่มีสินค้าไหนที่ฝั่งขายผ่านเกณฑ์ (เฉลี่ย +0.10R และ PF 1.25 ขึ้นไป อย่างน้อย 20 เทรด) "
                     "แนะนำคงซื้ออย่างเดียว")
    lines.append("ข้อจำกัด: ไม่รวมค่า swap ข้ามคืนและสลิปเพจ, ไม่มีบิทคอยน์เพราะโบรกเกอร์เดโมไม่มีข้อมูล, "
                 "ผลในอดีตไม่รับประกันอนาคต")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trend-join: long vs short vs both, per symbol")
    parser.add_argument("--symbols", default="XAUUSD,EURUSD,USDJPY,USDTHB,GBPUSD")
    parser.add_argument("--days", type=int, default=3700)
    parser.add_argument("--send", action="store_true", help="send the Thai summary to the owner chat")
    parser.add_argument("--output-dir", type=Path, default=config.OUTPUT_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    results: dict[str, dict[str, backtest.SymbolResult]] = {}
    first, last = None, None
    for s in symbols:
        bars = backtest.load_mt5_hourly(s, args.days)
        if bars.empty:
            logging.warning("%s: no MT5 history, skipped", s)
            continue
        logging.info("%s: %d hourly bars %s .. %s", s, len(bars), bars.index[0], bars.index[-1])
        results[s] = {side: backtest.run_symbol(s, bars, live_params(s, side), "mt5") for side, _ in SIDES}
        trades = [t for r in results[s].values() for t in r.trades]
        if trades:
            t0 = min(t.entry_time for t in trades)
            first = t0 if first is None else min(first, t0)
        last = max(last or "", bars.index[-1].isoformat())
    if not results:
        print("no data")
        return 1

    since = (datetime.fromisoformat(last) - timedelta(days=RECENT_DAYS)).isoformat()
    period = f"{first[:7]} ถึง {last[:7]}" if first else f"{args.days} วัน"
    names = {s: special.display_name(s, True) for s in results}

    print(f"{'SYMBOL':<8}{'SIDE':<7}{'TRADES':>7}{'WIN%':>6}{'AVG R':>8}{'TOTAL R':>9}{'PF':>6}{'MAXDD':>8}  EXITS / SKIPPED")
    for s, by_side in results.items():
        for side, _ in SIDES:
            m = by_side[side].metrics
            if not m.get("trades"):
                print(f"{s:<8}{side:<7}{0:>7}")
                continue
            print(f"{s:<8}{side:<7}{m['trades']:>7}{m['win_rate_pct']:>6.1f}{m['avg_r']:>8.2f}{m['total_r']:>9.1f}"
                  f"{backtest._f(m['profit_factor']):>6}{m['max_drawdown_r']:>8.1f}  {m['exits']} {by_side[side].skipped}")
    message = summary_th(results, names, period, since)
    print()
    print(message)

    stamp = datetime.now(ZoneInfo(config.MARKET_TZ)).date().isoformat()
    out = args.output_dir / f"backtest_sides_mt5_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "generated_at": datetime.now(ZoneInfo(config.MARKET_TZ)).isoformat(timespec="seconds"),
        "source": "mt5", "days": args.days, "recent_since": since,
        "results": {s: {side: {"params": live_params(s, side).__dict__, "metrics": r.metrics, "skipped": r.skipped,
                               "trades": [t.__dict__ for t in r.trades]}
                        for side, r in by_side.items()} for s, by_side in results.items()},
        "summary_th": message,
    }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nSaved: {out}")

    if args.send:
        from premarket_scanner import telegram_notify as tg
        print("Telegram:", "sent" if tg.send_message(message, admin_only=True) else "not sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
