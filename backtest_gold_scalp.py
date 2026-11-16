#!/usr/bin/env python
"""Gold scalping on 5-minute bars, both sides: which setups survive real costs?

    python backtest_gold_scalp.py                      # XAUUSD M5 from MT5 (about 17 months)
    python backtest_gold_scalp.py --cost 0.20 --split 2026-03-01
    python backtest_gold_scalp.py --timeframe M1       # 1-minute bars (about 3.5 months), exits in dollars:
                                                       # target 2-3 dollars, held a few minutes

Four families of setups, each long and short, one position at a time, never held longer
than ``--max-bars`` 5-minute bars:

* EMA pullback: EMA20 > EMA50 > EMA200 (mirror for shorts); a bar dips to the EMA20 and closes
  back above it in the trend's direction. Variant "H1": price must also be on the right side of
  the 1-hour trend (EMA600 on M5).
* Opening-range breakout: the first 30 minutes of London (07:00 UTC) or New York (13:30 UTC)
  set a range; the first close outside it within 3 hours is followed.
* Momentum: a wide bar (range > 1.2 ATR) closes beyond the high/low of the last hour.
* Band fade: in a flat market (EMA50 moved less than 0.5 ATR in an hour) a close outside the
  2-sigma band is faded.

Stop = k x ATR(14, M5), target = m x risk. Fills are at the signal bar's close; a bar that
touches both stop and target counts as a stop; a gap through the stop is filled at the open.
Every trade pays ``--cost`` dollars per ounce round trip (spread + commission; the demo's own
spread is about 0.10, a real account is nearer 0.30).

Rules are ranked on the first part of the history and judged on the part they never saw.

Results: output/backtest_gold_scalp_<date>.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from premarket_scanner import backtest, config

EXITS = {"SL1 TP1R": (1.0, 1.0), "SL1.5 TP1R": (1.5, 1.0), "SL1 TP1.5R": (1.0, 1.5), "SL2 TP1R": (2.0, 1.0),
         "SL2 TP0.5R": (2.0, 0.5)}
# 1-minute scalps: (stop, target) in dollars per ounce
EXITS_USD = {f"SL{sl:g}$ TP{tp:g}$": (sl, tp) for tp in (2.0, 2.5, 3.0) for sl in (2.0, 3.0, 5.0)}
SESSIONS = {"ทุกชั่วโมง": (0, 24), "ลอนดอน-นิวยอร์ก": (7, 20)}


def prepare(bars: pd.DataFrame, htf_span: int = 600) -> dict:
    tz = ZoneInfo(config.MT5_SERVER_TZ)
    utc = bars.index.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward").tz_convert("UTC")
    o, h, l, c = (bars[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    close = pd.Series(c)
    ema = lambda n: close.ewm(span=n, adjust=False).mean().to_numpy()  # noqa: E731
    prev = close.shift(1)
    tr = pd.concat([pd.Series(h - l), (pd.Series(h) - prev).abs(), (pd.Series(l) - prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    mid = close.rolling(20).mean().to_numpy()
    sd = close.rolling(20).std().to_numpy()
    hh12 = pd.Series(h).rolling(12).max().shift(1).to_numpy()
    ll12 = pd.Series(l).rolling(12).min().shift(1).to_numpy()
    e50 = ema(50)
    return {"o": o, "h": h, "l": l, "c": c, "atr": atr, "e20": ema(20), "e50": e50, "e200": ema(200), "e600": ema(htf_span),
            "mid": mid, "sd": sd, "hh12": hh12, "ll12": ll12, "e50_move": np.abs(e50 - pd.Series(e50).shift(12).to_numpy()),
            "hour": np.nan_to_num(utc.hour.to_numpy(float), nan=-1).astype(int), "minute": utc.minute.to_numpy(),
            "day": np.array([str(d)[:10] for d in utc.date]), "t": bars.index.to_numpy(), "n": len(c)}


def sig_ema_pullback(d: dict, htf: bool) -> np.ndarray:
    up = (d["e20"] > d["e50"]) & (d["e50"] > d["e200"])
    dn = (d["e20"] < d["e50"]) & (d["e50"] < d["e200"])
    if htf:
        up &= d["c"] > d["e600"]
        dn &= d["c"] < d["e600"]
    long = up & (d["l"] <= d["e20"]) & (d["c"] > d["e20"]) & (d["c"] > d["o"])
    short = dn & (d["h"] >= d["e20"]) & (d["c"] < d["e20"]) & (d["c"] < d["o"])
    return long.astype(int) - short.astype(int)


def sig_momentum(d: dict) -> np.ndarray:
    wide = (d["h"] - d["l"]) > 1.2 * d["atr"]
    return (wide & (d["c"] > d["hh12"])).astype(int) - (wide & (d["c"] < d["ll12"])).astype(int)


def sig_band_fade(d: dict) -> np.ndarray:
    flat = d["e50_move"] < 0.5 * d["atr"]
    return (flat & (d["c"] < d["mid"] - 2 * d["sd"])).astype(int) - (flat & (d["c"] > d["mid"] + 2 * d["sd"])).astype(int)


def sig_opening_range(d: dict, hour: int, minute: int) -> np.ndarray:
    sig = np.zeros(d["n"], dtype=int)
    mins = d["hour"] * 60 + d["minute"]
    start = hour * 60 + minute
    day, hi, lo, done = None, -np.inf, np.inf, True
    for i in range(d["n"]):
        if d["day"][i] != day:
            day, hi, lo, done = d["day"][i], -np.inf, np.inf, False
        m = mins[i]
        if start <= m < start + 30:
            hi, lo = max(hi, d["h"][i]), min(lo, d["l"][i])
        elif start + 30 <= m < start + 210 and not done and hi > lo:
            if d["c"][i] > hi:
                sig[i], done = 1, True
            elif d["c"][i] < lo:
                sig[i], done = -1, True
    return sig


def simulate(d: dict, sig: np.ndarray, sl_atr: float, tp_r: float, hours: tuple[int, int], cost: float, max_bars: int,
             dollars: bool = False, warmup: int = 700):
    """``dollars``: sl_atr and tp_r are a stop and a target in price units instead of ATRs and R."""
    o, h, l, c, atr, hour, t = d["o"], d["h"], d["l"], d["c"], d["atr"], d["hour"], d["t"]
    n, out, i = d["n"], [], warmup                    # skip the indicator warm-up
    gap = np.timedelta64(30, "m")
    while i < n - 1:
        s = sig[i]
        if s == 0 or not (hours[0] <= hour[i] < hours[1]) or not atr[i] > 0:
            i += 1
            continue
        entry, risk = c[i], (sl_atr if dollars else sl_atr * atr[i])
        stop, target = entry - s * risk, entry + s * (tp_r if dollars else tp_r * risk)
        exit_price, j = None, i
        for j in range(i + 1, min(i + max_bars, n - 1) + 1):
            if t[j] - t[j - 1] > gap:                 # the market was closed in between: out at the reopening
                exit_price = o[j]
                break
            if s * (o[j] - stop) <= 0:
                exit_price = o[j]
                break
            adverse, favourable = (l[j], h[j]) if s > 0 else (h[j], l[j])
            if s * (adverse - stop) <= 0:
                exit_price = stop
                break
            if s * (favourable - target) >= 0:
                exit_price = target
                break
        if exit_price is None:
            exit_price = c[j]
        out.append((str(t[i])[:10], s, (s * (exit_price - entry) - cost) / risk, risk))
        i = j + 1
    return out


def stats(trades: list) -> dict:
    if not trades:
        return {"trades": 0, "win": None, "avg_r": None, "total_r": 0.0, "pf": None, "dd": 0.0}
    rs = np.array([x[2] for x in trades])
    cum = np.cumsum(rs)
    loss = -rs[rs <= 0].sum()
    return {"trades": len(rs), "win": float((rs > 0).mean() * 100), "avg_r": float(rs.mean()), "total_r": float(rs.sum()),
            "pf": float(rs[rs > 0].sum() / loss) if loss > 0 else None, "dd": float((cum - np.maximum.accumulate(cum)).min())}


def cell(m: dict) -> str:
    if not m["trades"]:
        return f"{'-':>30}"
    return f"{m['trades']:>5}{m['win']:>5.0f}%{m['avg_r']:>+7.2f}{m['total_r']:>+8.1f}{m['pf'] or 0:>5.2f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gold scalping setups on M5 with an out-of-sample test")
    parser.add_argument("--cost", type=float, default=0.30, help="round-trip cost in dollars per ounce")
    parser.add_argument("--timeframe", choices=["M5", "M1"], default="M5")
    parser.add_argument("--split", help="default: 2026-03-01 for M5, 2026-09-25 for M1")
    parser.add_argument("--max-bars", type=int, help="longest hold in bars (default: 24 on M5 = 2 h, 10 on M1 = 10 min)")
    parser.add_argument("--min-trades", type=int, default=100)
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--output-dir", type=Path, default=config.OUTPUT_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    m1 = args.timeframe == "M1"
    args.split = args.split or ("2026-09-25" if m1 else "2026-03-01")
    args.max_bars = args.max_bars or (10 if m1 else 24)
    exits, warmup = (EXITS_USD, 3100) if m1 else (EXITS, 700)
    bars = backtest.load_mt5_bars("XAUUSD", args.timeframe)
    if not len(bars):
        print(f"XAUUSD: no {args.timeframe} data from MT5 (is the terminal open?)")
        return 1
    d = prepare(bars, 3000 if m1 else 600)
    days = len(set(d["day"]))
    print(f"XAUUSD {args.timeframe}: {d['n']} bars, hold <= {args.max_bars} bars, {bars.index[0]:%Y-%m-%d} to {bars.index[-1]:%Y-%m-%d} ({days} days); "
          f"median ATR {np.nanmedian(d['atr']):.2f}, cost {args.cost:.2f} per round trip; "
          f"rules chosen before {args.split}, tested after")

    signals = {"EMA pullback": sig_ema_pullback(d, False), "EMA pullback H1": sig_ema_pullback(d, True),
               "Momentum": sig_momentum(d), "Band fade": sig_band_fade(d)}
    orb = {"ORB ลอนดอน": sig_opening_range(d, 7, 0), "ORB นิวยอร์ก": sig_opening_range(d, 13, 30)}
    rows = []
    jobs = [(f, s, se, hrs) for f, s in signals.items() for se, hrs in SESSIONS.items()] + \
           [(f, s, "", (0, 24)) for f, s in orb.items()]
    for family, sig, session, hrs in jobs:
        for ex, (sl, tp) in exits.items():
            trades = simulate(d, sig, sl, tp, hrs, args.cost, args.max_bars, dollars=m1, warmup=warmup)
            first = [x for x in trades if x[0] < args.split]
            second = [x for x in trades if x[0] >= args.split]
            rows.append({"name": " | ".join(x for x in (family, session, ex) if x), "family": family,
                         "all": stats(trades), "first": stats(first), "second": stats(second),
                         "long": stats([x for x in trades if x[1] > 0]), "short": stats([x for x in trades if x[1] < 0]),
                         "per_day": len(trades) / days, "avg_usd": float(np.mean([x[2] * x[3] for x in trades])) if trades else None, "cost_r": float(np.mean([args.cost / x[3] for x in trades])) if trades else None})

    ranked = sorted((r for r in rows if r["first"]["trades"] >= args.min_trades), key=lambda r: r["first"]["avg_r"], reverse=True)
    good = [r for r in ranked if r["first"]["avg_r"] > 0]
    held = [r for r in good if r["second"]["trades"] >= 50 and r["second"]["avg_r"] > 0]
    solid = [r for r in held if min(r["first"]["avg_r"], r["second"]["avg_r"]) >= 0.05]
    sub = f"{'กฎ':<42}" + f"{'n  win   avgR    totR   PF':>30}" * 2 + "  เทรด/วัน"
    print(f"\n{args.top} ชุดที่ดีที่สุดในช่วงเลือกกฎ และผลในช่วงทดสอบ\n{'':<42}{'ช่วงเลือกกฎ':>30}{'ช่วงทดสอบ (ไม่เคยเห็น)':>30}\n{sub}")
    for r in ranked[:args.top]:
        print(f"{r['name'][:41]:<42}{cell(r['first'])}{cell(r['second'])}{r['per_day']:>8.1f}")
    print("\nดีที่สุดของแต่ละกลุ่ม (ทั้งช่วง) แยกฝั่งซื้อ/ขาย")
    for family in dict.fromkeys(r["family"] for r in rows):
        best = max((r for r in rows if r["family"] == family and r["all"]["trades"] >= args.min_trades),
                   key=lambda r: r["all"]["avg_r"], default=None)
        if best:
            print(f"{best['name'][:41]:<42}{cell(best['all'])}  ซื้อ{cell(best['long'])}  ขาย{cell(best['short'])}"
                  f"  ต้นทุน {best['cost_r']:.2f}R/เทรด  เฉลี่ย {best['avg_usd']:+.2f}$/ออนซ์")
    if m1:
        print("\nวินเรทที่ต้องได้เพื่อเท่าทุน (หลังหักต้นทุน): " + " · ".join(
            f"{k}: {(sl + args.cost) / (sl + tp) * 100:.0f}%" for k, (sl, tp) in exits.items()))
    print(f"\nทดสอบทั้งหมด {len(rows)} ชุด · มีเทรดพอจัดอันดับ {len(ranked)} · กำไรในช่วงเลือกกฎ {len(good)} · "
          f"ยังกำไรในช่วงทดสอบ {len(held)} · อย่างน้อย +0.05R ทั้งสองช่วง {len(solid)}")
    for r in solid[:5]:
        print(f"✓ {r['name']}: ทั้งช่วง{cell(r['all'])}  ขาดทุนสะสมลึกสุด {r['all']['dd']:.1f}R  {r['per_day']:.1f} เทรด/วัน")

    stamp = datetime.now(ZoneInfo(config.MARKET_TZ)).date().isoformat()
    out = args.output_dir / f"backtest_gold_scalp_{args.timeframe}_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"generated_at": datetime.now(ZoneInfo(config.MARKET_TZ)).isoformat(timespec="seconds"),
                               "cost": args.cost, "split": args.split, "max_bars": args.max_bars, "tested": len(rows),
                               "positive_first": len(good), "positive_both": len(held), "solid_both": len(solid),
                               "rows": rows}, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
