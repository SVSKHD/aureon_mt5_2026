# Replay Lab — measured performance

Measured on 24 Nov 2026 in the development container (4 vCPU Intel Xeon @ 2.30 GHz, no GPU, software rendering),
so these are conservative numbers. Re-measure on the trading PC with `python tests/ui_perf.py` (needs Playwright).

## Python engine (real XAUUSD M30, 1–30 Oct 2026, 980 candles, 1000-candle evaluation window)

| Run | Candles/s | Notes |
|---|---|---|
| indicators only | ~60 | 15 ms/candle: `labels.prepare` + `labels.run` on the 1001-candle window, exactly as the live bot |
| ema_cross + trade management | ~59 | the virtual broker and the trailing stop add < 1 ms/candle |
| desktop app (Qt, max speed, 408 candles) | 51–53 | same engine on a worker thread; UI batches at ≤ 60 Hz |

"Max" speed is therefore ~60 candles/s: a month of M30 in ~16 s, a year in ~3.5 min. Every candle is evaluated:
the UI batches what it draws, never what it evaluates.

## Chart (headless Chromium 153, 1600×900, the built UI, messages recorded from the engine)

1941 candles on the chart (961 warm-up + 980 revealed), 387 markers, EMA 8/32/200, ema_cross trades with SL/TP
lines.

| Candles per frame | Mean FPS | Frame p50 | Frame p95 | Frame max | Worst batch apply | Zoom frame p95 / max |
|---|---|---|---|---|---|---|
| 1 (normal playback) | 59.9 | 16.7 ms | 16.7 ms | 33.4 ms | 2.5 ms | 16.8 / 16.8 ms |
| 4 | 60.0 | 16.7 ms | 16.8 ms | 16.8 ms | 2.4 ms | 16.8 / 16.8 ms |
| 16 (max speed) | 60.0 | 16.7 ms | 16.7 ms | 16.8 ms | 8.7 ms | 16.8 / 16.8 ms |

Panning: 40 drag steps in ~1.7 s of wall time (input-driven) with no dropped frame in the zoom measurement.
One 33 ms frame in the 1-candle case (out of 978) is the only frame over budget.

## Verified in the same run (tests/ui_perf.py)

- **Incremental updates**: `setData` runs once per session (the warm-up); every revealed candle is `update()`
  (980 updates for 980 candles); 0 full redraws during playback.
- **No future candle**: the chart holds exactly warm-up + revealed candles, and its last candle is the last
  candle the engine revealed.
- **Indicator alignment**: the chart's EMA200 at the last candle equals the engine's value (difference < 1e-9).
- **No snapping while inspecting**: after scrolling back, 20 more candles are revealed and the visible range does
  not move ("Jump to latest" appears instead).
- **Attribution**: TradingView attribution logo and link present.
- **No page errors.**
