# Connection Guardian + Replay Lab V2

Status: **unreleased** (branch `claude/admiring-archimedes-mbjesh`). Audit and the before/after matrix:
[V2_AUDIT_MATRIX.md](V2_AUDIT_MATRIX.md). Nothing here changes live trading defaults: `TRADE_MANAGEMENT_ENABLED`,
`REPLAY_TRADE_MANAGEMENT_ENABLED` and `MT5_TERMINAL_RESTART_ENABLED` all stay **false**.

## 1. MT5 Connection Guardian (`guardian.py`, `terminal.py`)

The single authority on the live connection. It wraps the bot's existing reconnect loop (30 s grace, then 5 / 10 / 20 /
40 s backoff, unchanged and still tested) instead of adding a second one.

| State | When |
|---|---|
| CONNECTED | healthy, account verified (or not required); also during a normal market closure |
| DEGRADED | stale feed, clock mismatch, symbol missing (terminal still there) |
| DISCONNECTED | broker link lost / terminal gone, before the first reconnect attempt |
| RECONNECTING | API reconnect attempts running (the existing backoff) |
| RESTARTING_TERMINAL | restarting the owned terminal (only when enabled) |
| AUTH_FAILED | MT5 refused the login (`last_error` −6) or no session |
| MANUAL_INTERVENTION_REQUIRED | wrong account / server, or restarts exhausted |
| RECOVERED | one pass after an outage: account verified, positions reconciled, then CONNECTED |

**Classification.** Causes the Guardian distinguishes:
- broker link lost (network or broker);
- terminal crash (IPC errors −10001…−10005);
- API initialisation failure;
- authorisation failure or invalid session;
- wrong account, wrong server;
- stale feed, frozen terminal (open market, connected, no tick for 30 min);
- clock mismatch;
- market closed: the weekend, the daily break `MT5_MARKET_BREAK` (default 23:55–01:10 server), 25 Dec and 1 Jan.
  A market closure is never a failure.

**Recovery, in order:**
1. Detect.
2. Confirm: a second look, 20 s, before any restart.
3. Let it heal on its own: the grace window.
4. API reconnects with the existing backoff.
5. Restart the owned terminal. Only if `MT5_TERMINAL_RESTART_ENABLED=true`, only for a dead or frozen terminal, at
   most `MT5_RECOVERY_MAX_ATTEMPTS` per outage, with `MT5_RECOVERY_COOLDOWN_SECONDS` between restarts. The
   cooldown is persisted, so it survives a bot restart.
6. Verify the login and server.
7. Reconcile: re-read every position, and replay missed candles as a digest (existing).
8. Resume.
9. Notify. Cards are posted by the notification dispatcher thread, so they never block MT5 polling.

**Safety rules:**
- An auth failure, wrong account or wrong server never triggers a restart. It needs manual intervention: the
  Guardian re-checks every cooldown and resumes by itself once fixed. The state survives a bot restart.
- Fail closed: while the account is unverified, or the link is not CONNECTED/DEGRADED, every order is refused
  (trailing-stop moves, Apply, `/place`, Close), checked inside `trade_mgmt.check` and `trader.py`.
- No order is sent during recovery; broker-side SL/TP live on the broker's server and are untouched.
- Only the terminal whose executable path is exactly `MT5_TERMINAL_PATH` (plus `MT5_TERMINAL_DATA_DIR` when the data
  folder differs) is ever stopped. Other `terminal64.exe` processes, brokers and accounts are never touched.
- No credentials are passed or logged. Account numbers are masked in cards (`•••456`).
- The Replay Lab never imports the Guardian or the terminal code, so pausing or closing a replay can never restart
  a terminal.
- `bot._connect()` now passes `path=MT5_TERMINAL_PATH` to `initialize()` when it is set, so it attaches to the owned
  terminal instead of the last-used one.

**Configuration** (none of these existed before; `PLACE_ACCOUNT_LOGINS` stays the `/place` account pin):

