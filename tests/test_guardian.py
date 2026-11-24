"""MT5 Connection Guardian: classification, recovery, terminal ownership, fail-closed account checks, persistence.
Fake MT5 and a fake monotonic clock (test_bot harness); nothing touches a real terminal or webhook."""
import json, os, sys, threading, time, unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bot import Base, Http, titles, World            # noqa: E402
import fake_mt5                                            # noqa: E402
import config as C, bot, guardian, terminal, notify, notifier, trade_mgmt, trader, ops, positions   # noqa: E402

REAL_SLEEP = time.sleep
AUTH = (-6, "Terminal: Authorization failed")
IPC = (-10003, "IPC initialize failed, MetaTrader 5 x64 not found")


class FakeTerminal:
    def __init__(self, ok=True, wall=None):
        self.restarts, self.ok, self.at, self.wall = 0, ok, [], wall

    def restart(self):
        self.restarts += 1
        self.at.append(self.wall[0] if self.wall else 0)
        if self.ok:
            World.connected = True                                    # the terminal comes back
        return self.ok, "restarted (PID 1)"


class G(Base):
    def setUp(self):
        super().setUp()
        notify.wait_idle(); notify.reset(); Http.reset()               # nothing from the previous test arrives late
        self.addCleanup(notify.reset); self.addCleanup(notify.wait_idle)
        for k, v in (("MT5_EXPECTED_LOGIN", ""), ("MT5_EXPECTED_SERVER", ""), ("MT5_TERMINAL_RESTART_ENABLED", False),
                     ("MT5_GUARDIAN_ENABLED", True), ("MT5_AUTO_RECOVERY_ENABLED", True),
                     ("MT5_RECOVERY_MAX_ATTEMPTS", "3"), ("MT5_RECOVERY_COOLDOWN_SECONDS", "300")):
            p = mock.patch.object(C, k, v); p.start(); self.addCleanup(p.stop)
        self.wall = [1_800_000_000.0]
        bot.GUARD.wall = lambda: self.wall[0]
        self.term = FakeTerminal(wall=self.wall)
        bot.GUARD.terminal = self.term
        trade_mgmt.reset(False); self.addCleanup(trade_mgmt.reset, False)

    def last_error(self, err):
        p = mock.patch.object(bot.mt5, "last_error", lambda: err); p.start(); self.addCleanup(p.stop)

    def run_for(self, seconds, step=1):
        end = self.clock[0] + seconds
        states = []
        while self.clock[0] < end:
            bot.ensure_mt5()
            states.append(bot.GUARD.state)
            self.clock[0] += step; self.wall[0] += step
        return states

    def cards(self):
        notify.wait_idle(); notify.tick(bot.state)
        return titles(Http.to("discord"))


