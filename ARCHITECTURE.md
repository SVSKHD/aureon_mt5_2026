# Aureon — v1.7.1

**What it is:** a labelled trade assistant for XAUUSD on M30 (XAGUSD built in, switched off by default). It reads
closed candles from your MT5 terminal, labels the whole life of each EMA 8/32 trend, tracks your positions, trails
their stop-loss in steps, and posts to Discord / Slack / ntfy.

**Trading:** the bot never opens and never closes a trade on its own. An order is sent only by `/place` + **Confirm**,
or by **Close** + **Confirm** on a MAX POINT card. The only order action it takes without a tap is *tightening* a
stop-loss (never widening, never removing). All order code is in `trader.py`; `/place` and Close are off unless
`PLACE_ENABLED=1`.

**Version:** `VERSION` (shown at startup and in `/status`). Major + minor always add up to 8: 1.7.x → 2.6.x → 3.5.x.

## Quick start

```
pip install -r requirements.txt
put at least one delivery line in .env (see ".env keys" below)
python -m unittest discover -s tests      # 140 offline tests, no MT5 or internet needed
python bot.py --check-time                # during market hours: shows the MT5 clock the bot uses
python watchdog.py                        # run this, never bot.py directly (it also auto-updates)
```

## Files

| File | Owns | Must NOT do |
|---|---|---|
| `config.py` | every setting, `.env` reading, the `SYMBOLS` table, `validate()` | hold logic |
| `signals.py` | MT5 candle/tick reads, EMAs, cross + EMA200 events (`detect()`), the MT5 clock | send messages, write files |
| `labels.py` | the label engine: bad-tick guard, Wilder ATR, trend phases, pullbacks, pre-cross, TSL steps | call MT5 (pure functions) |
| `notifier.py` | Discord/Slack/ntfy delivery, channel routing, retry queue, outbox, `State` (JSON) | call MT5, decide what to alert |
| `alerts.py` | price alerts, the signed command inbox, Discord slash commands, buttons, outbox poster | call MT5 or webhooks |
| `positions.py` | position tracking, stepped TSL decisions, in-trade alerts, MAX POINT, `/positions` | send orders itself |
| `monitor.py` | the live monitor card per symbol | anything else |
| `weekend.py` | Friday warnings, `/place` block before the close, Monday carry-over | close anything |
| `report.py` | `/last-week` | write state |
| `store.py` | `record(kind, payload)` → JSONL sink (database-ready) | be read back by the bot |
| `uploader.py` | optional: ships the JSONL records to the Assembler API from its own thread (`RECORD_SINK=api`) | call MT5, block or change anything else |
| `backfill.py` | by hand: upload a research CSV or terminal history to the Assembler bars route | run while the bot runs (`--mt5`) |
| `chart.py` | candle + EMA pictures (PNG) | call MT5 or the network |
| `trader.py` | **the only module that sends order requests**: `/place`, `modify_sl` (tighten only), confirmed close | run without an explicit command (except tightening a stop) |
| `bot.py` | the loop and its stages | indicator math, webhook code |
| `updater.py` | git fetch / fast-forward / rollback, notices | touch positions |
| `watchdog.py` | starts `bot.py`, restarts it, runs updates and the 90 s health check | anything else |
| `tests/` | fake MT5 + regression and golden tests | touch real `.env`, logs, terminal |

Runtime data lives in `logs/`: `state/`, `inbox/` and `outbox/` (Discord thread ↔ loop), `alerts/`, `trades/`,
`records/` (JSONL records, plus `rejected-*.jsonl` from the uploader), `notices/` (watchdog → bot), `charts/`, `runtime/`.

## Symbols

`SYMBOLS` in `config.py`: XAUUSD enabled, XAGUSD built and tested but disabled until its thresholds are checked on
silver data. A disabled symbol is never touched (no MT5 reads, alerts, TSL or monitor); commands naming it answer
"XAGUSD is disabled in config"; a position on it gets one "not managed" alert. Enable with
`ENABLED_SYMBOLS=XAUUSD,XAGUSD` in `.env` and restart (no tracked file changes, so auto-update keeps working).
Broker suffixes: `SYMBOL_MAP=XAUUSD:XAUUSD.m`. The bot refuses to start if an enabled symbol is missing from
Market Watch. Every state key, dedupe key, retry entry, record and title is per symbol.

