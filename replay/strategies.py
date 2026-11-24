"""Backtest strategies for the Replay Lab. A strategy sees ONLY what was known at a candle's close (the candle, the
evaluation window ending at it, the events the live engines produced for it, the label state, its open virtual
trades) and returns Intents that the virtual broker fills at the NEXT candle's open.

    indicators              no trades: the replay reports indicator events only (never presented as profit or loss)
    ema_cross               enter on the EMA 8/32 CROSS event, exit on the opposite cross (and/or SL/TP)
    pullback                enter on a RE-ENTRY label in the trend's direction, exit on the opposite cross
    manual                  explicitly defined virtual trades
    ema_20_50_basic         EXPERIMENTAL basic EMA 20/50 crossover with a cloud (see EmaCloudBasic.UNVERIFIED)

An EMA cross is NEVER an entry unless the selected strategy says so. Protective SL/TP (Protective) and lot sizing
(Sizing) are shared by every strategy. Every strategy has a version and a parameter hash (describe()), and every
entry records the conditions that passed and failed, so each trade is explainable and reproducible.
The frozen C5 strategy and H1 Horizon One are NOT implemented here: their definitions are not in this repository.
"""
from __future__ import annotations
import hashlib, json
from dataclasses import dataclass, field, asdict
import pandas as pd
import labels, signal_engine
from replay.broker import Intent, SymbolSpec

CROSS_TYPES = ("CROSS", "CROSS_AFTER_TREND")


@dataclass
class Protective:
    """Initial stop and target. sl_mode: atr | price | none. tp_mode: atr | price | r (multiple of the stop) | none."""
    sl_mode: str = "atr"
    sl: float = 2.0
    tp_mode: str = "r"
    tp: float = 2.0

    def distances(self, atr: float | None) -> tuple[float | None, float | None]:
        def one(mode, x, sl_d=None):
            if mode == "none" or x is None:
                return None
            if mode == "price":
                return float(x)
            if mode == "atr":
                return float(x) * float(atr) if atr else None
            if mode == "r":
                return float(x) * sl_d if sl_d else None
            raise ValueError(f"unknown mode {mode}")
        sl_d = one(self.sl_mode, self.sl)
        return sl_d, one(self.tp_mode, self.tp, sl_d)


