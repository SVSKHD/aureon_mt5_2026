"""The replay engine: closed historical candles, one at a time, through the SAME code the live bot runs.

For every candle i of the requested range the engine builds the window the live bot would hold at that candle's
close (the WARMUP_BARS candles before it plus the candle itself), then runs signal_engine.add_indicators,
labels.prepare/labels.run and signal_engine.candle_events(window, 1, lab) - exactly bot.check_ema's path - and
dedupes with signal_engine.event_key. The window ends at candle i, so no later candle can influence an event.

Isolation: a ReplaySession owns all of its state (dedupe keys, events, broker, counters). It never imports
bot, trader, alerts or positions, never reads or writes the live state files, never sends an order. Sinks
(Discord, UI, report) receive the results; a failing sink never stops the replay.

ReplayController runs one session on a worker thread with start / pause / resume / stop / step / speed / restart.
"""
from __future__ import annotations
import contextlib, logging, threading, time, uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import config as C
import labels, signal_engine
import notify_events
from replay import data as D
from replay.broker import VirtualBroker, Costs, SymbolSpec, spec_for
from replay.strategies import Strategy, IndicatorsOnly, Context

log = logging.getLogger("replay.engine")
IST = ZoneInfo("Asia/Kolkata")
LABEL = "HISTORICAL REPLAY"


class _QuietBadTicks(logging.Filter):
    def filter(self, record):                    # the label engine logs every cleaned candle on every window
        return "bad tick cleaned" not in record.getMessage()


@contextlib.contextmanager
def _quiet_labels():
    f = _QuietBadTicks()
    lg = logging.getLogger("labels")
    lg.addFilter(f)
    try:
        yield
    finally:
        lg.removeFilter(f)


@dataclass
class ReplaySettings:
    strategy: Strategy | None = None
    trade_management: bool = C.REPLAY_TRADE_MANAGEMENT_ENABLED
    balance: float = float(C.REPLAY_ACCOUNT_BALANCE)
    costs: Costs = field(default_factory=Costs)
    spec: SymbolSpec | None = None
    window: int = C.WARMUP_BARS                  # candles before the evaluated one (the live bot's warm-up)
    session_id: str | None = None


def _iso(ts) -> str:
    return pd.Timestamp(ts).isoformat()


