"""Replay Lab tests: data, engine parity with the live bot, no lookahead, timestamps, controller, isolation,
Discord replay delivery, virtual trades, strategies, reports. Offline: fake MT5 + the real XAUUSD sample."""
import glob, json, os, subprocess, sys, threading, time, unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bot import Base, Http, World                    # noqa: E402  (isolated environment + fake MT5 first)
import fake_mt5                                            # noqa: E402
import config as C, notifier, bot, signals, labels, signal_engine, trade_mgmt   # noqa: E402
from replay import data as D, engine as E, strategies as S, broker as B, report as R, api   # noqa: E402
from replay.discord import ReplayDiscord, card, EventLog  # noqa: E402

REAL_SLEEP = time.sleep                                    # the harness mocks time.sleep in every test
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "tests", "data", "XAUUSD_M30_sample_20260901_20261030.csv")


def sample(start="2026-10-20", end="2026-10-23", tf="M30", **kw):
    return D.load("XAUUSD", tf, datetime.fromisoformat(start), datetime.fromisoformat(end), source="csv",
                  csv_path=SAMPLE, csv_tz="utc", use_cache=False, **kw)


def session(ds=None, **kw):
    return E.ReplaySession(ds or sample(), E.ReplaySettings(**kw))


class ReplayBase(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(C, "SERVER_TZ", "Europe/Athens"); p.start(); self.addCleanup(p.stop)


# ================================================================================================= data
class Data(ReplayBase):
    def test_csv_load_warmup_coverage_and_timezone(self):
        ds = sample()
        cov = ds.coverage()
        self.assertGreaterEqual(ds.warmup_bars, D.MIN_WARMUP)
        self.assertEqual(cov["actual"][0], "2026-10-20 00:00:00")
        self.assertTrue(all(g["kind"] in ("weekend", "daily_break") for g in cov["gaps"]), cov["gaps"])
        row = ds.df.iloc[ds.first_index]
        self.assertEqual(row["utc"], pd.Timestamp("2026-10-19 21:00", tz="UTC"))    # EEST = UTC+3 in October

    def test_dst_change_is_per_candle(self):
        ds = sample("2026-10-23", "2026-10-28")
        before = ds.df[ds.df["time"] == pd.Timestamp("2026-10-23 12:00")].iloc[0]
        after = ds.df[ds.df["time"] == pd.Timestamp("2026-10-27 12:00")].iloc[0]
        self.assertEqual(before["utc"].hour, 9)                  # summer time: server = UTC+3
        self.assertEqual(after["utc"].hour, 10)                  # after 25 Oct: server = UTC+2

    def test_too_little_warmup_is_refused_not_faked(self):
        with self.assertRaises(D.DataError) as e:
            sample("2026-09-05", "2026-09-10")
        self.assertIn("warm-up", str(e.exception))

    def test_range_without_data_is_refused(self):
        with self.assertRaises(D.DataError):
            sample("2026-12-01", "2026-12-05")

    def test_gaps_invalid_rows_and_duplicates_are_reported(self):
        raw = pd.read_csv(SAMPLE)
        raw = raw[~raw["time_utc"].str.startswith("2026-10-21")]          # a missing weekday
        raw.loc[raw.index[-5], "high"] = 1.0                              # high below the body
        raw = pd.concat([raw, raw.iloc[[-3]]])                            # a duplicate
        path = os.path.join(C.DATA_DIR, "gappy.csv"); raw.to_csv(path, index=False)
        ds = D.load("XAUUSD", "M30", datetime(2026, 10, 19), datetime(2026, 10, 30), source="csv", csv_path=path,
                    csv_tz="utc", use_cache=False)
        cov = ds.coverage()
        self.assertEqual(cov["unexpected_gaps"], 1)
        self.assertGreaterEqual(cov["missing_candles_unexpected"], 40)
        self.assertEqual(cov["dropped_rows"], 2)
        self.assertTrue(any("invalid" in p for p in ds.problems) and any("duplicate" in p for p in ds.problems))
        self.assertTrue(any(g["kind"] == "weekend" for g in cov["gaps"]))

    def test_resample_to_h1_h4_and_lower_tf_refused(self):
        h1 = sample("2026-10-12", "2026-10-20", tf="H1")
        self.assertEqual((h1.df["time"].diff().dropna() >= pd.Timedelta(hours=1)).all(), True)
        h4 = sample("2026-10-20", "2026-10-28", tf="H4", warmup=150, min_warmup=150)
        self.assertTrue((h4.df["time"].dt.hour % 4 == 0).all())          # server-time H4 boundaries
        with self.assertRaises(D.DataError):
            sample(tf="M15")

    def test_mt5_download_excludes_the_forming_candle_and_is_read_only(self):
        mt5 = D.ReadOnlyMT5(sys.modules["MetaTrader5"])
        now_srv = (World.now + World.stamp_offset).replace(tzinfo=None)
        df = D.from_mt5("XAUUSD", "M30", now_srv - timedelta(days=2), now_srv + timedelta(hours=1), mt5=mt5,
                        now_server=now_srv)
        self.assertTrue((df["time"] + pd.Timedelta(minutes=30) <= now_srv).all())
        self.assertGreaterEqual(len(df), 1000 + 90)
        for name in ("order_send", "order_check", "positions_get"):
            with self.assertRaises(PermissionError):
                getattr(mt5, name)

    def test_cache_roundtrip(self):
        ds = D.load("XAUUSD", "M30", datetime(2026, 10, 20), datetime(2026, 10, 23), source="csv", csv_path=SAMPLE,
                    csv_tz="utc", use_cache=True)
        cached = D.load_cache("XAUUSD", "M30", ds.source, D.server_tz())
        self.assertEqual(len(cached), 1943)
        self.assertIsNone(D.load_cache("XAUUSD", "M30", ds.source, "UTC+5"))   # another server clock: not reused


# ============================================================================================= engine
class EngineParity(ReplayBase):
    def test_same_events_as_the_live_bot_candle_by_candle(self):
        """Drive the REAL bot.check_ema over fake-MT5 candles, then replay the same candles: identical events."""
        World.now = datetime(2026, 11, 10, 10, 30, 5, tzinfo=timezone.utc)
        bot.check_ema("M30")                                     # first run: sets the checkpoint
        first_ckpt = pd.Timestamp(bot.state.last_bar("M30", "XAUUSD"))
        keys0 = set(bot.state.d["keys"])
        for _ in range(120):
            World.advance(minutes=30); bot.check_ema("M30")
        live = {k for k in set(bot.state.d["keys"]) - keys0 if k.startswith("XAUUSD:M30:")}
        last_ckpt = pd.Timestamp(bot.state.last_bar("M30", "XAUUSD"))
        self.assertGreater(len(live), 20)
        mt5 = D.ReadOnlyMT5(sys.modules["MetaTrader5"])
        start = (first_ckpt + pd.Timedelta(minutes=30)).to_pydatetime()
        end = (last_ckpt + pd.Timedelta(minutes=30)).to_pydatetime()
        with mock.patch.object(C, "SERVER_UTC_OFFSET_HOURS", 2):
            ds = D.load("XAUUSD", "M30", start, end, source="mt5", mt5=mt5, use_cache=False)
        s = session(ds)
        s.run()
        self.assertEqual(s.done, 120)
        replay = {e["key"] for e in s.events if e["kind"] == "signal"}
        self.assertEqual(replay, live)

    def test_no_lookahead_future_candles_cannot_change_past_events(self):
        ds = sample()
        a = session(ds); a.run()
        k = ds.first_index + 60
        df2 = ds.df.copy()
        df2.loc[k + 1:, ["open", "high", "low", "close"]] *= 1.07          # a completely different future
        ds2 = D.Dataset(ds.symbol, ds.tf, df2, ds.source, ds.server_tz, ds.requested, ds.first_index, ds.last_index)
        b = session(ds2); b.run(until=61)
        cut = ds.df["time"].iloc[k]
        past = lambda s: [(e["key"], e["price"]) for e in s.events if pd.Timestamp(e["bar_time"]) <= cut]
        self.assertEqual(past(a), past(b))
        w, _ = a.window(k)
        self.assertEqual(w["time"].iloc[-1], cut)                       # the window ends at the evaluated candle

    def test_indicator_values_match_the_shared_functions_and_candle_alignment(self):
        ds = sample()
        s = session(ds)
        res = s.step()
        i = ds.first_index
        ref = signal_engine.add_indicators(ds.df.iloc[max(0, i - C.WARMUP_BARS):i + 1][["time", "open", "high", "low", "close"]].copy())
        self.assertAlmostEqual(res["bar"]["ema200"], float(ref["ema200"].iloc[-1]), places=9)
        self.assertEqual(res["bar"]["time"], ds.df["time"].iloc[i].isoformat())
        t = res["times"]
        self.assertEqual(pd.Timestamp(t["close_time"]) - pd.Timestamp(t["bar_time"]), pd.Timedelta(minutes=30))

    def test_events_carry_historical_times_session_and_label(self):
        s = session(); s.run()
        e = next(x for x in s.events if x["kind"] == "signal")
        self.assertEqual(e["label"], "HISTORICAL REPLAY"); self.assertEqual(e["session"], s.id)
        self.assertEqual(pd.Timestamp(e["close_time"]) - pd.Timestamp(e["bar_time"]), pd.Timedelta(minutes=30))
        self.assertTrue(e["close_utc"].endswith("+00:00")); self.assertIn("IST", e["close_ist"])
        self.assertEqual([x["seq"] for x in s.events], list(range(1, len(s.events) + 1)))
        self.assertEqual(sorted(s.events, key=lambda x: x["bar_time"]), s.events)        # historical order

    def test_restart_isolation_and_no_live_state(self):
        before = sorted(os.listdir(C.STATE_DIR))
        keys = list(bot.state.d["keys"])
        ds = sample()
        a, b = session(ds), session(ds)
        for _ in range(40):
            a.step(); b.step()
        b.run(); a.run()
        strip = lambda s: [{k: v for k, v in e.items() if k not in ("session", "key")} for e in s.events]
        self.assertEqual(strip(a), strip(b)); self.assertNotEqual(a.id, b.id)
        self.assertEqual(sorted(os.listdir(C.STATE_DIR)), before)
        self.assertEqual(bot.state.d["keys"], keys)
        self.assertEqual(World.orders, [])

    def test_notification_mode_does_not_reach_the_replay(self):
        import notify
        notify.reset(); notify.ns()["mode"] = "intelligent"
        self.addCleanup(notify.reset)
        s = session(); s.run()
        self.assertEqual(Http.sent, [])                                   # nothing delivered through the live path
        self.assertTrue(all(e["severity"] in ("low", "normal", "high", "critical") for e in s.events))


class Controller(ReplayBase):
    def test_pause_resume_step_speed_stop_restart(self):
        ds = sample()
        results, states = [], []
        c = E.ReplayController(lambda: session(ds), on_result=results.append, on_state=lambda st, _: states.append(st),
                               speed=1000)
        c.start(paused=True)
        REAL_SLEEP(0.05); self.assertEqual(results, [])
        c.step(); c.step(2)
        for _ in range(200):
            if len(results) == 3:
                break
            REAL_SLEEP(0.01)
        self.assertEqual([r["index"] for r in results], [ds.first_index + k for k in range(3)])
        c.resume(); REAL_SLEEP(0.1); c.pause()
        REAL_SLEEP(0.1)                                                   # a candle in evaluation finishes (atomic)
        n = len(results); REAL_SLEEP(0.1)
        self.assertEqual(len(results), n)                                 # paused: nothing moves
        self.assertGreater(n, 3)
        first_id = c.session.id
        c.restart(paused=True)
        self.assertNotEqual(c.session.id, first_id); self.assertEqual(c.session.done, 0)
        c.set_speed(None); c.resume()
        self.assertTrue(c.wait(60))
        self.assertEqual(c.state, "finished"); self.assertTrue(c.session.finished)
        self.assertIn("paused", states); self.assertIn("running", states)
        c.stop(); self.assertEqual(c.state, "finished")

    def test_enhanced_animation_needs_genuine_lower_timeframe_data(self):
        s = session()
        self.assertEqual(s.forming(s.next), [])                          # OHLC only: nothing invented
        t0 = s.ds.df["time"].iloc[s.next]
        m1 = pd.DataFrame({"time": [t0 + pd.Timedelta(minutes=k) for k in range(30)],
                           "open": 1.0, "high": [2.0 + k for k in range(30)], "low": 0.5, "close": 1.5})
        s.intrabar = m1
        frames = s.forming(s.next)
        self.assertEqual(len(frames), 30)
        self.assertEqual(frames[4]["high"], 6.0); self.assertTrue(all(f["forming"] for f in frames))


# ============================================================================================ discord
class ReplayDiscordDelivery(ReplayBase):
    def evs(self, n=10):
        s = session(); s.run(until=40)
        return s, s.events[:n] if n else s.events

    def test_disabled_by_default_but_the_local_log_is_kept(self):
        self.assertFalse(C.REPLAY_DISCORD_ENABLED)
        s = session(); rd = ReplayDiscord(); log = EventLog(os.path.join(C.DATA_DIR, "rlog"))
        s.sinks += [log, rd]; s.run(); log.close()
        self.assertEqual(rd.status()["attempted"], 0); self.assertEqual(Http.sent, [])
        with open(log.path) as f:
            self.assertEqual(len(f.readlines()), len(s.events))

    def test_never_the_live_channel(self):
        with mock.patch.object(C, "REPLAY_DISCORD_WEBHOOK", C.DISCORD_WEBHOOK), mock.patch.object(C, "REPLAY_DISCORD_ENABLED", True):
            rd = ReplayDiscord()
        self.assertFalse(rd.enabled); self.assertIn("LIVE", rd.problem)

    def test_cards_are_marked_ordered_and_rate_limited(self):
        posted, slept, clock = [], [], [0.0]
        def post(payload):
            posted.append(payload); return "ok"
        def sleep(x):
            slept.append(x); clock[0] += x
        rd = ReplayDiscord(webhook="https://discord.test/api/webhooks/9/replay", enabled=True, rate=5, post=post,
                           clock=lambda: clock[0], sleep=sleep)
        s = session(); s.sinks.append(rd); s.run(until=60)
        st = rd.finish()
        self.assertEqual(st["delivered"], st["attempted"]); self.assertEqual(st["failed"], 0)
        self.assertEqual(st["delivered"], len(s.events))
        self.assertEqual(rd.order, sorted(rd.order))                     # historical order
        for p in posted:
            emb = p["embeds"][0]
            self.assertIn("HISTORICAL REPLAY", emb["title"]); self.assertNotIn("content", p)   # no mention
            self.assertIn("Historical candle close", [f["name"] for f in emb["fields"]])
            self.assertIn(s.id, json.dumps(emb))
        self.assertAlmostEqual(sum(slept), (len(posted) - 1) / 5, delta=0.01)

    def test_overflow_becomes_ordered_digests_and_failures_are_counted(self):
        gate, posted = threading.Event(), []
        def post(payload):
            gate.wait(5); posted.append(payload); return "failed" if len(posted) == 2 else "ok"
        rd = ReplayDiscord(webhook="https://discord.test/api/webhooks/9/replay", enabled=True, rate=1000,
                           max_queue=5, post=post)
        s = session(); s.sinks.append(rd); s.run(until=80)
        n = len(s.events)
        gate.set()
        st = rd.finish()
        self.assertGreater(st["digests"], 0)
        self.assertEqual(st["events_in_digests"] + (st["attempted"] - st["digests"]), n)   # every event accounted
        self.assertEqual(st["failed"], 1); self.assertEqual(st["delivered"], st["attempted"] - 1)
        self.assertEqual(rd.order, sorted(rd.order))
        self.assertTrue(any("DIGEST" in p["embeds"][0]["title"] for p in posted))
        self.assertEqual(len(s.events), n)                                 # the local record is untouched

    def test_card_reuses_the_live_card_format(self):
        s, evs = self.evs(0)
        sig = next(e for e in evs if e["kind"] == "signal")
        c = card(sig)
        self.assertTrue(c["title"].startswith("🕰️ HISTORICAL REPLAY · XAUUSD"))
        self.assertIn("Market now", [f[0] for f in c["fields"]])           # notifier.format_event's field
        self.assertFalse(c["ping"])


# =========================================================================================== trading
class VirtualTrading(ReplayBase):
    def bar(self, o, h, l, c, t="2026-10-20T10:00:00"):
        return {"time": t, "open": o, "high": h, "low": l, "close": c}

    def broker(self, **kw):
        return B.VirtualBroker("XAUUSD", B.spec_for("XAUUSD"), B.Costs(0.3, 7.0, 0.05), 50000, **kw)

    def test_trade_management_defaults_to_false_and_is_independent_of_the_live_switch(self):
        self.assertFalse(C.REPLAY_TRADE_MANAGEMENT_ENABLED)
        self.assertFalse(E.ReplaySettings().trade_management)
        self.assertFalse(api.Request().trade_management)
        trade_mgmt.reset(True); self.addCleanup(trade_mgmt.reset, False)
        self.assertFalse(session().broker.trade_management)               # live ON does not turn the replay ON
        s = session(trade_management=True); s.run()
        self.assertTrue(trade_mgmt.enabled())                              # and the replay never touches the live one

    def test_ohlc_ambiguity_stop_first_gaps_and_ask_side(self):
        b = self.broker()
        b.submit([B.Intent("open", side=1, lots=1.0, sl_dist=5, tp_dist=5)], "t0", "test")
        b.on_open(1, self.bar(4000, 4001, 3999, 4000), "test")
        t = b.open[0]
        self.assertAlmostEqual(t.entry_price, 4000.35)                    # ask + slippage
        b.on_bar(2, self.bar(4000, 4010, 3990, 4000))                     # both levels inside one candle
        self.assertTrue(t.ambiguous); self.assertIn("stop assumed first", t.exit_reason)
        self.assertAlmostEqual(t.exit_price, 3995.30)                     # stop - slippage
        b.submit([B.Intent("open", side=1, lots=1.0, sl=3990, tp=4020)], "t1", "test")
        b.on_open(3, self.bar(4000, 4001, 3999, 4000), "test")
        b.on_bar(4, self.bar(3980, 3985, 3975, 3980))                     # gap through the stop
        self.assertEqual(b.closed[-1].exit_reason, "SL (gap)"); self.assertAlmostEqual(b.closed[-1].exit_price, 3979.95)
        b.submit([B.Intent("open", side=-1, lots=1.0, sl=4010, tp=3990)], "t2", "test")
        b.on_open(5, self.bar(4000, 4001, 3999, 4000), "test")
        b.on_bar(6, self.bar(4000, 4009.8, 3995, 4000))                   # ask = 4010.1 reaches the stop
        self.assertEqual(b.closed[-1].exit_reason, "SL")
        money = b.closed[0]
        self.assertAlmostEqual(money.gross, (3995.30 - 4000.35) * 100, places=2)  # tick 0.01 = $1 per lot
        self.assertEqual(money.commission, 7.0)

    def test_trailing_stop_is_simulated_only_when_enabled(self):
        ds = sample("2026-10-27", "2026-10-31")
        trade = [{"time": "2026-10-29 09:00", "side": "sell", "sl": 4120.0}]
        runs = {}
        for tm in (False, True):
            s = session(ds, strategy=S.make("manual", protective={"tp_mode": "none"}, trades=trade), trade_management=tm)
            s.run()
            runs[tm] = s.broker.closed[0]
        self.assertEqual(runs[False].sl_moves, []); self.assertTrue(runs[False].blocked_steps)
        self.assertTrue(runs[True].sl_moves); self.assertEqual(runs[True].blocked_steps, [])
        self.assertTrue(all(m["new"] < m["old"] for m in runs[True].sl_moves))    # a short's stop only goes down
        self.assertEqual(World.orders, [])

    def test_strategy_entries_fill_at_the_next_open_and_a_cross_is_not_an_entry_by_default(self):
        ds = sample()
        s = session(ds); s.run()
        self.assertFalse(any(e["kind"] == "virtual" for e in s.events))   # indicators only: no trades
        s = session(ds, strategy=S.make("ema_cross")); s.run()
        crosses = {e["bar_time"]: e for e in s.events if e["type"] in S.CROSS_TYPES}
        for t in s.broker.closed:
            decided = pd.Timestamp(t.decided_time)
            self.assertEqual(pd.Timestamp(t.entry_time), decided + pd.Timedelta(minutes=30))   # next candle
            self.assertIn(t.decided_time, crosses)
        self.assertTrue(s.broker.closed)

    def test_risk_sizing_uses_the_symbol_spec(self):
        z = S.Sizing(mode="risk", risk_pct=1.0)
        self.assertEqual(z.lots_for(50000, 10.0, B.spec_for("XAUUSD")), 0.5)     # $500 / ($10 x 100)
        self.assertEqual(S.Sizing().lots_for(50000, None, B.spec_for("XAUUSD")), 1.0)
        self.assertEqual(S.Sizing(mode="step", step_equity=25000).lots_for(60000, None, B.spec_for("XAUUSD")), 2.0)


# ============================================================================================ safety
class Safety(unittest.TestCase):
    def test_replay_never_imports_the_live_order_or_command_path(self):
        code = ("import sys, json; sys.path.insert(0, %r); import replay.api, replay.engine, replay.discord, "
                "replay.report, replay.strategies, replay.broker; "
                "print(json.dumps(sorted(m for m in ('bot','trader','alerts','positions','weekend','monitor') "
                "if m in sys.modules)))" % ROOT)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                             env={**os.environ, "AUREON_DATA": os.path.join(C.DATA_DIR, "sub")})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1]), [])

    def test_no_order_call_in_the_replay_source(self):
        for path in glob.glob(os.path.join(ROOT, "replay", "**", "*.py"), recursive=True):
            src = open(path, encoding="utf-8").read()
            for bad in ("order_send", "order_check", "import trader", "import bot", "import alerts",
                        "import positions", "modify_sl", "close_position"):
                self.assertNotIn(bad, src, f"{bad} in {path}")

    def test_execution_mode_must_be_simulation(self):
        with mock.patch.object(C, "REPLAY_EXECUTION_MODE", "live"):
            self.assertTrue(C.replay_problems())
            with self.assertRaises(ValueError):
                E.ReplaySession(sample())


