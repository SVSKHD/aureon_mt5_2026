# Aureon EMA Cross Bot — v2.1

**What it is:** an alert bot for XAUUSD M30, EMA 8/32/200. It reads closed candles from your MT5 terminal and
posts to Discord / Slack / ntfy, with a chart picture on signal alerts.

**Trading:** the signal loop never trades. An order is sent only when you run `/place` in Discord and press
**Confirm**. All order code is in `trader.py`, and it is off unless `PLACE_ENABLED=1` is in `.env`.

## Quick start

```
pip install -r requirements.txt
copy .env.example to .env and fill in at least one delivery line
python -m unittest discover -s tests      # 55 offline tests, no MT5 or internet needed
python bot.py --check-time                # during market hours: shows the MT5 clock the bot uses
python watchdog.py                        # run this, never bot.py directly
```

## Files

| File | Owns | Must NOT do |
|---|---|---|
| `config.py` | every setting, `.env` reading, `validate()` | hold logic |
| `signals.py` | MT5 reads, EMAs/ATR, `detect()`, the MT5 clock | send messages, write files |
| `notifier.py` | Discord/Slack/ntfy delivery, retry queue, `State` (JSON) | call MT5, decide what to alert |
| `alerts.py` | price alerts, the command inbox, Discord slash commands | call MT5 or webhooks |
| `chart.py` | draws the candle + EMA picture (PNG) | call MT5 or the network |
| `trader.py` | `/place`: validate, preview, send one market order with SL and TP | run without an explicit command |
| `bot.py` | the loop: pause → connect → candles → price alerts → notices | indicator math, webhook code |
| `watchdog.py` | starts `bot.py`, restarts it on exit or frozen heartbeat | anything else |
| `tests/` | fake MT5 + regression tests | touch real `.env`, logs, terminal |

Runtime data lives in `logs/`: `state/`, `inbox/`, `alerts/` (every alert ever sent), `trades/` (every order sent
and the broker's answer), `charts/` (7 days), `runtime/` (program logs).

## The loop

```
every pass (10 s, and exactly 3 s after each candle close)
 ├─ weekend_pause()        Sat 04:00 → Mon 03:25 IST
 ├─ ensure_mt5()           terminal? broker link? fresh tick? clock right?   → else "BLIND" notice
 ├─ check_ema(tf)          new closed candle? → detect() → dedupe → chart → send
 ├─ process_commands()     /place, /chart, /alert waiting in the inbox (the loop wakes at once for these)
 ├─ check_price_alerts()   exact ticks for new alerts, M1 bars afterwards
 ├─ start card / daily alive card
 └─ retry anything a service did not accept (never before the live work)
```

## Alerts

| Event | Meaning |
|---|---|
| `CROSS` / `CROSS ✔ with EMA200` | EMA 8 crossed EMA 32; ✔ = both already on that side of EMA 200 |
| `EMA 8 × EMA200`, `EMA 32 × EMA200`, `TOGETHER` | single crossings of the 200, or both within 3 candles |
| `PRE-CROSS` / `PRE-CROSS FAILED` | gap closing 3 candles running and ≤ 0.15 ATR / then widening again |
| `NARROW ZONE` start / ended | gap ≤ 0.25 ATR for 4 candles (chop) / gap > 0.5 ATR again |
| `🎯 HIT <price>` | your price alert was touched |
| `⚠️ MISSED WHILE OFFLINE` | one digest for everything that happened during an outage |
| `⚠️ BLIND` / `✅ feed is back` | the bot cannot see the market / can again |
| `▶️ online`, `✅ alive` | every (re)start, and once a day at 08:00 IST |

Only **closed** candles are used. Each event is sent once: dedupe key `SYMBOL:TF:TYPE:DIR:candle-or-episode`.

## Discord commands

Setup once: create a bot in the Discord Developer Portal, invite it with the scopes `bot` and
`applications.commands`, put its token in `DISCORD_BOT_TOKEN`, your user id in `DISCORD_ALLOWED_USER_IDS`.

| Command | What it does |
|---|---|
| `/chart` | market snapshot now: candles, EMA 8/32/200, live price line, EMA state |
| `/place side lot sl tp [filling]` | shows the exact order (entry, SL, TP, filling, DEMO/REAL account) → **Confirm** / Cancel → market order |
| `/alert price`, `/alert_remove price`, `/alerts` | price alerts |

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
- There is no `/close` or `/modify`: manage the position in MT5.

## Guarantees and how they are kept

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

## Known limits (by design)

- Alerts carry no stop, target, position size, news or session filter; `/place` sends exactly what you type.
- The pre-cross and chop thresholds are not backtested.
- Chart pictures go to Discord only (Slack webhooks and ntfy JSON cannot carry a file here).
- Alert cards have no BUY/SELL buttons: webhook messages cannot carry them, so placing is `/place`.
- Alerts are as punctual as the PC, internet and MT5 terminal the bot runs on.

## Changing things

- **New signal:** add the column in `signals.fetch()`, append the event in `signals.detect()`, add a title in
  `notifier.TITLES`. Add a test.
- **New timeframe:** `TIMEFRAMES = ["M30", "H1"]` in `config.py`.
- **New delivery service:** one entry in `notifier.SERVICES`.
- **Another symbol:** run a second copy with its own `SYMBOL` and `AUREON_DATA`.

## Start on boot

Windows (run at logon so MT5 is up; also put MT5 in `shell:startup` with auto-login on):

```
schtasks /Create /TN "Aureon" /SC ONLOGON /RL HIGHEST /F ^
  /TR "cmd /c cd /d C:\path\to\ema_cross_bot && python watchdog.py"
```

Linux: a systemd unit with `ExecStart=/usr/bin/python3 watchdog.py`, `WorkingDirectory=` this folder,
`Restart=always`.