class ReplaySession:
    def __init__(self, ds: D.Dataset, settings: ReplaySettings | None = None, intrabar: pd.DataFrame | None = None,
                 clock=time.time):
        if C.REPLAY_EXECUTION_MODE != "simulation":
            raise ValueError("REPLAY_EXECUTION_MODE must be simulation")
        self.ds, self.s = ds, settings or ReplaySettings()
        self.id = self.s.session_id or f"rp-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        self.strategy = self.s.strategy or IndicatorsOnly()
        self.spec = self.s.spec or spec_for(ds.symbol)
        self.broker = VirtualBroker(ds.symbol, self.spec, self.s.costs, self.s.balance, self.s.trade_management)
        self.intrabar = intrabar
        self.clock = clock
        self.next = ds.first_index                 # next candle to evaluate
        self.seen: set = set()                     # dedupe keys (this session only)
        self.events: list[dict] = []               # every event, in historical order
        self.counts: dict = {}
        self.cleaned = 0
        self.sinks: list = []
        self.started_at = self.finished_at = None
        self.bar_min = D.TF_MINUTES[ds.tf]
        self._seq = 0
        self._last_lab = None

    # --------------------------------------------------------------------------------------- facts
    @property
    def total(self) -> int:
        return self.ds.last_index - self.ds.first_index + 1

    @property
    def done(self) -> int:
        return self.next - self.ds.first_index

    @property
    def finished(self) -> bool:
        return self.next > self.ds.last_index

    def close_time(self, i: int) -> pd.Timestamp:
        return self.ds.df["time"].iloc[i] + pd.Timedelta(minutes=self.bar_min)

    def times(self, i: int) -> dict:
        """Historical times of candle i: open + close in server time, close in UTC and IST."""
        row = self.ds.df.iloc[i]
        close_utc = row["utc"] + pd.Timedelta(minutes=self.bar_min)
        return {"bar_time": _iso(row["time"]), "close_time": _iso(self.close_time(i)),
                "close_utc": close_utc.isoformat(), "close_ist": close_utc.tz_convert(IST).strftime("%d %b %Y %H:%M IST")}

    def bar_payload(self, i: int, w: pd.DataFrame | None = None) -> dict:
        row = self.ds.df.iloc[i]
        out = {"i": int(i), "time": _iso(row["time"]), "utc": row["utc"].isoformat(), "open": float(row["open"]),
               "high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"])}
        if "tick_volume" in row and row["tick_volume"] == row["tick_volume"]:
            out["volume"] = float(row["tick_volume"])
        if w is not None:
            last = w.iloc[-1]
            for p in (C.EMA_FAST, C.EMA_SLOW, C.EMA_TREND):
                out[f"ema{p}"] = float(last[f"ema{p}"])
        return out

    def warmup_payload(self) -> list[dict]:
        """The candles before the range (already history at the replay start), with their EMAs from the same
        causal formula. Shown on the chart as context; never evaluated for events."""
        w = signal_engine.add_indicators(self.ds.df.iloc[:self.ds.first_index].copy())
        return [{**self.bar_payload(i), **{f"ema{p}": float(w[f"ema{p}"].iloc[i]) for p in
                                           (C.EMA_FAST, C.EMA_SLOW, C.EMA_TREND)}} for i in range(len(w))]

    # ---------------------------------------------------------------------------------------- step
    def window(self, i: int) -> tuple[pd.DataFrame, int]:
        lo = max(0, i - self.s.window)
        cols = [c for c in ("time", "open", "high", "low", "close", "tick_volume") if c in self.ds.df]
        return self.ds.df.iloc[lo:i + 1][cols].reset_index(drop=True), lo

    def forming(self, i: int):
        """Genuine M1 candles inside candle i, as the growing candle (enhanced animation). Empty without M1 data:
        intrabar movement is never invented from OHLC."""
        if self.intrabar is None or not len(self.intrabar):
            return []
        t0 = self.ds.df["time"].iloc[i]
        m = self.intrabar[(self.intrabar["time"] >= t0) & (self.intrabar["time"] < t0 + pd.Timedelta(minutes=self.bar_min))]
        out, o, h, l = [], None, -1e18, 1e18
        for _, r in m.iterrows():
            o = float(r["open"]) if o is None else o
            h, l = max(h, float(r["high"])), min(l, float(r["low"]))
            out.append({"i": int(i), "time": _iso(t0), "open": o, "high": h, "low": l, "close": float(r["close"]),
                        "as_of": _iso(r["time"] + pd.Timedelta(minutes=1)), "forming": True})
        return out

    def step(self) -> dict | None:
        """Evaluate the next closed candle. Returns the step result (also given to every sink), None when done."""
        if self.finished:
            return None
        if self.started_at is None:
            self.started_at = self.clock()
        i = self.next
        self.next += 1
        w, lo = self.window(i)
        signal_engine.add_indicators(w)
        with _quiet_labels():
            bars = labels.prepare(w)
            lab = labels.run(bars)
        self._last_lab = lab
        if bool(bars["cleaned"].iloc[-1]):
            self.cleaned += 1
        bar = self.bar_payload(i, w)
        t = self.times(i)
        status = labels.status_line(lab.state)
        trades = []
        trades += self.broker.on_open(i, bar, self.strategy.name)          # orders decided at the last close
        trades += self.broker.on_bar(i, bar)                                # stops / targets inside this candle
        events = []
        for bt, e in signal_engine.candle_events(w, 1, lab):
            key = signal_engine.event_key(self.ds.symbol, self.ds.tf, bt, e)
            if key in self.seen:
                continue
            self.seen.add(key)
            events.append(self._event("signal", e["type"], e["dir"], float(e["price"]), key, t, status, e))
        trades += self.broker.manage(i, bars, lo, lab, bar)                 # trailing stop, from candles <= i
        atr = (lab.state.last or {}).get("atr")
        ctx = Context(i, {**bar, "next_open": t["close_time"]}, events, lab, atr, list(self.broker.open),
                      self.broker.equity(bar["close"]), self.spec)
        intents = self.strategy.on_close(ctx) if self.strategy.trades else []
        if intents:
            self.broker.submit(intents, bar["time"], self.strategy.name)
        if self.finished and self.broker.open:
            trades += self.broker.close_all(bar, "end of replay (marked to the last close)")
        self.broker.mark(bar)
        for tr in trades:
            events.append(self._virtual(tr, t, status))
        res = {"session": self.id, "index": i, "done": self.done, "total": self.total, "bar": bar,
               "times": t, "events": events, "status": status, "equity": self.broker.equity(bar["close"]),
               "balance": self.broker.balance, "open_trades": [x.to_dict() for x in self.broker.open],
               "finished": self.finished, "processed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
        if self.finished:
            self.finished_at = self.clock()
        for sink in list(self.sinks):
            try:
                sink(res)
            except Exception:
                log.exception("replay sink failed (the replay continues)")
        return res

    def run(self, until: int | None = None) -> int:
        """Maximum speed: evaluate every remaining candle (or `until` of them). Returns how many."""
        n = 0
        while not self.finished and (until is None or n < until):
            self.step(); n += 1
        return n

    # ---------------------------------------------------------------------------------------- events
    def _base(self, kind, typ, direction, price, key, t, status) -> dict:
        self._seq += 1
        return {"seq": self._seq, "label": LABEL, "session": self.id, "symbol": self.ds.symbol, "tf": self.ds.tf,
                "kind": kind, "type": typ, "dir": direction, "price": price, "key": key, **t, "status": status}

    def _event(self, kind, typ, direction, price, key, t, status, e) -> dict:
        ev = self._base(kind, typ, direction, price, key, t, status)
        ev["details"] = {k: (v if isinstance(v, (int, float, str, bool, type(None), dict, list)) else str(v))
                         for k, v in e.items() if k not in ("type", "dir", "price")}
        ev["severity"] = notify_events.classify({"kind": typ, "ping": typ in notify_events_pings()})
        self.events.append(ev)
        self.counts[typ] = self.counts.get(typ, 0) + 1
        return ev

    def _virtual(self, tr: dict, t, status) -> dict:
        trade = tr.get("trade") or {}
        tid = trade.get("id", tr.get("trade_id"))
        side = trade.get("side") if trade else None
        price = trade.get("exit_price") if tr["kind"] == "VIRTUAL_CLOSE" else (
            trade.get("entry_price") if tr["kind"] == "VIRTUAL_OPEN" else tr.get("new"))
        ev = self._base("virtual", tr["kind"], {1: "LONG", -1: "SHORT"}.get(side, ""), price,
                        f"{self.id}:trade{tid}:{tr['kind']}:{t['bar_time']}", t, status)
        ev["details"] = tr
        ev["severity"] = "high"
        self.events.append(ev)
        self.counts[tr["kind"]] = self.counts.get(tr["kind"], 0) + 1
        return ev


def notify_events_pings() -> set:
    import notifier
    return notifier.PINGS


# ------------------------------------------------------------------------------------------ controller
class ReplayController:
    """Runs a session on a worker thread. speed = candles per second (None or <= 0 = maximum speed).
    animate: when the speed allows it and genuine M1 data exists, the forming candle is shown minute by minute
    (UI only; indicators are evaluated on closed candles only, at every speed)."""
    STATES = ("idle", "running", "paused", "stopped", "finished", "error")

    def __init__(self, factory, on_result=None, on_state=None, on_forming=None, speed: float | None = 2.0,
                 animate: bool = False):
        self.factory, self.on_result, self.on_state, self.on_forming = factory, on_result, on_state, on_forming
        self.speed, self.animate = speed, animate
        self.session: ReplaySession | None = None
        self.state = "idle"
        self.error: str | None = None
        self._cv = threading.Condition()
        self._steps = 0
        self._stop = False
        self._thread: threading.Thread | None = None

    def _set(self, st):
        self.state = st
        if self.on_state:
            try:
                self.on_state(st, self)
            except Exception:
                log.exception("on_state failed")

    def start(self, paused: bool = False):
        with self._cv:
            if self._thread and self._thread.is_alive():
                return
            self.session = self.factory()
            if self.on_result:
                self.session.sinks.append(self.on_result)
            self._stop, self._steps = False, 0
            self._set("paused" if paused else "running")
            self._thread = threading.Thread(target=self._run, daemon=True, name="replay-engine")
            self._thread.start()

    def pause(self):
        """No new candle starts after this; a candle already being evaluated finishes (a step is atomic)."""
        with self._cv:
            if self.state == "running":
                self._set("paused"); self._cv.notify_all()

    def resume(self):
        with self._cv:
            if self.state == "paused":
                self._set("running"); self._cv.notify_all()

    def step(self, n: int = 1):
        """One candle forward (pauses first)."""
        with self._cv:
            if self.state == "running":
                self._set("paused")
            if self.state == "paused":
                self._steps += n; self._cv.notify_all()

    def set_speed(self, speed: float | None):
        with self._cv:
            self.speed = speed; self._cv.notify_all()

    def stop(self, wait: float = 5.0):
        with self._cv:
            self._stop = True; self._cv.notify_all()
        t = self._thread
        if t and t is not threading.current_thread():
            t.join(wait)
        if self.state not in ("finished", "error"):
            self._set("stopped")

    def restart(self, paused: bool = False):
        """Throw the session away and start a fresh one from the first candle (a new session id, nothing shared)."""
        self.stop()
        self._thread = None
        self.start(paused=paused)

    def wait(self, timeout: float = 60.0) -> bool:
        t = self._thread
        if t:
            t.join(timeout)
        return not (t and t.is_alive())

    def _interval(self) -> float:
        return 0.0 if not self.speed or self.speed <= 0 else 1.0 / self.speed

    def _run(self):
        s = self.session
        try:
            while True:
                with self._cv:
                    while not self._stop and self.state == "paused" and self._steps == 0:
                        self._cv.wait(0.5)
                    if self._stop:
                        return
                    single = self.state == "paused"
                    if single:
                        self._steps -= 1
                t0 = time.monotonic()
                gap = self._interval()
                if self.animate and self.on_forming and gap >= 0.25 and not single:
                    frames = s.forming(s.next)
                    for f in frames[:-1]:
                        with self._cv:
                            if self._stop or self.state != "running":
                                break
                            self._cv.wait(gap / max(1, len(frames)))
                        self.on_forming(f)
                s.step()
                if s.finished:
                    self._set("finished")
                    return
                if not single and gap:
                    with self._cv:
                        left = gap - (time.monotonic() - t0)
                        if left > 0 and not self._stop:
                            self._cv.wait(left)
                elif not single:
                    time.sleep(0)                                     # let the UI thread take the GIL
        except Exception as e:
            log.exception("replay failed")
            self.error = f"{type(e).__name__}: {e}"
            self._set("error")
