"""Telegram delivery of both scanners' results.

Every scheduled run sends one message with two sections, always in this order:

    [Scanner A: Pre-market Gappers]
    ...the latest pre-market list...

    [Scanner B: Trend-join Long Hits]
    ...the trade setups triggered so far today...

Credentials are re-read from ``.env`` before every send, so adding the bot token or chat
id later does not require restarting the scheduler.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from . import config, report, strategy, translate

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_CHARS = 4000          # Telegram's hard limit is 4096
HEADER_A = "[Scanner A: Pre-market Gappers]"
HEADER_B = "[Scanner B: Trend-join Long Hits]"
HEADER_A_TH = "[Scanner A: หุ้นที่พุ่งขึ้นก่อนตลาดเปิด]"
HEADER_B_TH = "[Scanner B: หุ้นที่เข้าเงื่อนไขซื้อ Trend-join Long]"
DISCLAIMER_TH = "ข้อมูลอัตโนมัติเพื่อประกอบการตัดสินใจ ไม่ใช่คำแนะนำการลงทุน"
ET = ZoneInfo(config.MARKET_TZ)
BKK = ZoneInfo("Asia/Bangkok")
THAI_MONTHS = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
_SUFFIX_RE = re.compile(
    r",?\s+(Inc\.?|Corp\.?|Corporation|Ltd\.?|Limited|Holdings?|PLC|plc|Co\.?|N\.V\.|S\.A\.|Group|Company)\s*$"
)


def is_thai() -> bool:
    return config.REPORT_LANGUAGE == "th"


# Credentials -------------------------------------------------------------------
def credentials() -> tuple[str, str]:
    """(token, chat_id), reloaded from .env each call."""
    load_dotenv(config.PROJECT_ROOT / ".env", override=True)
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").split("#")[0].strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").split("#")[0].strip()
    return token, chat_id


def is_configured() -> bool:
    token, chat_id = credentials()
    return bool(token and chat_id)


# Bot API -----------------------------------------------------------------------
def api_call(method: str, token: str, **params) -> dict:
    resp = requests.post(API_URL.format(token=token, method=method), json=params, timeout=config.HTTP_TIMEOUT)
    try:
        payload = resp.json()
    except ValueError:
        payload = {"ok": False, "description": resp.text[:200]}
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: HTTP {resp.status_code} {payload.get('description')}")
    return payload.get("result") or {}


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split on line boundaries so no chunk exceeds Telegram's size limit."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:                # a single oversized line
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_message(text: str) -> bool:
    token, chat_id = credentials()
    if not token or not chat_id:
        log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing); message not sent")
        return False
    try:
        for chunk in split_message(text):
            api_call("sendMessage", token, chat_id=chat_id, text=chunk, disable_web_page_preview=True)
    except Exception as exc:  # noqa: BLE001
        log.error("Telegram send failed: %s", exc)
        return False
    log.info("Telegram message sent (%d chars)", len(text))
    return True


# Report formatting -------------------------------------------------------------
def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return None


def _hhmm(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except ValueError:
        return iso


def _money(v) -> str:
    return "n/a" if v is None else f"${float(v):,.2f}"


def _important(warnings: list[str]) -> list[str]:
    keys = ("failed", "unavailable", "no yahoo quotes", "nothing to scan")
    return [w for w in warnings or [] if any(k in w.lower() for k in keys)]


def section_a(pre: dict | None) -> list[str]:
    lines = [HEADER_A]
    if not pre:
        lines.append("No pre-market scan has run yet today.")
        return lines
    c = pre.get("criteria") or {}
    results = pre.get("results") or []
    lines.append(
        f"{pre.get('date')} {_hhmm(pre.get('generated_at'))} ET | {len(results)} match(es) from "
        f"{pre.get('universe_size', 0):,} scanned | gap>{c.get('min_gap_pct')}% price>${c.get('min_price')} "
        f"pre-mkt vol>{int(c.get('min_premarket_volume', 0)):,}"
    )
    if not results:
        lines.append("No gappers matched.")
    for i, r in enumerate(results, 1):
        vol = r.get("premarket_volume")
        vol_txt = f"{int(vol):,}" if vol is not None else "n/a"
        tag = "" if r.get("session") == "premarket" else "  (regular session)"
        lines.append(
            f"{i}. {r['ticker']}  {_money(r.get('price'))}  {float(r.get('gap_pct') or 0):+.2f}%  "
            f"pre-mkt vol {vol_txt}{tag}"
        )
        if r.get("catalyst"):
            lines.append(f"   {r['catalyst']}")
    for w in _important(pre.get("warnings") or []):
        lines.append(f"Note: {w}")
    return lines


def section_b(strat: dict | None) -> list[str]:
    lines = [HEADER_B]
    if not strat:
        lines.append("No strategy scan has run yet today.")
        return lines
    signals = strat.get("signals") or []
    new = set(strat.get("new_signals") or [])
    checked = len(strat.get("scanned") or [])
    lines.append(
        f"{strat.get('date')} {_hhmm(strat.get('generated_at'))} ET | {len(signals)} hit(s) from "
        f"{checked} checked | {len(new)} new"
    )
    if not signals:
        gen = strat.get("generated_at")
        try:
            before_window = datetime.fromisoformat(gen).strftime("%H:%M") < config.STRATEGY_START_ET
        except (TypeError, ValueError):
            before_window = False
        lines.append(
            f"No valid setups yet (signals count from {config.STRATEGY_START_ET} ET)." if before_window
            else "No valid setups."
        )
    for i, s in enumerate(signals, 1):
        tag = "  NEW" if s["ticker"] in new else ""
        lines.append(
            f"{i}. {s['ticker']}  entry {_money(s.get('entry_price'))}{tag}  "
            f"(triggered {_hhmm(s.get('first_triggered_at'))} ET)"
        )
        lines.append(
            f"   above Y-high {_money(s.get('yesterday_high'))}, PM high {_money(s.get('premarket_high'))}, "
            f"HOD {_money(s.get('high_of_day'))} | Y-close {_money(s.get('yesterday_close'))} > "
            f"SMA200 {_money(s.get('sma200'))}"
        )
        if s.get("gap_pct") is not None:
            lines.append(f"   gap {float(s['gap_pct']):+.2f}% | pre-mkt vol {int(s.get('premarket_volume') or 0):,}")
        if s.get("catalyst"):
            lines.append(f"   {s['catalyst']}")
    for w in _important(strat.get("warnings") or []):
        lines.append(f"Note: {w}")
    return lines


# Thai, plain-language formatting ------------------------------------------------
def _when_th(iso: str | None) -> str:
    """'08:30 น. เวลาตลาดสหรัฐ (19:30 น. ไทย)'"""
    if not iso:
        return "ไม่ทราบเวลา"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return f"{dt.astimezone(ET).strftime('%H:%M')} น. เวลาตลาดสหรัฐ ({dt.astimezone(BKK).strftime('%H:%M')} น. ไทย)"


def _date_th(iso_date: str | None) -> str:
    try:
        d = date.fromisoformat(iso_date or "")
    except ValueError:
        return iso_date or ""
    return f"{d.day} {THAI_MONTHS[d.month - 1]} {d.year}"


def _short_name(name: str | None) -> str:
    text = (name or "").strip()
    for _ in range(3):
        text = _SUFFIX_RE.sub("", text).strip()
    return text


def _label(ticker: str, name: str | None) -> str:
    short = _short_name(name)
    return f"{ticker} ({short})" if short and short.upper() != ticker.upper() else ticker


def _news_line_th(catalyst: str | None) -> str | None:
    if not catalyst:
        return None
    if config.TRANSLATE_NEWS:
        th = translate.to_thai(catalyst)
        if th:
            return f"   ข่าว: {th}"
    return f"   ข่าว (ภาษาอังกฤษ): {catalyst}"


_WARNING_TH = (
    ("nasdaq) is unavailable", "ดึงปริมาณซื้อขายก่อนตลาดเปิดจาก Nasdaq ไม่ได้ในรอบนี้ รายการจึงยังไม่ได้กรองด้วยปริมาณซื้อขาย"),
    ("pre-market volume unavailable for", "บางตัวดึงปริมาณซื้อขายไม่ได้ จึงไม่ได้นำมาแสดง"),
    ("screener failed", "ดึงรายชื่อหุ้นจาก Yahoo ไม่ได้ ใช้รายชื่อที่บันทึกไว้ก่อนหน้าแทน"),
    ("tradingview scanner failed", "ดึงข้อมูลจาก TradingView ไม่ได้ ใช้ข้อมูลจาก Yahoo แทน"),
    ("tradingview had no data", "TradingView ไม่มีข้อมูลบางตัว ใช้ข้อมูลจาก Yahoo แทน"),
    ("nothing to scan", "ยังไม่มีข้อมูลราคาของวันนี้ (ตลาดปิด หรือยังไม่ถึงเวลาซื้อขายก่อนเปิด)"),
    ("yahoo quotes failed", "ดึงราคาจาก Yahoo ไม่ได้"),
    ("yahoo finance history failed", "ดึงข้อมูลราคาย้อนหลังจาก Yahoo ไม่ได้"),
)


def _warning_th(w: str) -> str:
    low = w.lower()
    for key, text in _WARNING_TH:
        if key in low:
            return f"หมายเหตุ: {text}"
    return f"หมายเหตุ: {w}"


def section_a_th(pre: dict | None) -> list[str]:
    lines = [HEADER_A_TH]
    if not pre:
        lines.append("วันนี้ยังไม่ได้สแกน")
        return lines
    c = pre.get("criteria") or {}
    results = pre.get("results") or []
    lines.append(f"สแกนเมื่อ {_when_th(pre.get('generated_at'))} วันที่ {_date_th(pre.get('date'))}")
    lines.append(
        f"ตรวจหุ้นสหรัฐ {int(pre.get('universe_size') or 0):,} ตัว พบ {len(results)} ตัวที่ราคาขึ้นมากกว่า "
        f"{c.get('min_gap_pct', 5):g}% จากราคาปิดเมื่อวาน ราคาสูงกว่า ${c.get('min_price', 3):g} "
        f"และมีคนซื้อขายช่วงก่อนตลาดเปิดมากกว่า {int(c.get('min_premarket_volume') or 0):,} หุ้น"
    )
    if not results:
        lines.append("ไม่มีหุ้นตัวไหนเข้าเกณฑ์ในรอบนี้")
    for i, r in enumerate(results, 1):
        vol = r.get("premarket_volume")
        vol_txt = f"{int(vol):,} หุ้น" if vol is not None else "ไม่ทราบ"
        tag = "" if r.get("session") == "premarket" else " (วัดหลังตลาดเปิดแล้ว)"
        lines.append(
            f"{i}. {_label(r['ticker'], r.get('name'))} ราคา {_money(r.get('price'))} "
            f"ขึ้น {abs(float(r.get('gap_pct') or 0)):.2f}% จากเมื่อวาน ซื้อขายแล้ว {vol_txt}{tag}"
        )
        news = _news_line_th(r.get("catalyst"))
        if news:
            lines.append(news)
    for w in _important(pre.get("warnings") or []):
        lines.append(_warning_th(w))
    return lines


def section_b_th(strat: dict | None, names: dict[str, str] | None = None) -> list[str]:
    names = names or {}
    lines = [HEADER_B_TH]
    if not strat:
        lines.append("วันนี้ยังไม่ได้ตรวจ (จะเริ่มตรวจหลังสแกน A รอบแรก 5 นาที)")
        return lines
    signals = strat.get("signals") or []
    new = set(strat.get("new_signals") or [])
    scanned = strat.get("scanned") or []
    lines.append(f"ตรวจเมื่อ {_when_th(strat.get('generated_at'))} จากหุ้น {len(scanned)} ตัวในรายการด้านบน")
    lines.append(
        "เงื่อนไขที่ต้องผ่านครบทุกข้อ: ราคาตอนนี้สูงกว่าจุดสูงสุดของเมื่อวาน, ราคาปิดเมื่อวานอยู่เหนือเส้นค่าเฉลี่ย 200 วัน "
        "(แนวโน้มระยะยาวเป็นขาขึ้น) และราคากำลังทำจุดสูงสุดใหม่ของวัน สูงกว่าจุดสูงสุดช่วงก่อนตลาดเปิด"
    )
    if not signals:
        start = strategy._parse_hhmm(config.STRATEGY_START_ET)
        try:
            gen = datetime.fromisoformat(strat["generated_at"]).astimezone(ET)
            before = gen.time() < start
            start_th = datetime.combine(gen.date(), start, ET).astimezone(BKK).strftime("%H:%M")
        except (KeyError, ValueError):
            before, start_th = False, ""
        if before:
            lines.append(
                f"ยังไม่มีหุ้นผ่านครบทุกเงื่อนไข (เริ่มนับสัญญาณตั้งแต่ {start.strftime('%H:%M')} น. เวลาตลาดสหรัฐ "
                f"หรือ {start_th} น. ไทย)"
            )
        else:
            lines.append("ยังไม่มีหุ้นผ่านครบทุกเงื่อนไขในรอบนี้")
    else:
        lines.append(f"ผ่านเงื่อนไข {len(signals)} ตัว (ใหม่ในรอบนี้ {len(new)} ตัว):")
        for i, s in enumerate(signals, 1):
            tag = " ใหม่!" if s["ticker"] in new else ""
            lines.append(
                f"{i}. {_label(s['ticker'], names.get(s['ticker']))} สัญญาณซื้อที่ราคา {_money(s.get('entry_price'))}{tag} "
                f"เมื่อ {_when_th(s.get('first_triggered_at'))}"
            )
            lines.append(
                f"   เหตุผล: สูงกว่าจุดสูงสุดเมื่อวาน ({_money(s.get('yesterday_high'))}) | "
                f"สูงกว่าจุดสูงสุดก่อนตลาดเปิด ({_money(s.get('premarket_high'))}) | "
                f"เป็นจุดสูงสุดของวัน ({_money(s.get('high_of_day'))}) | "
                f"ปิดเมื่อวาน {_money(s.get('yesterday_close'))} เหนือเส้นค่าเฉลี่ย 200 วัน ({_money(s.get('sma200'))})"
            )
            if s.get("gap_pct") is not None:
                lines.append(
                    f"   ก่อนตลาดเปิดวันนี้ขึ้น {float(s['gap_pct']):+.2f}% ซื้อขาย {int(s.get('premarket_volume') or 0):,} หุ้น"
                )
            news = _news_line_th(s.get("catalyst"))
            if news:
                lines.append(news)
    failed = [ev["ticker"] for ev in scanned if not ev.get("valid")]
    if failed:
        lines.append(f"ตรวจแล้วยังไม่ผ่านเงื่อนไข: {', '.join(failed)}")
    for w in _important(strat.get("warnings") or []):
        lines.append(_warning_th(w))
    return lines


def failure_line(job_name: str, error: Exception) -> str:
    if is_thai():
        return f"รอบนี้ทำงานผิดพลาด ({job_name}): {error}"
    return f"{job_name} failed: {error}"


# Scanner C: currency pairs, gold, bitcoin -------------------------------------------
HEADER_C = "[Scanner C: Special Watchlist]"
HEADER_C_TH = "[Scanner C: สินทรัพย์ที่สนใจเป็นพิเศษ]"


def _plan_text(entry, stop, target, currency: str, thai: bool) -> str:
    fp = lambda v: special_fmt(v, currency)  # noqa: E731
    if thai:
        return f"เข้า {fp(entry)} | ตัดขาดทุน {fp(stop)} | เป้าหมาย {fp(target)}"
    return f"entry {fp(entry)} | stop {fp(stop)} | target {fp(target)}"


def special_fmt(v, currency: str) -> str:
    from . import special

    return special.fmt_price(v, currency)


def _signal_text(it, rec: dict | None, thai: bool) -> str:
    """Status of the Trend-join signal for one Scanner C symbol, with the trade plan."""
    cur = it.currency
    if rec:
        plan = _plan_text(rec["entry"], rec["stop"], rec["target"], cur, thai)
        when = datetime.fromisoformat(rec["triggered_at"]).astimezone(BKK if thai else ET).strftime("%H:%M")
        if thai:
            return {
                "active": f"ผ่าน ({plan}, สัญญาณเมื่อ {when} น. ไทย)",
                "weakened": f"อ่อนลง ราคาหลุดใต้จุดสูงสุดเมื่อวานแล้ว ({plan}, สัญญาณเมื่อ {when} น. ไทย)",
                "stopped": f"ถึงจุดตัดขาดทุนแล้ว ({plan}, สัญญาณเมื่อ {when} น. ไทย)",
                "target": f"ถึงเป้าหมายแล้ว ({plan}, สัญญาณเมื่อ {when} น. ไทย)",
            }.get(rec["status"], rec["status"])
        return f"{rec['status'].upper()} ({plan}, since {when} ET)"
    if it.signal:
        if it.plan:
            plan = _plan_text(it.plan.entry, it.plan.stop, it.plan.target, cur, thai)
            return f"ผ่าน ({plan})" if thai else f"YES ({plan})"
        return "ผ่าน" if thai else "YES"
    if thai:
        why = []
        if it.above_yesterday_high is False:
            why.append(f"ยังต่ำกว่าจุดสูงสุดเมื่อวาน {special_fmt(it.yesterday_high, cur)}")
        if it.at_high_of_day is False:
            why.append("ยังไม่ใช่จุดสูงสุดของวัน")
        if it.uptrend is False:
            why.append("แนวโน้มระยะยาวยังเป็นขาลง")
        return "ยังไม่ผ่าน" + (f" ({', '.join(why)})" if why else "")
    return f"no (y-high {special_fmt(it.yesterday_high, cur)}, HOD {special_fmt(it.day_high, cur)})"


def section_c(thai: bool) -> list[str]:
    from . import special, special_watch  # local imports keep module load light

    lines = [HEADER_C_TH if thai else HEADER_C]
    try:
        items = special.fetch_special(thai=thai)
    except Exception as exc:  # noqa: BLE001
        log.warning("special watchlist failed: %s", exc)
        lines.append("ดึงข้อมูลไม่ได้ในรอบนี้" if thai else "Data unavailable this run.")
        return lines
    if not items:
        lines.append("ไม่ได้ตั้งรายการไว้" if thai else "No symbols configured.")
        return lines

    now = datetime.now(ET)
    records = special_watch.today_records(special_watch.load_state(), now.date())
    if thai:
        lines.append(f"ราคาล่าสุดเมื่อ {now.astimezone(BKK).strftime('%H:%M')} น. ไทย "
                     "(ตลาดเหล่านี้เปิดเกือบตลอด 24 ชม. จึงไม่มีช่วงก่อนตลาดเปิด)")
        if config.SPECIAL_WATCH_ENABLED:
            lines.append(f"ระบบเฝ้าสัญญาณกลุ่มนี้ทุก {config.SPECIAL_WATCH_INTERVAL_MIN} นาที และจะแจ้งทันทีเมื่อเกิดสัญญาณซื้อใหม่")
    else:
        lines.append(f"Prices as of {now.strftime('%H:%M')} ET (24-hour markets, no pre-market)")

    for i, it in enumerate(items, 1):
        price = special.fmt_price(it.price, it.currency)
        if it.price is None:
            lines.append(f"{i}. {it.name} ({it.symbol}) " + ("ไม่มีข้อมูลราคา" if thai else "no price"))
            continue
        chg = it.change_pct if it.change_pct is not None else 0.0
        sig = _signal_text(it, records.get(it.symbol), thai)
        if thai:
            move = f"ขึ้น {abs(chg):.2f}%" if chg >= 0 else f"ลง {abs(chg):.2f}%"
            trend = {True: "ขาขึ้น", False: "ขาลง", None: "ไม่ทราบ"}[it.uptrend]
            lines.append(f"{i}. {it.name} ({it.symbol}) ราคา {price} {move} จากเมื่อวาน")
            lines.append(f"   แนวโน้มระยะยาว: {trend} | สัญญาณซื้อ Trend-join: {sig}")
        else:
            trend = {True: "uptrend", False: "downtrend", None: "unknown"}[it.uptrend]
            lines.append(f"{i}. {it.name} ({it.symbol}) {price} {chg:+.2f}% vs prev close | "
                         f"200-day trend: {trend} | Trend-join signal: {sig}")
    return lines


def build_report(pre: dict | None, strat: dict | None) -> str:
    thai = is_thai()
    parts: list[list[str]] = []
    if thai:
        names = {r["ticker"]: r.get("name") for r in (pre or {}).get("results") or []}
        parts += [section_a_th(pre), section_b_th(strat, names)]
    else:
        parts += [section_a(pre), section_b(strat)]
    if config.SPECIAL_ENABLED:
        parts.append(section_c(thai))
    if thai:
        parts.append([DISCLAIMER_TH])
    lines: list[str] = []
    for part in parts:
        if lines:
            lines.append("")
        lines.extend(part)
    return "\n".join(lines)


def build_report_for(scan_date: date, output_dir: Path | None = None) -> str:
    pre = _load(report.output_path(scan_date.isoformat(), output_dir))
    strat = _load(strategy.strategy_file(scan_date, output_dir))
    return build_report(pre, strat)


def send_report_for(scan_date: date, output_dir: Path | None = None) -> bool:
    """Send today's combined report (both sections) to Telegram."""
    return send_message(build_report_for(scan_date, output_dir))
