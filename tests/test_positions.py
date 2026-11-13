"""Positions, stepped trailing stop, in-trade alerts, MAX POINT, /positions, Close (spec section 7).

The trailing stop is driven candle by candle through the real path (positions.on_candle -> trader.modify_sl ->
fake broker) over the real 29-30 Oct 2026 XAUUSD candles, so the stops the bot SETS must be the golden ones."""
import json, os, sys, unittest
from datetime import timedelta, timezone
from unittest import mock
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bot import Http, titles, World                 # noqa: E402
from test_v170 import V170, records                       # noqa: E402
from test_labels import load, SAMPLE                      # noqa: E402
import fake_mt5                                           # noqa: E402
import config as C, signals, notifier, bot, trader, positions, labels, store   # noqa: E402

GOLDEN_STEPS = [("2026-10-29 18:00", 4044.2), ("2026-10-30 02:30", 4037.4), ("2026-10-30 08:30", 4016.4),
                ("2026-10-30 14:00", 4004.2)]


def sltp_orders(ticket=None):
    return [o for o in World.orders if o["action"] == 6 and (ticket is None or o["position"] == ticket)]


def closing_deals():
    return [o for o in World.orders if o["action"] == 1 and o.get("position")]


class Pos(V170):
    def setUp(self):
        super().setUp()
        C.SERVER_UTC_OFFSET_HOURS = 0                          # the sample candles are in UTC
        World.stamp_offset = timedelta(0)
        signals._server_offset, signals._offset_source = timedelta(0), "pinned"
        self.df = load(SAMPLE)
        self.addCleanup(setattr, C, "SERVER_UTC_OFFSET_HOURS", None)

    def at(self, when):
        """Candles up to and including the one that opens at `when`; the clock just after it closed."""
        t = pd.Timestamp(when)
        World.now = (t + timedelta(minutes=30, seconds=3)).to_pydatetime().replace(tzinfo=timezone.utc)
        return self.df[self.df["time"] <= t].reset_index(drop=True)

    def candle(self, when, close_price=None):
        """One closed candle through the bot's position path. Returns the label result."""
        df = self.at(when)
        World.tick_override["XAUUSD"] = float(close_price if close_price is not None else df["close"].iloc[-1])
        lab = labels.run(labels.prepare(df))
        bot.LABELS["XAUUSD"] = lab
        newest = df["time"].iloc[-1]
        fresh = [(newest, bot.label_event(e)) for e in lab.events if e["time"] == newest and e["label"] in bot.LIVE_LABELS]
        positions.on_candle(bot.state, "XAUUSD", df, lab, fresh)
        return lab

    def walk(self, start, end):
        for t in pd.date_range(start, end, freq="30min"):
            if (self.df["time"] == t).any():
                self.candle(t)

    def short_at_cross(self, sl=4060.0, ticket=7001):
        self.at("2026-10-29 05:30")
        World.now += timedelta(seconds=2)                      # 06:00:05 UTC, right after the cross candle closed
        return fake_mt5.open_position(ticket, -1, 4038.1, sl=sl, tp=3800.0)


