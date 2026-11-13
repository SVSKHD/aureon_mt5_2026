"""Pre-cross alerts (watch signals, Monitor button only) and monitor mode (spec 5.4 and 6)."""
import glob, json, os, sys, unittest
from datetime import timedelta, timezone
from unittest import mock
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bot import Http, World                          # noqa: E402
from test_v170 import V170                                 # noqa: E402
from test_labels import load, SAMPLE                       # noqa: E402
import config as C, signals, notifier, bot, labels, monitor   # noqa: E402


def outbox():
    out = []
    for p in sorted(glob.glob(os.path.join(C.OUTBOX_DIR, "*.json"))):
        with open(p) as f:
            out.append(json.load(f))
    return out


class PreCross(V170):
    def test_pre_cross_is_a_watch_signal_with_only_a_monitor_button(self):
        notifier.BOT_CLIENT["on"] = True
        bot.check_ema("M30")
        for _ in range(150):
            self.to_close(); bot.check_ema("M30")
        items = outbox()
        pre = [i for i in items if i["kind"] == "PRE-CROSS"]
        self.assertTrue(pre, "no pre-cross in 150 candles")
        for i in pre:
            self.assertEqual([b["id"] for b in i["buttons"]], ["aureon:monitor:XAUUSD"])
            text = json.dumps(i, ensure_ascii=False)
            self.assertIn("WATCH SIGNAL, not a trade signal", text)
            for banned in ("place", "Place", "Entry", "SL "):
                self.assertNotIn(banned, json.dumps(i["buttons"]) + " ".join(str(f[0]) for f in i["fields"]))
            self.assertTrue(i["title"].startswith("XAUUSD"))
            self.assertRegex(i["title"], r"PRE-CROSS (8/32|8/200|32/200)")
        sent_failed = [p for p in Http.to("discord") if "PRE-CROSS FAILED" in p["embeds"][0]["title"]]
        for p in sent_failed:                                     # failed: a plain webhook card, no buttons
            self.assertNotIn("components", p)

    def test_config_thresholds_replace_the_old_ones(self):
        self.assertEqual(labels.DEFAULTS["pre_8_32"], (0.8, 0.35, 3, 0.8))
        self.assertEqual(labels.DEFAULTS["pre_8_200"][:2], (1.0, 0.5))
        self.assertEqual(labels.DEFAULTS["pre_32_200"][:2], (1.0, 0.35))
        import inspect
        self.assertNotIn("PRE_CROSS_ATR_FRAC", inspect.getsource(signals.detect))


class Monitor(V170):
    def setUp(self):
        super().setUp()
        notifier.BOT_CLIENT["on"] = True
        p = mock.patch.object(monitor, "MIN_EDIT_SECONDS", 0); p.start(); self.addCleanup(p.stop)

    def cmd(self, name, **kw):
        return bot.handle_json_command({"cmd": name, "ts": World.now.timestamp(), "id": name + str(kw), "user": 42, **kw})

    def test_survives_restart_and_stops_only_on_unmonitor(self):
        self.assertIn("Monitoring XAUUSD", self.cmd("monitor", symbol="XAUUSD")["text"])
        bot.check_ema("M30")
        for _ in range(100):                                      # crosses come and go: still on
            self.to_close(); bot.check_ema("M30")
        self.assertTrue(monitor.active(bot.state, "XAUUSD"))
        bot.state = notifier.State()                              # restart (or auto update)
        self.assertTrue(monitor.active(bot.state, "XAUUSD"))
        self.assertIn("Monitor: ON", self.cmd("status", symbol="XAUUSD")["text"])
        cards = [i for i in outbox() if i["kind"] == "MONITOR"]
        self.assertGreater(len(cards), 5)
        self.assertEqual({i["upsert"] for i in cards}, {"monitor:XAUUSD"})   # one card, edited in place
        self.assertTrue(all(i["channel"] == "updates" for i in cards))
        self.assertIn("Stopped", self.cmd("unmonitor", symbol="XAUUSD")["text"])
        self.assertFalse(monitor.active(bot.state, "XAUUSD"))
        n = len(outbox())
        for _ in range(5):
            self.to_close(); bot.check_ema("M30"); bot.refresh_monitors()
        self.assertEqual(len([i for i in outbox()[n:] if i["kind"] == "MONITOR"]), 0)

    def test_disabled_symbol(self):
        self.assertIn("XAGUSD is disabled in config", self.cmd("monitor", symbol="XAGUSD")["text"])

    def test_rate_limit_and_change_detection(self):
        self.cmd("monitor", symbol="XAUUSD")
        bot.check_ema("M30")
        monitor.MIN_EDIT_SECONDS = 60
        n = len(outbox())
        for _ in range(5):
            bot.refresh_monitors()
        self.assertLessEqual(len(outbox()) - n, 1)               # at most one edit a minute

    def test_card_before_and_after_the_cross(self):
        df = load(SAMPLE)
        for p in (8, 32, 200):
            df[f"ema{p}"] = df["close"].ewm(span=p, adjust=False).mean()
        C.SERVER_UTC_OFFSET_HOURS = 0; signals._server_offset = timedelta(0); World.stamp_offset = timedelta(0)
        self.addCleanup(setattr, C, "SERVER_UTC_OFFSET_HOURS", None)

        def at(when):
            t = pd.Timestamp(when)
            World.now = (t + timedelta(minutes=30, seconds=3)).to_pydatetime().replace(tzinfo=timezone.utc)
            d = df[df["time"] <= t].reset_index(drop=True)
            return d, labels.run(labels.prepare(d))

        d, lab = at("2026-10-29 03:30")
        monitor.start(bot.state, "XAUUSD")
        c = monitor.card(bot.state, "XAUUSD", lab, d, 4041.0)
        text = json.dumps(c, ensure_ascii=False)
        self.assertIn("Before the cross", c["desc"]); self.assertIn("live, not confirmed", text)
        self.assertEqual([f[0] for f in c["fields"][:3]], ["EMA 8/32", "EMA 8/200", "EMA 32/200"])
        d, lab = at("2026-10-29 11:30")                          # after the cross, PARALLEL RUNNING on this candle
        c = monitor.card(bot.state, "XAUUSD", lab, d, None)
        text = json.dumps(c, ensure_ascii=False)
        self.assertIn("After the cross", c["desc"]); self.assertIn("SHORT", c["desc"])
        self.assertIn("no failed cross in the study ever showed it", text)
        self.assertIn("no follow-through yet", text)            # 12 candles, no GAP WIDENING yet
        d, lab = at("2026-10-30 06:00")                          # widening at 05:30
        text = json.dumps(monitor.card(bot.state, "XAUUSD", lab, d, None), ensure_ascii=False)
        self.assertNotIn("no follow-through yet", text); self.assertIn("✅ seen", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
