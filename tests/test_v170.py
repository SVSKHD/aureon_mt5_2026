"""1.7.0 tests. Same isolated harness as test_bot.py: fake MT5, fake webhooks, temp data folder.
Run:  python -m unittest discover -s tests -v"""
import glob, json, os, sys, types, unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bot import Base, Http, titles, World          # noqa: E402  (sets up the isolated environment first)
import fake_mt5                                          # noqa: E402
import config as C, signals, notifier, alerts, bot, trader, store   # noqa: E402


def records(kind):
    store.flush()
    out = []
    for path in sorted(glob.glob(os.path.join(C.RECORD_DIR, f"{kind}-*.jsonl"))):
        with open(path, encoding="utf-8") as f:
            out += [json.loads(line) for line in f if line.strip()]
    return out


class V170(Base):
    """Base + symbol table reset + empty record folder."""
    def setUp(self):
        self._symbols = {k: dict(v) for k, v in C.SYMBOLS.items()}
        self.addCleanup(self._restore)
        store.flush()
        for p in glob.glob(os.path.join(C.RECORD_DIR, "*")):
            os.remove(p)
        for p in glob.glob(os.path.join(C.OUTBOX_DIR, "*")):
            os.remove(p)
        store.sink = None
        notifier.BOT_CLIENT["on"] = False
        super().setUp()

    def _restore(self):
        for k, v in self._symbols.items():
            C.SYMBOLS[k] = v
        C.SYMBOL_MAP.clear()
        store.sink = None
        notifier.BOT_CLIENT["on"] = False

    def enable(self, sym, on=True):
        C.SYMBOLS[sym]["enabled"] = on

    def json_cmd(self, cmd, **payload):
        req = {"cmd": cmd, "ts": World.now.timestamp(), "id": cmd, "user": 42, **payload}
        return bot.handle_json_command(req)


class Version(V170):
    def test_version_file_and_status(self):
        with open(os.path.join(C.PROJECT_DIR, "VERSION")) as f:
            self.assertEqual(f.read().strip(), "1.7.1")
        self.assertEqual(C.VERSION, "1.7.1")
        r = self.json_cmd("status", symbol="XAUUSD")
        self.assertTrue(r["ok"], r); self.assertIn("v1.7.1", r["text"]); self.assertTrue(r["image"])
        bot.announce_start()
        self.assertTrue(any("v1.7.1" in t and t.startswith("XAUUSD") for t in titles(Http.to("discord"))))


