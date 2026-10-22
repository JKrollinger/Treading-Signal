# Signal Bot

Two scanners that run on US market days and report to Telegram after every run:

1. **Scanner A, pre-market gappers** (08:30 to 14:00 ET, every 30 minutes): finds stocks
   gapping up on volume with a news catalyst. See "Pre-market Gap Scanner" below.
2. **Scanner B, Trend-join Long** (5 minutes after each Scanner A run, until 15:00 ET):
   takes the latest Scanner A list and triggers an entry signal when the setup is
   confirmed. See "Strategy scanner: Trend-join Long" below.

Both save a dated JSON file in `output/`; `scheduler.py` runs them and sends the combined
report to your Telegram bot (see "Telegram delivery").

# Pre-market Gap Scanner

Scans the US stock market every weekday morning before the 09:30 ET open and lists
stocks that are gapping up on volume, each with a one-sentence news catalyst.

Default criteria (change them in `.env`):

| Rule | Default |
| --- | --- |
| Gap vs previous close | more than +5 % |
| Share price | above $3 |
| Pre-market volume | more than 50,000 shares |

Every run prints a table (Ticker, Price, Gap %, Pre-market volume, Catalyst) and saves
`output/premarket_scan_YYYY-MM-DD.json`.

## Data sources

| Data | Source | Notes |
| --- | --- | --- |
| Ticker universe, pre-market price and % change | Yahoo Finance (screener + quote API via `yfinance`) | Every US-listed stock above the price floor with 3-month average volume above `UNIVERSE_MIN_AVG_VOLUME` (about 4,600 names). |
| Pre-market volume | Nasdaq.com extended-trading API | Yahoo's pre-market minute bars carry no volume, so the volume check uses Nasdaq's consolidated pre-market total. Only the stocks that already passed the gap and price checks are looked up. |
| News catalyst | Benzinga News API | Needs `BENZINGA_API_KEY` (https://www.benzinga.com/apis/). Without a key, or if the key is rejected, Yahoo Finance headlines are used and the report says so in `warnings`. |

## Setup (Windows)

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` and paste your Benzinga key into `BENZINGA_API_KEY=` (leave it empty to use
Yahoo headlines).

## Run once

```bash
.venv\Scripts\python scan.py
```

Options: `--no-news` skips catalyst lookups, `-v` prints debug logs, `--output-dir DIR`
changes where the JSON goes.

A scan takes roughly one to two minutes: about 20 screener pages from Yahoo, then one
Nasdaq request and one news request per candidate.

# Strategy scanner: Trend-join Long

`strategy_scan.py` reads today's `output/premarket_scan_<date>.json` and, after 10:00 ET,
checks every ticker in it against the setup:

| # | Rule | Check |
| --- | --- | --- |
| 1 | Price above yesterday's daily high | `price > yesterday_high` |
| 2 | Yesterday's close above the 200 SMA | `yesterday_close > sma200` (200-day SMA as of yesterday) |
| 3 | Price above today's pre-market high and at the high of day | `price > premarket_high` and `price >= high_of_day * (1 - HOD_TOLERANCE_PCT/100)` |

A ticker that passes all three during the regular session at or after
`STRATEGY_START_ET` triggers a **valid entry signal**. "At the high of day" means the stock
is breaking out right now: the price is within `HOD_TOLERANCE_PCT` (default 0.25 %) of
the day's high. Set it to 0 for a strict new high.

Run once (after 10:00 ET):

```bash
.venv\Scripts\python strategy_scan.py
```

`--ignore-time` evaluates outside the window (for testing), `--tickers QS,SAIQ` checks a
custom list, `--input FILE` reads another pre-market file.

## Where the strategy data comes from

| Data | Default source | Notes |
| --- | --- | --- |
| Yesterday's high and close, 200 SMA, pre-market high | **TradingView** screener API (`STRATEGY_DATA_SOURCE=tradingview`) | Same numbers as the TradingView screener and charts. Before the open TradingView's "current" daily bar is still yesterday's; the scanner handles that. |
| Live price and high of day | **Yahoo Finance** quote | Real-time. TradingView prices are delayed 15 minutes for anonymous requests, so they are not used for the live price unless `TRADINGVIEW_SESSIONID` (the `sessionid` cookie of a logged-in account with real-time US data) is set and TradingView reports streaming data. |
| Everything | Yahoo Finance (`STRATEGY_DATA_SOURCE=yahoo`) | Daily history for yesterday's bar and the 200 SMA, 1-minute bars for the pre-market high. Also the automatic fallback when TradingView fails. |

# TradingView integration

Three connections exist, each with different requirements:

| Connection | What it does | Needs |
| --- | --- | --- |
| Scanner B reference levels | Yesterday's high/close, 200 SMA, pre-market high for the stock setups come from TradingView's screener API | Nothing. Anonymous access. Prices are 15-minute delayed for US stocks, so the live stock price comes from Yahoo unless the cookie below is set. |
| Scanner C live data (`SPECIAL_DATA_SOURCE=tradingview`, default) | Real-time spot gold (`OANDA:XAUUSD`), currency pairs (`FX:EURUSD`, `FX_IDC:USDTHB`) and bitcoin (`BITSTAMP:BTCUSD`) with ATR, used by the watch mode alerts | Nothing. Forex, commodity and crypto quotes stream in real time without a login. |
| TradingView alerts -> Telegram (webhook) | Any alert you create on a TradingView chart (for example from `tradingview/trend_join_long.pine`) is forwarded to Telegram in Thai with price, stop and target | A **paid TradingView plan** (Essential or higher; webhook notifications are not on the free plan), the bot running, and a **public HTTPS URL** that reaches the bot (a tunnel, see below). |

Optional, stocks only: `TRADINGVIEW_SESSIONID`. If your TradingView account has real-time
US exchange data (a paid add-on), the bot can take the live stock price from TradingView
too. Log in at tradingview.com, press F12, open Application > Cookies >
https://www.tradingview.com, copy the value of `sessionid` into `.env`. It stops working
when you log out of that browser session. Never share it; it is a login.

## Webhook: TradingView alert -> Telegram

The scheduler runs a small web server on `TV_WEBHOOK_BIND:TV_WEBHOOK_PORT`
(127.0.0.1:8787). It only accepts calls that carry `TV_WEBHOOK_SECRET` (already generated
in your `.env`), either as `?key=SECRET` in the URL or as a `"secret"` field in the JSON.
Everything received is forwarded to Telegram and logged to
`output/tradingview_alerts_<date>.json`.

1. **Expose the port to the internet** (TradingView must reach it). With `cloudflared`
   installed (`winget install Cloudflare.cloudflared`) and `TUNNEL_ENABLED=true` the
   scheduler starts a Cloudflare quick tunnel itself, keeps it alive, writes the public
   address to `data/tunnel_url.txt` and **sends the complete webhook URL to Telegram**
   every time it changes (it changes on every restart; no account needed). The tunnel
   lives and dies with the scheduler: each `cloudflared` is tied to the scheduler
   process through a Windows job object, so stopping the task (even a hard kill) ends the
   tunnel too, and on start the scheduler kills any `cloudflared` an earlier run left
   behind (recognised by the bot's own config file or its port). Print the URL any time
   with:

   ```bash
   .venv\Scripts\python tunnel_url.py
   ```

   For a fixed URL create a free Cloudflare account and a named tunnel
   (`cloudflared tunnel login`, `cloudflared tunnel create signalbot`, route a hostname),
   or use ngrok (free account, one static domain); then set `TUNNEL_ENABLED=false` and run
   that tunnel yourself.

2. **Create the alert in TradingView.** Open a chart (1 or 5 minutes), add the
   `Trend-join Long` indicator from `tradingview/trend_join_long.pine`, click the alarm
   icon > Create alert. Condition: `Trend-join Long` > `Trend-join Long signal`. In the
   Notifications tab tick **Webhook URL** and paste

   ```
   https://<your-tunnel-host>/tv?key=<TV_WEBHOOK_SECRET from .env>
   ```

   The message box is pre-filled by the indicator with JSON (signal, ticker, exchange,
   price, stop, target, interval); leave it, or replace `CHANGE_ME` with the secret if you
   prefer not to put it in the URL. Set the expiration (alerts on most plans expire after a
   while; "open-ended" needs Premium). Create one alert per symbol you want watched.

3. **Test** without TradingView:

   ```bash
   .venv\Scripts\python tv_webhook_server.py --test
   ```

   posts a sample alert to the running bot; a Telegram message should arrive.

Any TradingView alert works, not only this indicator: a plain-text message is forwarded as
is, and a JSON message with `signal`, `ticker`, `exchange`, `price`, `stop`, `target`,
`interval` fields is formatted into the Thai layout.

# MetaTrader 5 (phase 1: your broker's prices, read-only)

With `SPECIAL_DATA_SOURCE=mt5` Scanner C takes live prices, today's and yesterday's
levels, the 200-day SMA and the ATR from the **MetaTrader 5 terminal running on this PC**
(logged in to your broker, demo or live), through the official `MetaTrader5` Python
package. Symbols are the broker's own names (`XAUUSD`, `EURUSD`, `BTCUSD`, sometimes with
a suffix such as `XAUUSD.m`). Nothing in this phase places orders.

Requirements: the MT5 desktop terminal installed and open on this Windows machine. Leave
`MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` empty to attach to the account already logged
in; set `MT5_PATH` to `terminal64.exe` only if auto-detection fails. Check the connection
and find your broker's symbol names with:

```bash
.venv\Scripts\python mt5_probe.py
```

If MT5 is unreachable, or a symbol does not exist at the broker, the scanner falls back to
TradingView, then Yahoo, mapping the broker symbol to the equivalent
(`XAUUSD` -> `OANDA:XAUUSD` -> `GC=F`, `BTCUSD` -> `BITSTAMP:BTCUSD` -> `BTC-USD`).
The MetaQuotes-Demo server, for example, has spot gold and forex but no crypto CFDs, so
bitcoin keeps coming from TradingView there.

## Account report and position alerts (read-only)

With `PORTFOLIO_ENABLED=true` the owner's copy of every scheduled report ends with an
**[MT5 account]** section: balance, equity, floating P/L, each open position with its
S/L, T/P and profit (and whether it matches a bot signal), and the trades closed today.
Groups do not receive it unless `PORTFOLIO_TO_GROUP=true`. Every 5 minutes the bot also
compares the open positions with the previous check and sends, to the owner chat only,
a "📗" note when a position appears (warning if it has no stop-loss) and a "📕" note
when one disappears, with the close price, the reason (stop loss, take profit, manual)
and the realised profit. The bot never places, modifies or closes orders.

## Confirm-to-trade button and the EA bridge

The bot itself never sends orders. Instead, with `EA_BRIDGE_ENABLED=true`, the owner's
copy of every new Scanner C signal (for symbols quoted through MT5) carries two buttons:
**✅ ยืนยันเข้าเทรด (ส่งให้ EA)** and **❌ ข้าม**. Pressing confirm appends an order request
to `<MT5 Common data>\Files\signalbot\orders.csv` (`id, created_utc, symbol, side, lots,
sl, tp, entry_ref, comment`): BUY, lot size computed so that the distance to the stop
risks `EA_RISK_PCT` of the balance (tick value from the broker), S/L = the signal's initial
stop, T/P = entry + `EA_TP_R` x risk. The reply shows the request and whether the EA is
alive (`heartbeat.txt`). Only the owner's Telegram user can press the buttons; the bot
persists the update offset so a restart never replays old presses.

Telegram commands (owner only): `/status` (account section), `/report` (today's full
report with the account section), `/orders` (pending requests, latest EA results, EA
heartbeat), `/test [SYMBOL]` (a minimum-lot dry-run request to check the pipeline),
`/help`, `/menu`. The owner's private chat also gets a persistent menu keyboard under the
message box (📊 พอร์ต MT5, 🔔 รายงานล่าสุด, 📋 รายการ EA, 🧪 ทดสอบ EA, ❓ ช่วยเหลือ) that
sends the same commands with one tap; `/menu` brings it back.

`mt5_ea/SignalBotBridge.mq5` is the Expert Advisor skeleton that completes the loop:
every `InpPollSeconds` it reads `orders.csv`, skips ids listed in `processed.txt`, checks
age, symbol, position limits, lot step/min/max (and `InpMaxLots`), minimum stop distance
and AutoTrading, then writes `results.csv` (`id, processed_utc, status, ticket, price,
lots, message`) which the bot reports to Telegram within seconds. **The order-sending
line is deliberately left out**: look for `>>> ADD ORDER CODE HERE <<<` in
`HandleRequest()` and add it yourself (CTrade::Buy in the MQL5 reference). Until then,
and while `InpDryRun = true`, the EA only validates and reports `DRYRUN`.

Install the EA: MetaEditor (IDE button in MT5) > File > Open the .mq5 > Compile (F7) >
drag it from Navigator > Expert Advisors onto any chart > tick "Allow Algo Trading" > press
the AutoTrading toolbar button. Keep `InpDryRun = true` until a `/test` request comes back
as `DRYRUN` in Telegram and you have reviewed the plan it prints.

## Backtest of the Trend-join Long rules

`backtest_trend_join.py` replays the rules on hourly history: yesterday's close above the
200-day SMA, a bar closing above yesterday's high while at the high of the day, entry at
that close, stop 1 x ATR(14) below, target 2 x the risk, exit at stop/target or after 5
trading days, one trade per symbol per day, a flat 0.03 % round-trip cost.

```bash
.venv\Scripts\python backtest_trend_join.py                 # Yahoo hourly data, ~2 years
.venv\Scripts\python backtest_trend_join.py --source mt5    # your broker's data via MT5
.venv\Scripts\python backtest_trend_join.py --hold 3 --stop-atr 1.5 --target-r 2 --send
```

It prints a per-symbol table (trades, win rate, average R, total R, profit factor, max
drawdown in R, exit reasons), writes `output/backtest_trend_join_<source>_<date>.json`
and, with `--send`, posts a plain-Thai summary to Telegram. "R" is the initial risk per
trade (entry minus stop). Real spreads and slippage are not modelled beyond the flat cost.

### Version 2 rules and the comparison

`backtest_compare.py` runs the same history through five variants and reports them side
by side (`output/backtest_compare_<source>_<date>.json`, Thai summary with `--send`):

| Variant | What changes |
| --- | --- |
| v1 | the live rules above |
| v2-regime | only trade when the daily ADX(14) is at least 20, yesterday's ATR is no more than 2x its 100-day median, and the 200-day SMA rose over the last 20 days |
| v2-trail | no fixed target: a Chandelier trailing stop 3 x ATR below the highest high since entry (never lowered), half the position taken at +1R, hold up to 10 days |
| v2-time | entries only 07:00-20:00 UTC (London/New York), none on Friday from 15:00 UTC, none in the US payrolls window (first Friday, 12:00-14:00 UTC) or around FOMC decisions (17:30-20:00 UTC on meeting days) |
| v2-all | all of the above |

Each variant also reports why candidate days were skipped (ADX too low, volatility
extreme, outside the session, news blackout, ...). The news filter is an approximation
built from recurring events only; live mode can be extended with an economic calendar.

## Running it on TradingView charts

`tradingview/trend_join_long.pine` is a Pine Script v6 indicator with the same three rules.
Open a 1- or 5-minute chart with **extended trading hours enabled** (so pre-market bars
exist), paste the script into the Pine Editor, add it to the chart, and create an alert on
"Trend-join Long signal". It plots yesterday's high (orange) and the pre-market high (blue)
and marks signal bars with a green triangle. The script has not been compiled here; if
TradingView reports an error, the fix is usually a one-line change.

## Strategy output

`output/trend_join_<date>.json` is updated on every run of the day and keeps the signals
already triggered:

```json
{
  "strategy": "Trend-join Long",
  "date": "2026-10-22",
  "generated_at": "2026-10-22T10:05:01-04:00",
  "data_source": { "levels": "tradingview", "price": "yahoo" },
  "new_signals": ["ABCD"],
  "signals": [
    {
      "ticker": "ABCD", "first_triggered_at": "2026-10-22T10:05:01-04:00", "last_valid_at": "...",
      "entry_price": 7.95, "yesterday_high": 7.10, "yesterday_close": 6.90, "sma200": 5.42,
      "premarket_high": 7.80, "high_of_day": 7.96, "tv_symbol": "NASDAQ:ABCD",
      "gap_pct": 25.76, "premarket_volume": 1834210, "catalyst": "..."
    }
  ],
  "scanned": [ { "ticker": "...", "valid": false, "checks": { "...": true }, "reasons": ["..."], "snapshot": { "...": 0 } } ],
  "report_text": "Trend-join Long - Thu 2026-10-22 10:05 ET\n1 entry signal(s) today (1 new): ..."
}
```

`report_text` is a plain-text summary ready to send anywhere (Telegram, email); `scanned`
holds every ticker's values and which checks failed, so a miss is explainable.

# Telegram delivery

After every scheduled run the scheduler sends one report. The owner chat gets every
section (`REPORT_SECTIONS`, default `A,B,C`): Scanner A (pre-market gappers), Scanner B
(Trend-join Long hits) and Scanner C (the special watchlist: currency pairs, gold,
bitcoin), plus the MT5 account section when that is enabled. Groups and channels in
`TELEGRAM_EXTRA_CHAT_IDS` get only the signal section by default
(`GROUP_REPORT_SECTIONS`, default `C`); set it to `A,B,C` to send them the full report.
The default language is **plain Thai** written for readers without a trading background
(`REPORT_LANGUAGE=th`). Sections A and B list signals only, one line per ticker; the news
headline under each ticker is off by default and comes back with `REPORT_NEWS=true`
(translated to Thai when `TRANSLATE_NEWS=true`, free public service, falls back to the
English headline). Example of the owner's copy:

```
[Scanner A: หุ้นที่พุ่งขึ้นก่อนตลาดเปิด]
22 ต.ค. 2026 19:30 น. ไทย (08:30 ET) | ตรวจ 4,569 ตัว เข้าเกณฑ์ 2 ตัว
(เกณฑ์: ขึ้น >5% จากปิดเมื่อวาน ราคา >$3 ซื้อขายก่อนเปิด >50,000 หุ้น)
1. SAIQ (WISeSat.Space) $6.21 ขึ้น 11.69% ซื้อขาย 293,070 หุ้น
2. QS (QuantumScape) $4.29 ขึ้น 7.79% ซื้อขาย 159,961 หุ้น

[Scanner B: หุ้นที่เข้าเงื่อนไขซื้อ Trend-join Long]
21:05 น. ไทย (10:05 ET) | ตรวจ 2 ตัวจากรายการ A
ผ่านเงื่อนไข 1 ตัว (ใหม่ 1):
1. QS (QuantumScape) 🟢 BUY ➡️ เข้า $4.52 ใหม่! (21:05 น. ไทย)
ยังไม่ผ่าน: SAIQ

[Scanner C: สินทรัพย์ที่สนใจเป็นพิเศษ]
ราคาล่าสุด 21:05 น. ไทย (เฝ้าสัญญาณทุก 5 นาที)
1. ทองคำ (OANDA:XAUUSD) $4,100 ลง 1.21% | แนวโน้ม: ขาลง | สัญญาณ: ยังไม่ผ่าน (ต่ำกว่าจุดสูงสุดเมื่อวาน $4,161)
2. ดอลลาร์/บาท (FX_IDC:USDTHB) 33.74 THB ขึ้น 0.40% | แนวโน้ม: ขาขึ้น | สัญญาณ: 🟢 BUY ถืออยู่ | ➡️ เข้า 33.71 THB | 🔻 ตัดขาดทุน 33.53 THB | 🔺 T/P 33.89 THB | ตั้งแต่ 22/10 17:08

ข้อมูลอัตโนมัติเพื่อประกอบการตัดสินใจ ไม่ใช่คำแนะนำการลงทุน
```

Every trade plan in the bot (Scanner B and C, the Scanner C alerts, the MT5 account
report and position alerts, EA order requests) uses the same marks, defined in
`premarket_scanner/trade_fmt.py`: 🟢 BUY / 🔴 SELL for the side, ➡️ the entry, ⬅️ an
exit that has happened, 🔻 the stop level and 🔺 the take-profit level (the two level
arrows flip for a SELL, whose stop sits above the entry).

Scanner C lists each symbol in `SPECIAL_WATCHLIST` (Yahoo symbols: `EURUSD=X` style
pairs, `GC=F` gold, `BTC-USD`) with the latest price, change versus the previous
daily close, the long-term trend (previous close versus the 200-day average) and whether
the Trend-join rules that apply to 24-hour markets hold (above yesterday's high and at the
day's high; there is no pre-market for these). It is fetched live when each message is
built. Set `SPECIAL_ENABLED=false` to drop the section.

Gold: Yahoo has no XAUUSD spot feed. `GC=F` is the global reference price (within a few
dollars of spot, same trading hours as spot brokers, quotes delayed about 10 minutes).
`PAXG-USD` is a token that tracks spot gold in real time around the clock, including
weekends; swap it in if that suits your broker better.

## Scanner C watch mode: entry alerts with a trade plan

`scheduler.py` also checks the Scanner C symbols every `SPECIAL_WATCH_INTERVAL_MIN`
(5) minutes, all day, every day (symbols whose quote is older than `STALE_QUOTE_MIN` are
skipped, which is how closed FX/gold sessions are ignored while bitcoin keeps running).
The moment a symbol newly passes the Trend-join rules it sends an alert like:

```
🔔 สัญญาณใหม่ 🟢 BUY: ทองคำ (OANDA:XAUUSD) 23:05 น. ไทย
➡️ เข้า $4,120 | 🔻 ตัดขาดทุน $4,080 (-0.97%) | 🔺 T/P $4,160 (+1R ปิดครึ่ง)
ที่เหลือเลื่อนตัดขาดทุนตามราคา (ห่าง 3 ATR ≈ $120) ถือไม่เกิน 10 วันทำการ (ถึง 05/11)
เหตุผล: ทะลุจุดสูงสุดเมื่อวาน $4,100 ที่จุดสูงสุดของวัน แนวโน้มระยะยาวขาขึ้น
ข้อมูลอัตโนมัติเพื่อประกอบการตัดสินใจ ไม่ใช่คำแนะนำการลงทุน
```

The follow-up alerts for that signal are one line each:

```
✅ ⬅️ ออกครึ่งที่ T/P: ทองคำ (OANDA:XAUUSD) $4,160 (+1R) ปิดครึ่งไม้ | 🔻 ตัดขาดทุนตอนนี้ $4,080 (เลื่อนตามราคา)
🔼 ทองคำ (OANDA:XAUUSD) 🔻 ตัดขาดทุนเลื่อนขึ้นเป็น $4,140 (สูงสุด $4,260) ล็อกกำไร +0.50R
⚠️ ทองคำ (OANDA:XAUUSD) $4,095 หลุดใต้จุดสูงสุดเมื่อวาน $4,100 สัญญาณ 22/10 23:05 น. ไทย อ่อนลง | 🔻 ตัดขาดทุนยัง $4,080
⛔ ⬅️ ออกที่ตัดขาดทุน: ทองคำ (OANDA:XAUUSD) $4,080 ถึงจุดตัดขาดทุน $4,080 ปิดไม้ที่เหลือ | ผลรวม -1.00R (สัญญาณ 22/10 23:05 น. ไทย)
⏱ ⬅️ ออกครบกำหนด: ทองคำ (OANDA:XAUUSD) ครบ 10 วันทำการ ปิดไม้ที่เหลือที่ $4,150 | ผลรวม +0.88R
```

The plan follows the exit that won the backtest comparison (v2-trail): initial stop =
entry minus `STOP_ATR_MULT` x the 14-day ATR; half the position is taken at
`PARTIAL_R` R ("✅" alert); the rest trails a stop `TRAIL_ATR_MULT` x ATR below the
highest price since entry, which only ever rises ("🔼" alert each time it climbs by
`TRAIL_NOTIFY_ATR` x ATR); the signal closes when the price reaches the current stop
("⛔", with the combined result in R), after `MAX_HOLD_DAYS` trading days ("⏱"), and an
early "⚠️" warns once if the price falls back below yesterday's high before the partial.
Signals are followed across days; one new signal per symbol per US trading day and none
while that symbol is still open. State: `data/special_watch_state.json`; records touched
today: `output/special_signals_<date>.json`. GBP/USD is no longer in the default list
because it lost money under every rule variant tested.

Time and news filter (gold only by default, `TIME_FILTER_SYMBOLS`): a new gold signal is
only accepted between `SESSION_UTC_START` and `SESSION_UTC_END` (07:00-20:00 UTC, the
London/New York sessions), never on Friday from `FRIDAY_CUTOFF_UTC` (15:00 UTC), and never
within `NEWS_BLACKOUT_MIN` (30) minutes of a high-impact US release. Releases come from
ForexFactory's weekly calendar (`NEWS_CALENDAR_ENABLED`) plus the fixed payrolls/FOMC rule
used in the backtest. A setup that qualifies while held back produces one "⏸" note per
day and is picked up as soon as the window reopens if it still qualifies. The backtest
showed this layer helping gold (+0.43R to +0.55R per trade) and hurting the currency
pairs, which is why it is not applied to them.

Manual checks:

```bash
.venv\Scripts\python special_watch_once.py --status
```

shows every symbol's current values, whether it is signalling, and the plan it would get;
`special_watch_once.py --no-send` runs one check and prints any alerts instead of sending.

With `REPORT_LANGUAGE=en` the message uses the compact English layout below:

```
[Scanner A: Pre-market Gappers]
2026-10-22 08:30 ET | 2 match(es) from 4,569 scanned | gap>5.0% price>$3.0 pre-mkt vol>50,000
1. SAIQ  $6.21  +11.69%  pre-mkt vol 293,070
2. QS  $4.29  +7.79%  pre-mkt vol 159,961

[Scanner B: Trend-join Long Hits]
2026-10-22 10:05 ET | 1 hit(s) from 2 checked | 1 new
1. QS  🟢 BUY ➡️ entry $4.52  NEW  (triggered 10:05 ET)
   above Y-high $4.08, PM high $4.38, HOD $4.52 | Y-close $3.98 > SMA200 $6.89
   gap +7.79% | pre-mkt vol 159,961
```

(`REPORT_NEWS=true` adds the headline under each ticker in both layouts.)

Section A is always the latest Scanner A list of the day and section B the setups
triggered so far today, so every message is complete on its own. Scanner A runs after the
09:30 open are tagged "(regular session)" because they measure today's move against the
previous close rather than a pre-market print.

One-time setup:

1. In Telegram, talk to **@BotFather**, send `/newbot`, follow the prompts and copy the
   token. Put it in `.env` as `TELEGRAM_BOT_TOKEN=...` (never paste it anywhere else).
2. Open a chat with your new bot and press **Start**.
3. Run the checker. It verifies the token, finds your chat id from that Start message,
   stores it in `.env`, and sends a test message in the report layout:

```bash
.venv\Scripts\python telegram_setup.py --save
```

The scheduler re-reads the Telegram settings before every send, so no restart is needed
after step 3. Until the bot is configured the scans still run and save their JSON files;
the log says the report was not sent.

### Sharing the reports with friends (group or channel)

Friends cannot join your private chat with the bot. Create a Telegram **group** (members
can talk) or **channel** (one-way broadcast), add **@your_bot** to it (for a channel make
it an administrator with "Post messages"), send any message there once, then run:

```bash
.venv\Scripts\python telegram_setup.py --add-chat
```

It finds the newest group/channel the bot was added to, stores its id in
`TELEGRAM_EXTRA_CHAT_IDS`, and posts a test message there. Scan reports and signal alerts
go to your chat **and** every extra chat; system messages (the webhook URL with its
secret, failure notes) stay in your private chat only. Invite friends with the
group's/channel's invite link (Telegram: group info > Add members / Invite link). Repeat
`--add-chat` for more chats, or edit the comma-separated list in `.env`.

# Scheduling

The schedule is defined in **US Eastern time** so it stays correct across US
daylight-saving changes regardless of this PC's time zone. `scheduler.py` runs, on
weekdays:

| Job | When (ET) | Settings |
| --- | --- | --- |
| Scanner A, pre-market | 08:30, 09:00, ... 14:00 (every 30 min) | `SCAN_TIME_ET`, `SCAN_END_ET`, `SCAN_INTERVAL_MIN` |
| Scanner B, Trend-join Long | 08:35, 09:05, ... 14:35 (5 min after each Scanner A run, until 15:00) | `STRATEGY_OFFSET_MIN`, `STRATEGY_END_ET` |
| Telegram report | after every run above | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

Scanner B signals only count as valid in the regular session at or after
`STRATEGY_START_ET` (10:00); earlier runs report "No valid setups yet".
In Bangkok 08:30 ET is 19:30 while the US is on daylight time (March to early November)
and 20:30 the rest of the year, so the day's runs span roughly 19:30 to 02:35 local.

Foreground:

```bash
.venv\Scripts\python scheduler.py
```

`--run-now` runs Scanner A and then Scanner B immediately and then keeps the schedule;
`--print-schedule` lists every run time. Logs go to `logs/scheduler.log`.

Background, surviving reboots (registers a Windows Task Scheduler job that starts
`scheduler.py` hidden at every logon):

```bash
powershell -ExecutionPolicy Bypass -File .\register_task.ps1
```

Remove it with `Unregister-ScheduledTask -TaskName "PremarketScanner" -Confirm:$false`.

## Output

`output/premarket_scan_2026-10-22.json`:

```json
{
  "date": "2026-10-22",
  "generated_at": "2026-10-22T08:30:02-04:00",
  "market_state": "PRE",
  "criteria": { "min_gap_pct": 5.0, "min_price": 3.0, "min_premarket_volume": 50000, "universe_source": "yahoo", "universe_min_avg_volume": 50000 },
  "universe_size": 4592,
  "gap_candidates": 37,
  "count": 12,
  "warnings": [],
  "results": [
    {
      "ticker": "ABCD",
      "name": "Abcd Therapeutics, Inc.",
      "price": 7.42,
      "previous_close": 5.9,
      "gap_pct": 25.76,
      "session": "premarket",
      "market_state": "PRE",
      "quote_time": "2026-10-22T08:29:41-04:00",
      "exchange": "NasdaqGM",
      "premarket_volume": 1834210,
      "volume_source": "nasdaq",
      "catalyst": "Abcd Therapeutics announced positive Phase 3 results for its lead candidate.",
      "catalyst_source": "benzinga",
      "catalyst_url": "https://www.benzinga.com/...",
      "catalyst_time": "2026-10-22T07:02:11-04:00"
    }
  ]
}
```

Results are sorted by gap size, largest first. Running the scan twice on the same day
overwrites that day's file.

## Configuration (`.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `BENZINGA_API_KEY` | empty | Benzinga News API token. Empty = Yahoo headlines. |
| `MIN_GAP_PCT` | `5` | Minimum gap up, percent. |
| `MIN_PRICE` | `3` | Minimum pre-market price, USD. |
| `MIN_PREMARKET_VOLUME` | `50000` | Minimum pre-market shares traded. |
| `UNIVERSE_SOURCE` | `yahoo` | `yahoo` = full US market from the screener, `file` = tickers in `WATCHLIST_FILE`. |
| `UNIVERSE_MIN_AVG_VOLUME` | `50000` | 3-month average daily volume floor for the Yahoo universe. |
| `MAX_UNIVERSE` | `8000` | Safety cap on tickers scanned. |
| `WATCHLIST_FILE` | `watchlist.txt` | One ticker per line, used with `UNIVERSE_SOURCE=file`. |
| `SCAN_TIME_ET` | `08:30` | First Scanner A run, US Eastern, weekdays. |
| `SCAN_END_ET` | `14:00` | Last Scanner A run. |
| `SCAN_INTERVAL_MIN` | `30` | Minutes between Scanner A runs (Scanner B follows the same cadence). |
| `STRATEGY_OFFSET_MIN` | `5` | Scanner B runs this many minutes after each Scanner A run. |
| `STRATEGY_END_ET` | `15:00` | Last Scanner B run. |
| `STRATEGY_START_ET` | `10:00` | Earliest time a Scanner B signal counts as valid. |
| `OUTPUT_DIR` | `output` | Folder for the JSON files. |
| `NEWS_LOOKBACK_HOURS` | `48` | Only headlines newer than this count as a catalyst. |
| `STRATEGY_DATA_SOURCE` | `tradingview` | `tradingview` = reference levels from TradingView, live price from Yahoo; `yahoo` = everything from Yahoo. |
| `TRADINGVIEW_SESSIONID` | empty | TradingView `sessionid` cookie; with real-time entitlement the live price comes from TradingView too. |
| `HOD_TOLERANCE_PCT` | `0.25` | How close to the high of day counts as "at the high". |
| `TELEGRAM_BOT_TOKEN` | empty | Bot token from @BotFather. |
| `TELEGRAM_CHAT_ID` | empty | Chat that receives the reports; `telegram_setup.py --save` fills it in. |
| `REPORT_LANGUAGE` | `th` | `th` plain Thai, `en` compact English. |
| `REPORT_SECTIONS` | `A,B,C` | Sections in the owner chat's report: `A` pre-market gappers, `B` Trend-join hits, `C` special watchlist. |
| `GROUP_REPORT_SECTIONS` | `C` | Sections sent to the groups/channels in `TELEGRAM_EXTRA_CHAT_IDS`; signals only by default. |
| `REPORT_NEWS` | `false` | Show the news headline under each ticker in sections A and B. Off keeps the message to signals only. |
| `TRANSLATE_NEWS` | `true` | Translate English headlines to Thai when they are shown (cached in `data/translations_th.json`). |
| `SPECIAL_ENABLED` | `true` | Include Scanner C in every message. |
| `SPECIAL_DATA_SOURCE` | `tradingview` | `tradingview` (real-time, symbols like `OANDA:XAUUSD`), `mt5` (your broker via MetaTrader 5, symbols like `XAUUSD`) or `yahoo` (symbols like `GC=F`). |
| `MT5_PATH` / `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | empty | Optional MetaTrader 5 connection details; empty attaches to the terminal already logged in. |
| `PORTFOLIO_ENABLED` | `true` | Account section in the owner's report and open/close position alerts. |
| `PORTFOLIO_TO_GROUP` | `false` | Also send the account section and position alerts to the groups. |
| `EA_BRIDGE_ENABLED` | `true` | Confirm button on MT5-quoted signals; order requests written for the EA. |
| `EA_FILES_DIR` | empty | Override the request/result folder (default `<MT5 Common>\Files\signalbot`). |
| `EA_RISK_PCT` | `1.0` | Percent of balance risked to the stop when sizing a request. |
| `EA_TP_R` | `1.0` | Take-profit written to the request, in R (0 = none; the bot still trails). |
| `EA_REQUEST_MAX_AGE_MIN` | `15` | Informational; the EA's `InpMaxAgeMinutes` enforces it. |
| `TELEGRAM_COMMANDS_ENABLED` | `true` | Long-poll Telegram for button presses and /commands. |
| `SPECIAL_WATCHLIST` | `FX:EURUSD,FX:GBPUSD,FX:USDJPY,FX_IDC:USDTHB,OANDA:XAUUSD,BITSTAMP:BTCUSD` | Symbols for Scanner C, in the style of the chosen source. |
| `TV_WEBHOOK_ENABLED` | `true` | Start the TradingView webhook receiver with the scheduler. |
| `TV_WEBHOOK_BIND` / `TV_WEBHOOK_PORT` | `127.0.0.1` / `8787` | Where the receiver listens (expose it with a tunnel). |
| `TV_WEBHOOK_SECRET` | generated | Shared secret TradingView must send (`?key=` or JSON `secret`). |
| `TUNNEL_ENABLED` | `true` | Scheduler starts a Cloudflare quick tunnel to the webhook port. |
| `CLOUDFLARED_PATH` | empty | Path to `cloudflared.exe` if auto-detection fails. |
| `TUNNEL_NOTIFY` | `true` | Send the new webhook URL to Telegram when the tunnel address changes. |
| `SPECIAL_WATCH_ENABLED` | `true` | Watch mode: entry alerts every few minutes, all day. |
| `SPECIAL_WATCH_INTERVAL_MIN` | `5` | Minutes between watch checks. |
| `ATR_PERIOD` | `14` | Days in the average true range used for the stop. |
| `STOP_ATR_MULT` | `1.0` | Initial stop = entry - this x ATR. |
| `PARTIAL_R` | `1.0` | Take half the position at this many R. |
| `TRAIL_ATR_MULT` | `3.0` | Trailing stop distance below the highest price, in ATR. |
| `MAX_HOLD_DAYS` | `10` | Close what is left after this many trading days. |
| `TRAIL_NOTIFY_ATR` | `0.5` | Alert when the trailing stop rises by this many ATR. |
| `TARGET_R_MULT` | `2.0` | Fixed target used only by the v1 backtest. |
| `TIME_FILTER_SYMBOLS` | `XAUUSD,GOLD,GC=F` | Symbols the session/news filter applies to. |
| `SESSION_UTC_START` / `SESSION_UTC_END` | `7` / `20` | Hours (UTC) during which new filtered signals are accepted. |
| `FRIDAY_CUTOFF_UTC` | `15` | No new filtered signals on Friday from this hour (UTC). |
| `NEWS_BLACKOUT_MIN` | `30` | Minutes before/after a high-impact US release with no new filtered signals. |
| `NEWS_CALENDAR_ENABLED` | `true` | Read this week's high-impact events from ForexFactory. |
| `STALE_QUOTE_MIN` | `30` | Skip symbols whose quote is older than this (market closed). |

## How a scan works

1. `universe.py` pages through the Yahoo Finance screener (250 rows per page) and keeps
   every US equity above the price and average-volume floors. Each row already carries
   the pre-market price, change and previous close.
2. `yahoo.py` keeps only quotes whose latest pre-market trade is from today and applies
   the gap and price rules. If the scan runs after 09:30 ET, today's regular-session
   change is used instead and the row is tagged `"session": "regular"`.
3. `volume.py` asks Nasdaq for each candidate's pre-market volume and drops anything at
   or below the floor. If Nasdaq is unreachable for every candidate, the volume rule is
   skipped and a warning is written rather than returning an empty report.
4. `news.py` fetches Benzinga stories tagged with the ticker from the last
   `NEWS_LOOKBACK_HOURS` and keeps the best one: a "Why Is It Moving" brief first, then a
   headline naming the company, then a story tagged with that ticker alone, then the
   newest. The teaser (or headline) is cut to one sentence. The Yahoo fallback ranks the
   same way using Yahoo's `relatedTickers` tags.
5. `report.py` prints the table and writes the JSON file.

## Limitations

* Pre-market data exists only between 04:00 and 09:30 ET on trading days. On weekends,
  market holidays, or before 04:00 ET the report is empty and `warnings` explains why.
* Yahoo and Nasdaq endpoints are public but unofficial; if either changes, the matching
  module is the only place to update.
* Nasdaq's pre-market volume is the consolidated total for the session, which can differ
  slightly from what a broker shows.

## Layout

```
scan.py                 run one pre-market scan (Scanner A)
strategy_scan.py        run one Trend-join Long check (Scanner B)
scheduler.py            daily runner (weekdays; both scanners + Telegram report after each run)
telegram_setup.py       one-time Telegram bot check: verifies token, stores chat id, sends a test
register_task.ps1       optional Windows Task Scheduler registration
tradingview/
  trend_join_long.pine  Pine Script v6 indicator with the same rules
premarket_scanner/
  config.py             settings from .env
  universe.py           ticker universe (Yahoo screener or watchlist)
  yahoo.py              quotes and gap calculation
  volume.py             Nasdaq pre-market volume
  news.py               Benzinga / Yahoo catalysts
  scanner.py            pre-market pipeline
  report.py             console table + JSON file
  models.py             Candidate / ScanResult
  tradingview.py        TradingView screener API client
  strategy.py           Trend-join Long data, rules, signals, report text
  telegram_notify.py    Telegram Bot API client + Thai/English report formats (sections A, B, C)
  special.py            Scanner C data: currency pairs, gold, bitcoin, ATR trade plan
  special_watch.py      Scanner C watch mode: new-signal / stop / target / weakened alerts
  tv_webhook.py         TradingView webhook receiver (alert -> Telegram + log)
  translate.py          English -> Thai headline translation with disk cache
special_watch_once.py   one manual Scanner C check (--status / --no-send)
tv_webhook_server.py    run the webhook receiver alone, or --test to post a sample alert
tunnel_url.py           print (or --send to Telegram) the current TradingView webhook URL
mt5_probe.py            check the MetaTrader 5 connection and list broker symbols
backtest_trend_join.py  replay the Trend-join Long rules on hourly history (Yahoo or MT5)
premarket_scanner/tunnel.py       Cloudflare quick tunnel supervisor used by the scheduler
premarket_scanner/mt5_source.py   MetaTrader 5 prices, levels and hourly history (read-only)
premarket_scanner/mt5_portfolio.py account snapshot + position opened/closed alerts (read-only)
premarket_scanner/ea_bridge.py    order request / result files for the EA, risk-based lot sizing
premarket_scanner/telegram_commands.py  owner-only buttons and /commands (long polling)
mt5_ea/SignalBotBridge.mq5        Expert Advisor skeleton that reads requests and writes results (no order line)
premarket_scanner/filters.py      session / Friday / news filters shared by live watch and backtest
premarket_scanner/backtest.py     backtest engine and reports
output/                 daily JSON files (premarket_scan_<date>.json, trend_join_<date>.json)
```
