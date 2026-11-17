"""1.7.2: MT5 reconnect without noise, and the cross alert ahead of everything slow.
Same isolated harness as test_bot.py: fake MT5, fake webhooks, temp data folder, a fake monotonic clock."""
import os, sys, unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bot import Base, Http, titles, World          # noqa: E402  (sets up the isolated environment first)
import config as C, signals, notifier, bot, chart, store   # noqa: E402


class Reconnect(Base):
    def lose_link(self):
        """The terminal stays attached but loses the broker (terminal_info().connected == False)."""
        World.connected = False

    def init_counter(self):
        real = bot.mt5.initialize
        calls = []

        def counted():
            calls.append(self.clock[0]); return real()
        p = mock.patch.object(bot.mt5, "initialize", counted); p.start(); self.addCleanup(p.stop)
        return calls

    def test_no_initialize_during_the_30s_grace_window(self):
        calls = self.init_counter()
        self.lose_link()
        start = self.clock[0]
        while self.clock[0] - start < C.RECONNECT_GRACE_SECONDS:
            self.assertEqual(bot.ensure_mt5(), signals.LINK_LOST)
            self.assertEqual(bot.reconnect_wait(), C.RECONNECT_POLL_SECONDS)   # poll terminal_info() every 2 s
            self.clock[0] += C.RECONNECT_POLL_SECONDS
        self.assertEqual(calls, [])                                   # not one initialize() inside the window
        bot.ensure_mt5()                                              # 30 s are up: the first real attempt
        self.assertEqual(calls, [start + C.RECONNECT_GRACE_SECONDS])

    def test_backoff_5_10_20_40_after_the_window_and_warnings_only(self):
        calls = self.init_counter()
        self.lose_link()
        start = self.clock[0]
        with self.assertLogs("bot", "WARNING") as logs:
            while self.clock[0] - start < 200:
                bot.ensure_mt5(); self.clock[0] += 1
        gaps = [round(b - a) for a, b in zip(calls, calls[1:])]
        self.assertEqual(calls[0] - start, C.RECONNECT_GRACE_SECONDS)
        self.assertEqual(gaps[:5], [5, 10, 20, 40, 40])
        self.assertTrue(all(r.levelname == "WARNING" for r in logs.records))
        self.assertTrue(any("attempt 1 failed" in r.getMessage() for r in logs.records))

    def test_terminal_reconnects_by_itself_one_recovery_line(self):
        calls = self.init_counter()
        self.lose_link()
        with self.assertLogs("bot", "INFO") as logs:
            for _ in range(6):                                        # 12 s down, the terminal comes back by itself
                bot.ensure_mt5(); self.clock[0] += 2
            World.connected = True
            for _ in range(5):
                self.assertIsNone(bot.ensure_mt5()); self.clock[0] += 2
        lines = [r.getMessage() for r in logs.records if "reconnected" in r.getMessage()]
        self.assertEqual(lines, ["MT5 reconnected after 12s (0 attempts)"])
        self.assertEqual(calls, [])

    def test_single_recovery_line_after_attempts(self):
        self.lose_link()
        with self.assertLogs("bot", "INFO") as logs:
            for _ in range(36):                                       # grace + attempts at 30 s and 35 s, both fail
                bot.ensure_mt5(); self.clock[0] += 1
            World.connected = True                                    # the broker is back; the next attempt (45 s) works
            for _ in range(15):
                bot.ensure_mt5(); self.clock[0] += 1
        lines = [r.getMessage() for r in logs.records if "reconnected" in r.getMessage()]
        self.assertEqual(lines, ["MT5 reconnected after 45s (3 attempts)"])
        self.assertFalse(any("MT5 connected" in r.getMessage() for r in logs.records))   # no second line

    def test_error_and_one_discord_card_only_after_2_minutes(self):
        self.lose_link()
        start = self.clock[0]
        with self.assertLogs("bot", "WARNING") as logs:
            while self.clock[0] - start < 119:
                bot.report_blind(bot.ensure_mt5()); self.clock[0] += 2
            self.assertFalse(any("BLIND" in t for t in titles(Http.to("discord"))))
            self.assertFalse(any(r.levelname == "ERROR" for r in logs.records))
            for _ in range(30):
                bot.report_blind(bot.ensure_mt5()); self.clock[0] += 2
        self.assertEqual(sum("BLIND" in t for t in titles(Http.to("discord"))), 1)
        self.assertEqual(sum(r.levelname == "ERROR" for r in logs.records), 1)

    def test_cross_missed_during_a_short_outage_comes_as_a_digest(self):
        bot.check_ema("M30")
        self.run_until_event()                                        # find a candle with an alert ...
        event_now = World.now
        World.reset(); Http.reset(); bot.state = notifier.State(); bot.reset_link()
        for f in os.listdir(C.STATE_DIR):
            os.remove(os.path.join(C.STATE_DIR, f))
        bot.state = notifier.State()
        World.now = event_now - timedelta(minutes=30)                 # ... and replay it with the link down over it
        self.assertIsNone(bot.ensure_mt5()); bot.check_ema("M30"); Http.reset()
        self.lose_link()
        bot.ensure_mt5(); self.clock[0] += 50                         # down shortly before the close
        World.now = event_now + timedelta(seconds=40)                 # back 40 s after it (old rule: a live ping)
        World.connected = True
        bot.ensure_mt5(); self.clock[0] += 5; bot.ensure_mt5()        # grace over -> first initialize works
        self.assertIsNone(bot.ensure_mt5())
        bot.check_ema("M30")
        d = titles(Http.to("discord"))
        self.assertEqual(len(d), 1, d); self.assertIn("MISSED WHILE OFFLINE", d[0])
        self.assertFalse(any(p.get("content") for p in Http.to("discord")))   # nobody's phone buzzes for it
        n = len(Http.sent); bot.check_ema("M30"); self.assertEqual(len(Http.sent), n)