class Symbols(V170):
    def test_disabled_symbol_is_never_touched(self):
        self.assertFalse(C.is_enabled("XAGUSD"))
        World.calls = []
        bot.check_ema("M30")
        for _ in range(20):
            self.to_close(); bot.check_ema("M30"); bot.check_ema("M30", "XAGUSD"); bot.check_price_alerts("M30")
        self.assertIsNone(bot.ensure_mt5())
        self.assertEqual([c for c in World.calls if c[1] == "XAGUSD"], [])
        self.assertFalse(any(t.startswith("XAGUSD") for t in titles(Http.to("discord"))))
        for cmd in ("chart", "status", "place", "quote"):
            r = self.json_cmd(cmd, symbol="XAGUSD", side="buy", lot=0.1, sl=1, tp=2)
            self.assertFalse(r["ok"]); self.assertIn("XAGUSD is disabled in config", r["text"])
        self.assertIn("XAGUSD is disabled in config", alerts.handle_command("add 51.5 XAGUSD"))
        self.assertEqual(World.orders, [])

    def test_enabling_is_config_only_and_symbols_do_not_share_state(self):
        self.enable("XAGUSD")
        bot.check_ema("M30", "XAUUSD"); bot.check_ema("M30", "XAGUSD")
        self.assertIn("XAUUSD:M30", bot.state.d["last_bar"]); self.assertIn("XAGUSD:M30", bot.state.d["last_bar"])
        gold_cp = bot.state.d["last_bar"]["XAUUSD:M30"]
        gold_keys = [k for k in bot.state.d["keys"] if k.startswith("XAUUSD")]
        for _ in range(60):                                        # silver moves on; gold is never re-checked
            self.to_close(); bot.check_ema("M30", "XAGUSD")
        self.assertEqual(bot.state.d["last_bar"]["XAUUSD:M30"], gold_cp)
        self.assertEqual([k for k in bot.state.d["keys"] if k.startswith("XAUUSD")], gold_keys)
        silver = [t for t in titles(Http.to("discord")) if t.startswith("XAGUSD")]
        self.assertTrue(silver, "silver produced no alert")
        self.assertTrue(all(k.split(":")[0] in C.SYMBOLS for k in bot.state.d["keys"] if ":" in k and "M30" in k))
        r = self.json_cmd("chart", symbol="XAGUSD")
        self.assertTrue(r["ok"]); self.assertIn("XAGUSD", r["text"])

    def test_price_alerts_are_per_symbol(self):
        self.enable("XAGUSD")
        silver = fake_mt5.price(World.stamp(), "XAGUSD")
        self.assertIn("XAGUSD", alerts.handle_command(f"add {silver + 0.2:.3f} XAGUSD", price_of=lambda s: silver))
        self.assertEqual(len(alerts.pending("XAGUSD")), 1); self.assertEqual(alerts.pending("XAUUSD"), [])
        self.assertIn("No alert", alerts.remove(round(silver + 0.2, 3), "XAUUSD"))

    def test_symbol_map_and_validation(self):
        C.SYMBOL_MAP["XAUUSD"] = "XAUUSD.m"
        World.symbols = {"XAUUSD.m": True, "XAGUSD": True}
        bot.ensure_mt5()
        self.assertEqual(signals.symbol_problem("XAUUSD.m"), None)
        World.calls = []
        bot.check_ema("M30")
        self.assertTrue(World.calls)
        self.assertTrue(all(c[1] == "XAUUSD.m" for c in World.calls if c[1]))
        C.SYMBOL_MAP.clear()                                       # broker has no plain XAUUSD: refuse to start
        problems = C.validate(symbol_check=signals.symbol_problem)
        self.assertTrue(any("XAUUSD" in p and "Market Watch" in p for p in problems), problems)
        World.symbols = {"XAUUSD": False}                          # exists but hidden and cannot be selected
        with mock.patch.object(sys.modules["MetaTrader5"], "symbol_select", lambda s, b: False):
            self.assertIn("not visible", signals.symbol_problem("XAUUSD"))


class Channels(V170):
    def test_updates_go_to_discord_only_and_alerts_go_everywhere(self):
        notifier.deliver(bot.state, notifier.info("quiet", symbol="XAUUSD", channel="updates"))
        self.assertEqual((len(Http.to("discord")), len(Http.to("slack")), len(Http.to("ntfy"))), (1, 0, 0))
        notifier.deliver(bot.state, notifier.info("loud", symbol="XAUUSD", channel="alerts", ping=True))
        self.assertEqual((len(Http.to("discord")), len(Http.to("slack")), len(Http.to("ntfy"))), (2, 1, 1))

    def test_per_channel_webhooks(self):
        hooks = {"alerts": "https://discord.test/a", "updates": "https://discord.test/u",
                 "positions": "https://discord.test/p"}
        with mock.patch.dict(C.DISCORD_WEBHOOKS, hooks):
            for ch in ("alerts", "updates", "positions"):
                notifier.send(notifier.info(ch, channel=ch))
        self.assertEqual([u for u, _ in Http.sent if "discord" in u], list(hooks.values()))

    def test_cards_with_buttons_go_through_the_bot_client(self):
        notifier.BOT_CLIENT["on"] = True
        msg = notifier.info("press me", symbol="XAUUSD", buttons=[notifier.button("Monitor", "monitor", "XAUUSD")])
        st = notifier.send(msg)
        self.assertEqual(st["Discord"], "ok"); self.assertEqual(Http.to("discord"), [])   # not the webhook
        self.assertEqual(len(Http.to("slack")), 1)                                         # others as usual
        files = glob.glob(os.path.join(C.OUTBOX_DIR, "*.json"))
        self.assertEqual(len(files), 1)
        with open(files[0]) as f:
            item = json.load(f)
        self.assertEqual(item["buttons"][0]["id"], "aureon:monitor:XAUUSD"); self.assertEqual(item["channel_id"], 777)

    def test_titles_start_with_the_symbol(self):
        bot.check_ema("M30"); self.run_until_event()
        for t in titles(Http.to("discord")):
            self.assertTrue(t.startswith("XAUUSD"), t)

    def test_retry_queue_entries_carry_the_symbol(self):
        Http.down.add("slack")
        notifier.deliver(bot.state, notifier.info("x", symbol="XAUUSD"))
        self.assertEqual(bot.state.d["pending"][0]["symbol"], "XAUUSD")


