# Aureon Replay Lab — historical replay and backtests

Watch history candle by candle in a TradingView-style window while the bot's own indicator and label engines produce
their alerts, optionally test a strategy on virtual trades, and get a report. **Simulation only**: nothing in the
Replay Lab can place, close or modify a real order, post to the live alert channels or touch the live bot's state.

Audit that preceded the work: [REPLAY_AUDIT.md](REPLAY_AUDIT.md). Measurements: [REPLAY_PERFORMANCE.md](REPLAY_PERFORMANCE.md).
Examples: [examples/](examples/). Screenshot: [replay_ui.png](replay_ui.png) (real XAUUSD, October 2026, ema_cross).

## Install (Windows)

```
pip install -r requirements-replay.txt        # adds PySide6 (Qt + Qt WebEngine) to the bot's requirements
python -m replay app                          # the desktop window
python -m replay --help                       # headless replays and backtests (no PySide6 needed)
```

The UI is shipped built (`replay/app/web/`), so Node.js is **not** needed to run it. To change the UI:
`cd replay_ui && npm ci && npm run build` (React 19 + TypeScript + Lightweight Charts 5.2.1, built with Vite).
On Linux/macOS (no MetaTrader5 package) CSV and cached replays work; MT5 downloads need the Windows terminal.

## The window

Top bar: symbol, timeframe (M15/M30/H1/H4), from/to dates (server time), data source (MT5 / CSV / cache),
strategy, *Trade mgmt (sim)*, *Discord* (only when a replay webhook is configured), *Intrabar*, **Load & start**.
Playback bar: Play/Pause, Step, Restart (a new session from the first candle), Stop, speed 0.5×–60× candles/s or
Max, candle spacing, Jump to latest, markers on/off, panels, full screen, progress.
Chart: candles, EMA 8/32/200, a marker for every event (high-severity signals labelled, context labels as dots;
click one for details), virtual BUY/SELL/EXIT markers with entry/SL/TP lines, optional volume pane, a crosshair
readout (server time, UTC, IST, OHLC, EMAs). Panels (collapsible): signal details, notifications timeline,
trades, statistics. Status bar: the **historical** candle time (server + IST) next to the **replay clock** (your
own time) and the measured candles/s.

Shortcuts: `Space` play/pause · `→` step · `+`/`−` speed · `End` jump to latest · `F` full screen · `P` panels ·
`M` markers.

When you scroll back to inspect older candles the view stays where you put it (no snapping to the newest candle);
**Jump to latest** brings it back. At the end (or on Stop) the report opens with buttons for every export.

## Headless

```
python -m replay --symbol XAUUSD --tf M30 --from 2026-10-01 --to 2026-10-31 --source mt5
python -m replay --source csv --csv history.csv --csv-tz utc --from 2026-10-01 --to 2026-10-31 \
    --strategy ema_cross --protective '{"sl_mode":"atr","sl":2,"tp_mode":"r","tp":2}' --sizing '{"mode":"fixed","lots":1}'
python -m replay ... --strategy manual --params '{"trades":[{"time":"2026-10-29 09:00","side":"sell","sl":4120}]}' --trade-management
```

## Data

- **Sources.** MT5 (`copy_rates_from` / `copy_rates_range` through a read-only wrapper: it has no attribute that
  can trade), CSV (a `time_utc` column, or `time` + `--csv-tz utc|server`; `open/high/low/close`; optional
  `tick_volume`), or the cache. A CSV at a lower timeframe is aggregated upward in server time (M30 → H1/H4);
  a lower timeframe is never invented from a higher one.
- **Time.** Candle times are broker **server** time, as the live bot and the MT5 chart use; every candle also has
  its UTC time converted with the server timezone *at that date* (DST-correct across history). Reports show
  server, UTC and IST.
- **Warm-up.** `WARMUP_BARS` (1000) candles before the start, like the live bot. Fewer than 600 (3 × EMA200)
  refuses the request; fewer than 1000 is reported.
- **Validation.** Invalid rows (missing values, high/low outside the body) and duplicates are dropped and
  reported. Holes are reported as gaps: weekend, daily break, Christmas/New Year, or **unexpected** (anything
  else, with the missing candle count). A requested range the data does not cover is reported, never filled.
- **Cache.** `logs/replay/cache/<SYMBOL>_<TF>_<source>.csv.gz` + `.json` (server timezone recorded; a cache
  written for another server clock is ignored).

## How a candle is evaluated (and why there is no lookahead)

For each candle the engine builds the window the live bot holds at that candle's close (the 1000 candles before it
plus itself) and runs the **same functions** as `bot.check_ema`: `signal_engine.add_indicators`,
`labels.prepare/run`, `signal_engine.candle_events`, `signal_engine.event_key` (moved out of `signals.py` and
`bot.py` unchanged so both share them). The window ends at the candle being evaluated, so no later candle can
influence an event. Tests check this three ways: the replay produces exactly the live bot's event keys over 120
fake-MT5 candles; rewriting every future candle does not change a single past event; the chart never holds a
candle beyond the revealed one.

**Intrabar animation** builds the active candle from genuine M1 history (MT5) minute by minute; indicators are
still evaluated only on closed candles. Without M1 history the replay reveals whole candles and says so: price
movement inside a candle is never invented from OHLC.

## Discord replay notifications (off by default)

