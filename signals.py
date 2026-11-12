"""Fetch closed bars from MT5, compute EMAs, detect events, convert server time -> IST."""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import MetaTrader5 as mt5
import pandas as pd
import config as C

TF = {"M1": mt5.TIMEFRAME_M1, "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
      "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4}
IST = timezone(timedelta(hours=5, minutes=30))
F, S, T = f"ema{C.EMA_FAST}", f"ema{C.EMA_SLOW}", f"ema{C.EMA_TREND}"
# ----------------------------------------------------------------------------- clock
# MT5 hands out epoch seconds. Most brokers stamp them in SERVER wall time (e.g. UTC+2), although the
# MetaQuotes docs call them UTC. We do not assume either: the offset starts from config and is then
# MEASURED from the live feed (observe_clock) - a tick whose time is still changing is provably fresh,
# so (tick time - real UTC) is the true offset. If the feed disagrees with config, the feed wins.
_server_offset = timedelta(0)   # MT5 stamp - real UTC
_offset_source = "unset"        # "pinned" | tz name | "measured" | "unset"
_live = {"msc": None, "seen": 0.0, "mismatch": 0, "cand": None}
_clock_note = None              # text for the bot to deliver once when the feed overrode config
_log = logging.getLogger("signals")


def utc_now() -> datetime:
    """The only place the wall clock is read (tests replace it)."""
    return datetime.now(timezone.utc)


def _configured_offset():
    """(offset, source) from config, or (None, None)."""
    if C.SERVER_UTC_OFFSET_HOURS is not None:
        return timedelta(hours=C.SERVER_UTC_OFFSET_HOURS), "pinned"
    if C.SERVER_TZ:
        try:
            return utc_now().astimezone(ZoneInfo(C.SERVER_TZ)).utcoffset(), C.SERVER_TZ
        except Exception:
            _log.error("SERVER_TZ %r not found (pip install tzdata)", C.SERVER_TZ)
    return None, None


def sync_server_offset() -> timedelta:
    """Called on every (re)connect: start from config. The live feed corrects it within ~30 s if it is wrong.
    The measurement in progress is kept, so reconnecting can never stop the clock from converging."""
    global _server_offset, _offset_source
    off, src = _configured_offset()
    if off is not None and _offset_source != "measured":
        _server_offset, _offset_source = off, src
    return _server_offset


def observe_clock(tick) -> None:
    """Measure the offset from a tick that is provably live: its time changed since our previous look,
    and that look was under a minute ago. Three disagreeing measurements in a row replace the offset."""
    global _server_offset, _offset_source, _clock_note
    now = utc_now().timestamp()
    msc = getattr(tick, "time_msc", None) or tick.time * 1000
    prev_msc, prev_seen = _live["msc"], _live["seen"]
    _live["msc"], _live["seen"] = msc, now
    if prev_msc is None or msc == prev_msc or now - prev_seen > 60:
        return                                                    # not provably live -> no measurement
    measured = timedelta(minutes=round((tick.time - now) / 1800) * 30)
    if measured == _server_offset:
        _live["mismatch"] = 0
        if _offset_source == "measured":                          # config agrees again (e.g. after a DST change)
            off, src = _configured_offset()
            if off == measured:
                _offset_source = src
        return
    if measured != _live["cand"]:                                 # must be the SAME value three looks running
        _live["cand"], _live["mismatch"] = measured, 0
    _live["mismatch"] += 1
    if _live["mismatch"] >= 3:
        old = offset_text()
        _server_offset, _offset_source, _live["mismatch"] = measured, "measured", 0
        _clock_note = (f"Live feed shows the MT5 clock is {offset_text()}, config said {old}. Using the feed. "
                       f"Set SERVER_TZ / SERVER_UTC_OFFSET_HOURS to match, or check the PC clock.")
        _log.warning(_clock_note)


def pop_clock_note():
    global _clock_note
    note, _clock_note = _clock_note, None
    return note


def offset_text() -> str:
    return f"UTC{_server_offset.total_seconds() / 3600:+g} ({_offset_source})"


def _mt5_dt(dt: datetime) -> datetime:
    """MT5's Python API shifts NAIVE datetimes by the PC's local timezone. Pass them tz-aware so the
    MT5-stamped value we hold is used as-is."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def stamp_to_dt(epoch) -> datetime:
    """MT5 epoch -> naive datetime in MT5 (server) time."""
    return datetime.fromtimestamp(int(epoch), timezone.utc).replace(tzinfo=None)


def server_now() -> datetime:
    return utc_now().replace(tzinfo=None) + _server_offset


def to_ist(server_dt: datetime) -> datetime:
    return (server_dt - _server_offset).replace(tzinfo=timezone.utc).astimezone(IST)


def fmt_times(server_dt: datetime) -> str:
    return f"IST {to_ist(server_dt):%d %b %H:%M} | MT5 server {server_dt:%d %b %H:%M}"


def health() -> str | None:
    """None if healthy, else a reason. Checks terminal, symbol and tick freshness."""
    ti = mt5.terminal_info()
    if ti is None:
        return "terminal not connected"
    if not getattr(ti, "connected", True):
        return "MT5 terminal has no connection to the broker"
    info = mt5.symbol_info(C.SYMBOL)
    if info is None or not info.visible:
        return f"symbol {C.SYMBOL} not available"
    tick = mt5.symbol_info_tick(C.SYMBOL)
    if tick is None:
        return "no tick"
    observe_clock(tick)
    age = (server_now() - stamp_to_dt(tick.time)).total_seconds()
    if age < -120:                                              # tick "from the future": our server clock is behind
        return f"clock mismatch: tick is {-age / 60:.0f} min ahead of the bot's MT5 clock ({offset_text()})"
    return f"stale tick ({age:.0f}s)" if age > 600 else None


def live_price() -> float | None:
    tick = mt5.symbol_info_tick(C.SYMBOL)
    return tick.bid if tick else None


def price_range_since(since_server: datetime | None) -> dict | None:
    """M1 lows/highs from `since_server` (server time) to now, plus live bid.
    Returns {"low","high","last","gap"} where gap is None if coverage is complete, else a text describing
    the uncovered period. Returns None only if there is no tick at all."""
    tick = mt5.symbol_info_tick(C.SYMBOL)
    if tick is None:
        return None
    now = server_now()
    lo = hi = tick.bid
    want = since_server or (now - timedelta(minutes=2))
    rates = mt5.copy_rates_range(C.SYMBOL, TF["M1"], _mt5_dt(want - timedelta(minutes=1)), _mt5_dt(now + timedelta(hours=1)))
    gap = None
    if rates is None or len(rates) == 0:
        if (now - want) > timedelta(minutes=3):
            gap = f"no M1 history returned for {fmt_times(want)} → now"
    else:
        lo, hi = min(lo, float(rates["low"].min())), max(hi, float(rates["high"].max()))
        first, last_t = stamp_to_dt(rates["time"][0]), stamp_to_dt(rates["time"][-1])
        # tolerate weekend/holiday emptiness: only flag if the first bar is late by > 5 min AND it's not a market-closed stretch
        if first - want > timedelta(minutes=5) and (first - want) < timedelta(days=2):
            gap = f"M1 history starts at {fmt_times(first)}, requested from {fmt_times(want)}"
        elif first - want >= timedelta(days=2):
            gap = f"M1 history unavailable before {fmt_times(first)} (requested from {fmt_times(want)})"
        if now - last_t > timedelta(minutes=5) and health() is None:
            gap = (gap + "; " if gap else "") + f"M1 history ends at {fmt_times(last_t)}"
    m1 = [] if rates is None else [(int(t), float(l), float(h)) for t, l, h in zip(rates["time"], rates["low"], rates["high"])]
    return {"low": lo, "high": hi, "last": tick.bid, "gap": gap, "m1": m1}


def bid_range_since(since_server: datetime) -> tuple[float, float] | None:
    """Exact (low, high) of the bid from ticks since `since_server`, or None if ticks are unavailable.
    Used for the first minutes of a new price alert, where an M1 bar would also contain older prices."""
    try:
        ticks = mt5.copy_ticks_range(C.SYMBOL, _mt5_dt(since_server), _mt5_dt(server_now() + timedelta(hours=1)),
                                     mt5.COPY_TICKS_ALL)
    except Exception:
        return None
    if ticks is None or len(ticks) == 0:
        return None
    bids = ticks["bid"][ticks["bid"] > 0]
    return (float(bids.min()), float(bids.max())) if len(bids) else None


def fetch(tf: str, since: datetime | None = None) -> pd.DataFrame | None:
    """Closed bars. Without `since`: last BARS bars. With `since` (checkpoint, server time): from
    since - WARMUP_BARS to now, so the whole outage is covered with a stable EMA200 at its start."""
    bar_min = C.BAR_MINUTES[tf]
    clamped_from = None
    if since is None:
        rates = mt5.copy_rates_from_pos(C.SYMBOL, TF[tf], 1, C.BARS)        # start_pos=1 -> closed only
    else:
        now = server_now()
        floor = now - timedelta(minutes=bar_min * C.MAX_REPLAY_BARS)
        rng_start = since
        if since < floor:                                               # outage longer than the sanity cap
            clamped_from, rng_start = since, floor
        # warm-up by BAR COUNT (weekend gaps don't matter), then everything after the checkpoint by range
        before = mt5.copy_rates_from(C.SYMBOL, TF[tf], _mt5_dt(rng_start), C.WARMUP_BARS)
        after = mt5.copy_rates_range(C.SYMBOL, TF[tf], _mt5_dt(rng_start), _mt5_dt(now + timedelta(hours=1)))
        parts = [r for r in (before, after) if r is not None and len(r)]
        rates = None
        if parts:
            import numpy as np
            rates = np.concatenate(parts)
            rates = rates[np.unique(rates["time"], return_index=True)[1]]  # dedupe + sort by time
            cur_open = now.replace(second=0, microsecond=0)
            cur_open -= timedelta(minutes=cur_open.minute % bar_min)
            rates = rates[rates["time"] < cur_open.replace(tzinfo=timezone.utc).timestamp()]   # drop forming bar
    if rates is None or len(rates) < C.EMA_TREND + 10:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")               # naive = MT5 server time
    df.attrs["clamped_from"] = clamped_from
    for p in (C.EMA_FAST, C.EMA_SLOW, C.EMA_TREND):
        df[f"ema{p}"] = df["close"].ewm(span=p, adjust=False).mean()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    return df


def ema_status(df: pd.DataFrame, price: float) -> str:
    r = df.iloc[-1]
    side = lambda v: "↑" if price > v else "↓"                 # price relative to each EMA
    trend = "▲ BULL" if r[F] > r[S] else "▼ BEAR"
    gap = abs(r[F] - r[S])
    return (f"{trend} · {C.EMA_FAST}/{C.EMA_SLOW} gap {gap:.2f} · price {side(r[F])}{C.EMA_FAST} "
            f"{side(r[S])}{C.EMA_SLOW} {side(r[T])}{C.EMA_TREND} · EMA{C.EMA_TREND} {r[T]:.0f}")


def _cross_dir(a_prev, b_prev, a_now, b_now):
    if a_prev <= b_prev and a_now > b_now:
        return "BULL"
    if a_prev >= b_prev and a_now < b_now:
        return "BEAR"
    return None


def _last_cross_vs_trend(df, col, window):
    """Most recent crossing of `col` over EMA_TREND within the last `window` bars, or None."""
    seg = df.iloc[-(window + 1):]
    for i in range(len(seg) - 1, 0, -1):                      # newest first
        d = _cross_dir(seg[col].iloc[i - 1], seg[T].iloc[i - 1], seg[col].iloc[i], seg[T].iloc[i])
        if d:
            return d
    return None


def last_8_32_cross_time(df) -> str:
    """Episode id = time of the last fast/slow cross (or the first bar if none in history)."""
    sign = (df[F] > df[S]).astype(int)
    idx = sign.diff().fillna(0).ne(0)
    idx = idx[idx].index
    return str(df.loc[idx[-1], "time"] if len(idx) else df["time"].iloc[0])


def detect(df: pd.DataFrame) -> list[dict]:
    """Events on the LAST bar of df. Pass a slice to evaluate an older bar (replay evaluates everything)."""
    now, prev = df.iloc[-1], df.iloc[-2]
    events = []

    d = _cross_dir(prev[F], prev[S], now[F], now[S])
    if d:
        aligned = (now[F] > now[T] and now[S] > now[T]) if d == "BULL" else \
                  (now[F] < now[T] and now[S] < now[T])
        events.append({"type": "CROSS_AFTER_TREND" if aligned else "CROSS", "dir": d, "price": now["close"]})

    # Individual fast/200 and slow/200 crossings on this bar
    xf = _cross_dir(prev[F], prev[T], now[F], now[T])
    xs = _cross_dir(prev[S], prev[T], now[S], now[T])
    if C.ALERT_SINGLE_TREND_CROSS:
        if xf:
            events.append({"type": "FAST_X_TREND", "dir": xf, "price": now["close"]})
        if xs:
            events.append({"type": "SLOW_X_TREND", "dir": xs, "price": now["close"]})

    # Parallel: both crossed EMA_TREND the same way within the window, one of them on THIS bar,
    # and both still sit on that side now (so a cross-back inside the window cannot pair up).
    d_f = _last_cross_vs_trend(df, F, C.PARALLEL_200_WINDOW)
    d_s = _last_cross_vs_trend(df, S, C.PARALLEL_200_WINDOW)
    if (xf or xs) and d_f and d_f == d_s:
        above = now[F] > now[T] and now[S] > now[T]
        below = now[F] < now[T] and now[S] < now[T]
        if (d_f == "BULL" and above) or (d_f == "BEAR" and below):
            events.append({"type": "PARALLEL_TREND", "dir": d_f, "price": now["close"]})

    # Narrow zone (chop): fast/slow glued together for NARROW_MIN_BARS bars; fires once at start, once at exit.
    gap_all = (df[F] - df[S]).abs()
    tight = gap_all <= C.NARROW_ATR_FRAC * df["atr"]
    n = C.NARROW_MIN_BARS
    if len(tight) > n and tight.iloc[-n:].all() and not tight.iloc[-n - 1]:
        events.append({"type": "NARROW_ZONE_START", "dir": "BULL" if now[F] >= now[S] else "BEAR",
                       "price": now["close"], "gap": gap_all.iloc[-1]})
    # Zone end: now loose (> EXIT), and since the last loose bar there was a tight run of >= n bars.
    loose = gap_all > C.NARROW_EXIT_FRAC * df["atr"]
    if loose.iloc[-1]:
        seg = []                                                  # tight flags since last loose bar, oldest first
        for t, l in zip(tight.iloc[-2::-1], loose.iloc[-2::-1]):
            if l:
                break
            seg.insert(0, bool(t))
        start, cur = None, 0
        for i, t in enumerate(seg):
            cur = cur + 1 if t else 0
            if cur == n:
                start = i - n + 1                                 # first bar of the (last) tight run
        if start is not None:
            zone = df.iloc[-(len(seg) - start + 1):-1]
            z_hi, z_lo = float(zone["high"].max()), float(zone["low"].min())
            ema_dir = "BULL" if now[F] > now[S] else "BEAR"
            if ema_dir == "BULL" and now["close"] > z_hi and now["close"] > now[F]:
                d_end = "BULL"
            elif ema_dir == "BEAR" and now["close"] < z_lo and now["close"] < now[F]:
                d_end = "BEAR"
            else:
                d_end = "UNCLEAR"                                  # EMAs lean one way, price hasn't left the box
            events.append({"type": "NARROW_ZONE_END", "dir": d_end, "price": now["close"],
                           "bars": len(seg) - start, "zone_hi": z_hi, "zone_lo": z_lo, "ema_dir": ema_dir})

    # Pre-cross: gap shrinking PRE_CROSS_BARS bars in a row and tight. Once per episode (dedupe by episode key).
    thr = C.PRE_CROSS_ATR_FRAC * df["atr"]
    pre_cond = (gap_all <= thr)
    for k in range(1, C.PRE_CROSS_BARS + 1):
        pre_cond &= gap_all < gap_all.shift(k)
    pre_cond = pre_cond.fillna(False)
    if not d and pre_cond.iloc[-1]:
        events.append({"type": "PRE_CROSS", "dir": "BULL" if now[F] < now[S] else "BEAR",
                       "price": now["close"], "episode": last_8_32_cross_time(df)})

    # Pre-cross invalidated: a pre-cross condition fired earlier in THIS episode, no cross happened,
    # and the gap has now widened 2 bars in a row to > 2x the pre-cross threshold. Once per episode.
    if not d:
        ep_start = pd.Timestamp(last_8_32_cross_time(df))
        in_ep = df["time"] > ep_start
        had_pre = bool(pre_cond[in_ep].iloc[:-1].any())
        widening = gap_all.iloc[-1] > gap_all.iloc[-2] > gap_all.iloc[-3] and gap_all.iloc[-1] > 2 * thr.iloc[-1]
        if had_pre and widening and not pre_cond.iloc[-1]:
            events.append({"type": "PRE_CROSS_FAILED", "dir": "BULL" if now[F] > now[S] else "BEAR",
                           "price": now["close"], "episode": last_8_32_cross_time(df),
                           "gap": float(gap_all.iloc[-1])})
    return events