# ============================================================================================ report
class Reports(ReplayBase):
    def test_indicator_replay_has_no_profit_claims(self):
        s = session(); t0 = time.monotonic(); s.run()
        rep = R.build(s, wall_seconds=time.monotonic() - t0)
        self.assertIsNone(rep["performance"])
        self.assertIn("no profit or loss", rep["notice"])
        files = R.write(rep, s, os.path.join(C.DATA_DIR, "rep1"))
        self.assertNotIn("trades", files)
        md = open(files["summary"], encoding="utf-8").read()
        self.assertNotIn("Net", md); self.assertIn("Events by type", md)
        self.assertEqual(json.load(open(files["json"]))["candles_processed"], s.done)

    def test_strategy_report_has_monthly_drawdown_equity_and_attribution(self):
        req = api.Request(start="2026-10-01", end="2026-10-31", source="csv", csv_path=SAMPLE, csv_tz="utc",
                          strategy="ema_cross", out_dir=os.path.join(C.DATA_DIR, "rep2"))
        p = api.prepare(req)
        p.session.run()
        rep, files = api.finish(p, 1.0)
        perf = rep["performance"]
        self.assertEqual(perf["trades"], len(p.session.broker.closed))
        self.assertEqual(perf["start_balance"], 50000.0)
        self.assertEqual([m["month"] for m in perf["monthly"]], ["2026-10"])
        self.assertGreaterEqual(perf["max_drawdown"], 0)
        self.assertEqual(sum(v["trades"] for v in perf["by_signal"].values()), perf["trades"])
        self.assertEqual(sum(perf["distribution"].values()), perf["trades"])
        for k in ("trades", "equity", "monthly", "events", "json", "summary", "gaps"):
            self.assertTrue(os.path.exists(files[k]), k)
        self.assertEqual(rep["data"]["requested"], ["2026-10-01 00:00:00", "2026-10-31 00:00:00"])
        self.assertFalse(rep["discord"]["enabled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
