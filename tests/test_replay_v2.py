"""Replay Lab V2: EMA 20/50 (experimental basic), trade debugger records, replay-this-trade, jump to date, strategy
comparison, cache fallback during an MT5 outage, separate LIVE/REPLAY stores, Discord controls, independent toggles.
Offline: fake MT5, mocked Discord, the real XAUUSD sample."""
import glob, json, os, sys, threading, time, unittest
from datetime import datetime
from unittest import mock
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_replay import ReplayBase, sample, session, SAMPLE, REAL_SLEEP     # noqa: E402
from test_bot import World, Http                                           # noqa: E402
import config as C, bot, signal_engine, signals, guardian, trade_mgmt, ops, notify   # noqa: E402
from replay import data as D, engine as E, strategies as S, compare, api, report as R   # noqa: E402
from replay.app.host import Host                                          # noqa: E402

REQ = {"symbol": "XAUUSD", "tf": "M30", "start": "2026-10-12", "end": "2026-10-24", "source": "csv",
       "csv_path": SAMPLE, "csv_tz": "utc", "strategy": "ema_20_50_basic", "speed": None}


def cloud_session(ds=None, **kw):
    return session(ds or sample("2026-10-12", "2026-10-24"), strategy=S.make("ema_20_50_basic"), **kw)


class Ema2050(ReplayBase):
    def test_crosses_match_the_shared_ema_on_closed_candles_only(self):
        ds = sample("2026-10-12", "2026-10-24")
        s = cloud_session(ds); s.run()
        got = {e["bar_time"]: e["dir"] for e in s.events if e["type"] == "EMA20_50_CROSS"}
        self.assertGreater(len(got), 3)
        want = {}
        for i in range(ds.first_index, ds.last_index + 1):                 # prefix only: what was known at the close
            w = signal_engine.add_emas(ds.df.iloc[max(0, i - C.WARMUP_BARS):i + 1][["close"]].copy(), (20, 50))
            d = signals._cross_dir(w["ema20"].iloc[-2], w["ema50"].iloc[-2], w["ema20"].iloc[-1], w["ema50"].iloc[-1])
            if d:
                want[ds.df["time"].iloc[i].isoformat()] = d
        self.assertEqual(got, want)
        self.assertTrue(all(e["strategy"] == "ema_20_50_basic" for e in s.events if e["kind"] == "strategy"))

    def test_overlays_on_every_bar_and_in_the_warmup(self):
        s = cloud_session()
        warm = s.warmup_payload()
        self.assertTrue(all("ema20" in b and "ema50" in b for b in warm))
        r = s.step()
        w, _ = s.window(r["index"])
        ref = signal_engine.add_emas(w[["close"]].copy(), (20, 50))
        self.assertAlmostEqual(r["bar"]["ema20"], float(ref["ema20"].iloc[-1]), places=9)
        self.assertNotIn("ema20", session().step()["bar"])                   # EMA 8/32 strategies are unchanged

    def test_entries_exits_and_conditions(self):
        s = cloud_session(); s.run()
        trades = s.broker.closed
        self.assertTrue(trades)
        cross_bars = {e["bar_time"]: e for e in s.events if e["type"] == "EMA20_50_CROSS"}
        spread, slip = s.broker.costs.spread, s.broker.costs.slippage
        df = s.ds.df.set_index(s.ds.df["time"].map(lambda t: t.isoformat()))
        for t in trades:
            self.assertEqual(pd.Timestamp(t.entry_time) - pd.Timestamp(t.decided_time), pd.Timedelta(minutes=30))
            o = float(df.loc[t.entry_time, "open"])
            self.assertAlmostEqual(t.entry_price, round(o + spread + slip if t.side > 0 else o - slip, 2), places=2)
            self.assertTrue(all(c["passed"] for c in t.conditions), t.conditions)
            if t.entry_signal.startswith("EMA20/50 cross"):
                self.assertIn(t.decided_time, cross_bars)
            else:
                self.assertIn("RE-ENTRY", t.entry_signal)
                self.assertEqual([c["name"] for c in t.conditions][:3],
                                 ["cloud still in the same direction", "price closed back inside the cloud after the exit",
                                  "price closed beyond the cloud again"])
            self.assertEqual((t.strategy_version, len(t.param_hash)), ("0.1-experimental", 12))
            self.assertIsNotNone(t.exit_index); self.assertIsNotNone(t.decided_index)
            self.assertGreaterEqual(t.mfe, 0); self.assertGreaterEqual(t.mae, 0)
            self.assertIsNotNone(t.mfe_money)
        desc = s.strategy.describe()
        self.assertTrue(desc["experimental"]); self.assertGreater(len(desc["unverified_rules"]), 5)

    def test_no_lookahead_for_strategy_trades(self):
        ds = sample("2026-10-12", "2026-10-24")
        a = cloud_session(ds); a.run()
        k = ds.first_index + 150
        df2 = ds.df.copy()
        df2.loc[k + 1:, ["open", "high", "low", "close"]] *= 0.95
        ds2 = D.Dataset(ds.symbol, ds.tf, df2, ds.source, ds.server_tz, ds.requested, ds.first_index, ds.last_index)
        b = cloud_session(ds2); b.run(until=k - ds.first_index + 1)
        cut = ds.df["time"].iloc[k].isoformat()
        decided = lambda s: [(t.id, t.decided_time, t.side, t.entry_signal) for t in s.broker.closed + s.broker.open
                             if t.decided_time < cut]
        self.assertEqual(decided(a)[:len(decided(b))], decided(b))
        self.assertTrue(decided(b))


