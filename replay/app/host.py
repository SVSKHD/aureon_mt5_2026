"""The desktop app's brain, without Qt (so it is unit-tested): commands from the UI, the replay worker, and the
batching between the worker thread and the UI.

Threads:  replay-engine (ReplayController) -> UiFeed.push()   [lock, no I/O]
          loader / finisher threads        -> Host.post()     [queue]
          UI thread                        -> Host.pump() every frame: forwards queued messages, then ONE batch with
                                              every candle and event revealed since the last frame (none skipped).
The UI only displays; it never computes a market value.
"""
from __future__ import annotations
import logging, queue, threading, time
from collections import deque
from datetime import datetime
import config as C
from replay import api, data as D, strategies as S
from replay.engine import ReplayController

log = logging.getLogger("replay.app")


class UiFeed:
    """Engine results -> one batch per UI frame. Bars and events are never dropped or reordered."""

    def __init__(self, clock=time.monotonic):
        self.lock = threading.Lock()
        self.clock = clock
        self._reset()

    def _reset(self):
        self.bars, self.events, self.forming = [], [], None
        self.progress = self.open_trades = self.equity = self.balance = self.status = None
        self.stamps: deque = deque(maxlen=50)

    def clear(self):
        with self.lock:
            self._reset()

    def push(self, res: dict):
        with self.lock:
            self.bars.append(res["bar"])
            self.events.extend(res["events"])
            self.forming = None                                  # the candle closed: its forming state is over
            self.stamps.append(self.clock())
            rate = None
            if len(self.stamps) > 1 and self.stamps[-1] > self.stamps[0]:
                rate = (len(self.stamps) - 1) / (self.stamps[-1] - self.stamps[0])
            t = res["times"]
            self.progress = {"done": res["done"], "total": res["total"], "hist_time": t["close_time"],
                             "close_ist": t["close_ist"], "wall": datetime.now().strftime("%H:%M:%S"), "rate": rate}
            self.open_trades, self.equity, self.balance = res["open_trades"], res["equity"], res["balance"]
            self.status = res["status"]

    def push_forming(self, bar: dict):
        with self.lock:
            self.forming = bar

    def drain(self) -> dict | None:
        with self.lock:
            if not self.bars and not self.events and self.forming is None:
                return None
            out = {"type": "batch", "bars": self.bars, "events": self.events, "forming": self.forming,
                   "progress": self.progress, "open_trades": self.open_trades, "equity": self.equity,
                   "balance": self.balance, "status": self.status}
            self.bars, self.events, self.forming = [], [], None
            return out


