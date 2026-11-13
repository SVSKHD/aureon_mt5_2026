"""Regression tests. Run from the project folder:   python -m unittest discover -s tests -v
Nothing here touches your real .env, logs, MT5 terminal or webhooks."""
import json, os, sys, tempfile, types, unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- isolated environment: temp data folder, fake webhooks, fake MT5, fake HTTP ----
TMP = tempfile.mkdtemp(prefix="aureon_test_")
with open(os.path.join(TMP, ".env"), "w") as f:
    f.write(f"DISCORD_WEBHOOK=https://discord.test/hook\nSLACK_WEBHOOK=https://slack.test/hook\n"
            f"NTFY_TOPIC=topic\nNTFY_SERVER=https://ntfy.test\nDISCORD_MENTION=<@42>\n"
            f"DISCORD_ALERT_CHANNEL_ID=777\nDISCORD_ALLOWED_USER_IDS=42, 43\nAUREON_DATA={TMP}/data\n")
os.environ["AUREON_ENV_FILE"] = os.path.join(TMP, ".env")

import fake_mt5
from fake_mt5 import World
fake_mt5.install()


class Http:
    sent, down, rate_limited, files = [], set(), {}, []

    @classmethod
    def reset(cls):
        cls.sent, cls.down, cls.rate_limited, cls.files = [], set(), {}, []

    @classmethod
    def to(cls, name):
        return [p for u, p in cls.sent if name in u]


class _Resp:
    def __init__(self, code, headers=None): self.status_code, self.text, self.headers = code, "", headers or {}


def _post(url, json=None, timeout=None, data=None, files=None):
    host = url.split("/")[2].split(".")[0]
    if data is not None:                                              # multipart = card + chart picture
        import json as _json
        json = _json.loads(data["payload_json"]); Http.files.append(files["files[0]"][1])
    if host in Http.down:
        raise requests.RequestException("down")
    if Http.rate_limited.get(host, 0) > 0:
        Http.rate_limited[host] -= 1
        return _Resp(429, {"Retry-After": "0"})
    Http.sent.append((url, json)); return _Resp(204)


requests = types.ModuleType("requests")
requests.RequestException = type("RequestException", (Exception,), {})
requests.post = _post
sys.modules["requests"] = requests

import config as C, signals, notifier, alerts, bot, trader

signals.utc_now = lambda: World.now
alerts._now = lambda: World.now.timestamp()
IST = signals.IST


def titles(payloads):
    return [p["embeds"][0]["title"] for p in payloads if "embeds" in p]