class Debugger(ReplayBase):
    def host(self, req=REQ):
        sent = []
        h = Host(send=sent.append)
        h.handle({"cmd": "start", "request": req})
        end = time.monotonic() + 120
        while time.monotonic() < end and not any(m["type"] == "report" for m in sent):
            h.pump(); REAL_SLEEP(0.02)
        h.pump()
        self.addCleanup(h.shutdown)
        return h, sent

    def wait_paused(self, h, sent):
        end = time.monotonic() + 60
        while time.monotonic() < end and h.ctl.state != "paused":
            h.pump(); REAL_SLEEP(0.02)
        h.pump()

    def test_replay_this_trade_reproduces_it_exactly(self):
        h, sent = self.host()
        trade = h.ctl.session.broker.closed[2]
        before = trade.to_dict()
        reports = sum(m["type"] == "report" for m in sent)
        h.handle({"cmd": "replay_trade", "trade_id": trade.id, "before": 8})
        self.wait_paused(h, sent)
        s = h.ctl.session
        self.assertEqual(s.next - 1, trade.decided_index - 8)               # paused 8 candles before the decision
        self.assertFalse(any(t.id == trade.id for t in s.broker.closed + s.broker.open))
        focus = [m for m in sent if m["type"] == "focus_trade"][-1]
        self.assertEqual(focus["trade_id"], trade.id)
        for _ in range(40):                                                 # step forward candle by candle
            h.ctl.step()
            end = time.monotonic() + 5
            while time.monotonic() < end and (h.ctl.state != "paused" or h.ctl._steps):
                REAL_SLEEP(0.005)
        again = next(t for t in s.broker.closed + s.broker.open if t.id == trade.id).to_dict()
        for k in ("decided_time", "entry_time", "entry_price", "side", "lots", "initial_sl", "tp", "conditions",
                  "param_hash", "strategy_version"):
            self.assertEqual(again[k], before[k], k)
        self.assertEqual(sum(m["type"] == "report" for m in sent), reports)  # the discarded run got no report

    def test_jump_to_a_date_evaluates_up_to_it_and_pauses(self):
        sent = []
        h = Host(send=sent.append); self.addCleanup(h.shutdown)
        h.handle({"cmd": "start", "request": {**REQ, "speed": 0.5}})
        end = time.monotonic() + 30
        while time.monotonic() < end and not (h.ctl and h.ctl.session and h.ctl.session.done):
            h.pump(); REAL_SLEEP(0.02)
        h.handle({"cmd": "jump", "time": "2026-10-20T10:00"})
        self.wait_paused(h, sent)
        s = h.ctl.session
        self.assertEqual(s.ds.df["time"].iloc[s.next - 1], pd.Timestamp("2026-10-20 10:00"))
        self.assertTrue(any(m["type"] == "jump_view" for m in sent))
        bars = [b for m in sent if m["type"] == "batch" for b in m["bars"]]
        self.assertLessEqual(max(b["time"] for b in bars), "2026-10-20T10:00:00")   # nothing after the target