| Per-symbol value | XAUUSD | XAGUSD (untested) |
|---|---|---|
| `tsl_buffer` | 5.0 price units | 0.4 × ATR |
| `maxpoint_giveback` | 10.0 | 0.75 × ATR |
| `maxpoint_min_profit` | 20.0 | 1.5 × ATR |

## The loop

```
every pass (10 s, and exactly 3 s after each candle close); every stage is wrapped: one failing stage is
reported (rate-limited) and the others still run
 ├─ notices                watchdog / updater messages (updated, skipped, rolled back, crashed)
 ├─ weekend_pause()        Sat 04:00 → Mon 03:25 IST
 ├─ ensure_mt5()           terminal? broker link? fresh tick? clock right?   → else "BLIND" notice
 ├─ warm start (once)      labels rebuilt from candles, positions re-read, pending TSL step applied
 ├─ check_ema(symbol)      new closed candle → detect() + labels → dedupe → chart → send
 │                         → positions.on_candle (TSL, in-trade alerts) → monitor refresh
 ├─ process_commands()     slash commands and buttons waiting in the inbox (the loop wakes at once for these)
 ├─ positions.track()      opened / closed / MAX POINT giveback
 ├─ monitor, weekend       live card edits; Friday warnings
 ├─ check_price_alerts()   exact ticks for new alerts, M1 bars afterwards
 ├─ start / alive cards, retry queue
 └─ update requested?      stop HERE (inbox empty, nothing in flight) so the watchdog can update
```

## Alerts

Labels (`labels.py`, closed candles only; gap = EMA8 − EMA32, ga = gap / ATR14 on bad-tick-cleaned bars):

| Label | Fires when | Channel |
|---|---|---|
| `CROSS` (tag above/below 200, LESSER GAP, TREND FINISHED line) | the gap changes sign | alerts |
| `GAP WIDENING` | ga ≥ 1.0 and 4 rising candles (3 rises); re-arms after 2 falls | updates |
| `PARALLEL RUNNING` | ga ≥ 0.8, 6-candle spread ≤ 20 % of g, EMA32 moving with the trend | updates |
| `WIDE GAP` | ga ≥ 2.5 (re-arms below 1.5) | updates |
| `GAP PEAKED` | after WIDE GAP, 2 falls in a row (re-arms on 4 rising candles with ga ≥ 2.5) | alerts |
| `PULLBACK STARTED` / `RE-ENTRY` | close back across EMA8 against / with the trend (see `labels.Pullback`) | updates |
| `PULLBACK FAILED` | close beyond EMA32 against the trend by > 0.25 ATR | alerts |
| `PRE-CROSS 8/32, 8/200, 32/200` / `FAILED` | armed → converging → (failed) — a WATCH signal, Monitor button only | alerts |
| `EMA 8 × EMA200`, `EMA 32 × EMA200`, `BOTH × EMA200` | from `signals.detect()` | alerts |

Every alert carries the present market status line. Quiet labels on the same candle as an alerts event ride on its
card. Bad ticks (lower wick > 2 ATR, upper wick > 2.5 ATR beyond the body) end at the body edge for labels, ATR and
pullback extremes, and are logged; this cannot protect a live stop from a real broker quote.

Positions (`#positions`): `POSITION OPENED` / `CLOSED` (SL/TP/manual from deal history), `TSL MOVED`,
`TREND RUNNING`, `TREND STRENGTHENING, SL TIGHTENED` (both on one candle = one card), `MAX POINT` (GAP PEAKED, or
profit gave back `maxpoint_giveback` after `maxpoint_min_profit`; once per peak; Close/Keep buttons, in `#alerts`).
Stepped TSL: after a RE-ENTRY, a close beyond the pre-pullback extreme moves the SL to the pullback extreme ∓
`tsl_buffer` — only with the trend, only if tighter, within the broker's stops/freeze levels, verified by reading
the position back; a failed move is reported once, never retried. `TSL_MODE=suggest` shows an Apply button instead.

