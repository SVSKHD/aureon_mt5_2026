"""Chart snapshot: candles + EMA 8/32/200 as a PNG. Pure drawing - no MT5, no network.
render() returns the file path, or None if drawing failed (an alert is never held back by a chart)."""
import glob, logging, os, time
import config as C

log = logging.getLogger("chart")
UP, DOWN, BG, GRID, TEXT = "#26a69a", "#ef5350", "#131722", "#2a2e39", "#d1d4dc"
EMA_COLORS = {C.EMA_FAST: "#f5c542", C.EMA_SLOW: "#42a5f5", C.EMA_TREND: "#e040fb"}


def render(df, tf: str, title: str, to_ist, mark_last: str | None = None, price_line: float | None = None,
           symbol: str | None = None, bars: int | None = None, marks: list | None = None,
           lines: list | None = None) -> str | None:
    """df: bars with time/open/high/low/close/ema columns (oldest first). to_ist: server datetime -> IST datetime.
    mark_last: "BULL" / "BEAR" draws an arrow on the last candle. price_line: a dashed level (live price).
    bars: candles shown (default CHART_BARS). marks: [(bar_time, "BULL"|"BEAR")] arrows (week chart crosses).
    lines: [(price, label)] extra dashed levels (entry, SL)."""
    try:
        import matplotlib
        matplotlib.use("Agg")                                     # no window, safe in a background process
        import matplotlib.pyplot as plt
        from datetime import timedelta

        d = df.tail(bars or C.CHART_BARS).reset_index(drop=True)
        x = range(len(d))
        fig, ax = plt.subplots(figsize=(10, 5.4), dpi=100)
        fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
        colors = [UP if c >= o else DOWN for o, c in zip(d["open"], d["close"])]
        ax.vlines(x, d["low"], d["high"], colors=colors, linewidth=1)
        body_lo = d[["open", "close"]].min(axis=1)
        body_h = (d["open"] - d["close"]).abs().clip(lower=(d["high"].max() - d["low"].min()) * 0.0008)
        ax.bar(x, body_h, bottom=body_lo, width=0.7, color=colors, linewidth=0)
        for p, col in EMA_COLORS.items():
            ax.plot(x, d[f"ema{p}"], color=col, linewidth=1.4, label=f"EMA {p}")

        last = len(d) - 1
        span = d["high"].max() - d["low"].min()
        if mark_last in ("BULL", "BEAR"):
            up = mark_last == "BULL"
            y = d["low"].iloc[-1] - span * 0.04 if up else d["high"].iloc[-1] + span * 0.04
            ax.scatter([last], [y], marker="^" if up else "v", s=160, color=UP if up else DOWN, zorder=5)
        times = list(d["time"])
        for t, direction in marks or []:
            if t in times:
                k = times.index(t); up = direction == "BULL"
                y = d["low"].iloc[k] - span * 0.03 if up else d["high"].iloc[k] + span * 0.03
                ax.scatter([k], [y], marker="^" if up else "v", s=90, color=UP if up else DOWN, zorder=5)
        for level, label in lines or []:
            ax.axhline(level, color="#ffb74d", linewidth=0.8, linestyle=":", alpha=0.9)
            ax.text(last + 0.6, level, f"{label} {level:.2f}", color="#ffb74d", fontsize=8, va="center")
        if price_line:
            ax.axhline(price_line, color=TEXT, linewidth=0.8, linestyle="--", alpha=0.7)
            ax.text(last + 0.6, price_line, f"{price_line:.2f}", color=TEXT, fontsize=9, va="center")

        bar_min = timedelta(minutes=C.BAR_MINUTES[tf])
        step = max(1, len(d) // 8)
        ticks = list(range(len(d) - 1, -1, -step))[::-1]
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{to_ist(d['time'].iloc[i].to_pydatetime() + bar_min):%d %b\n%H:%M}" for i in ticks],
                           color=TEXT, fontsize=8)
        ax.set_xlim(-1, len(d) + 4)
        ax.yaxis.tick_right(); ax.tick_params(colors=TEXT, labelsize=9, length=0)
        ax.grid(color=GRID, linewidth=0.6)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.set_title(title, color=TEXT, fontsize=12, loc="left", pad=10)
        ax.text(0.995, 1.02, "candle close times in IST", transform=ax.transAxes, color=TEXT, alpha=0.6,
                fontsize=8, ha="right")
        ax.legend(loc="upper left", frameon=False, labelcolor=TEXT, fontsize=9, ncol=3)

        os.makedirs(C.CHART_DIR, exist_ok=True)
        path = os.path.join(C.CHART_DIR, f"{symbol or C.SYMBOL}-{tf}-{time.time_ns()}.png")
        fig.savefig(path, facecolor=BG, bbox_inches="tight")
        plt.close(fig)
        return path
    except Exception:
        log.exception("chart render failed")
        return None


def prune():
    """Delete chart files older than CHART_KEEP_DAYS."""
    cutoff = time.time() - C.CHART_KEEP_DAYS * 86400
    for p in glob.glob(os.path.join(C.CHART_DIR, "*.png")):
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass
