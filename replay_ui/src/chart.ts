// The replay chart: TradingView Lightweight Charts, driven imperatively (no React re-render per candle).
// Incremental: every revealed candle is series.update(); markers are set once per batch; SL/TP are price lines.
// Only candles the Python engine has revealed are ever given to the chart (no future candle, no future EMA).
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Bar, ReplayEvent, Trade } from "./types";

export const THEME = {
  bg: "#131722", grid: "#1e222d", text: "#d1d4dc", border: "#2a2e39", up: "#26a69a", down: "#ef5350",
  ema8: "#f5c542", ema32: "#42a5f5", ema200: "#e040fb", sl: "#ef5350", tp: "#26a69a", entry: "#b2b5be",
};

// chart time = the broker SERVER wall clock (the axis reads like the MT5 chart); readouts also show UTC and IST
export const toTime = (serverIso: string) => (Date.parse(serverIso + "Z") / 1000) as UTCTimestamp;

const BULL = new Set(["BULL", "LONG"]);

function markerFor(e: ReplayEvent): SeriesMarker<Time> {
  const t = toTime(e.bar_time);
  if (e.kind === "virtual") {
    const open = e.type === "VIRTUAL_OPEN";
    const long = e.dir === "LONG";
    if (e.type === "VIRTUAL_SL_MOVED" || e.type === "VIRTUAL_SL_BLOCKED")
      return { time: t, position: "inBar", shape: "square", color: e.type === "VIRTUAL_SL_MOVED" ? "#ffb74d" : "#787b86", size: 0.6, text: e.type === "VIRTUAL_SL_MOVED" ? "SL" : "SL✕", id: String(e.seq) };
    return {
      time: t, id: String(e.seq), size: 1.4, color: open ? (long ? THEME.up : THEME.down) : "#b2b5be",
      position: open === long ? "belowBar" : "aboveBar", shape: open ? (long ? "arrowUp" : "arrowDown") : "circle",
      text: open ? `${long ? "BUY" : "SELL"} #${(e.details as { trade?: Trade }).trade?.id ?? ""}` : `EXIT #${(e.details as { trade?: Trade }).trade?.id ?? ""}`,
    };
  }
  const up = BULL.has(e.dir);
  const strong = e.severity === "high" || e.severity === "critical";
  const short: Record<string, string> = {
    CROSS: "X", CROSS_AFTER_TREND: "X✔", FAST_X_TREND: "8×200", SLOW_X_TREND: "32×200", PARALLEL_TREND: "‖200",
    "PRE-CROSS": "pre", "PRE-CROSS FAILED": "pre✕", "GAP PEAKED": "peak", "PULLBACK FAILED": "pb✕",
    "GAP WIDENING": "wid", "PARALLEL RUNNING": "par", "WIDE GAP": "wide", "PULLBACK STARTED": "pb", "RE-ENTRY": "re",
    NARROW_ZONE_START: "chop", NARROW_ZONE_END: "chop✓",
  };
  return {
    time: t, id: String(e.seq), position: up ? "belowBar" : "aboveBar", shape: strong ? (up ? "arrowUp" : "arrowDown") : "circle",
    color: e.dir === "UNCLEAR" ? "#f1c40f" : up ? THEME.up : THEME.down, size: strong ? 1 : 0.4,
    text: strong ? short[e.type] ?? e.type : undefined,      // context labels: a dot (details on click / timeline)
  };
}

export interface Readout {
  bar: Bar | null;
}

export class ReplayChart {
  chart: IChartApi;
  candles: ISeriesApi<"Candlestick">;
  ema: Record<"ema8" | "ema32" | "ema200", ISeriesApi<"Line">>;
  volume: ISeriesApi<"Histogram"> | null = null;
  markers: ISeriesMarkersPluginApi<Time>;
  private allMarkers: SeriesMarker<Time>[] = [];
  private bars = new Map<number, Bar>();          // chart time -> bar (crosshair readout)
  private lines = new Map<string, IPriceLine>();  // trade id:kind -> price line
  private lastTime = 0;
  private showSignals = true;
  private signalMarkers = new Set<SeriesMarker<Time>>();
  following = true;
  onFollow: (f: boolean) => void = () => {};
  onCrosshair: (r: Readout) => void = () => {};
  onClickEvent: (seq: number) => void = () => {};
  stats = { updates: 0, batches: 0, lastBatchMs: 0, maxBatchMs: 0, resets: 0 };