class Base(unittest.TestCase):
    def setUp(self):
        p = mock.patch("time.sleep"); p.start(); self.addCleanup(p.stop)
        World.reset(); Http.reset()
        C.PLACE_ENABLED, C.PLACE_ALLOW_REAL, C.PLACE_ACCOUNT_LOGINS = True, False, set()
        C.PLACE_MAX_LOT = C.PLACE_MAX_RISK = C.PLACE_MAX_POSITIONS = C.PLACE_DAILY_LOSS_LIMIT = None
        for folder in (C.STATE_DIR, C.ALERT_LOG_DIR, C.INBOX_DIR, C.CHART_DIR, C.TRADE_LOG_DIR):
            for name in os.listdir(folder):
                os.remove(os.path.join(folder, name))
        bot.state = notifier.State()
        bot._blind.update(since=None, notified=False); bot._stale_loops = 0
        notifier._last_fail_notice.clear()
        C.SERVER_UTC_OFFSET_HOURS, C.SERVER_TZ = None, "Europe/Athens"
        signals._offset_source = "unset"; signals._clock_note = None
        self.assertIsNone(bot.ensure_mt5())

    @staticmethod
    def _write(path, text):
        with open(path, "w") as f:
            f.write(text)

    def to_close(self, extra_bars=0, after=3):
        """Move the clock to `after` seconds past the next M30 close (+ extra bars)."""
        s = World.now.timestamp()
        World.now = datetime.fromtimestamp((s // 1800 + 1 + extra_bars) * 1800 + after, timezone.utc)

    def run_until_event(self, limit=200):
        for _ in range(limit):
            self.to_close(); before = len(Http.sent)
            bot.check_ema("M30")
            if len(Http.sent) > before:
                return Http.sent[before:]
        self.fail("no event produced")


class Signals(Base):
    def test_alert_arrives_seconds_after_candle_close_with_right_times(self):
        bot.check_ema("M30"); Http.reset()
        new = self.run_until_event()
        self.assertEqual(len(new), 3)                                # Discord + Slack + ntfy, once each
        card = Http.to("discord")[0]
        self.assertEqual(card["embeds"][0]["image"]["url"], "attachment://chart.png")
        self.assertTrue(Http.files[-1].startswith(b"\x89PNG"))        # a real chart picture went with it
        closed_ist = World.now.astimezone(IST).replace(second=0)
        self.assertIn(f"{closed_ist:%d %b %H:%M} IST", json.dumps(card))
        self.assertEqual(World.now.second, 3)                         # sent on the pass 3 s after the close

    def test_forming_candle_is_never_used(self):
        bot.check_ema("M30")
        cp = bot.state.d["last_bar"]["XAUUSD:M30"]
        World.advance(seconds=5)                                      # 10:29:55, candle still open
        with mock.patch.object(signals, "fetch", wraps=signals.fetch) as f:
            bot.check_ema("M30")
            self.assertEqual(f.call_count, 0)                         # no history pull between closes
        self.assertEqual(bot.state.d["last_bar"]["XAUUSD:M30"], cp)
        df = signals.fetch("M30", since=datetime.fromisoformat(cp))
        forming_open = int(World.stamp()) // 1800 * 1800
        self.assertLess(df["time"].iloc[-1].timestamp(), forming_open)

    def test_no_duplicates_same_pass_or_after_restart(self):
        bot.check_ema("M30"); self.run_until_event()
        n = len(Http.sent)
        bot.check_ema("M30"); self.assertEqual(len(Http.sent), n)
        bot.state = notifier.State()                                  # process restart: state re-read from disk
        bot.check_ema("M30"); self.assertEqual(len(Http.sent), n)

    def test_outage_is_replayed_as_one_digest_without_duplicates(self):
        bot.check_ema("M30"); Http.reset()
        World.advance(hours=30)                                       # bot was down for 60 candles
        bot.check_ema("M30")
        d = titles(Http.to("discord"))
        self.assertEqual(len(d), 1); self.assertIn("MISSED WHILE OFFLINE", d[0])
        newest_closed = int(World.stamp()) // 1800 * 1800 - 1800
        self.assertEqual(datetime.fromisoformat(bot.state.d["last_bar"]["XAUUSD:M30"]).replace(tzinfo=timezone.utc).timestamp(), newest_closed)
        n = len(Http.sent); bot.check_ema("M30"); self.assertEqual(len(Http.sent), n)

    def test_ema200_matches_full_history(self):
        import pandas as pd
        bot.check_ema("M30")
        df = signals.fetch("M30", since=datetime.fromisoformat(bot.state.d["last_bar"]["XAUUSD:M30"]))
        full = fake_mt5._bars("M30")[:-1]
        ref = pd.Series(full["close"]).ewm(span=200, adjust=False).mean().iloc[-1]
        self.assertAlmostEqual(df["ema200"].iloc[-1], ref, delta=0.01)

    def test_detect_cross_direction_and_trend_tag(self):
        import pandas as pd
        n = 400
        close = [4000.0] * 300 + [4000.0 + 3 * k for k in range(1, 101)]          # flat, then a steady rally
        df = pd.DataFrame({"time": pd.date_range("2026-01-01", periods=n, freq="30min"), "close": close})
        df["high"], df["low"] = df.close + 1, df.close - 1
        for p in (8, 32, 200): df[f"ema{p}"] = df.close.ewm(span=p, adjust=False).mean()
        df["atr"] = 2.0
        df.loc[299, ["ema8"]] = df.loc[299, "ema32"] - 0.01                          # fast just below slow before the rally
        ev = signals.detect(df.iloc[:301])
        cross = [e for e in ev if e["type"].startswith("CROSS")]
        self.assertEqual([(e["type"], e["dir"]) for e in cross], [("CROSS_AFTER_TREND", "BULL")])
        self.assertEqual([e for e in signals.detect(df.iloc[:350]) if e["type"].startswith("CROSS")], [])


class Clock(Base):
    def test_config_offset_and_ist(self):
        self.assertEqual(signals._server_offset, timedelta(hours=2))
        self.assertEqual(signals.to_ist(datetime(2026, 11, 10, 12, 30)).strftime("%H:%M"), "16:00")

    def test_stale_tick_never_changes_the_clock(self):
        World.frozen_tick = World.now - timedelta(minutes=45)
        for _ in range(6):
            World.advance(seconds=10); p = signals.health()
        self.assertTrue(p.startswith("stale tick"))
        self.assertEqual(signals._server_offset, timedelta(hours=2))
        signals.sync_server_offset()
        self.assertEqual(signals._server_offset, timedelta(hours=2))

    def _poll(self, n):
        for _ in range(n):
            World.advance(seconds=10); r = signals.health()
        return r

    def test_feed_stamped_in_plain_utc_is_detected_and_used(self):
        World.stamp_offset = timedelta(0)                             # the MetaQuotes-docs case
        self.assertIsNone(self._poll(5))
        self.assertEqual(signals._server_offset, timedelta(0))
        self.assertIn("measured", signals.offset_text())
        self.assertIn("UTC+0", signals.pop_clock_note())
        bot.check_ema("M30")                                          # and candles are read up to the real newest one
        newest_closed = int(World.stamp()) // 1800 * 1800 - 1800
        self.assertEqual(datetime.fromisoformat(bot.state.d["last_bar"]["XAUUSD:M30"]).replace(tzinfo=timezone.utc).timestamp(), newest_closed)

    def test_broker_on_other_offset_is_detected(self):
        World.stamp_offset = timedelta(hours=3)                       # e.g. different DST dates than config
        self.assertIsNone(self._poll(5))
        self.assertEqual(signals._server_offset, timedelta(hours=3))

    def test_clock_converges_through_the_real_reconnect_loop(self):
        for hours in (3, 0, -5, 5.5, 2):                              # feed ahead of, behind, and equal to config
            World.reset(); World.stamp_offset = timedelta(hours=hours)
            signals._offset_source, bot._stale_loops = "unset", 0
            for loops in range(12):
                if bot.ensure_mt5() is None:
                    break
                World.advance(seconds=10)
            self.assertIsNone(bot.ensure_mt5(), f"feed UTC{hours:+g} never became healthy")
            self.assertEqual(signals._server_offset, timedelta(hours=hours))
            self.assertLessEqual(loops, 4)                            # within ~40 s
            bot.check_ema("M30"); bot.state = notifier.State()

    def test_wrong_clock_is_reported_until_corrected(self):
        World.stamp_offset = timedelta(hours=3)
        World.advance(seconds=10)
        self.assertIn("clock mismatch", signals.health())

    def test_pause_window_follows_the_gold_week(self):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Kolkata")
        left = lambda s: bot.pause_seconds_left(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=tz))
        self.assertEqual(left("2026-11-14 03:00"), 0)                 # Sat 03:00 IST: market still open
        self.assertAlmostEqual(left("2026-11-14 04:00") / 3600, 47 + 25 / 60, places=2)
        self.assertGreater(left("2026-11-15 12:00"), 0)
        self.assertEqual(left("2026-11-16 03:25"), 0)                 # Mon 03:25 IST: back on
        self.assertEqual(left("2026-11-11 15:00"), 0)

    def test_wakes_right_after_the_close(self):
        self.assertEqual(bot.next_sleep(), 13)                        # 10 s to the close + 3
        World.advance(seconds=-20)
        self.assertEqual(bot.next_sleep(), C.POLL_SECONDS)


class Delivery(Base):
    def test_dead_service_does_not_block_others_and_is_retried_once(self):
        Http.down.add("slack")
        self.assertTrue(notifier.deliver(bot.state, notifier.card("hello", ping=True)))
        self.assertEqual(len(Http.to("discord")), 2)                  # the alert + "Slack is failing" notice
        self.assertEqual([p["to"] for p in bot.state.d["pending"]], [["Slack"]])
        for i in range(5): bot.state.queue(f"m{i}", ["Slack"])
        notifier.flush_pending(bot.state); self.assertEqual(len(bot.state.d["pending"]), 6)
        Http.down.clear(); notifier.flush_pending(bot.state)
        self.assertEqual(bot.state.d["pending"], []); self.assertEqual(len(Http.to("slack")), 6)
        notifier.flush_pending(bot.state); self.assertEqual(len(Http.to("slack")), 6)

    def test_rate_limit_is_retried(self):
        Http.rate_limited["discord"] = 1
        st = notifier.send("x"); self.assertEqual(st["Discord"], "ok")

    def test_ping_card_buzzes_phone(self):
        notifier.send(notifier.card("T", ping=True))
        self.assertEqual(Http.to("discord")[0]["content"], "<@42> T")
        self.assertEqual(Http.to("ntfy")[0]["priority"], 5)

    def test_corrupt_state_recovers_and_keeps_dedupe(self):
        bot.state.mark("K1"); bot.state.mark("K2")
        self._write(C.STATE_FILE, "{broken")
        s = notifier.State(); self.assertIn("restored from .bak", s.problem); self.assertTrue(s.seen("K1"))
        notifier._logfile("evt", "K9")
        self._write(C.STATE_FILE, "{broken"); self._write(C.STATE_FILE + ".bak", "{broken")
        s = notifier.State(); self.assertIn("rebuilt", s.problem); self.assertTrue(s.seen("K9"))

    def test_blind_and_recovery_notices(self):
        with mock.patch.object(C, "BLIND_NOTICE_SECONDS", 0):
            World.connected = False; World.initialised = False
            p = bot.ensure_mt5(); self.assertIsNotNone(p)
            bot.report_blind(p); bot.report_blind(p)
            self.assertEqual(sum("BLIND" in t for t in titles(Http.to("discord"))), 1)
            World.connected = True
            self.assertIsNone(bot.ensure_mt5()); bot.report_recovered(); bot.report_recovered()
            self.assertEqual(sum("feed is back" in t for t in titles(Http.to("discord"))), 1)

    def test_start_card_once_and_no_same_day_alive_ping(self):
        bot.announce_start(); bot.announce_start(); bot.alive_ping()
        self.assertEqual(len(Http.to("discord")), 1)


class PriceAlerts(Base):
    def _bar(self):
        return int(World.stamp()) // 60 * 60

    def test_touch_before_creation_in_same_minute_is_ignored(self):
        World.now = World.now.replace(second=20)
        level = fake_mt5.price(World.stamp()) + 5
        World.m1_override[self._bar()] = (level - 8, level + 1)       # this minute already traded through the level
        World.ticks = [(World.stamp() - 15, level + 0.5)]             # ... 15 s BEFORE the alert exists
        alerts.submit(f"add {level}", wait_s=0)
        for _ in range(4):
            bot.process_commands(); bot.check_price_alerts("M30"); World.advance(seconds=10)
        self.assertEqual(titles(Http.to("discord")), [])
        self.assertEqual(len(alerts.pending()), 1)

    def test_touch_and_reverse_right_after_creation_is_caught(self):
        World.now = World.now.replace(second=20)
        level = fake_mt5.price(World.stamp()) + 5
        alerts.submit(f"add {level}", wait_s=0); bot.process_commands(); bot.check_price_alerts("M30")
        World.advance(seconds=4); World.ticks = [(World.stamp(), level + 0.2)]   # spike between two polls
        World.advance(seconds=6); bot.check_price_alerts("M30")
        self.assertEqual(len([t for t in titles(Http.to("discord")) if "HIT" in t]), 1)
        self.assertEqual(alerts.pending(), [])
        bot.check_price_alerts("M30")
        self.assertEqual(len([t for t in titles(Http.to("discord")) if "HIT" in t]), 1)   # acked: no repeat

    def test_later_touch_is_caught_from_m1_bars(self):
        level = fake_mt5.price(World.stamp()) + 40
        alerts.submit(f"add {level}", wait_s=0); bot.process_commands(); bot.check_price_alerts("M30")
        World.advance(minutes=10); World.m1_override[self._bar() - 120] = (level - 50, level + 1)
        bot.check_price_alerts("M30")
        self.assertEqual(len([t for t in titles(Http.to("discord")) if "HIT" in t]), 1)

    def test_discord_command_authorisation(self):
        self.assertTrue(alerts.authorised(42, 777))
        self.assertFalse(alerts.authorised(99, 777))                  # right channel, wrong person
        self.assertFalse(alerts.authorised(42, 123))                  # right person, wrong channel


class Trading(Base):
    """/place: the bot loop's side of it (the Discord buttons only forward these requests)."""
    REQ = {"side": "buy", "lot": 0.5, "sl": 20, "tp": 50, "filling": "auto", "user": 42}

    def send(self, cmd, **over):
        """Queue a command exactly as the Discord side does, let the bot loop handle it, return the reply."""
        import threading
        out = {}
        t = threading.Thread(target=lambda: out.update(alerts.request(cmd, {**self.REQ, **over}, wait_s=2)))
        with mock.patch("time.sleep", lambda s: __import__("threading").Event().wait(0.01)):
            t.start()
            for _ in range(200):
                if alerts.inbox_waiting():
                    break
                __import__("threading").Event().wait(0.005)
            bot.process_commands(); t.join()
        return out

    def test_no_order_is_ever_sent_by_the_signal_loop(self):
        bot.check_ema("M30")
        for _ in range(60):
            self.to_close(); bot.check_ema("M30"); bot.check_price_alerts("M30")
        self.assertGreater(len(Http.sent), 0)                         # alerts happened ...
        self.assertEqual(World.orders, [])                            # ... and not one order

    def test_quote_previews_without_sending(self):
        r = self.send("quote")
        self.assertTrue(r["ok"]); self.assertIn("BUY 0.5 XAUUSD", r["text"]); self.assertIn("DEMO", r["text"])
        self.assertIn("-1,000.00 USD", r["text"]); self.assertIn("+2,500.00 USD", r["text"])   # 0.5 lot: $20 stop, $50 target
        self.assertEqual(World.orders, [])

    def test_buy_places_market_order_with_sl_and_tp_from_distances(self):
        r = self.send("place")
        self.assertTrue(r["ok"], r); o = World.orders[0]
        ask = round(fake_mt5.price(World.stamp()) + World.spread, 2)
        self.assertAlmostEqual(o["price"], ask, places=2)
        self.assertAlmostEqual(o["sl"], ask - 20, places=2); self.assertAlmostEqual(o["tp"], ask + 50, places=2)
        self.assertEqual((o["volume"], o["type"], o["type_filling"]), (0.5, 0, 0))   # buy, FOK (the symbol's mode)
        self.assertIn("FILLED · position", r["text"]); self.assertIn("both set on the position", r["text"])
        self.assertEqual(len(os.listdir(C.TRADE_LOG_DIR)), 1)         # journalled

    def test_sell_with_absolute_prices(self):
        bid = fake_mt5.price(World.stamp())
        r = self.send("place", side="sell", sl=round(bid + 15, 2), tp=round(bid - 30, 2))
        self.assertTrue(r["ok"], r); o = World.orders[0]
        self.assertEqual(o["type"], 1); self.assertAlmostEqual(o["sl"], round(bid + 15, 2)); self.assertAlmostEqual(o["tp"], round(bid - 30, 2))

    def test_sl_and_tp_are_mandatory_and_must_be_on_the_right_side(self):
        for bad in ({"sl": 0}, {"tp": None}, {"sl": 9999.0}, {"lot": 0}, {"lot": 0.005}, {"sl": 0.1}, {"side": "hold"}):
            r = self.send("place", **bad)
            self.assertFalse(r["ok"], bad); self.assertIn("NOT placed", r["text"])
        self.assertEqual(World.orders, [])

    def test_switched_off_means_no_orders(self):
        C.PLACE_ENABLED = False
        self.assertIn("switched off", self.send("place")["text"]); self.assertEqual(World.orders, [])

    def test_algo_trading_off_is_explained(self):
        World.algo_trading = False
        self.assertIn("Algo Trading is OFF", self.send("place")["text"]); self.assertEqual(World.orders, [])

    def test_auto_filling_falls_back_when_broker_rejects_the_type(self):
        World.retcodes = [10030]                                      # "unsupported filling" for FOK
        r = self.send("place")
        self.assertTrue(r["ok"], r); self.assertEqual([o["type_filling"] for o in World.orders], [0, 2])

    def test_chosen_filling_is_not_silently_changed(self):
        World.retcodes = [10030]
        r = self.send("place", filling="ioc")
        self.assertFalse(r["ok"]); self.assertEqual([o["type_filling"] for o in World.orders], [1])

    def test_broker_rejection_is_reported_and_not_retried(self):
        World.retcodes = [10019]                                      # no money
        r = self.send("place")
        self.assertFalse(r["ok"]); self.assertIn("10019", r["text"]); self.assertEqual(len(World.orders), 1)

    def test_requote_is_retried_once_at_the_new_price(self):
        World.retcodes = [10004]
        self.assertTrue(self.send("place")["ok"]); self.assertEqual(len(World.orders), 2)

    def test_stale_command_is_refused(self):
        alerts.request("place", self.REQ, wait_s=0)                   # nobody picked it up -> withdrawn
        self.assertFalse(alerts.inbox_waiting())
        with open(os.path.join(C.INBOX_DIR, "old.cmd"), "w") as f:    # a command that sat for a minute
            json.dump(alerts.envelope("place", self.REQ, ts=World.now.timestamp() - 60), f)
        bot.process_commands()
        self.assertEqual(World.orders, [])
        with open(os.path.join(C.INBOX_DIR, "old.reply")) as f:
            self.assertIn("NOT done", f.read())

    def test_a_crash_cannot_run_a_place_command_twice(self):
        with open(os.path.join(C.INBOX_DIR, "x.cmd"), "w") as f:
            json.dump(alerts.envelope("place", self.REQ), f)
        with mock.patch.object(bot, "handle_json_command", side_effect=SystemExit):   # process dies mid-order
            with self.assertRaises(SystemExit):
                bot.process_commands()
        self.assertFalse(alerts.inbox_waiting())                      # the command file is already gone

    def test_only_listed_users_may_trade(self):
        self.assertTrue(alerts.can_trade(42, 777)); self.assertFalse(alerts.can_trade(99, 777))
        with mock.patch.object(C, "DISCORD_ALLOWED_USER_IDS", set()):
            self.assertFalse(alerts.can_trade(42, 777))               # nobody listed = nobody trades

    def _drop(self, name, content):
        with open(os.path.join(C.INBOX_DIR, name + ".cmd"), "w") as f:
            json.dump(content, f)
        bot.process_commands()
        with open(os.path.join(C.INBOX_DIR, name + ".reply")) as f:
            return json.load(f)

    def test_command_file_from_another_program_is_refused(self):
        forged = {**self.REQ, "cmd": "place", "ts": World.now.timestamp(), "id": "x"}
        for content in (forged, {"body": forged, "sig": "0" * 64}, {"body": forged}):
            self.assertIn("Refused", self._drop("forged", content)["text"])
        self.assertEqual(World.orders, [])

    def test_a_copied_command_cannot_be_replayed(self):
        env = alerts.envelope("place", self.REQ)
        self.assertTrue(self._drop("first", env)["ok"])
        self.assertIn("Refused", self._drop("copy", env)["text"])
        self.assertEqual(len(World.orders), 1)

    def test_user_is_checked_again_where_the_order_is_sent(self):
        for user in (99, None, "42"):
            r = self.send("place", user=user)
            self.assertIn("not in DISCORD_ALLOWED_USER_IDS", r["text"])
        self.assertIn("not in DISCORD_ALLOWED_USER_IDS", self.send("quote", user=99)["text"])
        self.assertEqual(World.orders, [])

    def test_real_account_needs_its_own_switch(self):
        World.account_mode = 2
        r = self.send("place")
        self.assertIn("REAL account", r["text"]); self.assertIn("PLACE_ALLOW_REAL=1", r["text"]); self.assertEqual(World.orders, [])
        C.PLACE_ALLOW_REAL = True
        r = self.send("place")
        self.assertTrue(r["ok"]); self.assertIn("REAL · 555", r["text"])

    def test_account_allowlist(self):
        C.PLACE_ACCOUNT_LOGINS = {111}
        self.assertIn("not in PLACE_ACCOUNT_LOGINS", self.send("place")["text"])
        C.PLACE_ACCOUNT_LOGINS = {555}
        self.assertTrue(self.send("place")["ok"])

    def test_optional_limits(self):
        C.PLACE_MAX_LOT = 0.3
        self.assertIn("PLACE_MAX_LOT", self.send("place")["text"]); C.PLACE_MAX_LOT = None
        C.PLACE_MAX_RISK = 999                                         # this order risks 1,000
        self.assertIn("PLACE_MAX_RISK", self.send("place")["text"]); C.PLACE_MAX_RISK = 1000
        self.assertTrue(self.send("place")["ok"])                     # exactly at the limit is allowed
        C.PLACE_MAX_POSITIONS = 1                                     # one is open now
        self.assertIn("PLACE_MAX_POSITIONS", self.send("place")["text"]); C.PLACE_MAX_POSITIONS = None
        C.PLACE_DAILY_LOSS_LIMIT = 500
        World.deals = [types.SimpleNamespace(type=0, profit=-300.0, swap=0, commission=-5.0, fee=0),
                       types.SimpleNamespace(type=2, profit=10000.0, swap=0, commission=0, fee=0)]   # a deposit: ignored
        self.assertTrue(self.send("place")["ok"])                     # -305 so far
        World.deals.append(types.SimpleNamespace(type=1, profit=-200.0, swap=0, commission=0, fee=0))
        self.assertIn("PLACE_DAILY_LOSS_LIMIT", self.send("place")["text"])
        self.assertEqual(len([o for o in World.orders if o["action"] == 1]), 2)

    def test_no_precheck_means_no_order(self):
        World.check_unavailable = True
        self.assertIn("could not pre-check", self.send("place")["text"]); self.assertEqual(World.orders, [])

    def test_partial_fill_is_reported_as_partial(self):
        World.retcodes = [10010]
        r = self.send("place")
        self.assertIn("PARTLY FILLED · 0.3 of 0.5", r["text"]); self.assertNotIn("✅", r["text"])

    def test_accepted_but_not_filled_is_not_called_filled(self):
        World.retcodes = [10008]
        r = self.send("place")
        self.assertIn("ACCEPTED, not filled yet", r["text"]); self.assertIn("CHECK MT5", r["text"]); self.assertNotIn("✅", r["text"])

    def test_missing_sl_tp_on_the_position_is_repaired_or_shouted(self):
        World.drop_sltp = True
        r = self.send("place")
        self.assertIn("both set on the position", r["text"])
        self.assertEqual([o["action"] for o in World.orders], [1, 6])  # the order, then the SL/TP fix
        self.assertGreater(World.positions[0].sl, 0)
        World.sltp_fails = True
        r = self.send("place")
        self.assertIn("OPEN WITHOUT ITS SL/TP", r["text"])

    def test_chart_command_returns_a_picture(self):
        r = self.send("chart")
        self.assertTrue(r["ok"]); self.assertTrue(os.path.getsize(r["image"]) > 5000)


class Config(Base):
    def tearDown(self):
        C.DISCORD_BOT_TOKEN = ""

    def test_validate(self):
        self.assertEqual(len(C.validate()), 1)                        # /place on, but no bot token -> refused at startup
        C.DISCORD_BOT_TOKEN = "token"
        self.assertEqual(C.validate(), [])
        with mock.patch.object(C, "PAUSE_END", (0, "3:25")), mock.patch.object(C, "WARMUP_BARS", 250):
            self.assertEqual(len(C.validate()), 2)

    def test_discord_commands_register_with_dropdowns(self):
        try:
            import discord  # noqa: F401
        except ImportError:
            self.skipTest("discord.py not installed")
        tree = alerts.build_discord_client().tree
        cmds = {c.name: c for c in tree.get_commands()}
        self.assertEqual(set(cmds), {"chart", "status", "place", "alert", "alert_remove", "alerts", "positions", "monitor", "unmonitor", "last-week"})
        params = {p.name: p for p in cmds["place"].parameters}
        self.assertEqual([c.value for c in params["side"].choices], ["buy", "sell"])
        self.assertEqual([c.value for c in params["filling"].choices], ["auto", "fok", "ioc", "return"])
        self.assertTrue(all(params[n].required for n in ("side", "lot", "sl", "tp")))
        self.assertFalse(params["filling"].required)


class ConfirmButton(unittest.IsolatedAsyncioTestCase):
    """The Discord Confirm view: one click = one order, and nothing after it expires."""

    def setUp(self):
        try:
            import discord  # noqa: F401
        except ImportError:
            self.skipTest("discord.py not installed")
        self.calls = []
        p = mock.patch.object(alerts, "request", lambda cmd, payload=None, wait_s=8.0:
                              self.calls.append(cmd) or {"ok": True, "text": "FILLED"})
        p.start(); self.addCleanup(p.stop)

    def _click(self, user=42):
        inter = mock.MagicMock(); inter.user.id = user
        inter.response.defer = mock.AsyncMock(); inter.response.send_message = mock.AsyncMock()
        inter.edit_original_response = mock.AsyncMock()
        return inter

    async def _press(self, view, button, inter):
        if await view.interaction_check(inter):                      # what Discord's dispatcher does
            await button.callback(inter)

    async def test_double_click_sends_one_order(self):
        view = alerts.build_discord_client().Confirm(42, {"side": "buy"})
        a, b = self._click(), self._click()
        await __import__("asyncio").gather(self._press(view, view.confirm, a), self._press(view, view.confirm, b))
        self.assertEqual(self.calls, ["place"])
        self.assertTrue(all(i.disabled for i in view.children))

    async def test_other_user_cannot_confirm(self):
        view = alerts.build_discord_client().Confirm(42, {})
        await self._press(view, view.confirm, self._click(user=43))
        self.assertEqual(self.calls, [])

    async def test_cancel_then_confirm_places_nothing(self):
        view = alerts.build_discord_client().Confirm(42, {})
        await self._press(view, view.cancel, self._click())
        await self._press(view, view.confirm, self._click())
        self.assertEqual(self.calls, [])

    async def test_expired_view_places_nothing(self):
        view = alerts.build_discord_client().Confirm(42, {}); view.message = mock.MagicMock(); view.message.edit = mock.AsyncMock()
        await view.on_timeout()
        self.assertIn("Expired", view.message.edit.call_args.kwargs["content"])
        await self._press(view, view.confirm, self._click())
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