class Tracking(Pos):
    def test_opened_and_closed_alerts_with_records(self):
        self.at("2026-10-29 05:30")
        fake_mt5.open_position(7001, -1, 4038.1, sl=0.0, magic=0)          # from the MT5 app, no stop
        positions.track(bot.state, bot.LABELS); positions.track(bot.state, bot.LABELS)
        opened = [p["embeds"][0] for p in Http.to("discord") if "POSITION OPENED" in p["embeds"][0]["title"]]
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0]["title"].startswith("XAUUSD"))
        self.assertIn("NO STOP-LOSS", opened[0]["description"])
        World.advance(hours=3); fake_mt5.close(7001, 4030.0, reason=4)
        positions.track(bot.state, bot.LABELS); positions.track(bot.state, bot.LABELS)
        closed = [p["embeds"][0] for p in Http.to("discord") if "POSITION CLOSED" in p["embeds"][0]["title"]]
        self.assertEqual(len(closed), 1)
        text = json.dumps(closed[0], ensure_ascii=False)
        self.assertIn("SL hit", text); self.assertIn("+8.10 pts", text)
        recs = records("position")
        self.assertEqual([r["event"] for r in recs], ["opened", "closed"])
        self.assertEqual(recs[1]["close_reason"], "SL hit"); self.assertAlmostEqual(recs[1]["exit"], 4030.0)
        self.assertEqual(bot.state.d["positions"], {})

    def test_position_on_a_disabled_symbol_is_reported_once(self):
        fake_mt5.open_position(8001, 1, 50.0, symbol="XAGUSD")
        for _ in range(3):
            positions.track(bot.state, bot.LABELS)
        t = [x for x in titles(Http.to("discord")) if "disabled symbol" in x]
        self.assertEqual(len(t), 1); self.assertIn("NOT managed", t[0])
        self.assertEqual(sltp_orders(), [])

    def test_positions_report(self):
        r = positions.report(bot.state, bot.LABELS)
        self.assertIn("No open positions", r["text"]); self.assertIn("Balance 10,000.00", r["text"])
        self.short_at_cross()
        self.walk("2026-10-29 06:00", "2026-10-29 20:00")
        r = bot.handle_json_command({"cmd": "positions", "ts": World.now.timestamp(), "id": "p", "user": 42})
        for part in ("XAUUSD ▼ SHORT 0.5", "#7001", "Entry 4038.10", "P/L", "SL 4044.2", "TSL step 1",
                     "next:", "Market:", "Open P/L", "Equity", "free margin", "swap"):
            self.assertIn(part, r["text"])
        self.assertEqual(r["buttons"][0]["id"], "aureon:refresh:positions")


