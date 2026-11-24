# HISTORICAL REPLAY — XAUUSD M30 · session `rp-20261124-021429-0067e7`

> Indicator replay only: events are reported, no trading rule was tested, so no profit or loss is implied.

## Data
- Requested: 2026-10-01 00:00:00 → 2026-10-31 00:00:00 (server time, Europe/Athens)
- Actual: 2026-10-01 00:00:00 → 2026-10-30 23:30:00 · 980 candles · warm-up 961 candles
- Source: csv-73391cd7ff · dropped rows: 0
- Gaps in range: 22 (0 unexpected, 0 candles missing in them)
- ⚠️ warm-up has 961 candles (wanted 1000)
- ⚠️ unexpected gap 2026-09-08 00:00:00 → 2026-09-08 04:00:00 (7 candles)

## Replay
- Candles processed: 980 of 980
- Duration: 16.612 s (59.0 candles/s)
- Evaluation: per candle, the live bot's window: the 1000 candles before it plus itself (signal_engine + labels); no later candle is used

## Events by type

| Event | Count |
|---|---|
| CROSS | 23 |
| CROSS_AFTER_TREND | 15 |
| FAST_X_TREND | 18 |
| GAP PEAKED | 4 |
| GAP WIDENING | 32 |
| NARROW_ZONE_END | 9 |
| NARROW_ZONE_START | 13 |
| PARALLEL RUNNING | 23 |
| PARALLEL_TREND | 3 |
| PRE-CROSS | 37 |
| PRE-CROSS FAILED | 7 |
| PULLBACK FAILED | 17 |
| PULLBACK STARTED | 54 |
| RE-ENTRY | 37 |
| SLOW_X_TREND | 10 |
| WIDE GAP | 4 |

## Discord (replay channel)
- disabled

## Execution assumptions
- next candle open after the deciding close; bid prices; buys at ask = bid + spread; stop before target when one candle touches both; gaps through the stop fill at the open
- Costs: spread 0.3, commission 7.0/lot round turn, slippage 0.05
- Symbol spec: default XAUUSD (100 oz, 0.01 = $1/lot) (contract 100.0, tick 0.01 = 1.0 per lot)