class AlertFirst(Base):
    def test_alert_is_sent_before_chart_records_and_state(self):
        order = []
        real_post, real_record, real_save = notifier._post, store.record, notifier.State.save
        png = os.path.join(C.CHART_DIR, "fake.png")
        with open(png, "wb") as f:
            f.write(b"\x89PNG fake")

        def post(url, payload, name, attempts=3):
            order.append("chart_post" if payload.get("_file") else f"post:{name}")
            return real_post(url, payload, name, attempts)

        def record(kind, payload=None, symbol=None):
            order.append(f"record:{kind}"); return real_record(kind, payload, symbol)

        def save(st):
            order.append("state"); return real_save(st)
        with mock.patch.object(notifier, "_post", post), mock.patch.object(store, "record", record), \
                mock.patch.object(notifier.State, "save", save), \
                mock.patch.object(chart, "render", lambda *a, **k: order.append("chart_render") or png):
            bot.check_ema("M30")
            for _ in range(200):
                self.to_close(); order.clear(); bot.check_ema("M30")
                if any(o.startswith("post:") for o in order):
                    break
        first_post = next(i for i, o in enumerate(order) if o.startswith("post:"))
        self.assertEqual(first_post, 0, order)                        # nothing at all happens before the alert
        for later in ("chart_render", "chart_post", "record:event", "state"):
            self.assertIn(later, order)
            self.assertLess(first_post, order.index(later), f"{later} before the alert: {order}")
        self.assertLess(order.index("record:event"), order.index("chart_render"))
        self.assertFalse(any(o.startswith("record:") or o in ("state", "chart_render") for o in order[:first_post]))

    def test_latency_is_logged_per_alert(self):
        bot.check_ema("M30")
        with self.assertLogs("bot", "INFO") as logs:
            self.run_until_event()
        msgs = [r.getMessage() for r in logs.records]
        self.assertTrue(any("candle" in m and "detected" in m for m in msgs))
        line = next(m for m in msgs if m.startswith("latency XAUUSD M30"))
        for part in ("detected", "signal", "sent", "response", "Discord"):
            self.assertIn(part, line)

    def test_one_http_session_for_every_webhook(self):
        made, posts = [], []

        class Session:
            def __init__(self): made.append(self)

            def post(self, url, **kw):
                posts.append(url); return type("R", (), {"status_code": 204, "text": "", "headers": {}})()
        fake = type("requests", (), {"Session": Session, "RequestException": Exception})
        with mock.patch.object(notifier, "requests", fake), mock.patch.object(notifier, "_session", None):
            for i in range(3):
                notifier.send(notifier.card(f"t{i}"))
        self.assertEqual(len(made), 1)                                # one keep-alive session, no new TLS per alert
        self.assertEqual(len(posts), 9)                               # 3 alerts x Discord/Slack/ntfy through it


class FastPoll(Base):
    def at(self, close_offset_s: float):
        """World clock at `close_offset_s` seconds from the next M30 close."""
        s = World.now.timestamp()
        World.now = datetime.fromtimestamp((s // 1800 + 1) * 1800 + close_offset_s, timezone.utc)

    def test_polls_fast_around_the_close_and_reads_the_bar_on_the_next_candle_tick(self):
        bot.check_ema("M30")
        self.at(-0.3)                                                 # window opens 0.5 s before the close
        self.assertTrue(bot.in_close_window())
        close = World.now + timedelta(seconds=0.3)
        close_srv = (close + World.stamp_offset).replace(tzinfo=None)
        self.assertEqual(bot._window_close("M30"), close_srv)         # the coming close, not a second before it
        World.frozen_tick = close - timedelta(seconds=1)              # no tick of the new candle yet
        with mock.patch.object(bot, "check_ema", wraps=bot.check_ema) as ce, mock.patch("time.sleep") as sl:
            World.now = close + timedelta(seconds=0.2)
            bot.check_candles()
            self.assertEqual(ce.call_count, 0)                        # never read a candle that may still get ticks
            self.assertTrue(sl.call_args_list and all(c.args == (C.FAST_POLL_SECONDS,) for c in sl.call_args_list))
            World.frozen_tick = None                                  # first tick of the next candle arrives
            sl.reset_mock()
            bot.check_candles()
            self.assertEqual(ce.call_count, 1)
            self.assertEqual(sl.call_count, 0)                        # bar done: back to the normal rhythm
        self.at(-900)
        self.assertFalse(bot.in_close_window())
        self.assertEqual(bot.next_sleep(), C.POLL_SECONDS)


if __name__ == "__main__":
    unittest.main()