```
MT5_GUARDIAN_ENABLED=true          # false = exactly the previous behaviour
MT5_AUTO_RECOVERY_ENABLED=true
MT5_TERMINAL_RESTART_ENABLED=false # needs MT5_TERMINAL_PATH
MT5_HEALTH_CHECK_SECONDS=10
MT5_RECOVERY_MAX_ATTEMPTS=3        # terminal restarts per outage
MT5_RECOVERY_COOLDOWN_SECONDS=300
MT5_EXPECTED_LOGIN=                # empty = not checked
MT5_EXPECTED_SERVER=
MT5_TERMINAL_PATH=                 # C:\Program Files\<Broker> MetaTrader 5\terminal64.exe
MT5_TERMINAL_DATA_DIR=             # the install folder for a /portable terminal
MT5_MARKET_BREAK=23:55-01:10       # server time
```

State and timeline: `logs/state/guardian.json`. Operational events: `logs/ops/live-YYYY-MM.jsonl`.

## 2. Discord controls

| Command | Who | What |
|---|---|---|
| `/mt5_health` | allowed users | the status card: MT5 state, cause, account check, last tick age, order permission, restart setting, trade management, notification mode, with the Enable/Disable trade-management buttons |
| `/mt5_connection` | allowed users | the health timeline: disconnections, attempts, terminal restarts, recoveries |
| `/mt5_reconnect` | explicitly listed users (re-checked in the bot loop) | one API reconnect and account check now; never restarts the terminal, never sends an order |
| `/trade_management` | unchanged | the existing switch with confirmation |
| `/replay_status` | allowed users | what the Replay Lab last reported (`logs/replay/status.json`), labelled HISTORICAL REPLAY; read-only |
| `/notification_mode [mode]` | view: allowed users; change: explicitly listed | an alias routed to the existing `/notify` + `/notify_mode` handlers (not a second implementation) |

The persistent **⚙️ CONTROLS** card now also shows the MT5 connection state and is refreshed on every Guardian
transition.

## 3. Replay Lab V2

- **Data:** `--source auto` downloads from MT5 and, while MT5 is unavailable, replays the cached history (the report
  says so). The cache is now read bit-exact (round-trip floats), so a cached replay equals the MT5 replay.
- **Engine:** every candle is evaluated once. Any number of strategy *accounts*, each with its own virtual broker,
  trade on the same events in lockstep. The first account is the charted one; the API is unchanged.
- **Strategies:** every strategy has a version and a parameter hash. Every entry records the conditions that passed
  and failed. EMA 8/32 strategies trade exactly as before (verified: same results alone and in lockstep).
- **Jump to date:** a date ahead of the replay evaluates every candle up to it (nothing skipped) and pauses there;
  a date already revealed just scrolls. Nothing after the target is ever revealed.
- **Trade debugger:** click a BUY/SELL/EXIT marker or a trade row to see:
  - strategy, version and parameter hash; the signal and the candle that decided it;
  - entry, initial SL/TP, every trailing-stop move or blocked step;
  - exit time, price and reason; net, gross and commission; R; MFE/MAE in points and money;
  - every entry condition with ✅/❌.
- **Replay this trade:** restarts an identical session (deterministic) and pauses 8 candles before the deciding
  candle. Step forward candle by candle; tests check the trade re-appears identical.
- **Comparison:** `python -m replay compare …` or the **Compare** button (see section 5).
- **Status file:** `logs/replay/status.json`, written by the replay app and read by the bot's `/replay_status`.

## 4. EMA 20/50 Cloud — integration status

No definition of an "EMA 20/50 Cloud Crossover" strategy exists in this repository, its documents or its git
history. The same is true of C5 Freeze 001 and H1 Horizon One. So nothing claims to reproduce them.

Implemented instead: **`ema_20_50_basic` v0.1-experimental**, flagged in the UI, reports and comparison.
- **EMAs:** EMA20 and EMA50 from the shared `signal_engine.add_emas`, the same function as the live 8/32/200.
- **Cloud:** shaded between them, green while EMA20 is above EMA50, red otherwise; a custom Lightweight Charts series.
- **Entry:** EMA20 crosses EMA50 on a closed candle, and that candle closes beyond the cloud
  (`require_close_beyond_cloud`); filled at the next open.