class Comparison(ReplayBase):
    def test_reproducible_and_equal_to_single_runs(self):
        ds = sample("2026-10-12", "2026-10-24")
        a, b = compare.run(ds), compare.run(ds)
        strip = lambda r: [{k: v for k, v in row.items()} for row in r["rows"]]
        self.assertEqual(strip(a), strip(b))
        self.assertEqual(a["data_fingerprint"], b["data_fingerprint"])
        self.assertEqual({r["strategy"] for r in a["rows"]}, set(compare.DEFAULT_SET))
        self.assertIn("C5 Freeze 001", a["not_compared"]); self.assertIn("H1 Horizon One", a["not_compared"])
        single = session(ds, strategy=S.make("ema_cross")); single.run()      # lockstep == alone
        row = next(r for r in a["rows"] if r["strategy"] == "ema_cross" and r["sizing"] == "fixed_1_lot")
        self.assertEqual(row["trades"], len(single.broker.closed))
        self.assertAlmostEqual(row["net"], round(sum(t.net for t in single.broker.closed), 2), places=2)
        files = compare.write(a, os.path.join(C.DATA_DIR, "cmp"))
        md = open(files["summary"], encoding="utf-8").read()
        self.assertIn("experimental", md); self.assertIn("Not compared", md)
        for k in ("trades", "win_rate", "net", "profit_factor", "avg_r", "max_drawdown", "max_consecutive_losses",
                  "param_hash", "version", "costs", "monthly", "exits"):
            self.assertIn(k, row)


class DataAndIsolation(ReplayBase):
    def test_auto_source_falls_back_to_the_cache_during_an_mt5_outage(self):
        mt5 = D.ReadOnlyMT5(sys.modules["MetaTrader5"])
        now_srv = (World.now + World.stamp_offset).replace(tzinfo=None)
        rng = (now_srv - pd.Timedelta(days=3), now_srv - pd.Timedelta(days=1))
        with mock.patch.object(C, "SERVER_UTC_OFFSET_HOURS", 2):
            live = D.load("XAUUSD", "M30", *rng, source="auto", mt5=mt5)       # MT5 up: downloaded and cached
            self.assertEqual(live.source, "mt5")
            World.connected = False; sys.modules["MetaTrader5"].shutdown()    # MT5 down
            cached = D.load("XAUUSD", "M30", *rng, source="auto", mt5=mt5)
            self.assertEqual(cached.source, "cache(mt5)")
            self.assertTrue(cached.problems[0].startswith("MT5 unavailable"))
            self.assertEqual(list(live.df["time"].map(str)), list(cached.df["time"].map(str)))
            self.assertEqual(list(live.df["close"]), list(cached.df["close"]))
            a, b = session(live), session(cached)
            a.run(); b.run()                                                  # replay computation needs no MT5
            self.assertEqual([e["key"] for e in a.events], [e["key"] for e in b.events])
            for p in glob.glob(os.path.join(D.CACHE_DIR, "*")):
                os.remove(p)
            with self.assertRaises(D.DataError):
                D.load("XAUUSD", "M30", *rng, source="auto", mt5=mt5)

    def test_replay_events_go_to_the_session_store_never_the_live_one(self):
        before = ops.live().read()
        p = api.prepare(api.Request(start="2026-10-20", end="2026-10-22", source="csv", csv_path=SAMPLE, csv_tz="utc",
                                    out_dir=os.path.join(C.DATA_DIR, "opsrep")))
        p.session.run(); api.finish(p, 1.0)
        rows = ops.Store("REPLAY", folder=p.folder).read(10000)
        self.assertEqual(len(rows), len(p.session.events))
        self.assertTrue(all(r["mode"] == "REPLAY" and r["replay_session"] == p.session.id for r in rows))
        self.assertEqual(ops.live().read(), before)

    def test_replay_runs_unaffected_while_the_guardian_struggles(self):
        ds = sample()
        ref = session(ds); ref.run()
        stop = threading.Event()

        def chaos():                                                          # the live Guardian in trouble meanwhile
            g = bot.GUARD
            while not stop.is_set():
                g.observe("terminal not connected", attempts=5)
                g.observe("MT5 init failed: (-6, 'auth')")
        t = threading.Thread(target=chaos, daemon=True); t.start()
        try:
            s = session(ds); s.run()
        finally:
            stop.set(); t.join(5)
        self.assertEqual([e["key"] for e in s.events], [e["key"] for e in ref.events])
        code = ("import sys, json; sys.path.insert(0, %r); import replay.api, replay.compare, replay.app.host; "
                "print(json.dumps([m for m in ('guardian', 'terminal', 'bot', 'trader') if m in sys.modules]))"
                % os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import subprocess
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                             env={**os.environ, "AUREON_DATA": os.path.join(C.DATA_DIR, "sub2")})
        self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1]), [], out.stderr[-500:])

    def test_toggles_are_independent(self):
        trade_mgmt.reset(True); self.addCleanup(trade_mgmt.reset, False)
        self.assertFalse(api.Request().trade_management)                      # live ON does not turn replay ON
        s = cloud_session(trade_management=True); s.run()
        self.assertTrue(trade_mgmt.enabled())
        trade_mgmt.reset(False)
        s2 = cloud_session(trade_management=True); s2.run()                   # live OFF does not stop the simulation
        self.assertEqual([t.to_dict() for t in s.broker.closed], [t.to_dict() for t in s2.broker.closed])
        self.assertEqual(World.orders, [])