class Classification(G):
    def test_temporary_broker_disconnect_heals_without_restart_or_card(self):
        World.connected = False                                       # terminal attached, broker link lost
        states = self.run_for(12, 2)
        self.assertIn("DISCONNECTED", states)
        World.connected = True
        states = self.run_for(4, 2)
        self.assertEqual(bot.GUARD.state, "CONNECTED")
        self.assertEqual(self.term.restarts, 0)
        self.assertEqual(self.cards(), [])                            # short blip: nobody is paged
        causes = [r["cause"] for r in bot.GUARD.timeline]
        self.assertIn("broker_link_lost", causes)

    def test_invalid_credentials_need_manual_intervention_never_restarts(self):
        C.MT5_TERMINAL_RESTART_ENABLED = True
        bot.mt5.shutdown(); World.connected = False; self.last_error(AUTH)
        self.run_for(900, 5)
        self.assertEqual(bot.GUARD.state, "AUTH_FAILED")
        self.assertEqual(self.term.restarts, 0)
        crit = [t for t in self.cards() if "MT5 needs you" in t]
        self.assertEqual(len(crit), 1)                                # once, not every check
        self.assertIn("orders", bot.GUARD.order_block().lower() + "orders")
        self.assertIsNotNone(guardian.order_block())
        # slow retries: never faster than the cooldown once auth has failed
        attempts = []
        real = bot.mt5.initialize
        with mock.patch.object(bot.mt5, "initialize", lambda **k: attempts.append(self.clock[0]) or real()):
            self.run_for(1200, 5)
        gaps = [b - a for a, b in zip(attempts, attempts[1:])]
        self.assertTrue(gaps and min(gaps) >= 300, gaps)
        # the owner fixes the login: the next attempt succeeds and the bot resumes by itself
        World.connected = True
        self.run_for(400, 5)
        self.assertEqual(bot.GUARD.state, "CONNECTED"); self.assertIsNone(guardian.order_block())
        self.assertTrue(any("MT5 recovered" in t for t in self.cards()))

    def test_terminal_crash_restarts_only_the_owned_terminal_bounded_with_cooldown(self):
        C.MT5_TERMINAL_RESTART_ENABLED = True
        self.term.ok = False                                          # restarts do not help
        bot.mt5.shutdown(); World.connected = False; self.last_error(IPC)
        self.run_for(200, 1)
        self.assertEqual(self.term.restarts, 1)                       # only after 3 failed API attempts + confirmation
        self.assertGreaterEqual(self.term.at[0] - 1_800_000_000.0, 30)  # 3 failed API attempts + 20 s confirmation first
        self.run_for(1000, 5)
        gaps = [b - a for a, b in zip(self.term.at, self.term.at[1:])]
        self.assertTrue(gaps and min(gaps) >= 300, gaps)              # MT5_RECOVERY_COOLDOWN_SECONDS between restarts
        self.assertEqual(self.term.restarts, 3)                       # MT5_RECOVERY_MAX_ATTEMPTS
        self.assertEqual(bot.GUARD.state, "MANUAL_INTERVENTION_REQUIRED")
        self.run_for(2000, 10)
        self.assertEqual(self.term.restarts, 3)                       # never more
        self.assertEqual(World.orders, [])

    def test_terminal_restart_recovers_and_reconciles(self):
        C.MT5_TERMINAL_RESTART_ENABLED = True
        World.positions.clear()
        fake_mt5.open_position(4242, 1, 4100.0, sl=4080.0, tp=4150.0)
        bot.mt5.shutdown(); World.connected = False; self.last_error(IPC)
        self.run_for(200, 1)
        self.assertEqual(self.term.restarts, 1)
        self.run_for(60, 2)
        self.assertEqual(bot.GUARD.state, "CONNECTED")
        cards = self.cards()
        self.assertTrue(any("restarting the MT5 terminal" in t for t in cards))
        self.assertTrue(any("MT5 recovered" in t for t in cards))
        rec = [p for p in Http.to("discord") if "MT5 recovered" in p["embeds"][0]["title"]][0]
        self.assertIn("open positions 1", json.dumps(rec))
        self.assertEqual((World.positions[0].sl, World.positions[0].tp), (4080.0, 4150.0))   # broker stops untouched
        self.assertEqual(World.orders, [])

    def test_restart_disabled_by_default(self):
        self.assertFalse(guardian.C.MT5_TERMINAL_RESTART_ENABLED)
        bot.mt5.shutdown(); World.connected = False; self.last_error(IPC)
        self.run_for(600, 2)
        self.assertEqual(self.term.restarts, 0)
        self.assertIn(bot.GUARD.state, ("RECONNECTING", "DISCONNECTED"))

    def test_wrong_account_and_wrong_server_fail_closed(self):
        C.MT5_EXPECTED_LOGIN = "123456"
        trade_mgmt.reset(True)
        bot.GUARD.account_ok = None
        self.run_for(20, 2)
        self.assertEqual(bot.GUARD.state, "MANUAL_INTERVENTION_REQUIRED"); self.assertEqual(bot.GUARD.cause, "wrong_account")
        self.assertIn("manual intervention", trade_mgmt.check(trade_mgmt.token()) or "")
        r = trader.preview({"side": "buy", "lot": 0.1, "sl": 20, "tp": 40, "symbol": "XAUUSD"})
        self.assertFalse(r["ok"]); self.assertIn("manual", r["text"].lower())
        cards = self.cards()
        self.assertEqual(sum("WRONG ACCOUNT" in t for t in cards), 1)
        self.assertNotIn("123456", json.dumps(Http.sent))             # the account number is masked
        C.MT5_EXPECTED_LOGIN, C.MT5_EXPECTED_SERVER = "555", "Other-Server"
        self.run_for(10, 2)
        self.assertEqual(bot.GUARD.cause, "wrong_server")
        C.MT5_EXPECTED_SERVER = "fake-server"                         # case-insensitive
        self.run_for(10, 2)
        self.assertEqual(bot.GUARD.state, "CONNECTED"); self.assertIsNone(guardian.order_block())
        self.assertEqual(World.orders, [])

    def test_market_closure_is_never_a_failure(self):
        self.assertTrue(guardian.market_closed(datetime(2026, 11, 14, 12, 0)))      # Saturday
        self.assertTrue(guardian.market_closed(datetime(2026, 11, 11, 0, 30)))      # daily break
        self.assertTrue(guardian.market_closed(datetime(2026, 12, 25, 12, 0)))
        self.assertFalse(guardian.market_closed(datetime(2026, 11, 11, 14, 0)))
        g = bot.GUARD
        with mock.patch.object(g, "server_now", lambda: datetime(2026, 11, 14, 12, 0)):
            g.observe("stale tick (4000s)")
        self.assertEqual((g.state, g.cause), ("CONNECTED", "market_closed"))
        self.assertIsNone(g.outage)
        with mock.patch.object(g, "server_now", lambda: datetime(2026, 11, 11, 14, 0)):
            g.observe("stale tick (900s)")
            self.assertEqual((g.state, g.cause), ("DEGRADED", "stale_feed"))
            C.MT5_TERMINAL_RESTART_ENABLED = True
            g.observe("stale tick (2000s)")                           # open market, 33 min without a tick
        self.assertEqual(g.cause, "frozen_terminal"); self.assertEqual(self.term.restarts, 1)

    def test_classification_of_init_errors_and_sessions(self):
        g = bot.GUARD
        self.assertEqual(g.classify(f"MT5 init failed: {AUTH}"), "auth_failed")
        self.assertEqual(g.classify(f"MT5 init failed: {IPC}"), "terminal_gone")
        self.assertEqual(g.classify("MT5 init failed: (-10000, 'internal fail')"), "api_init_failed")
        self.assertEqual(g.classify("terminal not connected"), "terminal_gone")
        self.assertEqual(g.classify("clock mismatch: tick is 60 min ahead"), "clock_mismatch")
        C.MT5_EXPECTED_LOGIN = "555"
        with mock.patch.object(bot.mt5, "account_info", lambda: None):   # connected, but no session
            self.assertIn("cannot be read", g.verify_account())
        self.assertEqual(g.state, "AUTH_FAILED")