- **Exit:** the opposite cross, or the shared protective SL/TP.
- **Re-entry:** after a stop or target exit while the cloud keeps its direction, price closes back inside the cloud
  and then beyond it again.

Unverified rules, listed in every report:
- the EMA source;
- the confirmation rule;
- thickness / ATR filters;
- the original SL/TP, exit and re-entry rules;
- session, timeframe and symbol filters;
- the sizing used in the original results.

Give me the authoritative definition and it becomes a separate, verified strategy.

## 5. Strategy comparison (real XAUUSD M30, 1–30 Oct 2026, defaults)

`python -m replay compare --source csv --csv tests/data/XAUUSD_M30_sample_20260901_20261030.csv --csv-tz utc --from 2026-10-01 --to 2026-10-31`

| Strategy | Sizing | Trades | Win % | Net | PF | Avg R | Max DD % | Max losses in a row |
|---|---|---|---|---|---|---|---|---|
| ema_cross 1.0 | fixed 1 lot | 38 | 23.7 | −16,704 | 0.674 | −0.225 | 46.3 | 11 |
| ema_cross 1.0 | 1 % risk | 38 | 23.7 | −4,077 | 0.644 | −0.225 | 10.9 | 11 |
| pullback 1.0 | fixed 1 lot | 20 | 25.0 | −5,793 | 0.783 | −0.161 | 39.8 | 10 |
| pullback 1.0 | 1 % risk | 20 | 25.0 | −1,574 | 0.744 | −0.161 | 9.2 | 10 |
| ema_20_50_basic 0.1-exp ⚠️ | fixed 1 lot | 42 | 28.6 | −5,963 | 0.895 | −0.081 | 52.5 | 9 |
| ema_20_50_basic 0.1-exp ⚠️ | 1 % risk | 42 | 28.6 | −1,872 | 0.852 | −0.081 | 10.8 | 9 |

One month of data, $50,000 start, spread 0.30, commission 7/lot round turn, slippage 0.05, ATR×2 stop, 2R target,
trade management OFF, conservative intrabar fills. These are simulated results on one month, not a verdict on any
strategy. The data fingerprint and parameter hashes in `comparison.json` make the run reproducible.

## 6. Unified operational events (`ops.py`)

One schema for LIVE and REPLAY: `event_id`, `ts`, `source`, `mode`, `symbol`, `strategy`, `version`, `type`,
`severity`, `correlation_id`, `connection_state`, `replay_session`, `detail`.
- **LIVE events** (Guardian transitions): `logs/ops/live-YYYY-MM.jsonl`.
- **REPLAY events:** `<session folder>/ops.jsonl`.

A REPLAY event cannot enter the LIVE store and the reverse (tested). The existing dedupe, retry queue, notification
audit and records are unchanged.

## 7. Performance (headless Chromium 153, 4 vCPU, no GPU; `python tests/ui_perf.py`)

| Strategy | Candles/frame | Mean FPS | Frame p95 | Worst batch | Zoom p95 | Full redraws |
|---|---|---|---|---|---|---|
| ema_cross | 1 / 4 / 16 | 60.0 / 60.0 / 58.2 | 16.8 / 16.7 / 16.8 ms | 1.4 / 6.2 / 16.9 ms | 16.7–16.8 ms | 0 |
| ema_20_50_basic (cloud) | 1 / 4 / 16 | 60.0 / 60.0 / 59.0 | 16.7 / 16.8 / 16.7 ms | 2.2 / 2.8 / 9.6 ms | 16.7–16.8 ms | 0 |

Also checked:
- a cloud point on all 1941 candles, and EMA20/EMA200 equal to the engine's values;
- no candle beyond the revealed one, and no snapping while inspecting history;
- no page errors.

Engine: ~50–60 candles/s with up to 6 compared strategy variants (one evaluation per candle).

## 8. Tests

`python -m unittest discover -s tests`:
- `tests/test_guardian.py` (14): unauthorized session, invalid credentials, temporary disconnect, terminal crash,
  bounded restarts and cooldown, restart disabled by default, wrong account and server failing closed, market
  closure, frozen terminal, persistence across a restart, terminal ownership, reconciliation, non-blocking cards,
  ops stores.
