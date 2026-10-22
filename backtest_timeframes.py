#!/usr/bin/env python
"""Trend-join on 15/30-minute bars vs the daily system in use, with real-account costs.

    python backtest_timeframes.py --send

The intraday versions keep every rule and only shrink the "day": a 15- or 30-minute bar
takes its place, so the trend filter is the 200-bar SMA of that bar size, the trigger is a
5-minute close above the previous bar's high while at the high of the current bar, the stop
is 1 ATR(14) of that bar size, half comes off at 1R, the rest trails 3 ATR and is closed
after 10 of those bars (2.5 h for M15, 5 h for M30). Gold keeps its session/news filter,
as live. The daily system runs on hourly bars as live.

Costs are a round-trip spread + commission typical of a real account (not the demo's
near-zero spreads); slippage is not included. All three run over the same window: the
terminal keeps 100,000 bars per timeframe, so 5-minute history covers about 16 months.
Results: output/backtest_timeframes_mt5_<date>.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import backtest_sides
from premarket_scanner import backtest, config, special

# round-trip spread + commission in price units, typical of a real retail account
REAL_COST = {
    "XAUUSD": 0.30,      # ~0.20-0.25 spread + ~7 USD/lot commission (0.07 per oz)
    "EURUSD": 0.00010,   # 1.0 pip
    "GBPUSD": 0.00013,   # 1.3 pips
    "USDJPY": 0.012,     # 1.2 pips
    "USDTHB": 0.030,     # exotic pair, wide spread (rough)
}
COST_TEXT = "ทอง 0.30 ดอลลาร์, EURUSD 1.0 pip, GBPUSD 1.3 pip, USDJPY 1.2 pip, USDTHB 0.03 บาท ต่อรอบ"
WARMUP_DAYS = 7
SYSTEMS = [("daily", "รายวัน (ใช้อยู่)"), ("m30", "กราฟ 30 นาที"), ("m15", "กราฟ 15 นาที")]


def params_for(symbol: str, system: str) -> backtest.Params:
    p = backtest_sides.live_params(symbol, "long")
    p = replace(p, name=system, cost_abs=REAL_COST.get(symbol))
    if system == "m30":
        p = replace(p, level_minutes=30, hod_tol_atr=0.1)
    elif system == "m15":
        p = replace(p, level_minutes=15, hod_tol_atr=0.1)
    return p


def stats(trades: list[backtest.Trade], cost: float | None, exec_minutes: int) -> dict:
    m = backtest.compute_metrics(trades)
    if not trades:
        return m
    cost_rs = [(cost or 0.0) / abs(t.entry - t.stop) for t in trades]
    m["avg_cost_r"] = mean(cost_rs)
    m["avg_gross_r"] = m["avg_r"] + m["avg_cost_r"]
    m["avg_hold_hours"] = mean(t.bars_held for t in trades) * exec_minutes / 60.0
    days = max(1.0, (datetime.fromisoformat(trades[-1].entry_time) - datetime.fromisoformat(trades[0].entry_time)).days)
    m["trades_per_week"] = len(trades) / days * 7.0
    return m


def line(label: str, m: dict) -> str:
    if not m.get("trades"):
        return f"   {label}: ไม่มีเทรด"
    return (f"   {label}: {m['trades']} ไม้ ({m['trades_per_week']:.1f}/สัปดาห์) ชนะ {m['win_rate_pct']:.0f}% | "
            f"ก่อนหักต้นทุน {m['avg_gross_r']:+.2f}R ต้นทุน -{m['avg_cost_r']:.2f}R หลังหัก {m['avg_r']:+.2f}R | "
            f"รวม {m['total_r']:+.1f}R ลึกสุด {m['max_drawdown_r']:.1f}R")


def summary_th(per_symbol: dict, overall: dict, longrun: dict, window: str, names: dict) -> str:
    lines = [
        "📊 ทดสอบย้อนหลัง: กราฟ 15 นาที / 30 นาที เทียบระบบรายวันที่ใช้อยู่",
        f"ข้อมูล MT5 ช่วงเดียวกัน {window} | กฎเดิมทุกข้อ แค่ย่อ \"วัน\" เป็นแท่ง 15 หรือ 30 นาที "
        f"(ถือไม่เกิน 2.5 ชม. / 5 ชม.)",
        f"ต้นทุนแบบบัญชีจริง: {COST_TEXT} (ไม่รวมสลิปเพจ)",
        "R = เงินที่เสี่ยงต่อไม้ (ระบบตั้งไว้ 1% ของพอร์ต ดังนั้น +10R ≈ +10% ของพอร์ต)",
        "",
    ]
    for sym, by_sys in per_symbol.items():
        lines.append(f"• {names[sym]} ({sym})")
        for key, label in SYSTEMS:
            lines.append(line(label, by_sys[key]))
        best = max((k for k, _ in SYSTEMS if by_sys[k].get("trades", 0) >= 20),
                   key=lambda k: by_sys[k]["avg_r"], default=None)
        if best:
            lines.append(f"   ➜ ดีที่สุด: {dict(SYSTEMS)[best]}")
        lines.append("")
    lines.append("รวมทุกสินค้า")
    for key, label in SYSTEMS:
        lines.append(line(label, overall[key]))
    if longrun.get("trades"):
        lines.append(line("รายวัน ระยะยาว 2017-2026", longrun))
    if any(overall[k].get("max_drawdown_r", 0) <= -100 for k, _ in SYSTEMS):
        lines.append("หมายเหตุ: ขาดทุนสะสมลึกเกิน -100R เท่ากับพอร์ตหมดก่อนจบช่วงทดสอบ (ถ้าเสี่ยง 1% ต่อไม้)")
    lines.append("")
    d, m30, m15 = overall["daily"], overall["m30"], overall["m15"]
    better = [label for key, label in SYSTEMS[1:] if overall[key].get("trades") and d.get("trades")
              and overall[key]["avg_r"] > d["avg_r"] and overall[key]["total_r"] > d["total_r"]]
    if better:
        lines.append("สรุป: " + " และ ".join(better) + " ให้ผลดีกว่ารายวันในช่วงนี้ แต่ต้องทดสอบเดโมนานพอและระวังสลิปเพจ")
    else:
        lines.append("สรุป: กราฟสั้นไม่ดีกว่าระบบรายวันหลังหักต้นทุน แนะนำคงระบบรายวัน "
                     "และใช้กราฟ 15 นาทีช่วยหาจุดเข้าแทน")
    lines.append("ข้อจำกัด: ข้อมูลกราฟสั้นมีแค่ ~16 เดือน, ไม่รวมสลิปเพจซึ่งกระทบกราฟสั้นมากกว่า, "
                 "ต้นทุนเป็นค่าประมาณของบัญชีทั่วไป, ผลในอดีตไม่รับประกันอนาคต")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trend-join: M15 / M30 vs daily, real-account costs")
    parser.add_argument("--symbols", default="XAUUSD,EURUSD,USDJPY,USDTHB,GBPUSD")
    parser.add_argument("--send", action="store_true", help="send the Thai summary to the owner chat")
    parser.add_argument("--output-dir", type=Path, default=config.OUTPUT_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    per_symbol: dict[str, dict[str, dict]] = {}
    all_trades: dict[str, list] = {k: [] for k, _ in SYSTEMS}
    longrun_trades: list = []
    raw: dict = {}
    starts, ends = [], []
    for s in symbols:
        m5 = backtest.load_mt5_bars(s, "M5")
        h1 = backtest.load_mt5_hourly(s, 3700)
        if m5.empty or h1.empty:
            logging.warning("%s: no MT5 history, skipped", s)
            continue
        start = (m5.index[0] + timedelta(days=WARMUP_DAYS)).isoformat()
        starts.append(m5.index[0] + timedelta(days=WARMUP_DAYS))
        ends.append(m5.index[-1])
        logging.info("%s: M5 %d bars %s .. %s | H1 %d bars", s, len(m5), m5.index[0], m5.index[-1], len(h1))
        by_sys, raw[s] = {}, {}
        for key, _ in SYSTEMS:
            p = params_for(s, key)
            bars, exec_min = (h1, 60) if key == "daily" else (m5, 5)
            res = backtest.run_symbol(s, bars, p, "mt5")
            trades = [t for t in res.trades if t.entry_time >= start]
            by_sys[key] = stats(trades, p.cost_abs, exec_min)
            all_trades[key] += [(t, p.cost_abs, exec_min) for t in trades]
            raw[s][key] = {"params": p.__dict__, "metrics": by_sys[key], "skipped": res.skipped,
                           "trades": [t.__dict__ for t in trades]}
            if key == "daily":
                longrun_trades += [(t, p.cost_abs) for t in res.trades]
        per_symbol[s] = by_sys
    if not per_symbol:
        print("no data")
        return 1

    def pooled(items, exec_min_default=60):
        trades = sorted((x[0] for x in items), key=lambda t: t.entry_time)
        if not trades:
            return {"trades": 0}
        m = backtest.compute_metrics(trades)
        m["avg_cost_r"] = mean((x[1] or 0.0) / abs(x[0].entry - x[0].stop) for x in items)
        m["avg_gross_r"] = m["avg_r"] + m["avg_cost_r"]
        execs = [x[2] if len(x) > 2 else exec_min_default for x in items]
        m["avg_hold_hours"] = mean(x[0].bars_held * e / 60.0 for x, e in zip(items, execs))
        days = max(1.0, (datetime.fromisoformat(trades[-1].entry_time) - datetime.fromisoformat(trades[0].entry_time)).days)
        m["trades_per_week"] = len(trades) / days * 7.0
        return m

    overall = {k: pooled(v) for k, v in all_trades.items()}
    longrun = pooled(longrun_trades)
    window = f"{min(starts):%Y-%m} ถึง {max(ends):%Y-%m}"
    names = {s: special.display_name(s, True) for s in per_symbol}

    print(f"{'SYMBOL':8}{'SYSTEM':7}{'TRADES':>7}{'/WK':>6}{'WIN%':>6}{'GROSS R':>9}{'COST R':>8}{'NET R':>8}{'TOTAL':>8}{'MAXDD':>7}{'HOLD h':>8}")
    for s, by_sys in list(per_symbol.items()) + [("ALL", overall)]:
        for key, _ in SYSTEMS:
            m = by_sys[key]
            if not m.get("trades"):
                print(f"{s:8}{key:7}{0:>7}")
                continue
            print(f"{s:8}{key:7}{m['trades']:>7}{m['trades_per_week']:>6.1f}{m['win_rate_pct']:>6.1f}{m['avg_gross_r']:>9.3f}"
                  f"{m['avg_cost_r']:>8.3f}{m['avg_r']:>8.3f}{m['total_r']:>8.1f}{m['max_drawdown_r']:>7.1f}{m['avg_hold_hours']:>8.1f}")
    message = summary_th(per_symbol, overall, longrun, window, names)
    print()
    print(message)
    print(f"\n({len(message)} chars)")

    stamp = datetime.now(ZoneInfo(config.MARKET_TZ)).date().isoformat()
    out = args.output_dir / f"backtest_timeframes_mt5_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "generated_at": datetime.now(ZoneInfo(config.MARKET_TZ)).isoformat(timespec="seconds"),
        "window": window, "real_cost": REAL_COST, "overall": overall, "daily_longrun": longrun,
        "results": raw, "summary_th": message,
    }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"Saved: {out}")

    if args.send:
        from premarket_scanner import telegram_notify as tg
        print("Telegram:", "sent" if tg.send_message(message, admin_only=True) else "not sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