Also: `🎯 HIT`, `MISSED WHILE OFFLINE` digest, `BLIND` / `feed is back`, `online` / `alive`, weekend warnings,
`script failure`, update notices. Each event is sent once: dedupe key `SYMBOL:TF:TYPE:DIR:candle-or-episode`.

Channels (section 11): `DISCORD_CHANNEL_ALERTS/UPDATES/POSITIONS` (bot client, needed for buttons and edited cards)
and/or `DISCORD_WEBHOOK_UPDATES/POSITIONS`; anything missing falls back to the single channel. Slack and ntfy get the
`#alerts` set only.

## Discord commands

Setup once: create a bot in the Discord Developer Portal, invite it with the scopes `bot` and
`applications.commands`, put its token in `DISCORD_BOT_TOKEN`, your user id in `DISCORD_ALLOWED_USER_IDS`.

| Command | What it does |
|---|---|
| `/chart [symbol]` | market snapshot now: candles, EMA 8/32/200, live price line, EMA state |
| `/status [symbol]` | `/chart` + market status line, version, monitor state; one card per symbol without a symbol |
| `/positions` | every open position (entry, P/L, SL/TP, TSL step and what triggers the next) + account totals; Refresh |
| `/monitor [symbol]`, `/unmonitor [symbol]` | the live monitor card (stays on until /unmonitor, survives restarts) |
| `/last-week [symbol]` | previous Mon–Fri: crosses, 200 crosses, positions, P&L, week chart |
| `/place symbol side lot sl tp [filling]` | shows the exact order → **Confirm** / Cancel → market order |
| `/alert price [symbol]`, `/alert_remove price [symbol]`, `/alerts` | price alerts |

Buttons (Monitor, Apply, Close → Confirm, Keep, Refresh) are routed by custom id, so they keep working after a
restart; they go through the same signed inbox as the commands.

`/place` details:
- `side` and `filling` are drop-downs. Filling: Auto (what the broker lists for the symbol, with fallback), FOK, IOC, Return.
- `sl` and `tp` are **required**. A small number is a distance from entry (`20` = $20 away); a number at least
  half the current price is an exact level (`4095.5`). The confirm card always shows the resulting prices.
- The confirm card shows the money: loss at the stop, gain at the target, margin, in the account currency.
- Checks before sending (all fail closed — if a check cannot be done, no order): allowed user, lot limits and
  step, SL/TP on the right side, broker minimum stop distance, Algo Trading on, risk calculation, your optional
  limits, `order_check`.
- **Accounts:** a demo account works as soon as `PLACE_ENABLED=1`. A real-money account also needs
  `PLACE_ALLOW_REAL=1`. `PLACE_ACCOUNT_LOGINS` can pin the exact account numbers.
- **Optional limits** (`.env`, empty = off): `PLACE_MAX_LOT`, `PLACE_MAX_RISK` (loss at the stop per order),
  `PLACE_MAX_POSITIONS`, `PLACE_DAILY_LOSS_LIMIT` (closed loss since server midnight).
- **After sending**, the bot reads the position back from MT5 and reports what is really there: `FILLED`,
  `PARTLY FILLED x of y`, or `ACCEPTED, not filled yet`. If the broker opened the position without the SL/TP,
  the bot sets them; if that fails it says so in capitals.
- **Who can send an order:** the Discord user is checked twice (in Discord and again where the order is sent).
  Order commands are signed with a key that exists only in the running bot's memory, so a file dropped into
  `logs/inbox/` by another program is refused, and a copied command cannot be replayed.
- **Timing safety:** Confirm works once and expires after 30 s; a command older than 10 s is refused, never run
  late; a command is deleted before it runs, so a crash cannot repeat it; a requote is retried once.
