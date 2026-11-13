"""Label engine: the life of an EMA 8/32 trend, as labelled events on CLOSED candles.

Pure functions over a bar table - no MT5, no network, no files - so the exact same code runs live, after a
restart (state is rebuilt by replaying recent candles) and in the tests.

    prepare(df, cfg)            -> bars with bad ticks cleaned, Wilder ATR(14), EMA 8/32/200
    run(bars, cfg)              -> Result(events, state): every label on every bar, plus the state after the last bar
    tsl_steps(bars, start, side, buffer_at)  -> stepped trailing-stop levels for a position opened at bar `start`
    status_line(state)          -> the "present market status" line every alert carries

Definitions (spec 5.1): gap = EMA8 - EMA32, side = +1 long / -1 short, g = side * gap, ga = g / ATR14.
signals.detect() still produces the cross and EMA200 events; this module owns everything after a cross.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

log = logging.getLogger("labels")

# event names (stable: they are dedupe keys, record labels and alert titles)
CROSS, TREND_FINISHED = "CROSS", "TREND FINISHED"
GAP_WIDENING, PARALLEL, WIDE_GAP, GAP_PEAKED = "GAP WIDENING", "PARALLEL RUNNING", "WIDE GAP", "GAP PEAKED"
PULLBACK, RE_ENTRY, PULLBACK_FAILED = "PULLBACK STARTED", "RE-ENTRY", "PULLBACK FAILED"
PRE_CROSS, PRE_CROSS_FAILED = "PRE-CROSS", "PRE-CROSS FAILED"
LESSER_GAP = "LESSER GAP"
STEP = "TSL STEP"                    # internal: a stepped-stop trigger (positions.py decides what to do with it)

DEFAULTS = {
    "ema_fast": 8, "ema_slow": 32, "ema_trend": 200, "atr_period": 14,
    "badtick_low_atr": 2.0, "badtick_high_atr": 2.5,
    "lesser_gap_atr": 1.0,
    # Counting, as the golden data fixes it: "risen 4 candles in a row" = 4 candles each above the one before
    # them (3 rises, e.g. 07:00 < 07:30 < 08:00 < 08:30 on 30 Oct 2026); "narrows 2 candles in a row" = 2 candles
    # each below the one before (2 falls: GAP PEAKED 20:30, 21:00 on 30 Oct 2026). Values below are the counts of
    # rises / falls.
    "widening_atr": 1.0, "widening_rises": 3, "widening_rearm_falls": 2,
    "parallel_atr": 0.8, "parallel_bars": 6, "parallel_band": 0.20, "parallel_rearm_band": 0.40,
    "wide_atr": 2.5, "wide_rearm_atr": 1.5,
    "peak_falls": 2, "peak_rearm_rises": 3,
    "pullback_off_extreme_atr": 0.5, "pullback_min_atr": 0.6, "pullback_fail_atr": 0.25,
    # pre-cross pairs: (armed at >=, fires at <=, falls in a row, failed at >=), distances in ATR
    "pre_8_32": (0.8, 0.35, 3, 0.8),
    "pre_8_200": (1.0, 0.5, 3, 1.0),
    "pre_32_200": (1.0, 0.35, 3, 1.0),
}
PAIRS = (("8/32", "ema_f", "ema_s", "pre_8_32"),
         ("8/200", "ema_f", "ema_t", "pre_8_200"),
         ("32/200", "ema_s", "ema_t", "pre_32_200"))


def config(**over) -> dict:
    return {**DEFAULTS, **over}


# ------------------------------------------------------------------------------------------------ bars
def prepare(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """time/open/high/low/close -> + clean_high/clean_low, atr (Wilder, on cleaned bars), ema_f/ema_s/ema_t,
    cleaned (bool). A wick is cut at the body edge when it is longer than badtick_*_atr x the ATR of the bars
    BEFORE it (the bar's own range cannot vouch for itself). Every cleaned candle is logged."""
    cfg = cfg or DEFAULTS
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    n, p = len(df), cfg["atr_period"]
    ch, cl, atr = h.copy(), l.copy(), np.full(n, np.nan)
    cleaned = np.zeros(n, bool)
    trs, a = [], np.nan
    for i in range(n):
        body_lo, body_hi = min(o[i], c[i]), max(o[i], c[i])
        if i > 0 and not np.isnan(atr[i - 1]):
            ref = atr[i - 1]
            if body_lo - l[i] > cfg["badtick_low_atr"] * ref:
                cl[i], cleaned[i] = body_lo, True
            if h[i] - body_hi > cfg["badtick_high_atr"] * ref:
                ch[i], cleaned[i] = body_hi, True
        tr = ch[i] - cl[i] if i == 0 else max(ch[i] - cl[i], abs(ch[i] - c[i - 1]), abs(cl[i] - c[i - 1]))
        if np.isnan(a):
            trs.append(tr)
            if len(trs) == p:
                a = sum(trs) / p
        else:
            a = (a * (p - 1) + tr) / p                      # Wilder smoothing
        atr[i] = a
    out = df[["time", "open", "high", "low", "close"]].copy().reset_index(drop=True)
    out["clean_high"], out["clean_low"], out["atr"], out["cleaned"] = ch, cl, atr, cleaned
    close = out["close"]
    out["ema_f"] = close.ewm(span=cfg["ema_fast"], adjust=False).mean()
    out["ema_s"] = close.ewm(span=cfg["ema_slow"], adjust=False).mean()
    out["ema_t"] = close.ewm(span=cfg["ema_trend"], adjust=False).mean()
    for i in np.flatnonzero(cleaned):
        log.info("bad tick cleaned: %s H %.2f->%.2f L %.2f->%.2f", out["time"].iloc[i], h[i], ch[i], l[i], cl[i])
    return out


# ------------------------------------------------------------------------------------------- pullbacks
@dataclass
class Pullback:
    """The pullback / re-entry / step machine for one side. Shared by the labels and the trailing stop."""
    side: int
    extreme: float                       # trend extreme (highest high long / lowest low short) since start
    state: str = "none"                  # none | pullback | reentered
    pre_extreme: float | None = None     # trend extreme when the pullback started
    pb_extreme: float | None = None      # the pullback's own extreme (lowest low long / highest high short)

    def step(self, b: dict, prev: dict, cfg: dict) -> list[tuple[str, dict]]:
        s, out = self.side, []
        hi, lo, c, e8, e32, atr = b["clean_high"], b["clean_low"], b["close"], b["ema_f"], b["ema_s"], b["atr"]
        if self.state == "pullback":
            self.pb_extreme = min(self.pb_extreme, lo) if s > 0 else max(self.pb_extreme, hi)
            if s * (e32 - c) > cfg["pullback_fail_atr"] * atr:
                out.append((PULLBACK_FAILED, {"pullback_extreme": self.pb_extreme}))
                self.state, self.pre_extreme, self.pb_extreme = "none", None, None
            else:
                beyond_prev = c > prev["clean_high"] if s > 0 else c < prev["clean_low"]
                if s * (c - e8) > 0 and beyond_prev:
                    out.append((RE_ENTRY, {"pullback_extreme": self.pb_extreme}))
                    self.state = "reentered"
        elif self.state == "reentered":
            if s * (c - self.pre_extreme) > 0:
                out.append((STEP, {"pullback_extreme": self.pb_extreme, "pre_extreme": self.pre_extreme}))
                self.state, self.pre_extreme, self.pb_extreme = "none", None, None
        # trend extreme follows price (a closed-beyond step bar may also set a new extreme)
        self.extreme = max(self.extreme, hi) if s > 0 else min(self.extreme, lo)
        if self.state in ("none", "reentered"):
            g = s * (e8 - e32)
            if (s * (e8 - c) > 0 and s * (c - e32) > 0 and s * (self.extreme - c) >= cfg["pullback_off_extreme_atr"] * atr
                    and g / atr >= cfg["pullback_min_atr"]):
                if self.state == "none":                    # a fresh pullback from the current extreme
                    self.pre_extreme = self.extreme
                self.pb_extreme = lo if s > 0 else hi
                if self.state == "reentered":               # it slipped back before breaking out: still the same
                    self.pb_extreme = lo if s > 0 else hi   # pre-pullback extreme, a new pullback low
                self.state = "pullback"
                out.append((PULLBACK, {"pre_extreme": self.pre_extreme, "pullback_extreme": self.pb_extreme}))
        return out


# --------------------------------------------------------------------------------------------- engine
@dataclass
class State:
    side: int = 0
    cross_i: int | None = None
    cross_price: float | None = None
    tag: str = ""                        # "above 200" | "below 200" at the cross
    phase: str = ""
    widening_armed: bool = True
    parallel_armed: bool = True
    wide_armed: bool = True
    peak_armed: bool = False
    seen: dict = field(default_factory=dict)       # checklist: widening / parallel / wide
    pb: Pullback | None = None
    pre: dict = field(default_factory=dict)        # pair -> {"armed","fired","sign"}
    last: dict = field(default_factory=dict)       # the last bar's numbers, for the status line
    bars_since_cross: int = 0


@dataclass
class Result:
    events: list                         # [{"i","time","label","side","price",...}]
    state: State


def _rising(g: np.ndarray, i: int, n: int) -> bool:
    return i >= n and all(g[i - k] > g[i - k - 1] for k in range(n))


def _falling(g: np.ndarray, i: int, n: int) -> bool:
    return i >= n and all(g[i - k] < g[i - k - 1] for k in range(n))


def run(bars: pd.DataFrame, cfg: dict | None = None, start: int | None = None) -> Result:
    """Walk every bar (from `start`, default: once EMA200 and ATR are valid) and label it."""
    cfg = cfg or DEFAULTS
    n = len(bars)
    st = State()
    events: list = []
    if n == 0:
        return Result(events, st)
    cols = {k: bars[k].to_numpy(dtype=float) for k in ("open", "close", "clean_high", "clean_low", "atr",
                                                      "ema_f", "ema_s", "ema_t")}
    times = bars["time"].tolist()
    gap = cols["ema_f"] - cols["ema_s"]
    first = max(1, int(np.argmax(~np.isnan(cols["atr"]))) + 1) if start is None else max(1, start)
    rows = [{k: cols[k][i] for k in cols} for i in range(n)]

    def emit(i, label, **kw):
        e = {"i": i, "time": times[i], "label": label, "side": st.side, "price": float(cols["close"][i]),
             "gap": float(abs(gap[i])), "atr": float(cols["atr"][i]), "tag": st.tag}
        e.update(kw)
        events.append(e)
        return e

    for i in range(first, n):
        b, prev, atr = rows[i], rows[i - 1], cols["atr"][i]
        if np.isnan(atr) or atr <= 0:
            continue
        sgn_now = np.sign(gap[i])
        # ---------------- pre-cross (before the cross check: a pair that crosses on this bar is not "pre") ----
        for name, a_col, b_col, key in PAIRS:
            arm, fire, shrink, fail = cfg[key]
            d = cols[a_col] - cols[b_col]
            p = st.pre.setdefault(name, {"armed": False, "fired": False, "sign": 0})
            s_now = np.sign(d[i])
            if p["sign"] and s_now != p["sign"] and s_now != 0:          # the pair crossed: start over
                p.update(armed=False, fired=False)
            p["sign"] = s_now
            dist = abs(d[i]) / atr
            if name == "8/32" and st.side == 0:
                continue                                                 # needs a trend to be "in"
            if dist >= arm:
                if p["fired"] and dist >= fail:
                    emit(i, PRE_CROSS_FAILED, pair=name, dist_atr=float(dist), expect=int(-s_now))
                    p["fired"] = False
                p["armed"] = True
            elif p["armed"] and not p["fired"] and dist <= fire and _falling(np.abs(d), i, shrink):
                emit(i, PRE_CROSS, pair=name, dist_atr=float(dist), dist=float(abs(d[i])), expect=int(-s_now))
                p["fired"] = True
        # ---------------- the 8/32 cross ---------------------------------------------------------------------
        if sgn_now != 0 and sgn_now != st.side:
            old = (st.side, st.cross_i, st.cross_price)
            real = st.side != 0                              # the first trend of a replay was not SEEN crossing
            st.side, st.cross_i, st.cross_price = int(sgn_now), i, float(cols["close"][i])
            if not real:                                     # find where it really started, for the counters
                j = i
                while j > 0 and np.sign(gap[j - 1]) == sgn_now:
                    j -= 1
                st.cross_i, st.cross_price = j, float(cols["close"][j])
            st.tag = "above 200" if cols["close"][st.cross_i] > cols["ema_t"][st.cross_i] else "below 200"
            st.widening_armed = st.parallel_armed = st.wide_armed = True
            st.peak_armed, st.seen = False, {}
            st.pb = Pullback(st.side, float(cols["close"][i]))
            ga0 = abs(gap[i]) / atr
            st.phase = LESSER_GAP if ga0 < cfg["lesser_gap_atr"] else ""
            st.bars_since_cross = i - st.cross_i
            if real:
                fin = {"side": old[0], "bars": i - old[1], "ran": float(old[0] * (cols["close"][i] - old[2]))}
                emit(i, CROSS, lesser_gap=bool(ga0 < cfg["lesser_gap_atr"]), finished=fin)
                emit(i, TREND_FINISHED, side=old[0], bars=fin["bars"], ran=fin["ran"])
                continue
        if st.side == 0:
            continue
        st.bars_since_cross = i - st.cross_i
        s = st.side
        g = s * gap
        ga = g[i] / atr
        # ---------------- gap phases --------------------------------------------------------------------------
        if not st.widening_armed and _falling(g, i, cfg["widening_rearm_falls"]):
            st.widening_armed = True
        if st.widening_armed and ga >= cfg["widening_atr"] and _rising(g, i, cfg["widening_rises"]):
            emit(i, GAP_WIDENING); st.widening_armed = False; st.seen["widening"] = True; st.phase = GAP_WIDENING
        nb = cfg["parallel_bars"]
        if i >= nb - 1:
            win = g[i - nb + 1:i + 1]
            spread = float(win.max() - win.min())
            if not st.parallel_armed and spread > cfg["parallel_rearm_band"] * g[i]:
                st.parallel_armed = True
            if (st.parallel_armed and ga >= cfg["parallel_atr"] and spread <= cfg["parallel_band"] * g[i]
                    and s * (cols["ema_s"][i] - cols["ema_s"][i - 1]) > 0):
                emit(i, PARALLEL); st.parallel_armed = False; st.seen["parallel"] = True; st.phase = PARALLEL
        if not st.wide_armed and ga < cfg["wide_rearm_atr"]:
            st.wide_armed = True
        if st.wide_armed and ga >= cfg["wide_atr"]:
            emit(i, WIDE_GAP); st.wide_armed = False; st.peak_armed = True; st.seen["wide"] = True; st.phase = WIDE_GAP
        elif st.peak_armed and _falling(g, i, cfg["peak_falls"]):
            emit(i, GAP_PEAKED); st.peak_armed = False; st.phase = GAP_PEAKED
        elif (not st.peak_armed and st.seen.get("wide") and st.phase == GAP_PEAKED and ga >= cfg["wide_atr"]
              and _rising(g, i, cfg["peak_rearm_rises"])):
            st.peak_armed = True; st.phase = WIDE_GAP
        # ---------------- pullbacks ----------------------------------------------------------------------------
        for label, extra in st.pb.step(b, prev, cfg):
            if label == STEP:
                emit(i, STEP, **extra)
                continue
            emit(i, label, **extra)
            st.phase = label
    if n:
        i = n - 1
        st.last = {"time": times[i], "close": float(cols["close"][i]), "gap": float(abs(gap[i])),
                   "atr": float(cols["atr"][i]) if not np.isnan(cols["atr"][i]) else None,
                   "above_200": bool(cols["close"][i] > cols["ema_t"][i]),
                   "dist": {name: float(abs(cols[a][i] - cols[b][i])) for name, a, b, _ in PAIRS}}
    return Result(events, st)


# --------------------------------------------------------------------------------------- trailing stop
def tsl_steps(bars: pd.DataFrame, start: int, side: int, buffer_at, entry: float | None = None,
              cfg: dict | None = None) -> list[dict]:
    """Stepped trailing stop for a position on `side` opened at bar `start` (its close, or `entry`).
    The trend extreme is tracked from the open. After a RE-ENTRY, a candle closing beyond the pre-pullback
    extreme moves the stop to the pullback's extreme -/+ buffer. buffer_at(i) -> buffer in price units at bar i.
    Returns [{"i","time","sl","pullback_extreme","pre_extreme"}] in order. No breakeven step (spec 7.2)."""
    cfg = cfg or DEFAULTS
    cols = {k: bars[k].to_numpy(dtype=float) for k in ("close", "clean_high", "clean_low", "atr", "ema_f", "ema_s")}
    rows = [{k: cols[k][i] for k in cols} for i in range(len(bars))]
    pb = Pullback(side, float(entry if entry is not None else cols["close"][start]))
    out = []
    for i in range(start + 1, len(bars)):
        if np.isnan(cols["atr"][i]):
            continue
        if side * (cols["ema_f"][i] - cols["ema_s"][i]) <= 0:
            break                                            # the trend turned against the position: no more steps
        for label, extra in pb.step(rows[i], rows[i - 1], cfg):
            if label == STEP:
                sl = extra["pullback_extreme"] - side * buffer_at(i)
                out.append({"i": i, "time": bars["time"].iloc[i], "sl": float(sl), **extra})
    return out


# ------------------------------------------------------------------------------------------------ text
SIDE = {1: "LONG ▲", -1: "SHORT ▼", 0: "no trend"}


def status_line(st: State) -> str:
    """Side, tag above/below 200, phase, gap (price and ATR), candles since the cross."""
    if not st.last or st.side == 0:
        return "no trend yet"
    atr = st.last.get("atr") or 0
    ga = st.last["gap"] / atr if atr else 0
    where = "above 200" if st.last["above_200"] else "below 200"
    return (f"{SIDE[st.side]} · {where} · {st.phase or '-'} · gap {st.last['gap']:.2f} ({ga:.2f} ATR) · "
            f"{st.bars_since_cross} candles since the cross")
