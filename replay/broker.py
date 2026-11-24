"""The virtual broker of the Replay Lab. SIMULATION ONLY: no MT5 order call exists anywhere in replay/.

Money: price difference / tick_size * tick_value * lots, from the broker's symbol specification when MT5 is
available (replay.data.symbol_spec), else a documented default for the symbol.

Prices: candles are BID prices (as MT5 charts). A buy fills at the ask (bid + spread), a sell at the bid. Long
positions are closed at the bid, short positions at the ask, so a short's stop triggers when the ask (high + spread)
reaches it. Slippage is applied against you on market and stop fills, never on take-profit fills.

Timing (no lookahead): an order decided at a candle's close fills at the NEXT candle's open. Stops and targets are
then checked on every candle from that one on.

Intrabar ambiguity (only OHLC is known): if one candle reaches both the stop and the target, the STOP is assumed to
have been hit first (conservative) and the trade is flagged `ambiguous`. A candle that opens beyond the stop fills
at the open (the gap is not forgiven); a candle that opens beyond the target fills AT the target (the better gap
fill is not credited). Both are conservative by construction.

Trade management (REPLAY_TRADE_MANAGEMENT_ENABLED, default false, independent of the live switch): when ON, the live
stepped trailing stop rule (labels.tsl_track, the same function positions.py uses) moves the virtual stop, only
tighter, effective from the next candle. When OFF, each step is recorded as blocked and nothing moves.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field, asdict
import config as C
import labels


@dataclass
class SymbolSpec:
    contract_size: float = 100.0
    tick_size: float = 0.01
    tick_value: float = 1.0
    digits: int = 2
    point: float = 0.01
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float = 100.0
    currency: str = "USD"
    source: str = "default"

    def money(self, price_diff: float, lots: float) -> float:
        return price_diff / self.tick_size * self.tick_value * lots

    def round_lots(self, lots: float) -> float:
        steps = math.floor(lots / self.volume_step + 1e-9)
        return round(min(self.volume_max, max(self.volume_min, steps * self.volume_step)), 8)


DEFAULT_SPECS = {  # typical retail CFD specs; the report always says which spec was used
    "XAUUSD": SymbolSpec(100.0, 0.01, 1.0, 2, 0.01, source="default XAUUSD (100 oz, 0.01 = $1/lot)"),
    "XAGUSD": SymbolSpec(5000.0, 0.001, 5.0, 3, 0.001, source="default XAGUSD (5000 oz, 0.001 = $5/lot)"),
}


def spec_for(symbol: str, mt5_spec: dict | None = None) -> SymbolSpec:
    if mt5_spec:
        return SymbolSpec(**{k: mt5_spec[k] for k in SymbolSpec.__dataclass_fields__ if k in mt5_spec})
    return DEFAULT_SPECS.get(symbol, SymbolSpec(source=f"default (no spec for {symbol})"))


@dataclass
class Costs:
    spread: float = float(C.REPLAY_SPREAD)
    commission_per_lot: float = float(C.REPLAY_COMMISSION_PER_LOT)   # round turn
    slippage: float = float(C.REPLAY_SLIPPAGE)


@dataclass
class VirtualTrade:
    id: int
    side: int
    lots: float
    strategy: str
    reason: str
    event_key: str | None
    event_type: str | None
    decided_time: str                 # the candle whose close decided it
    entry_time: str | None = None
    entry_price: float | None = None
    open_index: int | None = None
    sl: float | None = None
    tp: float | None = None
    initial_sl: float | None = None
    exit_time: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    gross: float = 0.0
    commission: float = 0.0
    net: float = 0.0
    r_multiple: float | None = None
    ambiguous: bool = False
    sl_moves: list = field(default_factory=list)
    blocked_steps: list = field(default_factory=list)
    mfe: float = 0.0                  # maximum favourable / adverse excursion, price units (from candle extremes)
    mae: float = 0.0
    # the trade debugger: everything needed to explain (and reproduce) the trade
    strategy_version: str = ""
    param_hash: str = ""
    entry_signal: str = ""
    conditions: list = field(default_factory=list)      # [{"name", "passed", "value"}] at the deciding close
    decided_index: int | None = None
    exit_index: int | None = None
    mfe_money: float | None = None
    mae_money: float | None = None
    risk_money: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Intent:
    action: str                       # open | close
    side: int = 0
    lots: float | None = None
    sl: float | None = None           # absolute levels (the strategy turns distances into levels)
    tp: float | None = None
    sl_dist: float | None = None      # or distances from the fill price
    tp_dist: float | None = None
    trade_id: int | None = None
    reason: str = ""
    event_key: str | None = None
    event_type: str | None = None
    signal: str = ""                  # what triggered it, in words
    conditions: list = field(default_factory=list)


class VirtualBroker:
    def __init__(self, symbol: str, spec: SymbolSpec, costs: Costs, balance: float, trade_management: bool = False):
        self.symbol, self.spec, self.costs = symbol, spec, costs
        self.balance = self.start_balance = float(balance)
        self.trade_management = bool(trade_management)
        self.open: list[VirtualTrade] = []
        self.closed: list[VirtualTrade] = []
        self.pending: list[Intent] = []
        self.equity_curve: list[dict] = []
        self._next_id = 1
        self._tsl_done: set = set()

    # -------------------------------------------------------------------------------------- helpers
    def _fill_open(self, side: int, price: float) -> float:
        return price + self.costs.spread + self.costs.slippage if side > 0 else price - self.costs.slippage

    def floating(self, t: VirtualTrade, bid: float) -> float:
        exit_px = bid if t.side > 0 else bid + self.costs.spread
        return self.spec.money(t.side * (exit_px - t.entry_price), t.lots)

    def equity(self, bid: float) -> float:
        return self.balance + sum(self.floating(t, bid) for t in self.open)

    def submit(self, intents: list[Intent], decided_time: str, strategy: str, decided_index: int | None = None,
               version: str = "", param_hash: str = ""):
        for it in intents:
            it.reason = it.reason or strategy
            self.pending.append(it)
        self._decided = decided_time
        self._decided_meta = {"decided_index": decided_index, "strategy_version": version, "param_hash": param_hash}

    # --------------------------------------------------------------------------------------- per bar
    def on_open(self, i: int, bar: dict, strategy: str) -> list[dict]:
        """Fill the orders decided at the previous close, at this candle's open."""
        out, pend, self.pending = [], self.pending, []
        for it in pend:
            if it.action == "close":
                for t in [t for t in self.open if it.trade_id in (None, t.id) and (not it.side or t.side == it.side)]:
                    out.append(self._close(t, bar, self._exit_fill(t, bar["open"], stop=False), it.reason or "exit"))
                continue
            t = VirtualTrade(self._next_id, it.side, it.lots, strategy, it.reason, it.event_key, it.event_type,
                             getattr(self, "_decided", ""))
            self._next_id += 1
            t.entry_time, t.open_index = bar["time"], i
            for k, v in getattr(self, "_decided_meta", {}).items():
                setattr(t, k, v)
            t.entry_signal, t.conditions = it.signal or it.reason, list(it.conditions)
            t.entry_price = round(self._fill_open(it.side, bar["open"]), self.spec.digits)
            t.sl = it.sl if it.sl is not None else (t.entry_price - it.side * it.sl_dist if it.sl_dist else None)
            t.tp = it.tp if it.tp is not None else (t.entry_price + it.side * it.tp_dist if it.tp_dist else None)
            t.sl = None if t.sl is None else round(t.sl, self.spec.digits)
            t.tp = None if t.tp is None else round(t.tp, self.spec.digits)
            t.initial_sl = t.sl
            self.open.append(t)
            out.append({"kind": "VIRTUAL_OPEN", "trade": t.to_dict()})
        return out

    def _exit_fill(self, t: VirtualTrade, bid: float, stop: bool = False) -> float:
        """A market exit at `bid`: longs sell at the bid, shorts buy at the ask; slippage against you."""
        px = bid if t.side > 0 else bid + self.costs.spread
        return px - t.side * self.costs.slippage

    def on_bar(self, i: int, bar: dict) -> list[dict]:
        """Stops and targets inside this candle (conservative policy, see the module doc)."""
        out = []
        for t in list(self.open):
            s, o, h, l = t.side, bar["open"], bar["high"], bar["low"]
            sp = 0.0 if s > 0 else self.costs.spread                   # shorts close at the ask
            hit_sl = t.sl is not None and ((l <= t.sl) if s > 0 else (h + sp >= t.sl))
            hit_tp = t.tp is not None and ((h >= t.tp) if s > 0 else (l + sp <= t.tp))
            fav, adv = (h - t.entry_price, t.entry_price - l) if s > 0 else (t.entry_price - (l + sp), (h + sp) - t.entry_price)
            t.mfe, t.mae = max(t.mfe, fav), max(t.mae, adv)
            if hit_sl:
                gap = (o <= t.sl) if s > 0 else (o + sp >= t.sl)
                px = (o + sp if gap else t.sl) - s * self.costs.slippage
                t.ambiguous = bool(hit_tp)
                out.append(self._close(t, bar, px, "SL (gap)" if gap else ("SL (ambiguous bar: stop assumed first)"
                                                                          if hit_tp else "SL")))
            elif hit_tp:
                out.append(self._close(t, bar, t.tp, "TP"))
        return out

    def manage(self, i: int, window_bars, offset: int, lab, bar: dict) -> list[dict]:
        """After the close of candle i: the stepped trailing stop, from candles <= i only. window_bars =
        labels.prepare() of the evaluation window; offset = dataset index of window row 0."""
        out = []
        for t in self.open:
            if lab is None or lab.state.side != t.side:
                continue                                             # against the trend: no stop moves (as live)
            start_open = max(0, t.open_index - 1 - offset)           # the last candle closed when it opened
            start = max(start_open, lab.state.cross_i or 0)
            kind, x = C.SYMBOLS.get(self.symbol, {}).get("tsl_buffer", ("price", 0.0))
            atrs = window_bars["atr"].to_numpy()
            buf = (lambda k: float(x)) if kind == "price" else (lambda k: float(x) * float(atrs[k]))
            entry = t.entry_price if start == start_open else None
            steps, _ = labels.tsl_track(window_bars, start, t.side, buf, entry=entry)
            if not steps:
                continue
            step = steps[-1]
            new_sl = round(step["sl"], self.spec.digits)
            tighter = t.sl is None or (new_sl > t.sl if t.side > 0 else new_sl < t.sl)
            key = (t.id, str(step["time"]))
            if not tighter or key in self._tsl_done:
                continue
            self._tsl_done.add(key)
            rec = {"time": bar["time"], "step_time": str(step["time"]), "old": t.sl, "new": new_sl,
                   "pre_extreme": step["pre_extreme"], "pullback_extreme": step["pullback_extreme"]}
            if self.trade_management:
                t.sl = new_sl
                t.sl_moves.append(rec)
                out.append({"kind": "VIRTUAL_SL_MOVED", "trade_id": t.id, **rec})
            else:
                t.blocked_steps.append(rec)
                out.append({"kind": "VIRTUAL_SL_BLOCKED", "trade_id": t.id, **rec,
                            "why": "replay trade management is OFF"})
        return out

    def mark(self, bar: dict):
        self.equity_curve.append({"time": bar["time"], "balance": round(self.balance, 2),
                                  "equity": round(self.equity(bar["close"]), 2), "open": len(self.open)})

    def close_all(self, bar: dict, reason: str) -> list[dict]:
        return [self._close(t, bar, self._exit_fill(t, bar["close"], stop=False), reason) for t in list(self.open)]

    def _close(self, t: VirtualTrade, bar: dict, px: float, reason: str) -> dict:
        t.exit_time, t.exit_price, t.exit_reason = bar["time"], round(px, self.spec.digits), reason
        t.gross = round(self.spec.money(t.side * (t.exit_price - t.entry_price), t.lots), 2)
        t.commission = round(self.costs.commission_per_lot * t.lots, 2)
        t.net = round(t.gross - t.commission, 2)
        t.exit_index = bar.get("i")
        t.mfe_money = round(self.spec.money(t.mfe, t.lots), 2)
        t.mae_money = round(self.spec.money(t.mae, t.lots), 2)
        if t.initial_sl:
            risk = self.spec.money(abs(t.entry_price - t.initial_sl), t.lots)
            t.risk_money = round(risk, 2)
            t.r_multiple = round(t.net / risk, 3) if risk > 0 else None
        self.balance += t.net
        self.open.remove(t)
        self.closed.append(t)
        return {"kind": "VIRTUAL_CLOSE", "trade": t.to_dict()}