- There is no `/close` or `/modify` command. A position can be closed from a MAX POINT card (Close → Confirm);
  the bot itself only ever tightens stops. Otherwise manage the position in MT5.

## Guarantees and how they are kept

- **Restarts:** labels, pullbacks and the TSL are recomputed from closed candles, so a restart (or an update) in the
  middle of a pullback still fires the pending step; monitor state and position memory are in the state file.

- **On time:** the loop wakes 3 s after each close; history is not pulled between closes; services are posted in
  parallel; old retries cannot delay a fresh alert.
- **Right clock:** the MT5 time offset starts from config (`SERVER_TZ`, default Europe/Athens, follows DST) and is
  then *measured from live ticks*. If the feed disagrees (different DST dates, a broker that stamps plain UTC,
  wrong config) the bot switches to the feed within ~40 s and tells you, without reconnecting. A stale tick is
  never used to measure. Tested through the real reconnect loop for feeds ahead of and behind config.
- **Right EMA 200:** 1000 warm-up candles, so it equals the MT5 chart.
- **Nothing lost:** per-timeframe checkpoint; after an outage everything since the checkpoint is replayed as one
  digest. A message is marked sent only after it was delivered or queued on disk.
- **No silent failure:** BLIND notice after 3 min (terminal / broker link / clock) or 75 min (no ticks).
- **State safety:** atomic JSON writes with `.bak`; a corrupt file falls back to `.bak`, then to keys rebuilt from
  the alert log.
- **Price alerts:** an alert only sees prices from after it was created — exact ticks for its first 3 minutes,
  then M1 candles that opened after creation.
- **Discord commands:** accepted only in `DISCORD_ALERT_CHANNEL_ID` and only from `DISCORD_ALLOWED_USER_IDS`;
  `/place` needs the user listed explicitly.

## Records (database-ready)

`store.record(kind, payload)`; kinds: event, alert, position, sl_change, order, command, weekly, system, bar. Default
sink: `logs/records/<kind>-YYYY-MM.jsonl` (`RECORD_SINK=none` turns it off). Each row: record_id, kind, utc_time,
server_time, version, account_login, symbol + payload. Never blocks, never raises, never read back by the bot.