@dataclass
class Sizing:
    """fixed: `lots`. risk: risk_pct of equity at the stop distance. step: lots x floor(equity / step_equity)."""
    mode: str = "fixed"
    lots: float = 1.0
    risk_pct: float = 1.0
    step_equity: float = 50000.0

    def lots_for(self, equity: float, sl_dist: float | None, spec: SymbolSpec) -> float:
        if self.mode == "fixed":
            x = self.lots
        elif self.mode == "risk":
            if not sl_dist:
                raise ValueError("risk sizing needs a stop-loss")
            x = equity * self.risk_pct / 100 / spec.money(sl_dist, 1.0)
        elif self.mode == "step":
            x = self.lots * max(1, int(equity // self.step_equity))
        else:
            raise ValueError(f"unknown sizing mode {self.mode}")
        return spec.round_lots(x)


@dataclass
class Context:
    index: int
    bar: dict                       # the candle that just closed (server time strings)
    events: list                    # replay events of this candle (dicts)
    lab: object                     # labels.Result of the evaluation window (state after this candle)
    atr: float | None
    open_trades: list
    equity: float
    spec: SymbolSpec
    window: pd.DataFrame | None = None   # the evaluation window ending at this candle (indicators added)


def cond(name: str, passed: bool, value=None) -> dict:
    return {"name": name, "passed": bool(passed), "value": value}


class Strategy:
    name = "indicators"
    version = "1.0"
    trades = False
    overlay_emas: tuple = ()        # extra EMA periods drawn on the chart (from signal_engine.add_emas)

    def __init__(self, protective: Protective | None = None, sizing: Sizing | None = None, **params):
        self.protective = protective or Protective()
        self.sizing = sizing or Sizing()
        self.params = params

    def describe(self) -> dict:
        d = {"name": self.name, "version": self.version, "trades": self.trades, "params": self.params,
             "protective": asdict(self.protective), "sizing": asdict(self.sizing)}
        d["param_hash"] = hashlib.sha1(json.dumps({k: d[k] for k in ("name", "version", "params", "protective", "sizing")},
                                                  sort_keys=True, default=str).encode()).hexdigest()[:12]
        return d

    @property
    def param_hash(self) -> str:
        return self.describe()["param_hash"]

    def on_close(self, ctx: Context) -> list[Intent]:
        return []

    def strategy_events(self, ctx: Context) -> list[dict]:
        """The strategy's own signals at this close (shown, logged, sent like indicator events). Default: none."""
        return []

    def overlays(self, window: pd.DataFrame) -> dict:
        """Extra per-candle chart values for the newest candle of `window`, e.g. {"ema20": .., "ema50": ..}."""
        if not self.overlay_emas:
            return {}
        w = signal_engine.add_emas(window[["close"]].copy(), self.overlay_emas)
        return {f"ema{p}": float(w[f"ema{p}"].iloc[-1]) for p in self.overlay_emas}

    def warmup_overlays(self, df: pd.DataFrame) -> list[dict]:
        if not self.overlay_emas:
            return [{} for _ in range(len(df))]
        w = signal_engine.add_emas(df[["close"]].copy(), self.overlay_emas)
        return [{f"ema{p}": float(w[f"ema{p}"].iloc[i]) for p in self.overlay_emas} for i in range(len(df))]

    def _open(self, ctx: Context, side: int, reason: str, event: dict | None, conditions: list | None = None) -> Intent:
        sl_d, tp_d = self.protective.distances(ctx.atr)
        conditions = list(conditions or []) + [cond("protective stop computable", sl_d is not None or
                                                    self.protective.sl_mode == "none", sl_d)]
        return Intent("open", side=side, lots=self.sizing.lots_for(ctx.equity, sl_d, ctx.spec), sl_dist=sl_d,
                      tp_dist=tp_d, reason=reason, event_key=event and event.get("key"),
                      event_type=event and event.get("type"), signal=reason, conditions=conditions)


class IndicatorsOnly(Strategy):
    """Existing EMA 8/32 indicator events, reported as events. No virtual trades."""
    name = "indicators"


def _side(d: str) -> int:
    return 1 if d in ("BULL", "LONG") else -1


class EmaCross(Strategy):
    """params: align_200 (only crosses on the same side of EMA200), reverse (flip on the opposite cross)."""
    name, version, trades = "ema_cross", "1.0", True

    def on_close(self, ctx):
        out = []
        cross = next((e for e in ctx.events if e["type"] in CROSS_TYPES), None)
        if cross is None:
            return out
        side = _side(cross["dir"])
        against = [t for t in ctx.open_trades if t.side != side]
        if against:
            out.append(Intent("close", side=-side, reason=f"opposite {cross['type']}", event_key=cross["key"]))
        conds = [cond("EMA 8/32 cross on the closed candle", True, cross["type"]),
                 cond("aligned with EMA200 (align_200)", cross["type"] == "CROSS_AFTER_TREND" or not self.params.get("align_200"),
                      cross["type"] == "CROSS_AFTER_TREND"),
                 cond("no position already open in this direction", not any(t.side == side for t in ctx.open_trades)),
                 cond("reversal allowed", not against or self.params.get("reverse", True))]
        if not all(c["passed"] for c in conds):
            return out
        out.append(self._open(ctx, side, f"{cross['type']} {cross['dir']}", cross, conds))
        return out


class Pullback(Strategy):
    """Enter on RE-ENTRY in the trend direction; exit on the opposite 8/32 cross (exit_on_cross, default true)."""
    name, version, trades = "pullback", "1.0", True

    def on_close(self, ctx):
        out = []
        cross = next((e for e in ctx.events if e["type"] in CROSS_TYPES), None)
        if cross and self.params.get("exit_on_cross", True):
            side = _side(cross["dir"])
            if any(t.side != side for t in ctx.open_trades):
                out.append(Intent("close", side=-side, reason=f"opposite {cross['type']}", event_key=cross["key"]))
        re = next((e for e in ctx.events if e["type"] == labels.RE_ENTRY), None)
        if re and not ctx.open_trades:
            conds = [cond("RE-ENTRY label on the closed candle", True, re["dir"]), cond("flat (no open trade)", True)]
            out.append(self._open(ctx, _side(re["dir"]), f"RE-ENTRY {re['dir']}", re, conds))
        return out


class Manual(Strategy):
    """Explicitly defined virtual trades. params: trades=[{"time", "side", "lots"?, "sl"?, "tp"?}] (server time;
    the trade fills at the open of the first candle that opens at or after `time`)."""
    name, version, trades = "manual", "1.0", True

    def __init__(self, protective=None, sizing=None, trades: list | None = None, **params):
        super().__init__(protective, sizing, trades=trades or [], **params)
        self.todo = sorted(({**t, "time": pd.Timestamp(t["time"])} for t in (trades or [])), key=lambda t: t["time"])

    def on_close(self, ctx):
        out = []
        next_open = pd.Timestamp(ctx.bar["next_open"]) if ctx.bar.get("next_open") else None
        while self.todo and next_open is not None and self.todo[0]["time"] <= next_open:
            t = self.todo.pop(0)
            side = 1 if str(t["side"]).lower() in ("buy", "long", "1") else -1
            it = self._open(ctx, side, f"manual {t['side']}", None, [cond("manual trade time reached", True, str(t["time"]))])
            if t.get("lots"):
                it.lots = ctx.spec.round_lots(float(t["lots"]))
            if t.get("sl") is not None:
                it.sl, it.sl_dist = float(t["sl"]), None
            if t.get("tp") is not None:
                it.tp, it.tp_dist = float(t["tp"]), None
            out.append(it)
        return out


class EmaCloudBasic(Strategy):
    """EXPERIMENTAL basic EMA 20/50 crossover with a cloud. NOT the authoritative "EMA 20/50 Cloud Crossover"
    strategy: no definition of that strategy exists in this repository, so nothing here claims to reproduce it.

    Rules implemented (every one is visible in each trade's conditions):
      * EMA20 and EMA50 of the close (signal_engine.add_emas, the shared EMA); the cloud is the area between them,
        bullish when EMA20 > EMA50.
      * Entry: EMA20 crosses EMA50 on a CLOSED candle; filled at the next candle's open. Optional
        `require_close_beyond_cloud` (default true): that candle also closes beyond both EMAs.
      * Exit: the opposite cross (closes the trade at the next open), or the protective SL/TP.
      * Re-entry (`reentry`, default true): flat after a stop/target exit while the cloud keeps its direction, price
        closes back INSIDE the cloud and then closes beyond it again -> one new entry in the cloud's direction.
    """
    name, version, trades = "ema_20_50_basic", "0.1-experimental", True
    overlay_emas = (20, 50)
    UNVERIFIED = [
        "whether the original uses EMA of close or another source (HLC3, HA close)",
        "the exact confirmation rule (close beyond the cloud? beyond both EMAs? N candles?)",
        "any cloud-thickness / ATR filter",
        "the original stop-loss and take-profit placement (here: the shared Protective setting)",
        "the original exit rule (here: opposite cross or SL/TP)",
        "the original re-entry rule (here: close back inside the cloud, then beyond it again)",
        "session, timeframe and symbol filters",
        "position sizing used in the original results",
    ]

    def __init__(self, protective=None, sizing=None, **params):
        params.setdefault("require_close_beyond_cloud", True)
        params.setdefault("reentry", True)
        super().__init__(protective, sizing, **params)
        self.cloud = 0                    # +1 bullish / -1 bearish since the last cross
        self.inside_since_exit = False
        self.last_exit_kind = None        # "cross" | "stop" | None

    def describe(self) -> dict:
        d = super().describe()
        d["experimental"] = True
        d["unverified_rules"] = list(self.UNVERIFIED)
        return d

    def _emas(self, ctx):
        w = signal_engine.add_emas(ctx.window[["close"]].copy(), self.overlay_emas)
        return (float(w["ema20"].iloc[-2]), float(w["ema50"].iloc[-2]), float(w["ema20"].iloc[-1]),
                float(w["ema50"].iloc[-1]))

    def _cross(self, ctx):
        import signals
        a0, b0, a1, b1 = self._emas(ctx)
        return signals._cross_dir(a0, b0, a1, b1), a1, b1

    def strategy_events(self, ctx):
        d, a1, b1 = self._cross(ctx)
        if not d:
            return []
        return [{"type": "EMA20_50_CROSS", "dir": d, "price": float(ctx.bar["close"]),
                 "details": {"ema20": round(a1, 4), "ema50": round(b1, 4), "strategy": self.name,
                             "version": self.version, "experimental": True}}]

    def on_close(self, ctx):
        out = []
        d, a1, b1 = self._cross(ctx)
        close = float(ctx.bar["close"])
        hi, lo = max(a1, b1), min(a1, b1)
        if d:
            side = _side(d)
            self.cloud, self.inside_since_exit, self.last_exit_kind = side, False, None
            if any(t.side != side for t in ctx.open_trades):
                out.append(Intent("close", side=-side, reason=f"opposite EMA20/50 cross ({d})"))
                self.last_exit_kind = "cross"
            beyond = close > hi if side > 0 else close < lo
            conds = [cond("EMA20 crossed EMA50 on the closed candle", True, d),
                     cond("close beyond the cloud", beyond or not self.params["require_close_beyond_cloud"],
                          {"close": close, "ema20": round(a1, 4), "ema50": round(b1, 4)}),
                     cond("no position already open in this direction", not any(t.side == side for t in ctx.open_trades))]
            if all(c["passed"] for c in conds):
                out.append(self._open(ctx, side, f"EMA20/50 cross {d}", {"key": None, "type": "EMA20_50_CROSS"}, conds))
            return out
        # re-entry tracking within the same cloud direction
        if not self.params["reentry"] or not self.cloud or ctx.open_trades:
            if ctx.open_trades:
                self.inside_since_exit, self.last_exit_kind = False, None
            return out
        if self.last_exit_kind is None:
            self.last_exit_kind = "stop"                  # flat in a cloud = the last trade hit its stop/target
        if lo <= close <= hi:
            self.inside_since_exit = True
            return out
        beyond = close > hi if self.cloud > 0 else close < lo
        if beyond and self.inside_since_exit and self.last_exit_kind == "stop":
            conds = [cond("cloud still in the same direction", True, "bullish" if self.cloud > 0 else "bearish"),
                     cond("price closed back inside the cloud after the exit", True),
                     cond("price closed beyond the cloud again", True, {"close": close, "ema20": round(a1, 4),
                                                                         "ema50": round(b1, 4)})]
            self.inside_since_exit = False
            out.append(self._open(ctx, self.cloud, f"EMA20/50 cloud RE-ENTRY {'BULL' if self.cloud > 0 else 'BEAR'}",
                                  {"key": None, "type": "EMA20_50_REENTRY"}, conds))
        return out


STRATEGIES = {c.name: c for c in (IndicatorsOnly, EmaCross, Pullback, Manual, EmaCloudBasic)}


def make(name: str, protective: dict | None = None, sizing: dict | None = None, **params) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy {name!r} (known: {', '.join(STRATEGIES)})")
    return STRATEGIES[name](Protective(**(protective or {})), Sizing(**(sizing or {})), **params)
