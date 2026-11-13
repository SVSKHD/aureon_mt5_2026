"""Weekend rule (spec section 10): warn only, never close; cadence; flat confirmation; /place block; Monday report.
Friday 13 Nov 2026. Broker server = UTC+2 (Athens winter). Fallback close 23:57 server = 21:57 UTC = 03:27 IST Sat.
Warnings start Friday 20:00 IST = 14:30 UTC."""
import os, sys, unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bot import Http, titles, World                    # noqa: E402
from test_v170 import V170                                   # noqa: E402
import fake_mt5                                              # noqa: E402
import config as C, signals, notifier, bot, weekend          # noqa: E402

UTC = timezone.utc


class Weekend(V170):
    def setUp(self):
        super().setUp()
        World.now = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
        self.assertIsNone(bot.ensure_mt5())

    def run_until(self, end, step_min=1):
        while World.now < end:
            World.advance(minutes=step_min)
            weekend.check(bot.state)

    def warnings(self):
        return [t for t in titles(Http.to("discord")) if "WEEKEND:" in t]

    def test_no_warnings_when_flat(self):
        self.run_until(datetime(2026, 11, 13, 22, 30, tzinfo=UTC), step_min=5)
        self.assertEqual(self.warnings(), [])
        self.assertFalse(any("flat for the weekend" in t for t in titles(Http.to("discord"))))

    def test_cadence_hourly_then_every_15_min_and_never_a_close(self):
        fake_mt5.open_position(9001, 1, 4100.0, sl=4080.0)
        self.run_until(datetime(2026, 11, 13, 14, 29, tzinfo=UTC))
        self.assertEqual(self.warnings(), [])                       # nothing before Friday 20:00 IST
        self.run_until(datetime(2026, 11, 13, 22, 10, tzinfo=UTC))
        w = [p for p in Http.to("discord") if "WEEKEND:" in p["embeds"][0]["title"]]
        self.assertEqual(len(w), 7 + 4)                             # 14:30..20:30 hourly, then 60/45/30/15 min left
        self.assertTrue(all(p.get("content", "").startswith("<@42>") for p in w))   # new pings, not edits
        self.assertIn("#9001", w[0]["embeds"][0]["description"]); self.assertIn("SL 4080.00", w[0]["embeds"][0]["description"])
        self.assertIn("left", w[-1]["embeds"][0]["description"])
        self.assertEqual([o for o in World.orders if o["action"] == 1], [])          # NEVER a close
        bot.state = notifier.State()                                # restart: no repeats
        self.run_until(datetime(2026, 11, 13, 22, 20, tzinfo=UTC))
        self.assertEqual(len(self.warnings()), 11)

    def test_flat_confirmation_once_then_silence(self):
        fake_mt5.open_position(9001, 1, 4100.0, sl=4080.0)
        self.run_until(datetime(2026, 11, 13, 16, 0, tzinfo=UTC))
        n = len(self.warnings()); self.assertGreater(n, 0)
        fake_mt5.close(9001, 4110.0, reason=0)
        self.run_until(datetime(2026, 11, 13, 22, 0, tzinfo=UTC))
        self.assertEqual(len(self.warnings()), n)
        self.assertEqual(sum("flat for the weekend" in t for t in titles(Http.to("discord"))), 1)

    def test_place_is_refused_in_the_last_hour(self):
        req = {"cmd": "place", "side": "buy", "lot": 0.1, "sl": 20, "tp": 40, "user": 42, "symbol": "XAUUSD", "id": "p"}
        World.now = datetime(2026, 11, 13, 20, 0, tzinfo=UTC)       # 1 h 57 min left
        self.assertTrue(bot.handle_json_command({**req, "ts": World.now.timestamp()})["ok"])
        World.now = datetime(2026, 11, 13, 21, 30, tzinfo=UTC)      # 27 min left
        r = bot.handle_json_command({**req, "id": "q", "ts": World.now.timestamp()})
        self.assertFalse(r["ok"]); self.assertIn("closes for the weekend in 27 min", r["text"])
        with mock.patch.object(weekend, "BLOCK_PLACE_MIN", 0):
            self.assertTrue(bot.handle_json_command({**req, "id": "r", "ts": World.now.timestamp()})["ok"])

    def test_close_time_is_learned_from_the_broker(self):
        prev_close = datetime(2026, 11, 6, 23, 54)                  # last M1 candle of last Friday, server time
        bars = np.zeros(3, fake_mt5.DT)
        bars["time"] = [int((prev_close - timedelta(minutes=k)).replace(tzinfo=UTC).timestamp()) for k in (2, 1, 0)]
        with mock.patch.object(sys.modules["MetaTrader5"], "copy_rates_range", lambda *a: bars):
            t, learned = weekend.friday_close("XAUUSD")
        self.assertTrue(learned); self.assertEqual(t, datetime(2026, 11, 13, 23, 55))
        t, learned = weekend.friday_close("XAUUSD")                 # no history: the config fallback
        self.assertFalse(learned); self.assertEqual(t, datetime(2026, 11, 13, 23, 57))

    def test_monday_reports_what_was_carried_over(self):
        World.now = datetime(2026, 11, 13, 18, 0, tzinfo=UTC)
        fake_mt5.open_position(9002, -1, 4100.0, sl=4120.0)
        World.now = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)        # Monday, market open again
        World.advance(seconds=1)
        for _ in range(3):
            weekend.check(bot.state)
        t = [x for x in titles(Http.to("discord")) if "carried over the weekend" in x]
        self.assertEqual(len(t), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