- `tests/test_replay_v2.py` (13):
  - EMA 20/50: crosses against the shared EMA on prefixes, overlays, entries/exits/conditions, no lookahead;
  - replay this trade, jump to date;
  - comparison reproducibility;
  - MT5-outage cache fallback;
  - separate REPLAY store, replay unaffected by a struggling Guardian, independent toggles;
  - Discord controls with permission re-checks.
- `tests/ui_perf.py` (Playwright): chart responsiveness and cloud rendering.

## 9. Windows validation checklist (before production)

All automated tests use a fake MT5 and mocked Discord. On the trading PC, with a **demo** account first:

1. `python -m unittest discover -s tests` passes on Windows.
2. Set `MT5_EXPECTED_LOGIN` / `MT5_EXPECTED_SERVER`. Start the bot. `/mt5_health` shows CONNECTED and the account as
   verified.
3. Log the terminal into another account. Within one health check: MANUAL_INTERVENTION_REQUIRED, one 🛑 card,
   `/place` refused. Log back in: ✅ recovered.
4. Pull the network cable for 15 s: DISCONNECTED, then CONNECTED, no card. For 3 min: the existing BLIND card, then
   ✅ recovered with the positions count.
5. Wrong password saved in the terminal: AUTH_FAILED, one card, retries no faster than every 5 min, no restart.
6. With `MT5_TERMINAL_PATH` set and `MT5_TERMINAL_RESTART_ENABLED=true`, a second terminal (another broker)
   running: kill the owned terminal in Task Manager. Expect exactly one restart of the owned terminal. Confirm
   the other terminal is untouched, open positions keep their SL/TP, and the logs show no credentials.
7. Weekend / daily break: no failure cards, `/mt5_health` shows `market_closed`.
8. Replay Lab during a live outage: `python -m replay app`, source Auto, a cached range. The replay runs and the
   report says "MT5 unavailable: replaying the cached history".
9. Close the replay window mid-run: the live bot and the terminal are unaffected; `/replay_status` shows stopped.
10. Replay Discord with a dedicated test channel: cards labelled HISTORICAL REPLAY, in order, a summary at the end.
11. `python tests/ui_perf.py` (or watch the app) on the trading PC's hardware: smooth zoom and pan, 60 FPS.

## 10. Rollback

- **Guardian only:** `MT5_GUARDIAN_ENABLED=false` (exactly the previous connection behaviour); terminal restarts are
  already off by default.
- **Notifications:** unchanged unless `/notification_mode` is switched.
- **Replay V2:** the Replay Lab is a separate program. Stop using it, or run an older commit of `replay/`.
- **Code:** `git revert -m 1 <merge commit>` on `master`; the watchdog fast-forwards to the revert.
- **Runtime files** (unused after a revert, safe to delete): `logs/state/guardian.json`, `logs/ops/`,
  `logs/replay/status.json`.

## 11. Files

- **New:**
  - code: `guardian.py`, `terminal.py`, `ops.py`, `replay/compare.py`, `replay_ui/src/cloud.ts`;
  - tests: `tests/test_guardian.py`, `tests/test_replay_v2.py`;
  - docs: `docs/V2_AUDIT_MATRIX.md`, `docs/V2_GUARDIAN_REPLAY.md`, `docs/replay_v2_debugger.png`.
- **Modified (live):** `bot.py`, `config.py`, `alerts.py`, `notify.py`, `trade_mgmt.py`, `trader.py`,
  `signal_engine.py`.
- **Modified (replay):** `replay/__main__.py`, `replay/api.py`, `replay/app/host.py`, `replay/broker.py`,
  `replay/data.py`, `replay/discord.py`, `replay/engine.py`, `replay/report.py`, `replay/strategies.py`,
  `replay_ui/src/*`, `replay/app/web/*` (rebuilt).
- **Modified (tests):** `tests/test_bot.py` (the expected command list), `tests/ui_perf.py` (cloud cases).