class LabelAlerts(V170):
    def test_labels_are_sent_once_with_the_market_status_line(self):
        bot.check_ema("M30")
        for _ in range(80):
            self.to_close(); bot.check_ema("M30")
        cards = [p["embeds"][0] for p in Http.to("discord") if "embeds" in p]
        names = " ".join(c["title"] for c in cards)
        for label in ("GAP WIDENING", "WIDE GAP", "GAP PEAKED", "CROSS"):
            self.assertIn(label, names)
        live = [c for c in cards if "MISSED" not in c["title"] and "online" not in c["title"]]
        self.assertTrue(all(any(f["name"] == "Market now" for f in c["fields"]) for c in live))
        quiet = {"GAP WIDENING", "PARALLEL RUNNING", "WIDE GAP", "PULLBACK STARTED", "RE-ENTRY"}
        for p in Http.to("slack"):                                # #updates labels never reach Slack on their own
            self.assertFalse(any(p["text"].endswith(q + " ▲ LONG") or p["text"].endswith(q + " ▼ SHORT")
                                 for q in quiet), p["text"])
        n = len(Http.sent)
        bot.state = notifier.State(); bot.LABELS.clear(); bot.check_ema("M30")   # restart: no repeats
        self.assertEqual(len(Http.sent), n)

    def test_status_shows_the_market_line(self):
        bot.check_ema("M30")
        r = self.json_cmd("status")
        self.assertIn("Market:", r["text"]); self.assertIn("candles since the cross", r["text"])


class Records(V170):
    def test_every_event_and_alert_produces_exactly_one_record(self):
        bot.check_ema("M30"); self.run_until_event()
        ev, al = records("event"), records("alert")
        sent_keys = [k for k in bot.state.d["keys"] if k.startswith("XAUUSD:M30:")]
        self.assertEqual(len(ev), len(sent_keys))
        self.assertEqual(len(al), len(titles(Http.to("discord"))))
        for r in ev + al:
            for f in ("record_id", "utc_time", "server_time", "version", "account_login", "symbol"):
                self.assertIn(f, r)
            self.assertEqual(r["version"], "1.7.1"); self.assertEqual(r["account_login"], 555)
        self.assertEqual(len({r["record_id"] for r in ev + al}), len(ev + al))
        n = len(records("event"))
        bot.state = notifier.State(); bot.check_ema("M30")          # restart: nothing re-sent, nothing re-recorded
        self.assertEqual(len(records("event")), n)

    def test_orders_and_commands_are_recorded(self):
        r = self.json_cmd("place", side="buy", lot=0.1, sl=20, tp=40, symbol="XAUUSD")
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(records("order")), 1)
        self.assertEqual([c["name"] for c in records("command")], ["place"])

    def test_sink_off_or_broken_changes_nothing(self):
        def run():
            World.reset(); Http.reset(); bot.state = notifier.State()
            for name in os.listdir(C.STATE_DIR):
                os.remove(os.path.join(C.STATE_DIR, name))
            bot.state = notifier.State(); bot.ensure_mt5()
            bot.check_ema("M30")
            for _ in range(40):
                self.to_close(); bot.check_ema("M30")
            return titles(Http.to("discord"))
        normal = run()

        class Broken:
            def write(self, rows):
                raise OSError("disk on fire")
        store.sink = Broken()
        with self.assertLogs("store", "ERROR") as logs:
            broken = run(); store.flush()
        self.assertEqual(len(logs.output), 1)                      # logged once
        store.sink = None
        with mock.patch.object(C, "RECORD_SINK", "none"):
            off = run()
        self.assertEqual(normal, broken); self.assertEqual(normal, off)
        self.assertGreater(len(normal), 0)

    def test_record_never_raises(self):
        with mock.patch.object(store, "_q", types.SimpleNamespace(put_nowait=mock.Mock(side_effect=RuntimeError))):
            store.record("event", {"x": 1})                        # swallowed


if __name__ == "__main__":
    unittest.main(verbosity=2)
