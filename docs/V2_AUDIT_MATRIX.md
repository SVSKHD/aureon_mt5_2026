# Connection Guardian + Replay Lab V2 — audit and implementation matrix

Base: `master` @ `5fec7a0`. Baseline: `python -m unittest discover -s tests` → **247 tests OK, 1 skipped** (golden
data file not in the repository). Everything below was checked in the code, not taken from earlier prompts.

Status: ✅ exists and verified · 🟡 partial · ❌ missing · ⛔ cannot be done honestly with what is in the repository.

## Connection (Phase 1)

| Requirement | Before | Where / what was found | Plan |
|---|---|---|---|
| Reconnect with grace + backoff | ✅ | `bot.ensure_mt5/_outage/_link_up`, `RECONNECT_*`, tests in `test_reconnect.py` | keep; the Guardian wraps it (no second backoff) |
| BLIND / recovered notices | ✅ | `bot.report_blind/report_recovered` | keep; the recovered card gains the Guardian summary |
| Missed-candle digest after an outage | ✅ | `CATCH_UP` + `check_ema` digest | reuse as "reconcile missed events" |
| Position reconciliation | 🟡 | `positions.track` every pass (opened/closed from deal history) | call it explicitly on recovery and report the result |
| Process supervision | ✅ | `watchdog.py` restarts **bot.py** on crash/freeze; knows nothing about the terminal | unchanged |
| Connection state machine (8 states) | ❌ | only an internal `_link` dict | `guardian.py` (single authority) |
| Failure classification | 🟡 | `signals.health()` strings: terminal not connected / link lost / stale tick / clock mismatch | classify + `mt5.last_error()` codes (auth −6, IPC −10003/−10004/−10005) |
| Expected account / server check | ❌ | `PLACE_ACCOUNT_LOGINS` pins accounts for `/place` only | `MT5_EXPECTED_LOGIN/SERVER`; mismatch = fail closed for management orders and `/place` |
| Auth failure → manual intervention | ❌ | init failures retried forever every 40 s | stop restarts, slow retry, MANUAL_INTERVENTION_REQUIRED |
| Market closure ≠ failure | 🟡 | weekend pause handled; daily break/holidays show as "stale tick" | classify as market closed, no failure path |
| Terminal restart (owned instance only) | ❌ | `initialize()` attaches to whatever terminal; no process code | `terminal.py`: exact-path ownership, bounded, cooldown, off by default |
| Health timeline, persisted cooldowns | ❌ | in-memory only | `logs/state/guardian.json` |
| Non-blocking notifications | 🟡 | `deliver()` posts synchronously (parallel, short timeouts); `notify.dispatch` exists for async | Guardian cards via the existing dispatcher |

## Replay Lab (Phases 2–4, 7)

| Capability | Before | Verified in | Plan |
|---|---|---|---|
| MT5 / CSV / cache loading, gaps, DST, warm-up | ✅ | `replay/data.py`, `test_replay.Data` | add `auto` source: MT5, else the cache when it covers the range (MT5 outage) |
| Shared indicator evaluation, live parity | ✅ | `signal_engine`, `test_same_events_as_the_live_bot` | generalise EMA helper for 20/50 (no new EMA math) |
| Candle-by-candle, pause/step/speed/restart | ✅ | `engine.ReplayController`, tests | add run-to-candle (jump / replay this trade) |
| PySide6 window, React/TS, Lightweight Charts | ✅ | `replay/app`, `replay_ui`, `test_replay_app`, `ui_perf.py` | extend |
| Crosshair OHLC, zoom/pan, EMA overlays, entry/exit arrows, SL/TP lines, trade labels, speed, step, full screen, panels | ✅ | `chart.ts`, `App.tsx` | keep |
| Jump to date | 🟡 | `chart.jumpTo()` exists, no control | date input + jump (only within revealed candles) |
| Intrabar from real M1 only | ✅ | `engine.forming`, test | keep |
| Discord HISTORICAL REPLAY channel | ✅ | `replay/discord.py`, tests | add strategy events + ops ids |
| Virtual broker, conservative OHLC fills | ✅ | `replay/broker.py`, tests | add MFE/MAE money, condition record |
| Strategies: indicators / ema_cross / pullback / manual | ✅ | `replay/strategies.py` | add version, parameter hash, entry conditions |
| EMA 20/50 Cloud | ⛔ authoritative / ❌ basic | no EMA 20/50, "cloud", C5 or H1 Horizon definition anywhere in code, docs or git history | `ema_20_50_basic_experimental` only, unverified rules listed |
| Trade debugger, replay this trade | ❌ | trades panel shows rows only | debugger panel + run-to-candle |
| Strategy comparison | ❌ | one strategy per run | `replay/compare.py`: one evaluation, N strategies in lockstep |
| Replay isolation from live | ✅ | `test_replay.Safety`, no live imports | add: replay never imports the Guardian / terminal code; `ReadOnlyMT5` loses `shutdown` |

## Discord, toggles, observability (Phases 5, 6, 8)

| Item | Before | Plan |
|---|---|---|
| `/trade_management` | ✅ `alerts.py` + `trade_mgmt.py` | reuse; controls card gains MT5 health |
| `/notification_mode` | 🟡 exists as `/notify_mode` + `/notify` | `/notification_mode` = an alias routed to the SAME handler (no second implementation) |
| `/mt5_health`, `/mt5_connection`, `/mt5_reconnect` | ❌ | new, through `alerts.may()` and the signed inbox |
| `/replay_status` | ❌ | the replay app writes `logs/replay/status.json`; the bot only reads it |
| Live vs replay trade management | ✅ independent (`TRADE_MANAGEMENT_ENABLED`, `REPLAY_TRADE_MANAGEMENT_ENABLED`) | more coupling tests |
| Unified operational event schema | ❌ (notify audit + records only) | `ops.py`: one schema, LIVE and REPLAY stores kept apart |

## Live-trading risks found

1. `bot._connect()` calls `mt5.initialize()` without a path: with several terminals installed it attaches to the
   last-used one, possibly another account. → initialize(path=MT5_TERMINAL_PATH) when configured + account check.
2. An account switch inside the terminal is not detected: management orders could go to the wrong account.
   → expected login/server verification, fail closed.
3. Init failures (e.g. wrong password) are retried every 40 s forever. → AUTH_FAILED → manual intervention.
4. `ReadOnlyMT5` allows `shutdown`, harmless today (per-process), removed anyway.