class DiscordControls(ReplayBase):
    def cmd(self, name, user=42, **kw):
        return bot.handle_json_command({"cmd": name, "ts": World.now.timestamp(), "id": f"{name}{kw}{user}",
                                        "user": user, **kw})

    def test_health_connection_reconnect_and_replay_status(self):
        h = self.cmd("mt5_health")
        self.assertTrue(h["ok"]); self.assertIn("MT5: CONNECTED", h["text"]); self.assertIn("Trade management", h["text"])
        self.assertIn("aureon:tm:enable", json.dumps(h["buttons"]))
        World.connected = False; bot.ensure_mt5(); World.connected = True; bot.ensure_mt5()
        self.assertIn("timeline", self.cmd("mt5_connection")["text"])
        self.assertFalse(self.cmd("mt5_reconnect", user=99)["ok"])           # permission re-checked in the loop
        r = self.cmd("mt5_reconnect")
        self.assertTrue(r["ok"], r); self.assertEqual(World.orders, [])
        if os.path.exists(api.STATUS_FILE):
            os.remove(api.STATUS_FILE)                                         # a fresh PC
        self.assertIn("No replay", self.cmd("replay_status")["text"])
        api.write_status({"state": "running", "session": "rp-x", "symbol": "XAUUSD", "tf": "M30", "strategy": "ema_cross",
                          "version": "1.0", "done": 5, "total": 10, "hist_time": "2026-10-20T10:00:00",
                          "trade_management": False, "discord": {"enabled": False}})
        t = self.cmd("replay_status")["text"]
        self.assertIn("HISTORICAL REPLAY", t); self.assertIn("5/10", t)

    def test_notification_mode_alias_uses_the_same_handlers(self):
        notify.reset(); self.addCleanup(notify.reset)
        self.assertIn("Notification mode: standard", self.cmd("notification_mode")["text"])
        self.assertFalse(self.cmd("notification_mode", user=99, mode="intelligent")["ok"])
        self.assertTrue(self.cmd("notification_mode", mode="intelligent")["ok"])
        self.assertEqual(notify.mode(), "intelligent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