```
REPLAY_DISCORD_ENABLED=false
REPLAY_DISCORD_WEBHOOK=          # a webhook of a DEDICATED replay channel
REPLAY_DISCORD_RATE_LIMIT=5      # messages per second (Discord's own 429 Retry-After is always honoured)
REPLAY_DISCORD_QUEUE=200         # cards waiting before the oldest are folded into a digest
```

- A live alert webhook is refused (compared by URL and by webhook id).
- Cards reuse the live card (`notifier.format_event`). Every title starts with **HISTORICAL REPLAY**; the card
  shows the historical candle close (server, UTC, IST), the session id and the event number. It never pings.
- One worker posts in historical order; the replay only appends to a queue, so Discord never slows the replay.
  When the replay outruns Discord, the oldest waiting cards become a labelled digest (order kept).
- Counts: attempted / delivered (2xx only) / failed (after 3 tries) / digests. A final summary card is sent.
- Every event is written to `logs/replay/<session>/events.jsonl` whether Discord is on or not.

## Virtual trades

```
REPLAY_TRADE_MANAGEMENT_ENABLED=false     # independent of the live TRADE_MANAGEMENT_ENABLED
REPLAY_ACCOUNT_BALANCE=50000
REPLAY_EXECUTION_MODE=simulation          # the only accepted value
REPLAY_SPREAD=0.30  REPLAY_COMMISSION_PER_LOT=7.0  REPLAY_SLIPPAGE=0.05
```

- **Entries** come only from the chosen strategy. An EMA cross is not an entry unless the strategy is `ema_cross`.
- **Fill timing**: decided at a candle's close, filled at the next candle's open.
- **Prices**: candles are bid prices; buys fill at the ask (bid + spread), shorts close at the ask; slippage
  against you on market and stop fills.
- **Money**: price difference / tick size × tick value × lots, from the broker's symbol specification when MT5 is
  available (else a documented default: XAUUSD 100 oz, 0.01 = $1 per lot).
- **OHLC ambiguity (conservative fill policy)**: if one candle reaches both the stop and the target, the stop is
  assumed first and the trade is flagged *ambiguous* (counted in the report). A candle opening beyond the stop
  fills at the open; a candle opening beyond the target fills at the target.
- **Trade management OFF** (default): indicator events, chart and statistics as usual; trailing-stop steps are
  recorded as blocked and no stop moves.
- **Trade management ON**: the live stepped trailing stop (`labels.tsl_track`, the function `positions.py` uses)
  moves the virtual stop, only tighter, effective from the next candle.

## Strategies

| Name | Rule |
|---|---|
| `indicators` (default) | No trades: events and data quality only. No profit, loss or win rate is reported. |
| `ema_cross` | Enter on CROSS / CROSS_AFTER_TREND (`align_200` = only the latter), exit/reverse on the opposite cross. |
| `pullback` | Enter on RE-ENTRY in the trend direction; exit on the opposite cross (`exit_on_cross`). |
| `manual` | Explicitly defined virtual trades (`time`, `side`, optional `lots`, `sl`, `tp`). |

Shared by all of them: **protective SL/TP** (`sl_mode` atr | price | none, `tp_mode` atr | price | r | none) and
**sizing** (`fixed` 1.00 lot, `risk` % of equity at the stop, `step` lots per equity step). The frozen C5 strategy
is not part of this repository and is not reproduced or claimed.

## Report (`logs/replay/<session>/`)

`report.json`, `summary.md`, `events.csv`, `gaps.csv`, and for strategies `trades.csv`, `equity.csv`,
`monthly.csv`.
- **Data**: requested vs actual ranges, coverage and gaps, candles processed, bad ticks cleaned.
- **Events**: counts by type.
- **Discord**: attempted / delivered / failed.
- **Timing**: replay duration and candles/s.
- **Execution assumptions**: costs, symbol spec, fill policy.
- **Strategies only**: trades, win rate, net, profit factor, expectancy, max drawdown (amount and %), monthly
  table, P/L distribution, signal-to-trade attribution, ambiguous bars, stop moves and blocked steps.

## Safety (enforced by tests)

- `replay/` never imports `bot`, `trader`, `alerts`, `positions`, `weekend` or `monitor`, and contains no order
  call.
- MT5 is reached only through `ReadOnlyMT5`.
- No live state file is written. Sessions share no state, so two sessions run interleaved give identical results.
- `REPLAY_EXECUTION_MODE` must be `simulation`; the live notification mode and the live trade-management switch
  are never read or changed.

## Tests

`python -m unittest discover -s tests` covers:
- `test_replay.py`: data, parity with the live bot, no lookahead, timestamps, controller, isolation, Discord,
  virtual trades, strategies, reports.
- `test_replay_app.py`: the app host, plus a real offscreen Qt window when PySide6 is installed.

`python tests/ui_perf.py` needs Playwright + Chromium. It is the chart acceptance and performance run.

## Rollback

The live bot is unchanged apart from `signal_engine.py`: three functions moved there verbatim from `signals.py`
and `bot.py`, which now call them; the whole existing suite passes unchanged. To remove the Replay Lab, delete
`replay/`, `replay_ui/`, `requirements-replay.txt`, `tests/test_replay*.py`, `tests/ui_perf.py`, the
`docs/REPLAY_*` files and the `REPLAY_*` block in `config.py`, or revert the merge commit (`git revert -m 1
<merge>`). Replay output lives only in `logs/replay/` and can be deleted at any time.