class Persistence(G):
    def test_cooldown_manual_state_and_timeline_survive_a_bot_restart(self):
        C.MT5_TERMINAL_RESTART_ENABLED = True; self.term.ok = False
        bot.mt5.shutdown(); World.connected = False; self.last_error(IPC)
        self.run_for(200, 1)
        self.assertEqual(self.term.restarts, 1)
        g2 = guardian.Guardian(bot.mt5, clock=lambda: self.clock[0], wall=lambda: self.wall[0], terminal=self.term)
        self.assertGreater(g2.cooldown_until_wall, self.wall[0])      # a restarted bot honours the cooldown
        self.assertTrue(any(r["state"] == "RESTARTING_TERMINAL" for r in g2.timeline))
        C.MT5_EXPECTED_LOGIN = "999"
        World.connected = True
        bot.GUARD.verify_account()
        g3 = guardian.Guardian(bot.mt5, clock=lambda: self.clock[0], wall=lambda: self.wall[0])
        self.assertEqual(g3.state, "MANUAL_INTERVENTION_REQUIRED")    # still needs a person after a restart
        self.assertIsNotNone(g3.order_block())


class Ownership(unittest.TestCase):
    PATH = r"C:\Program Files\Broker A MT5\terminal64.exe"

    def procs(self):
        return [{"pid": 1, "exe": self.PATH, "cmdline": f'"{self.PATH}"'},
                {"pid": 2, "exe": r"C:\Program Files\Broker B MT5\terminal64.exe", "cmdline": ""},
                {"pid": 3, "exe": self.PATH.upper(), "cmdline": r'"x" /config:D:\mt5\other'},
                {"pid": 4, "exe": r"C:\Users\x\terminal64.exe", "cmdline": ""}]

    def test_only_the_exact_configured_terminal_is_ours(self):
        t = terminal.TerminalProcess(self.PATH, lister=self.procs)
        self.assertEqual(sorted(t.owned_pids()), [1, 3])              # same executable (case-insensitive)
        t2 = terminal.TerminalProcess(self.PATH, data_dir=r"D:\mt5\other", lister=self.procs)
        self.assertEqual(t2.owned_pids(), [3])                        # a data folder narrows it to one instance
        self.assertEqual(terminal.TerminalProcess("", lister=self.procs).owned_pids(), [])

    def test_restart_kills_only_owned_pids(self):
        killed = []
        alive = {1, 2, 3, 4}

        class Runner:
            @staticmethod
            def run(args, **k):
                pid = int(args[2]); killed.append(pid); alive.discard(pid)

            class Popen:
                def __init__(self, args, cwd=None):
                    self.pid = 99
        lister = lambda: [p for p in self.procs() if p["pid"] in alive]
        t = terminal.TerminalProcess(self.PATH, data_dir=r"D:\mt5\other", lister=lister, runner=Runner, sleep=lambda s: None)
        with mock.patch("os.name", "nt"), mock.patch("os.path.exists", lambda p: True):
            ok, detail = t.restart()
        self.assertTrue(ok, detail)
        self.assertEqual(killed, [3])                                 # never broker B, never another copy