class TrailingStop(Pos):
    def test_golden_short_steps_are_the_stops_the_bot_sets(self):
        self.short_at_cross()
        self.walk("2026-10-29 06:00", "2026-10-30 16:00")
        sent = [o["sl"] for o in sltp_orders(7001)]
        self.assertEqual(len(sent), 4, sent)
        for got, (_, want) in zip(sent, GOLDEN_STEPS):
            self.assertAlmostEqual(got, want, delta=0.1)
        self.assertAlmostEqual(World.positions[0].sl, 4004.2, delta=0.1)
        cards = [p["embeds"][0]["title"] for p in Http.to("discord")]
        self.assertEqual(sum("TSL MOVED" in t for t in cards), 3)
        c = [t for t in cards if "TREND STRENGTHENING, SL TIGHTENED" in t]
        self.assertEqual(len(c), 1)                             # 30 Oct 08:30: widening + step = ONE card
        self.assertFalse(any("TREND RUNNING" in t for t in cards if "08:30" in t))
        sl_recs = records("sl_change")
        self.assertEqual(len(sl_recs), 4); self.assertTrue(all(r["verified"] for r in sl_recs))
        self.assertEqual(closing_deals(), [])                   # the bot never closes

    def test_a_step_never_loosens_the_stop(self):
        self.short_at_cross(sl=4020.0)                          # already tighter than the first two steps
        self.walk("2026-10-29 06:00", "2026-10-30 16:00")
        sent = [o["sl"] for o in sltp_orders(7001)]
        self.assertTrue(sent)
        prev = 4020.0
        for s in sent:
            self.assertLess(s, prev); prev = s
        self.assertNotIn(4044.25, sent); self.assertNotIn(4037.39, sent)
        p = World.positions[0]
        r = trader.modify_sl(7001, p.sl + 5)                    # direct attempt to widen: refused, nothing sent
        self.assertFalse(r["ok"]); self.assertIn("would not tighten", r["text"])
        self.assertFalse(trader.modify_sl(7001, 0.0)["ok"])     # never removed
        self.assertEqual(len(sltp_orders(7001)), len(sent))

    def test_no_sl_gets_one_at_the_first_step(self):
        self.short_at_cross(sl=0.0)
        self.walk("2026-10-29 06:00", "2026-10-29 18:00")
        self.assertAlmostEqual(World.positions[0].sl, 4044.2, delta=0.1)

    def test_failed_modify_alerts_once_and_does_not_retry(self):
        self.short_at_cross()
        World.sltp_fails = True
        self.walk("2026-10-29 06:00", "2026-10-29 21:00")
        self.assertEqual(len(sltp_orders(7001)), 1)
        fails = [t for t in titles(Http.to("discord")) if "TSL step NOT applied" in t]
        self.assertEqual(len(fails), 1)
        self.assertEqual(records("sl_change")[0]["verified"], False)

    def test_restart_in_the_middle_of_a_pullback_still_fires_the_step(self):
        self.short_at_cross()
        self.walk("2026-10-29 06:00", "2026-10-29 15:00")       # pullback 11:30, re-entry 13:30, step not yet
        self.assertEqual(sltp_orders(7001), [])
        bot.state = notifier.State(); bot.LABELS.clear()        # process restart: state from disk only
        positions.OPEN.clear()
        self.walk("2026-10-29 15:30", "2026-10-29 18:00")
        self.assertEqual(len(sltp_orders(7001)), 1)
        self.assertAlmostEqual(sltp_orders(7001)[0]["sl"], 4044.2, delta=0.1)
        self.assertEqual(sum("POSITION OPENED" in t for t in titles(Http.to("discord"))), 1)   # not re-announced

    def test_position_against_the_trend_gets_one_alert_and_no_moves(self):
        self.at("2026-10-29 05:30"); World.now += timedelta(seconds=2)
        fake_mt5.open_position(7002, 1, 4038.4, sl=4000.0)
        self.walk("2026-10-29 06:00", "2026-10-30 10:00")
        self.assertEqual(sltp_orders(7002), [])
        self.assertEqual(sum("AGAINST the trend" in t for t in titles(Http.to("discord"))), 1)

    def test_suggest_mode_offers_apply_and_moves_nothing(self):
        self.short_at_cross()
        with mock.patch.object(positions, "tsl_mode", lambda: "suggest"):
            self.walk("2026-10-29 06:00", "2026-10-29 18:00")
        self.assertEqual(sltp_orders(7001), [])
        card = [p for p in Http.to("discord") if "TSL STEP suggested" in p["embeds"][0]["title"]]
        self.assertEqual(len(card), 1)
        self.assertIn("4044.25", json.dumps(card[0]))
        r = bot.handle_json_command({"cmd": "apply", "ticket": 7001, "sl": 4044.25, "ts": World.now.timestamp(),
                                     "id": "a", "user": 42})
        self.assertTrue(r["ok"], r); self.assertAlmostEqual(World.positions[0].sl, 4044.25)
        r = bot.handle_json_command({"cmd": "apply", "ticket": 7001, "sl": 4050.0, "ts": World.now.timestamp(),
                                     "id": "b", "user": 42})
        self.assertFalse(r["ok"])                               # looser than now: refused
        r = bot.handle_json_command({"cmd": "apply", "ticket": 7001, "sl": 4040.0, "ts": World.now.timestamp(),
                                     "id": "c", "user": 99})
        self.assertFalse(r["ok"]); self.assertIn("not in DISCORD_ALLOWED_USER_IDS", r["text"])


