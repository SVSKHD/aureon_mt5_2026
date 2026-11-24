"""Backtest strategies for the Replay Lab. A strategy sees ONLY what was known at a candle's close (the candle, the
events the live engines produced for it, the label state, the open virtual trades) and returns Intents that the
virtual broker fills at the NEXT candle's open.

    indicators   no trades: the replay reports indicator events only (never presented as profit or loss)
    ema_cross    enter on the EMA 8/32 CROSS event, exit on the opposite cross (and/or SL/TP)
    pullback     enter on a RE-ENTRY label in the trend's direction, exit on the opposite cross (and/or SL/TP)
    manual       explicitly defined virtual trades: [{"time": "2026-10-29 06:00", "side": "sell", "sl": .., "tp": ..}]

An EMA cross is NEVER an entry unless the selected strategy says so (ema_cross). Protective SL/TP (Protective) and
lot sizing (Sizing) are shared by every strategy. The frozen C5 strategy is not implemented here.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import pandas as pd
import labels
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


class Strategy:
    name = "indicators"
    trades = False

    def __init__(self, protective: Protective | None = None, sizing: Sizing | None = None, **params):
        self.protective = protective or Protective()
        self.sizing = sizing or Sizing()
        self.params = params

    def describe(self) -> dict:
        return {"name": self.name, "trades": self.trades, "params": self.params,
                "protective": asdict(self.protective), "sizing": asdict(self.sizing)}

    def on_close(self, ctx: Context) -> list[Intent]:
        return []

    def _open(self, ctx: Context, side: int, reason: str, event: dict | None) -> Intent:
        sl_d, tp_d = self.protective.distances(ctx.atr)
        return Intent("open", side=side, lots=self.sizing.lots_for(ctx.equity, sl_d, ctx.spec), sl_dist=sl_d,
                      tp_dist=tp_d, reason=reason, event_key=event and event["key"], event_type=event and event["type"])


class IndicatorsOnly(Strategy):
    """Existing EMA 8/32 indicator events, reported as events. No virtual trades."""
    name = "indicators"


def _side(d: str) -> int:
    return 1 if d in ("BULL", "LONG") else -1


class EmaCross(Strategy):
    """params: align_200 (only crosses on the same side of EMA200), reverse (flip on the opposite cross)."""
    name, trades = "ema_cross", True

    def on_close(self, ctx):
        out = []
        cross = next((e for e in ctx.events if e["type"] in CROSS_TYPES), None)
        if cross is None:
            return out
        side = _side(cross["dir"])
        against = [t for t in ctx.open_trades if t.side != side]
        if against:
            out.append(Intent("close", side=-side, reason=f"opposite {cross['type']}", event_key=cross["key"]))
        if self.params.get("align_200") and cross["type"] != "CROSS_AFTER_TREND":
            return out
        if any(t.side == side for t in ctx.open_trades):
            return out
        if against and not self.params.get("reverse", True):
            return out
        out.append(self._open(ctx, side, f"{cross['type']} {cross['dir']}", cross))
        return out


class Pullback(Strategy):
    """Enter on RE-ENTRY in the trend direction; exit on the opposite 8/32 cross (exit_on_cross, default true)."""
    name, trades = "pullback", True

    def on_close(self, ctx):
        out = []
        cross = next((e for e in ctx.events if e["type"] in CROSS_TYPES), None)
        if cross and self.params.get("exit_on_cross", True):
            side = _side(cross["dir"])
            if any(t.side != side for t in ctx.open_trades):
                out.append(Intent("close", side=-side, reason=f"opposite {cross['type']}", event_key=cross["key"]))
        re = next((e for e in ctx.events if e["type"] == labels.RE_ENTRY), None)
        if re and not ctx.open_trades:
            out.append(self._open(ctx, _side(re["dir"]), f"RE-ENTRY {re['dir']}", re))
        return out


class Manual(Strategy):
    """Explicitly defined virtual trades. params: trades=[{"time", "side", "lots"?, "sl"?, "tp"?}] (server time;
    the trade fills at the open of the first candle that opens at or after `time`)."""
    name, trades = "manual", True

    def __init__(self, protective=None, sizing=None, trades: list | None = None, **params):
        super().__init__(protective, sizing, trades=trades or [], **params)
        self.todo = sorted(({**t, "time": pd.Timestamp(t["time"])} for t in (trades or [])), key=lambda t: t["time"])

    def on_close(self, ctx):
        out = []
        next_open = pd.Timestamp(ctx.bar["next_open"]) if ctx.bar.get("next_open") else None
        while self.todo and next_open is not None and self.todo[0]["time"] <= next_open:
            t = self.todo.pop(0)
            side = 1 if str(t["side"]).lower() in ("buy", "long", "1") else -1
            it = self._open(ctx, side, f"manual {t['side']}", None)
            if t.get("lots"):
                it.lots = ctx.spec.round_lots(float(t["lots"]))
            if t.get("sl") is not None:
                it.sl, it.sl_dist = float(t["sl"]), None
            if t.get("tp") is not None:
                it.tp, it.tp_dist = float(t["tp"]), None
            out.append(it)
        return out


STRATEGIES = {c.name: c for c in (IndicatorsOnly, EmaCross, Pullback, Manual)}


def make(name: str, protective: dict | None = None, sizing: dict | None = None, **params) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy {name!r} (known: {', '.join(STRATEGIES)})")
    return STRATEGIES[name](Protective(**(protective or {})), Sizing(**(sizing or {})), **params)
