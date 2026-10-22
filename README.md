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

1. **Expose the port to the internet** (TradingView must reach it). Simplest free option,
   no account, new random URL every start:

   ```bash
   winget install Cloudflare.cloudflared
   ```

   ```bash
   cloudflared tunnel --url http://127.0.0.1:8787
   ```

   It prints a URL like `https://random-words.trycloudflare.com`. For a fixed URL create a
   free Cloudflare account and a named tunnel (`cloudflared tunnel login`,
   `cloudflared tunnel create signalbot`, route a hostname), or use ngrok (free account,
   one static domain). Keep the tunnel running alongside the scheduler.

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

After every scheduled run the scheduler sends one message with three sections:
Scanner A (pre-market gappers), Scanner B (Trend-join Long hits) and Scanner C (the
special watchlist: currency pairs, gold, bitcoin). The default language is **plain Thai**
written for readers without a trading background (`REPORT_LANGUAGE=th`); English headlines
are translated to Thai when `TRANSLATE_NEWS=true` (free public service, falls back to the
English headline). Example:

```
[Scanner A: หุ้นที่พุ่งขึ้นก่อนตลาดเปิด]
สแกนเมื่อ 08:30 น. เวลาตลาดสหรัฐ (19:30 น. ไทย) วันที่ 22 ต.ค. 2026
ตรวจหุ้นสหรัฐ 4,569 ตัว พบ 2 ตัวที่ราคาขึ้นมากกว่า 5% จากราคาปิดเมื่อวาน ราคาสูงกว่า $3 และมีคนซื้อขายช่วงก่อนตลาดเปิดมากกว่า 50,000 หุ้น
1. QS (QuantumScape) ราคา $4.29 ขึ้น 7.79% จากเมื่อวาน ซื้อขายแล้ว 159,961 หุ้น
   ข่าว: QuantumScape รายงานผลการดำเนินธุรกิจและการเงินไตรมาสที่สามปี 2026

[Scanner B: หุ้นที่เข้าเงื่อนไขซื้อ Trend-join Long]
ตรวจเมื่อ 10:05 น. เวลาตลาดสหรัฐ (21:05 น. ไทย) จากหุ้น 2 ตัวในรายการด้านบน
เงื่อนไขที่ต้องผ่านครบทุกข้อ: ...
ผ่านเงื่อนไข 1 ตัว (ใหม่ในรอบนี้ 1 ตัว):
1. QS (QuantumScape) สัญญาณซื้อที่ราคา $4.52 ใหม่! เมื่อ 10:05 น. เวลาตลาดสหรัฐ (21:05 น. ไทย)
   เหตุผล: สูงกว่าจุดสูงสุดเมื่อวาน ($4.08) | สูงกว่าจุดสูงสุดก่อนตลาดเปิด ($4.38) | ...

[Scanner C: สินทรัพย์ที่สนใจเป็นพิเศษ]
ราคาล่าสุดเมื่อ 21:05 น. ไทย (ตลาดเหล่านี้เปิดเกือบตลอด 24 ชม. จึงไม่มีช่วงก่อนตลาดเปิด)
1. ทองคำ (ฟิวเจอร์ส) (GC=F) ราคา $4,100 ลง 1.21% จากเมื่อวาน
   แนวโน้มระยะยาว: ขาลง | สัญญาณซื้อ Trend-join: ยังไม่ผ่าน (ยังต่ำกว่าจุดสูงสุดเมื่อวาน $4,161, ...)

ข้อมูลอัตโนมัติเพื่อประกอบการตัดสินใจ ไม่ใช่คำแนะนำการลงทุน
```

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
🔔 สัญญาณซื้อใหม่ (Trend-join Long): ทองคำ (ราคาอ้างอิงตลาดโลก) (GC=F)
เวลา 23:05 น. ไทย (12:05 น. เวลาตลาดสหรัฐ)
ราคาเข้าโดยประมาณ: $4,120
จุดตัดขาดทุน: $4,080 (ต่ำกว่าราคาเข้า 0.97% เท่ากับระยะที่ราคาแกว่งเฉลี่ยใน 1 วัน คิดจาก 14 วันล่าสุด)
เป้าหมายทำกำไร: $4,200 (สูงกว่าราคาเข้า 1.94% คือ 2 เท่าของระยะเสี่ยง)
เหตุผล: ราคาทะลุจุดสูงสุดเมื่อวาน $4,100 และกำลังอยู่ที่จุดสูงสุดของวัน แนวโน้มระยะยาวเป็นขาขึ้น (...)
ถ้าราคากลับลงต่ำกว่า $4,100 ก่อนถึงเป้าหมาย ถือว่าสัญญาณอ่อนลง จะแจ้งให้ทราบ
```

The plan is mechanical: stop-loss = entry minus `STOP_ATR_MULT` x the 14-day average true
range (ATR, the typical one-day swing), target = entry plus `TARGET_R_MULT` x the distance
to the stop. The signal is then followed for the rest of the day and one more alert goes
out when the price hits the stop ("⛔"), reaches the target ("✅") or falls back below
yesterday's high ("⚠️", the setup has weakened). One signal per symbol per US trading day.
State: `data/special_watch_state.json`; the day's signals: `output/special_signals_<date>.json`.

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
   WISeSat.Space and SEALSQ Advance Satellite Cybersecurity Hardening ...
2. QS  $4.29  +7.79%  pre-mkt vol 159,961
   QuantumScape Reports Third Quarter 2026 Business and Financial Results.

[Scanner B: Trend-join Long Hits]
2026-10-22 10:05 ET | 1 hit(s) from 2 checked | 1 new
1. QS  entry $4.52  NEW  (triggered 10:05 ET)
   above Y-high $4.08, PM high $4.38, HOD $4.52 | Y-close $3.98 > SMA200 $6.89
   gap +7.79% | pre-mkt vol 159,961
   QuantumScape Reports Third Quarter 2026 Business and Financial Results.
```

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
| `TRANSLATE_NEWS` | `true` | Translate English headlines to Thai (cached in `data/translations_th.json`). |
| `SPECIAL_ENABLED` | `true` | Include Scanner C in every message. |
| `SPECIAL_DATA_SOURCE` | `tradingview` | `tradingview` (real-time, symbols like `OANDA:XAUUSD`) or `yahoo` (symbols like `GC=F`). |
| `SPECIAL_WATCHLIST` | `FX:EURUSD,FX:GBPUSD,FX:USDJPY,FX_IDC:USDTHB,OANDA:XAUUSD,BITSTAMP:BTCUSD` | Symbols for Scanner C, in the style of the chosen source. |
| `TV_WEBHOOK_ENABLED` | `true` | Start the TradingView webhook receiver with the scheduler. |
| `TV_WEBHOOK_BIND` / `TV_WEBHOOK_PORT` | `127.0.0.1` / `8787` | Where the receiver listens (expose it with a tunnel). |
| `TV_WEBHOOK_SECRET` | generated | Shared secret TradingView must send (`?key=` or JSON `secret`). |
| `SPECIAL_WATCH_ENABLED` | `true` | Watch mode: entry alerts every few minutes, all day. |
| `SPECIAL_WATCH_INTERVAL_MIN` | `5` | Minutes between watch checks. |
| `ATR_PERIOD` | `14` | Days in the average true range used for the stop. |
| `STOP_ATR_MULT` | `1.0` | Stop = entry - this x ATR. |
| `TARGET_R_MULT` | `2.0` | Target = entry + this x (entry - stop). |
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
output/                 daily JSON files (premarket_scan_<date>.json, trend_join_<date>.json)
```
