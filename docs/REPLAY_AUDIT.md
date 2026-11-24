# Replay Lab — repository audit (before any code change)

Base: `master` @ `af79929` (1.7.2 + notification intelligence / trade management switch).

## 1. Reusable indicator and event code

| Function | Module | Pure? | Notes for replay |
|---|---|---|---|
| EMA 8/32/200 + `atr` columns | inside `signals.fetch()` | **no**: the math is inline in the MT5 download | must be extracted once (`add_indicators`) so the live fetch and the replay share it |
| `signals.detect(df)` | signals | yes (reads only `df`) | cross, EMA200 crosses, parallel, narrow zone **on the last bar of df** |
| `labels.prepare(df)` / `labels.run(bars)` | labels | yes | bad-tick cleaning, Wilder ATR, label state machine; causal (bar i reads bars ≤ i only) |
| `labels.tsl_track(bars, start, side, buffer_at, entry)` | labels | yes | the stepped trailing stop the live bot applies |
| `labels.status_line(state)` | labels | yes | market status line |
| `bot.candle_events`, `bot.label_event`, `bot.LIVE_LABELS`, the dedupe key | bot | functions are pure, **module is not** | importing `bot` reads the live state file, configures logging and imports MT5/trader; extract once into a pure module |
| `notifier.format_event`, `card`, `_discord`, `_post` | notifier | formatting pure except `to_ist` | `to_ist` uses the **global live** server offset; historical IST must be computed per candle (DST) |
| `notify_events.classify` | notify_events | yes | severity for replay events |
| `chart.render` | chart | yes (matplotlib PNG) | static PNG; not suitable for an interactive replay chart; colour palette reused |
| `config.SYMBOLS`, `sym_value`, `BAR_MINUTES`, `WARMUP_BARS`, EMA periods | config | values | reused as-is |

## 2. Wall clock, MT5 terminal and global state

| Dependency | Where | Replay handling |
|---|---|---|
| wall clock `signals.utc_now()` | `server_now`, `fetch(since)`, `health`, price ranges, `observe_clock` | replay never calls them; historical time comes from the candles |
| global `signals._server_offset` | `to_ist`, `stamp_to_dt` users, `notifier._times`, `record_bars` | replay converts with `zoneinfo(SERVER_TZ)` per candle (DST-correct for history) |
| MT5 terminal | `signals.fetch/live_price/health`, `positions.*`, `trader.*`, `weekend.*` | replay reads candles through a **read-only** MT5 wrapper (`copy_rates_*`, `symbol_info` only); `order_send`/`order_check` are not reachable |
| live state | `notifier.State` (state.json), `bot.LABELS/LAST_DF`, `positions.OPEN`, `monitor` book, `trade_mgmt._s`, `notify` state | replay keeps its own per-session state objects and its own folder `logs/replay/` |
| execution interfaces | `trader.place/modify_sl/close_position` (only order senders), `alerts` inbox (signed commands) | replay never imports `trader`, `bot`, `alerts`, `positions`; a test enforces it |

## 3. State machines

- `labels.run` (trend life: cross → widening/parallel/wide/peaked → pullback/re-entry/failed; pre-cross per pair).
- `labels.Pullback` (shared by labels and the trailing stop).
- `bot.check_ema` dedupe: key `SYMBOL:TF:TYPE:DIR:episode-or-bar`, `state.seen`.
- `positions` per-ticket book (live only), `monitor` on/off book, `notify` buffers, `trade_mgmt` switch.

## 4. How the live bot evaluates a candle (what the replay must reproduce)

`check_ema` fetches `WARMUP_BARS` (1000) candles before the checkpoint plus the new closed candle(s), computes EMAs on
that window, runs `signals.detect` on each new candle's prefix and `labels.run(labels.prepare(window))`, takes the
labels of the new candle, dedupes by key, then delivers. The replay does exactly this per historical candle with
the window ending at that candle: no future candle is in the window, so lookahead is impossible by construction.
Measured cost: ~15 ms per candle on 1001 bars (labels dominate) → ~60 candles/s at full speed.

## 5. Gaps found

1. Indicator columns are computed inside the MT5 fetch → extract `add_indicators` (shared).
2. Event enumeration and dedupe live in `bot.py` (impure module) → extract to `signal_engine.py` (shared).
3. No historical data service, no CSV import (outside `backfill.py`, which uploads, not loads), no cache.
4. No timezone-aware historical conversion (only the live offset).
5. No virtual broker / P&L model; `positions.py` is MT5-bound. The TSL rule itself (`labels.tsl_track`) is reusable.
6. No interactive chart (only matplotlib PNG); no "ASTRA" components exist in this repository.
7. No C5 Freeze 001 code exists in this repository; it is not touched or reproduced.