`bar` (1.7.1, `RECORD_BARS=1`): one record per newly closed candle per enabled symbol, missed candles after an
outage too: tf, bar_time_utc, bar_time_server, OHLC, tick_volume, spread, ema8/32/200 (the bot's EMAs) and atr14
(the label engine's Wilder ATR on bad-tick-cleaned bars). Built from values already computed; no extra MT5 call.

### Upload to Assembler (1.7.1, optional)

`RECORD_SINK=api` = JSONL **and** upload. `uploader.py` runs on its own daemon thread (never the MT5 thread),
reads the JSONL files in order and keeps a byte cursor per file in `logs/state/upload_cursor.json` (atomic, with
`.bak`). Only complete lines are sent; the cursor moves only after a 200. `bar` rows go to
`POST /api/trading/mt5/bars` (runs per symbol + timeframe), everything else to `POST /api/trading/mt5/records`,
gzip, `Authorization: Bearer`. At-least-once (the server is idempotent); JSONL files are never deleted.
Timeout / 5xx / 503: back off 15 s → 5 min. 429: wait `Retry-After`. 401: pause, retry every 5 min, notice at once.
413 / 422: halve the batch; a single refused row (or a row the server lists as rejected) goes to
`logs/records/rejected-YYYY-MM.jsonl` and is skipped. One "Record upload FAILING" notice in `#updates` after 30 min
of failures (at once for 401) and one "recovered" notice; both are also `system` records. `/status` shows
`Record upload: on/off · last success · backlog`. The token appears only in the HTTP header.

Backfill by hand: `python backfill.py --csv <file> --symbol XAUUSD --timeframe M30` (research CSV columns; missing
EMA/ATR are computed with the bot's math) or `python backfill.py --mt5 --symbol XAUUSD --timeframe M30 --from
2025-01-01` (needs the terminal; refuses while the bot's heartbeat is fresh). Chunks of 5,000; prints sent /
inserted / updated / rejected.

## Auto update

The watchdog checks `UPDATE_BRANCH` (default `main` — **this repository uses `master`, so set
`UPDATE_BRANCH=master`**) every `UPDATE_CHECK_MIN` minutes. Only a clean fast-forward is taken: the watchdog asks
the bot for a safe moment (`logs/state/update.request`), the bot stops itself at the end of a pass with an empty
inbox, the watchdog pulls (and runs `pip install -r requirements.txt` if it changed) and starts the new version on a
90 s probation: a crash or no heartbeat rolls it back (`git reset --hard` to the recorded commit) and that remote head
is not tried again. A dirty tree or local commits keep the running version and are reported once per remote head,
with the files. `UPDATE_ENABLED=0` turns it off. Positions are never touched by a restart.

## .env keys added in 1.7.0

`ENABLED_SYMBOLS`, `SYMBOL_MAP`, `DISCORD_CHANNEL_ALERTS`, `DISCORD_CHANNEL_UPDATES`, `DISCORD_CHANNEL_POSITIONS`,
`DISCORD_WEBHOOK_UPDATES`, `DISCORD_WEBHOOK_POSITIONS`, `TSL_MODE` (auto|suggest), `WEEKEND_WARN_TZ`
(Asia/Kolkata), `WEEKEND_WARN_FROM` (20:00), `WEEKEND_BLOCK_PLACE_MIN` (60, 0 = off), `WEEKEND_CLOSE_FALLBACK`
(23:57 server time, only without broker history), `RECORD_SINK` (jsonl|none), `UPDATE_ENABLED` (1), `UPDATE_BRANCH`
(main), `UPDATE_CHECK_MIN` (5).

## .env keys added in 1.7.1

`RECORD_SINK` (now jsonl | none | api), `ASSEMBLER_URL` (base URL, e.g. the server's Tailscale address),
`ASSEMBLER_RECORDS_TOKEN` (bearer token), `RECORD_API_INTERVAL_S` (15), `RECORD_API_BATCH` (500, capped at 500),
`RECORD_API_TIMEOUT_S` (10), `RECORD_BARS` (1; 0 = no bar records). `validate()` refuses `RECORD_SINK=api` without
the URL or the token.

## Known limits (by design)

- Alerts carry no target, position size or news filter; `/place` sends exactly what you type. No automatic entry.
- XAGUSD thresholds and the two EMA200 pre-cross pairs are untested defaults; the chop (narrow zone) thresholds are
  not backtested.
- The Friday close is learned from last Friday's last M1 candle (the MT5 Python API has no session schedule).
- Chart pictures go to Discord only (Slack webhooks and ntfy JSON cannot carry a file here).
- Buttons and edited-in-place cards need the Discord bot client; with webhooks only they are plain cards.
- Alerts are as punctual as the PC, internet and MT5 terminal the bot runs on.

## Changing things

- **New label:** add it in `labels.run()` and `bot.LIVE_LABELS`, a title in `notifier.TITLES`, its channel in
  `notifier.CHANNEL`. Add a test (and a golden assertion if the owner has the data).
- **New timeframe:** `TIMEFRAMES = ["M30", "H1"]` in `config.py`.
- **New delivery service:** one entry in `notifier.SERVICES`.
- **Another symbol:** add it to `SYMBOLS` with its per-symbol values; enable it with `ENABLED_SYMBOLS`.

## Start on boot

Windows (run at logon so MT5 is up; also put MT5 in `shell:startup` with auto-login on):

```
schtasks /Create /TN "Aureon" /SC ONLOGON /RL HIGHEST /F ^
  /TR "cmd /c cd /d C:\path\to\ema_cross_bot && python watchdog.py"
```

Linux: a systemd unit with `ExecStart=/usr/bin/python3 watchdog.py`, `WorkingDirectory=` this folder,
`Restart=always`.
