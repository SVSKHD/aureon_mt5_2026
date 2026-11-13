"""/last-week (spec section 9). Friday 13 Nov 2026 -> the last completed week is Mon 2 - Fri 6 Nov (server time)."""
import os, sys, types, unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bot import World                                   # noqa: E402
from test_v170 import V170, records                           # noqa: E402
import fake_mt5                                               # noqa: E402
import config as C, signals, bot, report, labels, alerts      # noqa: E402

UTC = timezone.utc


def deal(pid, entry, typ, price, t_srv, profit=0.0, reason=0, volume=0.5, symbol="XAUUSD"):
    return types.SimpleNamespace(position_id=pid, entry=entry, type=typ, price=price, volume=volume, profit=profit,
                                 swap=0.0, commission=-2.0, fee=0.0, reason=reason, symbol=symbol,
                                 time=int(t_srv.replace(tzinfo=UTC).timestamp()))


class LastWeek(V170):
    def setUp(self):
        super().setUp()
        World.now = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
        self.assertIsNone(bot.ensure_mt5())

    def test_week_boundaries(self):
        self.assertEqual(report.last_week(datetime(2026, 11, 13, 22, 0)), (datetime(2026, 11, 2), datetime(2026, 11, 7)))
        self.assertEqual(report.last_week(datetime(2026, 11, 14, 1, 0)), (datetime(2026, 11, 9), datetime(2026, 11, 14)))
        self.assertEqual(report.last_week(datetime(2026, 11, 16, 1, 0)), (datetime(2026, 11, 9), datetime(2026, 11, 14)))

    def test_crosses_are_recomputed_from_candles_even_if_the_bot_never_ran(self):
        self.assertEqual(bot.state.d["keys"], [])                   # the bot saw nothing that week
        start, end = datetime(2026, 11, 2), datetime(2026, 11, 7)
        df = signals.fetch("M30", since=start)
        res = labels.run(labels.prepare(df))
        want = [e for e in res.events if e["label"] == labels.CROSS and start <= e["time"].to_pydatetime() < end]
        self.assertGreater(len(want), 3)
        r = report.build("XAUUSD")
        self.assertTrue(r["ok"]); self.assertIn(f"crosses: {len(want)}**", r["text"])
        self.assertTrue(os.path.getsize(r["image"]) > 5000)
        self.assertIn("EMA200 crosses", r["text"]); self.assertIn("followed by a 200 cross", r["text"])

    def test_positions_and_pnl_from_deal_history(self):
        World.deals = [
            deal(1, 0, 0, 4100.0, datetime(2026, 11, 2, 10)), deal(1, 1, 1, 4120.0, datetime(2026, 11, 3, 11), 1000.0, 5),
            deal(2, 0, 1, 4110.0, datetime(2026, 11, 4, 10)), deal(2, 1, 0, 4116.0, datetime(2026, 11, 4, 15), -300.0, 4),
            deal(3, 0, 0, 4090.0, datetime(2026, 11, 6, 20)),                       # still open over the weekend
            deal(4, 0, 0, 4000.0, datetime(2026, 10, 30, 10)),                      # opened the week before ...
            deal(4, 1, 1, 4010.0, datetime(2026, 11, 2, 3), 500.0, 0),             # ... closed in this week
            deal(5, 0, 0, 50.0, datetime(2026, 11, 3, 10), symbol="XAGUSD"),       # other symbol: not counted
            types.SimpleNamespace(position_id=0, entry=0, type=2, price=0, volume=0, profit=5000.0, swap=0, commission=0,
                                  fee=0, reason=0, symbol="", time=int(datetime(2026, 11, 10, tzinfo=UTC).timestamp())),
        ]
        tr = report.trades("XAUUSD", datetime(2026, 11, 2), datetime(2026, 11, 7))
        self.assertEqual((tr["opened"], tr["closed"], tr["still_open"]), (3, 3, 1))
        self.assertEqual((tr["wins"], tr["losses"]), (2, 1)); self.assertAlmostEqual(tr["win_rate"], 200 / 3)
        self.assertEqual(tr["reasons"], {"TP": 1, "SL": 1, "manual": 1})
        self.assertAlmostEqual(tr["pts"], 20 - 6 + 10)
        self.assertEqual(tr["best"]["ticket"], 1); self.assertEqual(tr["worst"]["ticket"], 2)
        # money inside the week: all deals of this symbol timed Mon-Fri (each -2 commission)
        self.assertAlmostEqual(tr["money"], 1000 - 300 + 500 - 2 * 6)
        self.assertIn("Mon 02 Nov", tr["days"])
        start_bal, end_bal = report.balances(datetime(2026, 11, 2), datetime(2026, 11, 7))
        self.assertAlmostEqual(end_bal, World.balance - 5000.0)               # the deposit came after the week
        r = bot.handle_json_command({"cmd": "last_week", "ts": World.now.timestamp(), "id": "w", "user": 42})
        self.assertTrue(r["ok"]); self.assertIn("win rate 67%", r["text"]); self.assertIn("still open 1", r["text"])
        self.assertTrue(r["images"])
        self.assertEqual(len(records("weekly")), 1)

    def test_disabled_symbol(self):
        r = bot.handle_json_command({"cmd": "last_week", "symbol": "XAGUSD", "ts": World.now.timestamp(), "id": "x", "user": 42})
        self.assertIn("XAGUSD is disabled in config", r["text"])

    def test_long_replies_are_split_for_discord(self):
        text = "\n".join(f"line {k} " + "x" * 80 for k in range(100))
        parts = alerts.chunks(text)
        self.assertGreater(len(parts), 3); self.assertTrue(all(len(p) <= 1990 for p in parts))
        self.assertEqual("\n".join(parts), text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
