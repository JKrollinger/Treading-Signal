"""Telegram delivery of the scanners' results.

Every scheduled run sends one report, built from these sections in this order:

    [Scanner A: Pre-market Gappers]      the latest pre-market list
    [Scanner B: Trend-join Long Hits]    the stock setups triggered so far today
    [Scanner C: Special Watchlist]       currency pairs, gold, bitcoin with their signals
    [MT5 account]                        owner chat only (PORTFOLIO_ENABLED)

Which sections an audience gets is configurable: ``REPORT_SECTIONS`` for the owner chat
(default ``A,B,C``) and ``GROUP_REPORT_SECTIONS`` for the groups/channels in
``TELEGRAM_EXTRA_CHAT_IDS`` (default empty: no periodic report). Groups get trade
alerts instead, filtered by ``GROUP_ALERTS`` (default ``new``: entry signals only).

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

from . import config, report, strategy, trade_fmt, translate

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


def extra_chat_ids() -> list[str]:
    """Groups/channels that receive reports and signals (not system messages), from .env."""
    load_dotenv(config.PROJECT_ROOT / ".env", override=True)
    raw = (os.getenv("TELEGRAM_EXTRA_CHAT_IDS") or "").split("#")[0]
    return [c.strip() for c in raw.split(",") if c.strip()]


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


def send_message(text: str, admin_only: bool = False, extras_only: bool = False,
                 reply_markup: dict | None = None) -> bool:
    """Send to the owner chat and, unless ``admin_only``, to every extra group/channel.

    System messages (tunnel URL with the secret, failure notes, the account report) must
    stay ``admin_only``; ``extras_only`` sends to the groups/channels but not the owner.
    Returns True when every targeted chat received the message.
    """
    token, chat_id = credentials()
    if not token or not chat_id:
        log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing); message not sent")
        return False
    extras = [c for c in extra_chat_ids() if c != chat_id]
    if extras_only:
        targets = extras
    else:
        targets = [chat_id] + ([] if admin_only else extras)
    if not targets:
        return True
    ok = True
    for target in targets:
        try:
            chunks = split_message(text)
            for i, chunk in enumerate(chunks):
                params = {"chat_id": target, "text": chunk, "disable_web_page_preview": True}
                if reply_markup and i == len(chunks) - 1 and target == chat_id:
                    params["reply_markup"] = reply_markup          # buttons only on the owner's copy
                api_call("sendMessage", token, **params)
        except Exception as exc:  # noqa: BLE001
            ok = False
            log.error("Telegram send to %s failed: %s", target, exc)
    log.info("Telegram message sent (%d chars) to %d chat(s)%s", len(text), len(targets), "" if ok else " with errors")
    return ok


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
        if config.REPORT_NEWS and r.get("catalyst"):
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
            f"{i}. {s['ticker']}  {trade_fmt.BUY_BADGE} {trade_fmt.ENTRY} entry {_money(s.get('entry_price'))}{tag}  "
            f"(triggered {_hhmm(s.get('first_triggered_at'))} ET)"
        )
        lines.append(
            f"   above Y-high {_money(s.get('yesterday_high'))}, PM high {_money(s.get('premarket_high'))}, "
            f"HOD {_money(s.get('high_of_day'))} | Y-close {_money(s.get('yesterday_close'))} > "
            f"SMA200 {_money(s.get('sma200'))}"
        )
        if s.get("gap_pct") is not None:
            lines.append(f"   gap {float(s['gap_pct']):+.2f}% | pre-mkt vol {int(s.get('premarket_volume') or 0):,}")
        if config.REPORT_NEWS and s.get("catalyst"):
            lines.append(f"   {s['catalyst']}")
    for w in _important(strat.get("warnings") or []):
        lines.append(f"Note: {w}")
    return lines


# Thai, plain-language formatting ------------------------------------------------
def _when_th(iso: str | None) -> str:
    """'19:30 น. ไทย (08:30 ET)'"""
    if not iso:
        return "ไม่ทราบเวลา"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return f"{dt.astimezone(BKK).strftime('%H:%M')} น. ไทย ({dt.astimezone(ET).strftime('%H:%M')} ET)"


def _hhmm_th(iso: str | None) -> str:
    """'21:05 น. ไทย' - Thai wall-clock time only."""
    if not iso:
        return "ไม่ทราบเวลา"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return f"{dt.astimezone(BKK).strftime('%H:%M')} น. ไทย"


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
    lines.append(
        f"{_date_th(pre.get('date'))} {_when_th(pre.get('generated_at'))} | "
        f"ตรวจ {int(pre.get('universe_size') or 0):,} ตัว เข้าเกณฑ์ {len(results)} ตัว"
    )
    lines.append(
        f"(เกณฑ์: ขึ้น >{c.get('min_gap_pct', 5):g}% จากปิดเมื่อวาน ราคา >${c.get('min_price', 3):g} "
        f"ซื้อขายก่อนเปิด >{int(c.get('min_premarket_volume') or 0):,} หุ้น)"
    )
    if not results:
        lines.append("ไม่มีหุ้นเข้าเกณฑ์ในรอบนี้")
    for i, r in enumerate(results, 1):
        vol = r.get("premarket_volume")
        vol_txt = f"{int(vol):,} หุ้น" if vol is not None else "ไม่ทราบ"
        tag = "" if r.get("session") == "premarket" else " (หลังตลาดเปิด)"
        lines.append(
            f"{i}. {_label(r['ticker'], r.get('name'))} {_money(r.get('price'))} "
            f"ขึ้น {abs(float(r.get('gap_pct') or 0)):.2f}% ซื้อขาย {vol_txt}{tag}"
        )
        news = _news_line_th(r.get("catalyst")) if config.REPORT_NEWS else None
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
    lines.append(f"{_when_th(strat.get('generated_at'))} | ตรวจ {len(scanned)} ตัวจากรายการ A")
    if not signals:
        start = strategy._parse_hhmm(config.STRATEGY_START_ET)
        try:
            gen = datetime.fromisoformat(strat["generated_at"]).astimezone(ET)
            before = gen.time() < start
            start_th = datetime.combine(gen.date(), start, ET).astimezone(BKK).strftime("%H:%M")
        except (KeyError, ValueError):
            before, start_th = False, ""
        if before:
            lines.append(f"ยังไม่มีหุ้นผ่านเงื่อนไข (เริ่มนับสัญญาณตั้งแต่ {start_th} น. ไทย)")
        else:
            lines.append("ยังไม่มีหุ้นผ่านเงื่อนไขในรอบนี้")
    else:
        lines.append(f"ผ่านเงื่อนไข {len(signals)} ตัว (ใหม่ {len(new)}):")
        for i, s in enumerate(signals, 1):
            tag = " ใหม่!" if s["ticker"] in new else ""
            lines.append(
                f"{i}. {_label(s['ticker'], names.get(s['ticker']))} {trade_fmt.BUY_BADGE} "
                f"{trade_fmt.ENTRY} เข้า {_money(s.get('entry_price'))}{tag} ({_hhmm_th(s.get('first_triggered_at'))})"
            )
            news = _news_line_th(s.get("catalyst")) if config.REPORT_NEWS else None
            if news:
                lines.append(news)
    failed = [ev["ticker"] for ev in scanned if not ev.get("valid")]
    if failed:
        lines.append(f"ยังไม่ผ่าน: {', '.join(failed)}")
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
    return trade_fmt.plan(entry, stop, target, fp, thai=thai)


def special_fmt(v, currency: str) -> str:
    from . import special

    return special.fmt_price(v, currency)


def _signal_text(it, rec: dict | None, thai: bool, hold: dict | None = None) -> str:
    """Status of the Trend-join signal for one Scanner C symbol, with the trade plan."""
    cur = it.currency
    fp = lambda v: special_fmt(v, cur)  # noqa: E731
    if rec is None and hold and it.signal:
        return (f"เข้าเงื่อนไขแต่ยังไม่นับ: {hold['reason']}" if thai
                else f"qualifies but held: {hold['reason']}")
    if rec:
        partial = rec.get("partial_level", rec.get("target"))
        when = datetime.fromisoformat(rec["triggered_at"]).astimezone(BKK if thai else ET)
        when_txt = when.strftime("%d/%m %H:%M") if thai else when.strftime("%m-%d %H:%M")
        result_r = rec.get("result_r") or 0
        buy, out = trade_fmt.BUY_BADGE, trade_fmt.EXIT
        plan = trade_fmt.plan(rec["entry"], rec["stop"], partial, fp, thai=thai)
        if thai:
            return {
                "active": f"{buy} ถืออยู่ | {plan} | ตั้งแต่ {when_txt}",
                "partial": f"{buy} ปิดครึ่งแล้ว ถือที่เหลือ | {plan} | ตั้งแต่ {when_txt}",
                "weakened": f"{buy} อ่อนลง (หลุดจุดสูงสุดเมื่อวาน) | {plan} | ตั้งแต่ {when_txt}",
                "stopped": f"{out} ออกที่ตัดขาดทุนแล้ว ผล {result_r:+.2f}R (สัญญาณ {when_txt})",
                "expired": f"{out} ออกครบกำหนดแล้ว ผล {result_r:+.2f}R (สัญญาณ {when_txt})",
            }.get(rec["status"], rec["status"])
        return f"{buy} {rec['status'].upper()} | {plan} | since {when_txt} ET"
    if it.signal:
        if it.plan:
            plan = trade_fmt.plan(it.plan.entry, it.plan.stop, it.plan.target, fp, thai=thai)
            return f"ผ่าน {trade_fmt.BUY_BADGE} | {plan}" if thai else f"YES {trade_fmt.BUY_BADGE} | {plan}"
        return "ผ่าน" if thai else "YES"
    if thai:
        why = []
        if it.above_yesterday_high is False:
            why.append(f"ต่ำกว่าจุดสูงสุดเมื่อวาน {special_fmt(it.yesterday_high, cur)}")
        if it.at_high_of_day is False:
            why.append("ไม่ใช่จุดสูงสุดของวัน")
        if it.uptrend is False:
            why.append("แนวโน้มระยะยาวขาลง")
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
    state = special_watch.load_state()
    records = special_watch.visible_records(state, now.date())
    holds = special_watch.holds_today(state, now.date())
    if thai:
        every = f" (เฝ้าสัญญาณทุก {config.SPECIAL_WATCH_INTERVAL_MIN} นาที)" if config.SPECIAL_WATCH_ENABLED else ""
        lines.append(f"ราคาล่าสุด {now.astimezone(BKK).strftime('%H:%M')} น. ไทย{every}")
    else:
        lines.append(f"Prices as of {now.strftime('%H:%M')} ET (24-hour markets, no pre-market)")

    for i, it in enumerate(items, 1):
        price = special.fmt_price(it.price, it.currency)
        if it.price is None:
            lines.append(f"{i}. {it.name} ({it.symbol}) " + ("ไม่มีข้อมูลราคา" if thai else "no price"))
            continue
        chg = it.change_pct if it.change_pct is not None else 0.0
        sig = _signal_text(it, records.get(it.symbol), thai, holds.get(it.symbol))
        if thai:
            move = f"ขึ้น {abs(chg):.2f}%" if chg >= 0 else f"ลง {abs(chg):.2f}%"
            trend = {True: "ขาขึ้น", False: "ขาลง", None: "ไม่ทราบ"}[it.uptrend]
            lines.append(f"{i}. {it.name} ({it.symbol}) {price} {move} | แนวโน้ม: {trend} | สัญญาณ: {sig}")
        else:
            trend = {True: "uptrend", False: "downtrend", None: "unknown"}[it.uptrend]
            lines.append(f"{i}. {it.name} ({it.symbol}) {price} {chg:+.2f}% vs prev close | "
                         f"200-day trend: {trend} | Trend-join signal: {sig}")
    return lines


def section_d(thai: bool) -> list[str]:
    """Read-only MT5 account section (owner's copy of the report)."""
    from . import mt5_portfolio

    try:
        snap = mt5_portfolio.snapshot()
    except Exception as exc:  # noqa: BLE001
        log.warning("portfolio snapshot failed: %s", exc)
        snap = None
    return mt5_portfolio.format_section(snap, thai)


def _sections(raw: str) -> set[str]:
    """'a, b,C' -> {'A', 'B', 'C'}."""
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


def _without_stocks(sections: set[str]) -> set[str]:
    return sections if config.STOCKS_ENABLED else sections - {"A", "B"}


def owner_sections() -> set[str]:
    return _without_stocks(_sections(config.REPORT_SECTIONS))


def group_sections() -> set[str]:
    return _without_stocks(_sections(config.GROUP_REPORT_SECTIONS))


def build_report(pre: dict | None, strat: dict | None, include_portfolio: bool = False,
                 sections: set[str] | None = None, c_lines: list[str] | None = None) -> str:
    """Assemble the report from the requested sections (default: the owner's set).

    ``c_lines`` lets a caller that builds two copies (owner and group) fetch the live
    Scanner C data once and reuse it.
    """
    thai = is_thai()
    sections = owner_sections() if sections is None else sections
    parts: list[list[str]] = []
    if "A" in sections:
        parts.append(section_a_th(pre) if thai else section_a(pre))
    if "B" in sections:
        if thai:
            names = {r["ticker"]: r.get("name") for r in (pre or {}).get("results") or []}
            parts.append(section_b_th(strat, names))
        else:
            parts.append(section_b(strat))
    if "C" in sections and config.SPECIAL_ENABLED:
        parts.append(section_c(thai) if c_lines is None else c_lines)
    if include_portfolio and config.PORTFOLIO_ENABLED:
        parts.append(section_d(thai))
    if thai:
        parts.append([DISCLAIMER_TH])
    lines: list[str] = []
    for part in parts:
        if lines:
            lines.append("")
        lines.extend(part)
    return "\n".join(lines)


def build_report_for(scan_date: date, output_dir: Path | None = None, include_portfolio: bool = False,
                     sections: set[str] | None = None) -> str:
    pre = _load(report.output_path(scan_date.isoformat(), output_dir))
    strat = _load(strategy.strategy_file(scan_date, output_dir))
    return build_report(pre, strat, include_portfolio=include_portfolio, sections=sections)


def send_report_for(scan_date: date, output_dir: Path | None = None) -> bool:
    """Send today's report: the owner's sections (plus the account section) to the owner
    chat; the group sections, if any, to every extra group/channel. One send when both are
    equal. With ``GROUP_REPORT_SECTIONS`` empty (the default) groups get no report at all."""
    pre = _load(report.output_path(scan_date.isoformat(), output_dir))
    strat = _load(strategy.strategy_file(scan_date, output_dir))
    owner_set, group_set = owner_sections(), group_sections()
    if not extra_chat_ids():
        group_set = set()
    c_lines = section_c(is_thai()) if config.SPECIAL_ENABLED and "C" in (owner_set | group_set) else None
    owner = build_report(pre, strat, include_portfolio=config.PORTFOLIO_ENABLED, sections=owner_set, c_lines=c_lines)
    if not group_set:
        return send_message(owner, admin_only=True)
    group = build_report(pre, strat, include_portfolio=config.PORTFOLIO_ENABLED and config.PORTFOLIO_TO_GROUP,
                         sections=group_set, c_lines=c_lines)
    if owner == group:
        return send_message(owner)
    ok = send_message(owner, admin_only=True)
    return send_message(group, extras_only=True) and ok


# Trade alerts: owner gets every kind, groups only the kinds in GROUP_ALERTS ----------
def group_alert_kinds() -> set[str]:
    return {s.strip().lower() for s in config.GROUP_ALERTS.split(",") if s.strip()}


def send_alert(text: str, kind: str, owner: bool = True, reply_markup: dict | None = None) -> bool:
    """Send one trade alert. ``kind`` is new / exit / trail / weakened / held."""
    ok = send_message(text, admin_only=True, reply_markup=reply_markup) if owner else True
    if kind in group_alert_kinds() and extra_chat_ids():
        ok = send_message(text, extras_only=True) and ok
    return ok


def stock_signal_alerts(strat: dict | None, pre: dict | None = None) -> list[str]:
    """One entry alert per Scanner B ticker that triggered in this run (``new_signals``)."""
    if not strat:
        return []
    thai = is_thai()
    by_ticker = {s["ticker"]: s for s in strat.get("signals") or []}
    names = {r["ticker"]: r.get("name") for r in (pre or {}).get("results") or []}
    out = []
    for ticker in strat.get("new_signals") or []:
        s = by_ticker.get(ticker)
        if not s:
            continue
        label = _label(ticker, names.get(ticker))
        entry = f"{trade_fmt.ENTRY} {'เข้า' if thai else 'entry'} {_money(s.get('entry_price'))}"
        if thai:
            delayed = " (ราคาอ้างอิงอาจช้ากว่าจริง)" if s.get("price_delayed") else ""
            out.append("\n".join([
                f"🔔 สัญญาณใหม่ {trade_fmt.BUY_BADGE}: {label} หุ้นสหรัฐ {_when_th(s.get('first_triggered_at'))}",
                entry + delayed,
                f"เหตุผล: ทะลุจุดสูงสุดเมื่อวาน {_money(s.get('yesterday_high'))} และจุดสูงสุดก่อนตลาดเปิด "
                f"{_money(s.get('premarket_high'))} ที่จุดสูงสุดของวัน แนวโน้มระยะยาวขาขึ้น",
                DISCLAIMER_TH,
            ]))
        else:
            delayed = " (reference price may be delayed)" if s.get("price_delayed") else ""
            out.append("\n".join([
                f"NEW {trade_fmt.BUY_BADGE} Trend-join Long: {label} {_hhmm(s.get('first_triggered_at'))} ET",
                entry + delayed,
                f"Why: above yesterday's high {_money(s.get('yesterday_high'))} and the pre-market high "
                f"{_money(s.get('premarket_high'))}, at the high of day, y-close above SMA200",
            ]))
    return out


def send_stock_signal_alerts(scan_date: date, strat: dict, output_dir: Path | None = None) -> int:
    """Alert the new Scanner B entries. Groups get them per GROUP_ALERTS; the owner already
    sees them in report section B, so the owner copy is sent only when B is not reported."""
    pre = _load(report.output_path(scan_date.isoformat(), output_dir))
    alerts = stock_signal_alerts(strat, pre)
    for text in alerts:
        send_alert(text, "new", owner="B" not in owner_sections())
    return len(alerts)