class MaxPoint(Pos):
    def test_gap_peaked_gives_one_max_point_card_with_close_and_keep(self):
        notifier.BOT_CLIENT["on"] = True
        self.short_at_cross()
        self.walk("2026-10-29 06:00", "2026-10-30 22:00")       # GAP PEAKED 30 Oct 21:00
        import glob
        items = []
        for p in sorted(glob.glob(os.path.join(C.OUTBOX_DIR, "*.json"))):
            with open(p) as f:
                items.append(json.load(f))
        mp = [i for i in items if i["kind"] == "MAX_POINT"]
        peaked = [i for i in mp if "GAP PEAKED" in json.dumps(i)]
        self.assertEqual(len(peaked), 1)                        # the GAP PEAKED one, 30 Oct 21:00
        peaks = [f["value"] if isinstance(f, dict) else f[1] for i in mp for f in i["fields"] if (f[0] if isinstance(f, list) else f.get("name")) == "Peak profit"]
        self.assertEqual(len(peaks), len(set(peaks)))           # the others: giveback, each at a NEW peak
        mp = peaked
        self.assertEqual([b["id"] for b in mp[0]["buttons"]], ["aureon:close:7001", "aureon:keep:7001"])
        self.assertIn("Kept if SL hits", json.dumps(mp[0], ensure_ascii=False))
        self.assertTrue(mp[0]["image"])
        self.assertEqual(closing_deals(), [])

    def test_giveback_fires_once_per_peak_and_rearms_on_a_new_extreme(self):
        self.short_at_cross(sl=4060.0)
        self.candle("2026-10-29 06:00")
        for price in (4030, 4015, 4010.0, 4012, 4021.0, 4022.0):  # peak +28.1, then gives back 11
            World.tick_override["XAUUSD"] = price
            positions.track(bot.state, bot.LABELS)
        mp = lambda: sum("MAX POINT" in t for t in titles(Http.to("discord")))
        self.assertEqual(mp(), 1)
        World.tick_override["XAUUSD"] = 4012.0; positions.track(bot.state, bot.LABELS)
        self.assertEqual(mp(), 1)                               # same peak: silent
        for price in (4000.0, 4011.0):                          # new extreme, then gives back 11 again
            World.tick_override["XAUUSD"] = price; positions.track(bot.state, bot.LABELS)
        self.assertEqual(mp(), 2)
        r = bot.handle_json_command({"cmd": "keep", "ticket": 7001, "ts": World.now.timestamp(), "id": "k", "user": 42})
        self.assertIn("new extreme", r["text"])

    def test_close_needs_the_user_and_goes_through_trader(self):
        self.short_at_cross()
        r = bot.handle_json_command({"cmd": "close", "ticket": 7001, "ts": World.now.timestamp(), "id": "x", "user": 99})
        self.assertFalse(r["ok"]); self.assertEqual(closing_deals(), [])
        r = bot.handle_json_command({"cmd": "close", "ticket": 7001, "ts": World.now.timestamp(), "id": "y", "user": 42})
        self.assertTrue(r["ok"], r); self.assertIn("CLOSED position 7001", r["text"])
        self.assertEqual(len(closing_deals()), 1); self.assertEqual(World.positions, [])
        self.assertEqual(closing_deals()[0]["type"], 0)          # closing a short = a buy
        r = bot.handle_json_command({"cmd": "close", "ticket": 7001, "ts": World.now.timestamp(), "id": "z", "user": 42})
        self.assertFalse(r["ok"]); self.assertIn("not open", r["text"])


class Cross200Lines(Pos):
    def test_200_cross_card_says_with_or_against_the_position(self):
        self.short_at_cross()
        positions.track(bot.state, bot.LABELS)
        out = positions.event_lines(bot.state, "XAUUSD", {"type": "FAST_X_TREND", "dir": "BEAR", "price": 4000})
        self.assertIn("WITH the position", out["fields"][0][1]); self.assertIn("SL 4060.00", out["fields"][0][1])
        out = positions.event_lines(bot.state, "XAUUSD", {"type": "PRE-CROSS", "pair": "8/200", "dir": "BULL", "price": 1})
        self.assertIn("AGAINST", out["fields"][0][1])
        self.assertEqual(positions.event_lines(bot.state, "XAUUSD", {"type": "CROSS", "dir": "BULL", "price": 1}), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