class Host:
    def __init__(self, send, mt5=None):
        self.send = send                                  # callable(dict): UI thread only
        self.mt5 = mt5
        self.feed = UiFeed()
        self.out: queue.Queue = queue.Queue()
        self.ctl: ReplayController | None = None
        self.prepared: api.Prepared | None = None
        self.ui = {}                                      # pick_csv / fullscreen / open_path, set by the Qt window
        self._last_discord = 0.0
        self._last_status = 0.0
        self._finishing = False
        self._t0 = None

    # ------------------------------------------------------------------------------------ to the UI
    def post(self, msg: dict):
        self.out.put(msg)

    def pump(self):
        """UI thread, every frame."""
        while True:
            try:
                self.send(self.out.get_nowait())
            except queue.Empty:
                break
        b = self.feed.drain()
        if b:
            self.send(b)
        if self.ctl and self.ctl.session and time.monotonic() - self._last_status > 1.0:
            self._last_status = time.monotonic()
            self._write_status()
        if self.prepared and self.prepared.discord and time.monotonic() - self._last_discord > 1.0:
            self._last_discord = time.monotonic()
            self.send({"type": "discord", "status": self.prepared.discord.status()})

    def config_msg(self) -> dict:
        problems = [p for p in C.replay_problems() if "DISCORD" in p]
        return {"type": "config", "symbols": [s for s in C.SYMBOLS], "tfs": list(D.REPLAY_TFS),
                "strategies": list(S.STRATEGIES), "sources": ["mt5", "csv", "cache"],
                "discord_configured": bool(C.REPLAY_DISCORD_WEBHOOK) and not problems,
                "discord_problem": "; ".join(problems) or None,
                "defaults": {"trade_management": C.REPLAY_TRADE_MANAGEMENT_ENABLED, "discord": False,
                             "balance": float(C.REPLAY_ACCOUNT_BALANCE)}}

    # ---------------------------------------------------------------------------------- from the UI
    def handle(self, cmd: dict):
        name = cmd.get("cmd")
        try:
            if name == "hello":
                self.post(self.config_msg())
            elif name == "start":
                self.start(cmd.get("request") or {})
            elif name in ("pause", "resume", "stop", "restart") and self.ctl:
                getattr(self.ctl, name)()
                if name == "restart":
                    self._session_msg()
            elif name == "step" and self.ctl:
                self.ctl.step()
            elif name == "speed" and self.ctl:
                self.ctl.set_speed(cmd.get("value"))
            elif name == "jump" and self.ctl:
                self.jump(str(cmd.get("time", "")))
            elif name == "replay_trade" and self.ctl:
                self.replay_trade(int(cmd.get("trade_id")), int(cmd.get("before", 8)))
            elif name == "compare" and self.prepared:
                threading.Thread(target=self._compare, args=(cmd.get("strategies"),), daemon=True,
                                 name="replay-compare").start()
            elif name in self.ui:
                self.ui[name](cmd)
        except Exception as e:
            log.exception("command %s failed", name)
            self.post({"type": "error", "text": f"{name}: {e}"})

    def start(self, r: dict):
        if self.ctl and self.ctl.state in ("running", "paused"):
            raise RuntimeError("a replay is already running: stop it first")
        req = api.Request(symbol=r.get("symbol", "XAUUSD"), tf=r.get("tf", "M30"), start=str(r.get("start", "")),
                          end=str(r.get("end", "")), source=r.get("source", "mt5"), csv_path=r.get("csv_path"),
                          csv_tz=r.get("csv_tz"), strategy=r.get("strategy", "indicators"),
                          params=r.get("params") or {}, protective=r.get("protective") or {}, sizing=r.get("sizing") or {},
                          trade_management=bool(r.get("trade_management", C.REPLAY_TRADE_MANAGEMENT_ENABLED)),
                          balance=float(r.get("balance", C.REPLAY_ACCOUNT_BALANCE)), discord=bool(r.get("discord")),
                          animate=bool(r.get("animate")))
        speed = r.get("speed", 2)
        threading.Thread(target=self._load, args=(req, speed), daemon=True, name="replay-loader").start()

    def _load(self, req, speed):
        try:
            self.prepared = api.prepare(req, mt5=self.mt5)
        except (D.DataError, ValueError) as e:
            self.post({"type": "error", "text": str(e)}); self.post({"type": "state", "state": "idle"})
            return
        except Exception as e:
            log.exception("loading failed")
            self.post({"type": "error", "text": f"{type(e).__name__}: {e}"}); self.post({"type": "state", "state": "idle"})
            return
        first = [True]

        def factory():
            s = self.prepared.session if first[0] else self.prepared.fresh()
            first[0] = False
            self.feed.clear()
            return s
        self.ctl = ReplayController(factory, on_result=self.feed.push, on_state=self._on_state,
                                    on_forming=self.feed.push_forming, speed=speed, animate=req.animate)
        self._session_msg(self.prepared.session)
        self._t0 = time.monotonic()
        self.ctl.start()

    def _run_to(self, index: int):
        """Evaluate up to `index` (restarting a fresh, identical session when it is behind us) and pause there."""
        behind = index < self.ctl.session.next
        self.ctl.run_to(index)
        if behind:
            self._session_msg()

    def jump(self, when: str):
        """Jump to a date: ahead of the replay -> evaluate every candle up to it (nothing skipped) and pause; already
        revealed -> the chart scrolls there. Never reveals a candle after the target."""
        import pandas as pd
        s = self.ctl.session
        t = pd.Timestamp(when.replace("T", " "))
        idx = int((s.ds.df["time"] <= t).sum()) - 1
        idx = max(s.ds.first_index, min(idx, s.ds.last_index))
        if idx >= s.next:
            self._run_to(idx)
        self.post({"type": "jump_view", "time": s.ds.df["time"].iloc[idx].isoformat()})

    def replay_trade(self, trade_id: int, before: int = 8):
        """Go back to `before` candles ahead of the candle that decided trade `trade_id`, paused, ready to step."""
        s = self.ctl.session
        tr = next((t for t in s.broker.closed + s.broker.open if t.id == trade_id), None)
        if tr is None or tr.decided_index is None:
            raise ValueError(f"trade {trade_id} is not known in this session")
        target = max(s.ds.first_index, tr.decided_index - before)
        self._run_to(target)
        self.post({"type": "focus_trade", "trade_id": trade_id, "decided_time": tr.decided_time,
                   "jump_time": s.ds.df["time"].iloc[target].isoformat()})

    def _compare(self, names):
        from replay import compare
        try:
            p = self.prepared
            rep = compare.run(p.ds, names or compare.DEFAULT_SET, protective=p.req.protective or None,
                              costs=p.session.s.costs, balance=p.req.balance, trade_management=p.req.trade_management,
                              spec=p.spec)
            import os
            files = compare.write(rep, os.path.join(p.folder, "compare"))
            with open(files["summary"], encoding="utf-8") as f:
                self.post({"type": "report", "summary": f.read(), "files": files, "report": {"comparison": True}})
        except Exception as e:
            log.exception("comparison failed")
            self.post({"type": "error", "text": f"comparison: {e}"})

    def _write_status(self):
        s = self.ctl.session
        api.write_status({"state": self.ctl.state, "session": s.id, "symbol": s.ds.symbol, "tf": s.ds.tf,
                          "strategy": s.strategy.name, "version": s.strategy.version, "done": s.done, "total": s.total,
                          "hist_time": s.ds.df["time"].iloc[max(s.ds.first_index, s.next - 1)].isoformat(),
                          "trade_management": s.broker.trade_management,
                          "discord": self.prepared.discord.status() if self.prepared and self.prepared.discord else None})

    def _session_msg(self, s=None):
        s = s or (self.ctl.session if self.ctl else None)
        if s is None:
            return
        self.post({"type": "session", "id": s.id, "symbol": s.ds.symbol, "tf": s.ds.tf, "digits": s.spec.digits,
                   "warmup": s.warmup_payload(), "coverage": s.ds.coverage(), "animation": self.prepared.animate_note,
                   "strategy": s.strategy.describe(), "trade_management": s.broker.trade_management,
                   "balance": s.broker.start_balance})

    def _on_state(self, st, ctl):
        self.post({"type": "state", "state": st, "error": ctl.error})
        if ctl.session is not None:
            self._write_status()
        if st in ("finished", "stopped") and not self._finishing:
            self._finishing = True
            threading.Thread(target=self._finish, daemon=True, name="replay-report").start()
        if st == "running":
            self._finishing = False

    def _finish(self):
        try:
            p = self.prepared
            rep, files = api.finish(p, time.monotonic() - (self._t0 or time.monotonic()))
            with open(files["summary"], encoding="utf-8") as f:
                summary = f.read()
            self.post({"type": "report", "summary": summary, "files": files, "report": rep})
            self.post({"type": "discord", "status": rep["discord"]})
        except Exception as e:
            log.exception("report failed")
            self.post({"type": "error", "text": f"report: {e}"})

    def shutdown(self):
        if self.ctl:
            self.ctl.stop()
            if self.ctl.session is not None:
                self._write_status()