  constructor(el: HTMLElement) {
    this.chart = createChart(el, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: THEME.bg }, textColor: THEME.text, fontSize: 12, attributionLogo: true,
        panes: { separatorColor: THEME.border, enableResize: true } },
      grid: { vertLines: { color: THEME.grid }, horzLines: { color: THEME.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: THEME.border },
      timeScale: { borderColor: THEME.border, timeVisible: true, secondsVisible: false, rightOffset: 6, barSpacing: 8,
        shiftVisibleRangeOnNewBar: true },
      localization: { locale: "en-GB" },
    });
    this.candles = this.chart.addSeries(CandlestickSeries, {
      upColor: THEME.up, downColor: THEME.down, borderVisible: false, wickUpColor: THEME.up, wickDownColor: THEME.down,
    });
    const line = (color: string, width: 1 | 2) => this.chart.addSeries(LineSeries, {
      color, lineWidth: width, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    });
    this.ema = { ema8: line(THEME.ema8, 1), ema32: line(THEME.ema32, 1), ema200: line(THEME.ema200, 2) };
    this.markers = createSeriesMarkers(this.candles, []);
    this.chart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
      if (!r) return;
      const last = this.candles.data().length - 1;
      const following = r.to >= last - 0.5;            // the user is looking at the newest candle
      if (following !== this.following) {
        this.following = following;
        this.onFollow(following);
      }
    });
    this.chart.subscribeCrosshairMove((p) => {
      const t = p.time as number | undefined;
      this.onCrosshair({ bar: t !== undefined ? this.bars.get(t) ?? null : null });
    });
    this.chart.subscribeClick((p) => {
      const id = p.hoveredObjectId;
      if (id !== undefined && id !== null) this.onClickEvent(Number(id));
    });
  }

  reset(warmup: Bar[]) {
    this.stats.resets++;                                // the ONLY place setData is used (a new session)
    this.bars.clear();
    this.allMarkers = [];
    this.signalMarkers = new Set();
    this.markers.setMarkers([]);
    for (const l of this.lines.values()) this.candles.removePriceLine(l);
    this.lines.clear();
    const hasVol = warmup.some((b) => b.volume !== undefined);
    if (hasVol && !this.volume) {
      this.volume = this.chart.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false }, 1);
      this.chart.panes()[1]?.setHeight(90);
    }
    this.candles.setData(warmup.map((b) => this.candle(b)));
    for (const k of ["ema8", "ema32", "ema200"] as const)
      this.ema[k].setData(warmup.filter((b) => b[k] !== undefined).map((b) => ({ time: toTime(b.time), value: b[k]! })));
    this.volume?.setData(warmup.filter((b) => b.volume !== undefined).map((b) => this.vol(b)));
    for (const b of warmup) this.bars.set(toTime(b.time), b);
    this.lastTime = warmup.length ? toTime(warmup[warmup.length - 1].time) : 0;
    this.chart.timeScale().scrollToRealTime();
  }

  private candle(b: Bar) {
    return { time: toTime(b.time), open: b.open, high: b.high, low: b.low, close: b.close };
  }
  private vol(b: Bar) {
    return { time: toTime(b.time), value: b.volume ?? 0, color: b.close >= b.open ? "#26a69a55" : "#ef535055" };
  }

  /** Revealed closed candles (oldest first) plus, optionally, the forming candle built from genuine M1 data. */
  apply(bars: Bar[], events: ReplayEvent[], forming?: Bar | null) {
    const t0 = performance.now();
    for (const b of bars) {
      const t = toTime(b.time);
      if (t < this.lastTime) continue;                   // never rewrite history
      this.candles.update(this.candle(b));
      for (const k of ["ema8", "ema32", "ema200"] as const) if (b[k] !== undefined) this.ema[k].update({ time: t, value: b[k]! });
      if (this.volume && b.volume !== undefined) this.volume.update(this.vol(b));
      this.bars.set(t, b);
      this.lastTime = t;
      this.stats.updates++;
    }
    if (forming && toTime(forming.time) >= this.lastTime) {
      this.candles.update(this.candle(forming));        // the same time is updated in place until it closes
      this.lastTime = toTime(forming.time);
    }
    if (events.length) {
      for (const e of events) {
        const m = markerFor(e);
        this.allMarkers.push(m);
        if (e.kind === "signal") this.signalMarkers.add(m);
        this.tradeLines(e);
      }
      this.markers.setMarkers(this.showSignals ? this.allMarkers : this.allMarkers.filter((m) => !this.signalMarkers.has(m)));
    }
    const dt = performance.now() - t0;
    this.stats.batches++;
    this.stats.lastBatchMs = dt;
    this.stats.maxBatchMs = Math.max(this.stats.maxBatchMs, dt);
  }

  private tradeLines(e: ReplayEvent) {
    const d = e.details as { trade?: Trade; trade_id?: number; new?: number };
    const tr = d.trade;
    const id = tr?.id ?? d.trade_id;
    const set = (kind: "SL" | "TP" | "IN", price: number | null | undefined, color: string, style: LineStyle) => {
      const k = `${id}:${kind}`;
      const old = this.lines.get(k);
      if (price === null || price === undefined) {
        if (old) { this.candles.removePriceLine(old); this.lines.delete(k); }
        return;
      }
      if (old) old.applyOptions({ price });
      else this.lines.set(k, this.candles.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title: `${kind} #${id}` }));
    };
    if (e.type === "VIRTUAL_OPEN" && tr) {
      set("IN", tr.entry_price, THEME.entry, LineStyle.Dotted);
      set("SL", tr.sl, THEME.sl, LineStyle.Dashed);
      set("TP", tr.tp, THEME.tp, LineStyle.Dashed);
    } else if (e.type === "VIRTUAL_SL_MOVED") {
      set("SL", d.new, THEME.sl, LineStyle.Dashed);
    } else if (e.type === "VIRTUAL_CLOSE" && tr) {
      set("IN", null, "", LineStyle.Solid); set("SL", null, "", LineStyle.Solid); set("TP", null, "", LineStyle.Solid);
    }
  }

  setSignalsVisible(v: boolean) {
    this.showSignals = v;
    this.markers.setMarkers(v ? this.allMarkers : this.allMarkers.filter((m) => !this.signalMarkers.has(m)));
  }
  setSpacing(px: number) {
    this.chart.timeScale().applyOptions({ barSpacing: px });
  }
  jumpToLatest() {
    this.chart.timeScale().scrollToRealTime();
  }
  jumpTo(serverIso: string) {
    const t = toTime(serverIso);
    const idx = this.candles.data().findIndex((d) => (d.time as number) >= t);
    if (idx < 0) return;
    const r = this.chart.timeScale().getVisibleLogicalRange();
    const half = r ? (r.to - r.from) / 2 : 60;
    this.chart.timeScale().setVisibleLogicalRange({ from: idx - half, to: idx + half });
  }
  destroy() {
    this.chart.remove();
  }
}