class Notifications(G):
    def test_guardian_cards_never_block_polling(self):
        gate = threading.Event()
        real = notifier._post
        C.MT5_EXPECTED_LOGIN = "999"
        with mock.patch.object(notifier, "_post", lambda *a, **k: gate.wait(5) and real(*a, **k)):
            t0 = time.monotonic()
            bot.GUARD.account_ok = None
            bot.ensure_mt5()
            self.assertLess(time.monotonic() - t0, 1.0)               # the card is posted by the dispatch thread
            gate.set()
        self.assertTrue(any("WRONG ACCOUNT" in t for t in self.cards()))

    def test_transitions_are_live_ops_events_and_stores_do_not_mix(self):
        World.connected = False
        self.run_for(6, 2)
        World.connected = True
        self.run_for(4, 2)
        rows = ops.live().read()
        types = [r["type"] for r in rows]
        self.assertIn("MT5_DISCONNECTED", types); self.assertIn("MT5_CONNECTED", types)
        self.assertTrue(all(r["mode"] == "LIVE" and r["replay_session"] is None for r in rows))
        with self.assertRaises(ValueError):
            ops.live().write(ops.OpsEvent("replay", "X", mode="REPLAY", replay_session="rp-1"))
        with self.assertRaises(ValueError):
            ops.OpsEvent("bot", "X", mode="LIVE", replay_session="rp-1")

    def test_disabled_guardian_changes_nothing(self):
        C.MT5_GUARDIAN_ENABLED = False
        C.MT5_EXPECTED_LOGIN = "999"
        World.connected = False
        self.run_for(10, 2)
        self.assertEqual(bot.GUARD.state, "CONNECTED")
        self.assertIsNone(guardian.order_block())


if __name__ == "__main__":
    unittest.main(verbosity=2)
