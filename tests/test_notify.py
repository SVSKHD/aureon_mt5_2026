"""1.7.3: notification intelligence (notify*.py) and the trade management switch (trade_mgmt.py).
Same isolated harness as test_bot.py: fake MT5, fake webhooks, temp data folder. Nothing here can place an order on
a real account or post to a real webhook."""
import asyncio, glob, json, os, sys, threading, unittest
from datetime import timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bot import Http, titles, World                  # noqa: E402  (isolated environment first)
from test_v170 import V170, records                       # noqa: E402
from test_positions import Pos, sltp_orders               # noqa: E402
import fake_mt5                                            # noqa: E402
import config as C, notifier, bot, positions, trader, alerts, store   # noqa: E402
import notify, notify_events as E, notify_policy as P, trade_mgmt     # noqa: E402

SVC = {"discord": "Discord", "slack": "Slack", "ntfy": "Ntfy"}


def sent_titles(service):
    return titles(Http.to(service)) + [p.get("title") or p.get("text", "") for p in Http.to(service)
                                       if "embeds" not in p]


def by_service(sent):
    """Webhook payloads per service, in order (services are posted in parallel, so only this order is fixed)."""
    out = {}
    for url, payload in sent:
        out.setdefault(url, []).append(payload)
    return out


def audit_rows():
    out = []
    for p in sorted(glob.glob(os.path.join(C.NOTIFY_AUDIT_DIR, "*.jsonl"))):
        with open(p, encoding="utf-8") as f:
            out += [json.loads(x) for x in f if x.strip()]
    return out


def outbox():
    out = []
    for p in sorted(glob.glob(os.path.join(C.OUTBOX_DIR, "*.json"))):
        with open(p) as f:
            out.append(json.load(f))
    return out


class NotifyBase(V170):
    def setUp(self):
        super().setUp()
        notify.wait_idle()
        notify.reset()
        for p in glob.glob(os.path.join(C.NOTIFY_AUDIT_DIR, "*")):
            os.remove(p)
        p = mock.patch.object(notify, "_now", lambda: World.now.timestamp()); p.start(); self.addCleanup(p.stop)
        for key, val in (("NOTIFY_MODE", "standard"), ("NOTIFY_QUIET_HOURS", ""), ("NOTIFY_DISABLED_SERVICES", set()),
                         ("NOTIFY_SYMBOL_PREFS", ""), ("NOTIFY_ASSEMBLER_EXPORT", False),
                         ("NOTIFY_MIN_SEVERITY", {"Discord": "low", "Slack": "high", "Ntfy": "high"})):
            p = mock.patch.object(C, key, val); p.start(); self.addCleanup(p.stop)
        self.addCleanup(notify.reset)
        self.addCleanup(notify.wait_idle)
        trade_mgmt.reset(False)

    def set_mode(self, m):
        notify.ns()["mode"] = m; notify.save()

    def deliver(self, msg, key=None):
        return notifier.deliver(bot.state, msg, key=key)

    def tick(self, advance_s=0):
        World.now += timedelta(seconds=advance_s)
        notify.tick(bot.state)
        self.assertTrue(notify.wait_idle())
        notify.tick(bot.state)                                    # apply the results

    def cmd(self, name, user=42, **kw):
        return bot.handle_json_command({"cmd": name, "ts": World.now.timestamp(), "id": f"{name}{kw}{user}",
                                        "user": user, **kw})

    @staticmethod
    def high(symbol="XAUUSD", title="CROSS ▲", **kw):
        return notifier.card(f"{symbol} {title}", channel="alerts", symbol=symbol, kind="CROSS", ping=True, **kw)

    @staticmethod
    def normal(symbol="XAUUSD", title="GAP WIDENING", **kw):
        return notifier.card(f"{symbol} {title}", channel="updates", symbol=symbol, kind="GAP WIDENING", **kw)

    @staticmethod
    def critical(title="bot is BLIND", **kw):
        return notifier.warn(title, "terminal gone", ping=True, symbol="XAUUSD", kind="BLIND", **kw)


# ============================================================================================== severity
class Severity(unittest.TestCase):
    def test_fixed_table_and_escalations(self):
        c = lambda **kw: E.classify(notifier.card("t", **kw))
        self.assertEqual(c(kind="BLIND"), "critical")
        self.assertEqual(c(kind="TSL_FAILED"), "critical")
        self.assertEqual(c(kind="TRADE_MANAGEMENT"), "critical")
        self.assertEqual(c(kind="CROSS"), "high")
        self.assertEqual(c(kind="GAP WIDENING"), "normal")
        self.assertEqual(c(kind="START"), "low")
        self.assertEqual(c(kind="SOMETHING_NEW"), "high")                     # unknown: never quietly digested
        self.assertEqual(c(kind="START", ping=True), "high")                  # buzzes the phone -> at least high
        self.assertEqual(c(kind="GAP WIDENING", buttons=[notifier.button("x", "y")]), "high")   # actionable
        self.assertEqual(c(kind="POSITION_OPENED", ping=True), "critical")    # opened without a stop-loss
        self.assertEqual(E.classify("⚠️ Slack webhook is failing"), "high")

    def test_policy_is_deterministic_and_pure(self):
        s = P.Settings(mode="intelligent")
        ev = E.from_card(notifier.card("x", kind="GAP WIDENING", symbol="XAUUSD", channel="updates"), now=1000.0)
        mem = {"recent": {}, "rate": {}}
        from datetime import datetime
        local = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        a = P.decide(ev, s, mem, 1000.0, ["Discord", "Slack"], local, False)
        b = P.decide(ev, s, mem, 1000.0, ["Discord", "Slack"], local, False)
        self.assertEqual(a, b)
        self.assertEqual(mem, {"recent": {}, "rate": {}})                    # decide() never changes its inputs
        self.assertEqual(a.action, "group")


# ======================================================================================== standard parity
class StandardParity(NotifyBase):
    def scenario(self):
        """Fake MT5 candles through the real loop path + a few system cards. Returns every webhook payload."""
        World.reset(); Http.reset(); bot.state = notifier.State(); bot.reset_link(); notify.reset()
        for p in glob.glob(os.path.join(C.STATE_DIR, "*")):
            os.remove(p)
        bot.state = notifier.State()
        self.assertIsNone(bot.ensure_mt5())
        bot.check_ema("M30")
        for _ in range(60):
            World.advance(minutes=30); bot.check_ema("M30")
        notifier.deliver(bot.state, notifier.info("▶️ bot online", symbol="XAUUSD", channel="updates", kind="START"))
        notifier.deliver(bot.state, self.critical())
        return list(Http.sent)

    def test_standard_mode_sends_exactly_what_the_old_path_sends(self):
        new = self.scenario()
        self.assertGreater(len(new), 10)
        with mock.patch.dict(sys.modules, {"notify": None}), mock.patch.object(notifier.log, "exception"):
            old = self.scenario()                                 # `import notify` fails -> the pre-1.7.3 path
        self.assertEqual(by_service(new), by_service(old))
        self.assertFalse(os.path.exists(C.NOTIFY_STATE_FILE))     # standard mode keeps no notify state
        rows = audit_rows()
        self.assertTrue(rows and all(r["mode"] == "standard" and r["action"] == "send" for r in rows))

    def test_default_mode_is_standard(self):
        self.assertEqual(notify.mode(), "standard")
        with mock.patch.object(C, "NOTIFY_MODE", "bogus"):
            self.assertEqual(notify.mode(), "standard")           # bad .env value: standard (validate() says why)
            self.assertTrue(any("NOTIFY_MODE" in x for x in C.notify_problems()))

    def test_policy_crash_falls_back_to_standard_delivery(self):
        self.set_mode("intelligent")
        with mock.patch.object(notify, "route", side_effect=RuntimeError("boom")), \
                mock.patch.object(notifier.log, "exception"):
            self.assertTrue(self.deliver(self.normal()))
        self.assertIn("XAUUSD GAP WIDENING", sent_titles("discord"))   # never lost


# ========================================================================================== routing
class Routing(NotifyBase):
    def setUp(self):
        super().setUp()
        self.set_mode("intelligent")

    def test_high_goes_at_once_to_the_alert_services(self):
        self.assertTrue(self.deliver(self.high()))
        for svc in ("discord", "slack", "ntfy"):
            self.assertEqual(len(Http.to(svc)), 1, svc)
        self.assertIn("HIGH", Http.to("discord")[0]["embeds"][0]["footer"]["text"])

    def test_normal_is_grouped_then_sent_to_discord_only(self):
        for t in ("GAP WIDENING", "WIDE GAP", "PULLBACK STARTED"):
            self.assertTrue(self.deliver(self.normal(title=t)))
        self.assertEqual(Http.sent, [])                           # held
        self.tick(30)
        self.assertEqual(Http.sent, [])                           # group window not over
        self.tick(31)
        cards = Http.to("discord")
        self.assertEqual(len(cards), 1)
        emb = cards[0]["embeds"][0]
        self.assertIn("3 events", emb["title"])
        for t in ("GAP WIDENING", "WIDE GAP", "PULLBACK STARTED"):
            self.assertIn(t, emb["description"])
        self.assertEqual(Http.to("slack") + Http.to("ntfy"), [])  # min severity high there
        self.assertEqual(notify.ns()["buffer"], [])
        self.assertEqual(notify.ns()["flushing"], {})

    def test_low_waits_for_the_digest(self):
        self.deliver(notifier.info("▶️ online", symbol="XAUUSD", channel="updates", kind="START"))
        self.tick(120)
        self.assertEqual(Http.sent, [])
        self.tick(30 * 60)
        self.assertEqual(len(Http.to("discord")), 1)
        self.assertIn("DIGEST", Http.to("discord")[0]["embeds"][0]["title"])

    def test_held_items_ride_along_on_the_next_card_of_their_symbol(self):
        self.deliver(self.normal())
        self.deliver(self.high())
        emb = Http.to("discord")[0]["embeds"][0]
        self.assertIn("GAP WIDENING", json.dumps(emb, ensure_ascii=False))
        self.assertEqual(notify.ns()["buffer"], [])
        self.tick(120)
        self.assertEqual(len(Http.to("discord")), 1)              # not repeated later

    def test_context_lines_on_cards_that_go_out(self):
        self.deliver(self.high(title="CROSS ▲"))
        World.advance(minutes=10)
        self.deliver(self.high(title="PRE-CROSS ▼"))
        emb = Http.to("discord")[1]["embeds"][0]
        self.assertIn("Earlier this hour", [f["name"] for f in emb["fields"]])

    def test_edited_in_place_and_chart_cards_pass_through(self):
        notifier.BOT_CLIENT["on"] = True
        mon = notifier.info("👀 MONITOR", symbol="XAUUSD", channel="updates", kind="MONITOR", upsert="monitor:XAUUSD")
        self.deliver(mon)
        self.assertEqual([i["kind"] for i in outbox()], ["MONITOR"])

    def test_secrets_never_reach_a_notification(self):
        for mode in ("standard", "intelligent"):
            Http.reset(); self.set_mode(mode)
            self.deliver(notifier.warn("leak test", f"url {C.SLACK_WEBHOOK} topic {C.NTFY_TOPIC}x", kind="WARN"))
            blob = json.dumps([p for _, p in Http.sent])
            self.assertNotIn(C.SLACK_WEBHOOK, blob, mode); self.assertIn("[redacted]", blob)


# ================================================================================= critical bypass
class CriticalBypass(NotifyBase):
    def setUp(self):
        super().setUp()
        self.set_mode("intelligent")
        p = mock.patch.object(C, "NOTIFY_QUIET_HOURS", "00:00-23:59"); p.start(); self.addCleanup(p.stop)

    def test_critical_in_quiet_hours_goes_everywhere_at_once_with_the_buzz(self):
        self.deliver(self.critical())
        for svc in ("discord", "slack", "ntfy"):
            self.assertEqual(len(Http.to(svc)), 1, svc)
        self.assertIn("<@42>", Http.to("discord")[0].get("content", ""))
        self.assertEqual(Http.to("ntfy")[0]["priority"], 5)

    def test_critical_on_a_discord_only_channel_still_reaches_every_service(self):
        self.deliver(notifier.warn("TSL step NOT applied", "x", ping=True, symbol="XAUUSD", channel="positions",
                                   kind="TSL_FAILED"))
        self.assertEqual(len(Http.to("slack")), 1); self.assertEqual(len(Http.to("ntfy")), 1)

    def test_high_in_quiet_hours_is_sent_without_the_buzz_and_normal_waits(self):
        self.deliver(self.high())
        d = Http.to("discord")[0]
        self.assertNotIn("content", d)                            # no mention line
        self.assertEqual(Http.to("ntfy")[0]["priority"], 2)
        self.assertIn("quiet hours", d["embeds"][0]["footer"]["text"])
        self.deliver(self.normal())
        self.tick(120)
        self.assertEqual(len(Http.to("discord")), 1)              # held until the quiet window ends

    def test_critical_bypasses_rate_limit_digest_and_duplicates(self):
        with mock.patch.object(C, "NOTIFY_RATE_MAX", "2"):
            for i in range(4):
                self.deliver(self.high(title=f"CROSS {i}"))
            self.assertEqual(len(Http.to("slack")), 2)            # rate limited -> grouped
            self.deliver(self.critical()); self.deliver(self.critical())     # identical criticals: both go out
        self.assertEqual(sum("BLIND" in t for t in sent_titles("slack")), 2)

    def test_unrelated_duplicate_never_holds_back_a_new_critical(self):
        self.deliver(self.normal(title="BLIND-looking update"))
        self.deliver(self.normal(title="BLIND-looking update"))          # duplicate: dropped
        self.deliver(self.critical())
        self.assertEqual(sum("BLIND" in t for t in sent_titles("discord")), 1)
        self.assertEqual([r["action"] for r in audit_rows()], ["group", "duplicate", "send"])


# ============================================================================= dedupe and grouping
class DedupeAndGrouping(NotifyBase):
    def setUp(self):
        super().setUp()
        self.set_mode("intelligent")

    def test_identical_non_critical_card_is_suppressed_inside_the_window_only(self):
        self.deliver(self.high()); self.deliver(self.high())
        self.assertEqual(len(Http.to("discord")), 1)
        World.advance(minutes=11)
        self.deliver(self.high())
        self.assertEqual(len(Http.to("discord")), 2)

    def test_same_dedupe_key_is_held_once(self):
        self.deliver(self.normal(), key="XAUUSD:M30:GAP WIDENING:SHORT:t1")
        self.deliver(self.normal(title="GAP WIDENING again"), key="XAUUSD:M30:GAP WIDENING:SHORT:t1")
        self.assertEqual(len(notify.ns()["buffer"]), 1)

    def test_one_group_card_per_symbol(self):
        self.enable("XAGUSD")
        self.deliver(self.normal("XAUUSD")); self.deliver(self.normal("XAGUSD")); self.deliver(self.normal("XAUUSD", "WIDE GAP"))
        self.tick(61)
        emb = [p["embeds"][0] for p in Http.to("discord")]
        self.assertEqual(sorted(e["title"].split()[0] for e in emb), ["XAGUSD", "XAUUSD"])
        self.assertIn("2 events", [e for e in emb if e["title"].startswith("XAUUSD")][0]["title"])


# =========================================================================================== restarts
class RestartPersistence(NotifyBase):
    def test_mode_and_held_items_survive_a_restart(self):
        self.assertTrue(self.cmd("notify_mode", mode="intelligent")["ok"])
        self.deliver(self.normal())
        notify.reset()                                            # restart
        self.assertEqual(notify.mode(), "intelligent")
        self.assertEqual(len(notify.ns()["buffer"]), 1)
        self.tick(61)
        self.assertEqual(len(Http.to("discord")), 1)

    def test_a_group_card_left_in_flight_by_a_crash_is_posted_once_after_the_restart(self):
        self.set_mode("intelligent")
        self.deliver(self.normal())
        with mock.patch.object(notify, "dispatch"):               # crash right after "flushing" hit the disk
            World.advance(seconds=61); notify.tick(bot.state)
        self.assertEqual(len(notify.ns()["flushing"]), 1); self.assertEqual(Http.sent, [])
        notify.reset()                                            # restart
        self.tick(); self.tick(); self.tick()
        self.assertEqual(len(Http.to("discord")), 1)
        self.assertEqual(notify.ns()["flushing"], {})

    def test_corrupt_state_falls_back_to_defaults(self):
        self.set_mode("intelligent")
        for p in (C.NOTIFY_STATE_FILE, C.NOTIFY_STATE_FILE + ".bak"):
            with open(p, "w") as f:
                f.write("{not json")
        notify.reset()
        self.assertEqual(notify.mode(), "standard")
        self.assertIn("corrupt", notify.status_text(bot.state))


# ======================================================================================= retries
class RetryAndRecovery(NotifyBase):
    def setUp(self):
        super().setUp()
        self.set_mode("intelligent")

    def test_failed_group_card_goes_to_the_retry_queue_and_is_recovered(self):
        with mock.patch.object(C, "NOTIFY_MIN_SEVERITY", {"Discord": "low", "Slack": "low", "Ntfy": "high"}):
            self.deliver(notifier.card("XAUUSD CROSS-ish", channel="alerts", symbol="XAUUSD", kind="GAP WIDENING"))
            Http.down.add("slack")
            self.tick(61)
        self.assertEqual(sum("UPDATES" in t for t in sent_titles("discord")), 1)
        self.assertEqual([p["to"] for p in bot.state.d["pending"]], [["Slack"]])
        Http.down.clear()
        notifier.flush_pending(bot.state)
        self.assertEqual(len(Http.to("slack")), 1)
        self.assertEqual(bot.state.d["pending"], [])

    def test_critical_retry_keeps_its_route(self):
        Http.down.add("slack")
        self.deliver(notifier.warn("TSL step NOT applied", "x", ping=True, symbol="XAUUSD", channel="positions",
                                   kind="TSL_FAILED"))
        self.assertEqual(bot.state.d["pending"][0]["to"], ["Slack"])
        Http.down.clear()
        notifier.flush_pending(bot.state)                         # standard routing would have dropped Slack here
        self.assertEqual(len(Http.to("slack")), 1)

    def test_dispatcher_survives_an_exception(self):
        self.deliver(self.normal())
        with mock.patch.object(notifier, "send", side_effect=RuntimeError("boom")), \
                mock.patch.object(notify.log, "exception"):
            self.tick(61)
        self.assertEqual(bot.state.d["pending"][0]["to"], ["Discord"])   # queued, not lost
        self.deliver(self.normal(title="next one"))
        self.tick(61)
        self.assertEqual(len(Http.to("discord")), 1)              # thread still alive

    def test_group_posts_never_block_the_loop(self):
        gate = threading.Event()
        real = notifier._post
        with mock.patch.object(notifier, "_post", lambda *a, **k: gate.wait(5) and real(*a, **k)):
            self.deliver(self.normal())
            World.advance(seconds=61)
            notify.tick(bot.state)                                # returns while the post is still blocked
            self.assertFalse(gate.is_set()); self.assertEqual(Http.sent, [])
            gate.set()
            self.assertTrue(notify.wait_idle())
        self.assertEqual(len(Http.to("discord")), 1)


# ================================================================================ disabled channels
class DisabledChannels(NotifyBase):
    def setUp(self):
        super().setUp()
        self.set_mode("intelligent")

    def test_disabled_service_gets_nothing_but_critical_still_goes_elsewhere(self):
        with mock.patch.object(C, "NOTIFY_DISABLED_SERVICES", {"Slack"}):
            self.deliver(self.high()); self.deliver(self.critical())
        self.assertEqual(Http.to("slack"), [])
        self.assertEqual(len(Http.to("discord")), 2); self.assertEqual(len(Http.to("ntfy")), 2)

    def test_every_service_disabled_still_delivers_critical(self):
        with mock.patch.object(C, "NOTIFY_DISABLED_SERVICES", {"Slack", "Discord", "Ntfy"}):
            self.deliver(self.high()); self.deliver(self.critical())
        self.assertEqual([len(Http.to(s)) for s in ("discord", "slack", "ntfy")], [1, 1, 1])   # critical only

    def test_standard_mode_ignores_the_thresholds(self):
        self.set_mode("standard")
        with mock.patch.object(C, "NOTIFY_DISABLED_SERVICES", {"Slack"}):
            self.deliver(self.high())
        self.assertEqual(len(Http.to("slack")), 1)


# ================================================================================= symbols, concurrency
class SymbolsAndConcurrency(NotifyBase):
    def setUp(self):
        super().setUp()
        self.set_mode("intelligent")
        self.enable("XAGUSD")

    def test_per_symbol_minimum(self):
        with mock.patch.object(C, "NOTIFY_SYMBOL_PREFS", "XAGUSD:min=critical"):
            self.deliver(self.high("XAGUSD")); self.deliver(self.high("XAUUSD"))
            self.deliver(self.critical())
        self.assertEqual(sum(t.startswith("XAGUSD") for t in sent_titles("discord")), 0)
        self.assertEqual([b["symbol"] for b in notify.ns()["buffer"]], ["XAGUSD"])
        self.assertEqual(len(Http.to("slack")), 2)                # XAUUSD cross + the critical

    def test_rate_limits_are_per_symbol(self):
        with mock.patch.object(C, "NOTIFY_RATE_MAX", "1"):
            self.deliver(self.high("XAUUSD", "CROSS a")); self.deliver(self.high("XAUUSD", "CROSS b"))
            self.deliver(self.high("XAGUSD", "CROSS a"))
        self.assertEqual(len(Http.to("slack")), 2)

    def test_concurrent_audit_writes_and_dispatches(self):
        def burst(n):
            for i in range(50):
                notify.audit({"event_id": f"{n}:{i}", "kind": "X", "action": "t"})
        threads = [threading.Thread(target=burst, args=(n,)) for n in range(8)]
        [t.start() for t in threads]; [t.join() for t in threads]
        rows = audit_rows()
        self.assertEqual(len(rows), 400)
        for i in range(20):
            self.deliver(self.normal("XAUUSD" if i % 2 else "XAGUSD", f"GAP {i}"))
        self.tick(61)
        self.assertEqual(len(Http.to("discord")), 2)
        self.assertEqual(notify.ns()["buffer"], []); self.assertEqual(notify.ns()["flushing"], {})

    def test_assembler_export_is_opt_in(self):
        self.deliver(self.high())
        self.assertEqual(records("notification"), [])
        with mock.patch.object(C, "NOTIFY_ASSEMBLER_EXPORT", True):
            self.deliver(self.high("XAGUSD"))
        recs = records("notification")
        self.assertEqual(len(recs), 1); self.assertEqual(recs[0]["symbol"], "XAGUSD")


# ============================================================================== fake MT5 end to end
class FakeMT5EndToEnd(NotifyBase):
    def run_candles(self, n=80):
        bot.check_ema("M30")
        for _ in range(n):
            World.advance(minutes=30); bot.check_ema("M30"); notify.tick(bot.state)
        self.tick(3600)

    def test_intelligent_mode_loses_nothing_and_sends_fewer_messages(self):
        self.run_candles()
        std = len(Http.to("discord"))
        live = [r for r in audit_rows()]
        self.assertTrue(all(r["mode"] == "standard" for r in live))
        # same candles again, intelligent
        World.reset(); Http.reset(); notify.reset()
        for p in glob.glob(os.path.join(C.STATE_DIR, "*")) + glob.glob(os.path.join(C.NOTIFY_AUDIT_DIR, "*")):
            os.remove(p)
        bot.state = notifier.State(); bot.reset_link(); self.assertIsNone(bot.ensure_mt5())
        self.set_mode("intelligent")
        self.run_candles()
        rows = audit_rows()
        held = {r["event_id"] for r in rows if r["action"] in ("group", "digest")}
        flushed = {i for r in rows if r["action"] == "flushed" for i in r["items"]}
        rode = sum(r.get("riders", 0) for r in rows if r["action"] == "send")
        self.assertEqual(len(held - flushed), rode)               # every held event was posted (alone or riding along)
        self.assertLessEqual(len(Http.to("discord")), std)
        self.assertEqual(notify.ns()["buffer"], [])

    def test_monitor_mode_uses_one_feed_card_per_symbol(self):
        notifier.BOT_CLIENT["on"] = True
        self.set_mode("monitor")
        self.run_candles(40)
        feed = [i for i in outbox() if i["kind"] == "NOTIFY_FEED"]
        self.assertTrue(feed)
        self.assertEqual({i["upsert"] for i in feed}, {"feed:XAUUSD"})
        self.assertFalse(any(r["action"] in ("group", "digest") for r in audit_rows()
                             if r.get("severity") == "normal"))

    def test_monitor_mode_without_the_bot_client_groups_instead(self):
        self.set_mode("monitor")
        self.deliver(self.normal())
        self.assertEqual(notify.ns()["buffer"][0]["action"], "group")


# ========================================================================================== commands
class NotifyCommands(NotifyBase):
    def test_status_prefs_and_no_secrets(self):
        r = self.cmd("notify")
        self.assertTrue(r["ok"]); self.assertIn("Notification mode: standard", r["text"])
        self.assertIn("Trade management: OFF", r["text"])
        p = self.cmd("notify", action="prefs")
        self.assertIn("critical", p["text"])
        for secret in (C.DISCORD_WEBHOOK, C.SLACK_WEBHOOK, C.NTFY_TOPIC):
            self.assertNotIn(secret, r["text"] + p["text"])

    def test_switch_mode_authorised_only(self):
        self.assertFalse(self.cmd("notify_mode", user=99, mode="intelligent")["ok"])
        self.assertFalse(self.cmd("notify_mode", mode="loud")["ok"])
        self.assertEqual(notify.mode(), "standard")
        self.assertTrue(self.cmd("notify_mode", mode="intelligent")["ok"])
        self.assertEqual(notify.mode(), "intelligent")
        self.deliver(self.normal())
        self.assertTrue(self.cmd("notify_mode", mode="standard")["ok"])
        self.tick()                                               # held cards go out after switching back
        self.assertEqual(len(Http.to("discord")), 1)

    def test_channel_test_places_no_trade_and_reports(self):
        self.assertFalse(self.cmd("notify", user=99, action="test")["ok"])
        r = self.cmd("notify", action="test")
        self.assertTrue(r["ok"]); self.assertIn("Discord", r["text"])
        self.tick()
        self.assertEqual([len(Http.to(s)) for s in ("discord", "slack", "ntfy")], [1, 1, 1])
        self.assertEqual(World.orders, [])
        self.assertIn("Last test", self.cmd("notify")["text"])
        self.assertNotIn("notification test", open(C.alerts_log_path(), encoding="utf-8").read()
                         if os.path.exists(C.alerts_log_path()) else "")


# ================================================================================ trade management
class TradeManagementSwitch(NotifyBase):
    def tm(self, action, user=42, **kw):
        return self.cmd("trade_management", user=user, action=action, channel=777, **kw)

    def enable(self, user=42):                                   # noqa: shadows V170.enable on purpose here
        r = self.tm("enable_request", user)
        return self.tm("enable_confirm", user, nonce=r.get("nonce"))

    def test_default_is_off(self):
        for p in (C.TRADE_MANAGEMENT_FILE, C.TRADE_MANAGEMENT_FILE + ".bak"):
            if os.path.exists(p):
                os.remove(p)
        self.assertFalse(C.TRADE_MANAGEMENT_ENABLED)
        trade_mgmt.load()
        self.assertFalse(trade_mgmt.enabled())
        self.assertIn("OFF", self.tm("status")["text"])

    def test_authorised_enable_needs_the_confirmation_and_disable_is_immediate(self):
        self.assertFalse(self.tm("enable")["ok"])                 # "enable" alone never enables
        self.assertFalse(trade_mgmt.enabled())
        r = self.tm("enable_request")
        self.assertTrue(r["ok"]); self.assertTrue(r["nonce"]); self.assertFalse(trade_mgmt.enabled())
        r = self.tm("enable_confirm", nonce=r["nonce"])
        self.assertTrue(r["ok"], r); self.assertTrue(trade_mgmt.enabled())
        self.assertTrue(self.tm("disable")["ok"]); self.assertFalse(trade_mgmt.enabled())
        with open(C.TRADE_MANAGEMENT_FILE) as f:
            hist = json.load(f)["history"]
        self.assertEqual([(h["previous"], h["new"], h["user"]) for h in hist], [(False, True, 42), (True, False, 42)])
        self.assertTrue(all(h["ts"] for h in hist))
        self.assertEqual(sum("TRADE MANAGEMENT" in t for t in sent_titles("discord")), 2)
        self.assertEqual(len([r for r in records("system") if r["event"] == "trade_management"]), 2)

    def test_unauthorised_and_malformed_attempts_never_enable(self):
        self.assertFalse(self.enable(user=99)["ok"])              # not in DISCORD_ALLOWED_USER_IDS
        with mock.patch.object(C, "TRADE_MANAGEMENT_USER_IDS", {42}):
            self.assertFalse(self.enable(user=43)["ok"])          # listed for Discord, not for management
        r = self.tm("enable_request")
        self.assertFalse(self.tm("enable_confirm", user=43, nonce=r["nonce"])["ok"])   # someone else's nonce
        self.assertFalse(self.tm("enable_confirm", nonce="forged")["ok"])
        self.assertFalse(self.tm("enable_confirm", nonce=r["nonce"])["ok"])            # used up by the attempt above
        self.assertFalse(self.cmd("trade_management", action="enable_confirm", nonce=None, channel=777)["ok"])
        self.assertFalse(self.cmd("trade_management", action=["enable"], channel=777)["ok"])
        self.assertFalse(self.cmd("trade_management", action="enable_request", channel=12345)["ok"])  # wrong channel
        r = self.tm("enable_request")
        with mock.patch("time.monotonic", return_value=10 ** 9):
            self.assertFalse(self.tm("enable_confirm", nonce=r["nonce"])["ok"])        # expired
        self.assertFalse(trade_mgmt.enabled())
        old = alerts.envelope("trade_management", {"action": "enable_confirm", "user": 42})
        old["sig"] = "0" * 64                                     # a dropped/forged inbox file
        path = os.path.join(C.INBOX_DIR, "x.cmd")
        with open(path, "w") as f:
            json.dump(old, f)
        alerts.process_inbox(json_handler=bot.handle_json_command)
        self.assertFalse(trade_mgmt.enabled())

    def test_restart_persistence_needs_an_explicit_policy_to_restore_on(self):
        self.assertTrue(self.enable()["ok"])
        self.assertTrue(any("was ON before the restart" in n for n in trade_mgmt.load()))
        self.assertFalse(trade_mgmt.enabled())                    # restart without the policy: OFF
        self.assertTrue(self.enable()["ok"])
        with mock.patch.object(C, "TRADE_MANAGEMENT_RESTORE_ENABLED", True):
            trade_mgmt.load()
        self.assertTrue(trade_mgmt.enabled())
        self.tm("disable")
        with mock.patch.object(C, "TRADE_MANAGEMENT_ENABLED", True):
            trade_mgmt.load()
        self.assertFalse(trade_mgmt.enabled())                    # a persisted OFF always wins

    def test_corrupt_state_or_config_fails_closed(self):
        self.assertTrue(self.enable()["ok"])
        for p in (C.TRADE_MANAGEMENT_FILE, C.TRADE_MANAGEMENT_FILE + ".bak"):
            with open(p, "w") as f:
                f.write("{broken")
        with mock.patch.object(C, "TRADE_MANAGEMENT_RESTORE_ENABLED", True):
            self.assertTrue(trade_mgmt.load())
        self.assertFalse(trade_mgmt.enabled())
        with open(C.TRADE_MANAGEMENT_FILE, "w") as f:
            json.dump({"enabled": "yes", "generation": 1, "history": []}, f)
        with mock.patch.object(C, "TRADE_MANAGEMENT_RESTORE_ENABLED", True):
            trade_mgmt.load()
        self.assertFalse(trade_mgmt.enabled())
        with mock.patch.object(C, "TRADE_MANAGEMENT_MALFORMED", True), \
                mock.patch.object(C, "TRADE_MANAGEMENT_ENABLED", False):
            self.assertTrue(trade_mgmt.load())
            self.assertFalse(trade_mgmt.enabled())
            self.assertFalse(self.tm("enable_request")["ok"])

    def test_enable_that_cannot_be_saved_stays_off(self):
        r = self.tm("enable_request")
        with mock.patch.object(notifier, "save_json", side_effect=OSError("disk full")), \
                mock.patch.object(trade_mgmt.log, "exception"):
            self.assertFalse(self.tm("enable_confirm", nonce=r["nonce"])["ok"])
        self.assertFalse(trade_mgmt.enabled())

    def test_change_card_is_critical_even_in_quiet_intelligent_mode(self):
        self.set_mode("intelligent")
        with mock.patch.object(C, "NOTIFY_QUIET_HOURS", "00:00-23:59"):
            self.enable()
        self.assertEqual(sum("TRADE MANAGEMENT ON" in t for t in sent_titles("slack")), 1)

    def test_controls_card_has_persistent_buttons(self):
        notifier.BOT_CLIENT["on"] = True
        self.enable()
        card = [i for i in outbox() if i["kind"] == "CONTROLS"][-1]
        self.assertEqual(card["upsert"], "controls")
        self.assertEqual([b["id"] for b in card["buttons"]], ["aureon:tm:enable", "aureon:tm:disable"])
        self.assertIn("ON", card["desc"])


class TradeManagementOrders(Pos):
    """Through the real TSL path (positions.on_candle -> trader.modify_sl -> fake broker)."""
    def setUp(self):
        super().setUp()
        trade_mgmt.reset(False)
        notify.reset()

    def test_no_mt5_modification_while_off_but_monitoring_and_notifications_continue(self):
        p = self.short_at_cross()
        self.walk("2026-10-29 06:00", "2026-10-30 16:00")
        self.assertEqual(sltp_orders(), [])                        # no order of any kind touched the position
        self.assertEqual([o for o in World.orders], [])
        self.assertEqual((p.sl, p.tp), (4060.0, 3800.0))           # broker-side SL/TP untouched
        cards = titles(Http.to("discord"))
        self.assertTrue(any("POSITION OPENED" in t for t in cards))
        blocked = [t for t in cards if "TSL step NOT applied" in t]
        self.assertEqual(len(blocked), 4)                          # one per golden step, each once
        self.assertEqual(len([r for r in records("sl_change") if r["mode"] == "blocked"]), 4)
        fake_mt5.close(7001, 4010.0, reason=5)
        positions.track(bot.state, bot.LABELS); positions.track(bot.state, bot.LABELS)
        self.assertTrue(any("POSITION CLOSED" in t for t in titles(Http.to("discord"))))

    def test_a_disable_between_decision_and_submission_invalidates_the_order(self):
        trade_mgmt.reset(True)
        self.short_at_cross()
        real = trader.modify_sl

        def disable_first(*a, **kw):                               # the disable lands after the TSL decided
            trade_mgmt.disable(42, "test")
            return real(*a, **kw)
        with mock.patch.object(trader, "modify_sl", disable_first):
            self.walk("2026-10-29 06:00", "2026-10-29 18:30")
        self.assertEqual(sltp_orders(), [])

    def test_old_token_stays_invalid_after_re_enabling(self):
        trade_mgmt.reset(True)
        p = self.short_at_cross()
        self.at("2026-10-29 18:00")
        World.tick_override["XAUUSD"] = 4030.0
        tok = trade_mgmt.token()
        trade_mgmt.disable(42, "test")
        trade_mgmt._set(True, 42, "test")
        res = trader.modify_sl(7001, 4050.0, why="tsl", token=tok)
        self.assertFalse(res["ok"]); self.assertIn("no longer valid", res["text"])
        self.assertEqual(sltp_orders(), [])
        self.assertTrue(trader.modify_sl(7001, 4050.0, token=trade_mgmt.token())["ok"])
        self.assertEqual(p.sl, 4050.0)

    def test_suggest_mode_apply_follows_the_switch(self):
        self.short_at_cross()
        with mock.patch.object(positions, "tsl_mode", lambda: "suggest"):
            self.walk("2026-10-29 06:00", "2026-10-29 18:00")
        card = [p for p in Http.to("discord") if "TSL STEP suggested" in p["embeds"][0]["title"]][0]
        self.assertIn("Apply is refused", json.dumps(card, ensure_ascii=False))
        gen_off = trade_mgmt.token()
        apply = lambda gen: bot.handle_json_command({"cmd": "apply", "ticket": 7001, "sl": 4044.25, "gen": gen,
                                                     "ts": World.now.timestamp(), "id": f"a{gen}", "user": 42})
        self.assertFalse(apply(gen_off)["ok"])                     # OFF
        trade_mgmt._set(True, 42, "test")
        self.assertFalse(apply(gen_off)["ok"])                     # suggestion from before the switch: void
        self.assertFalse(apply(None)["ok"])                        # an old button without a token: refused
        self.assertEqual(sltp_orders(), [])
        self.assertTrue(apply(trade_mgmt.token())["ok"])
        self.assertAlmostEqual(World.positions[0].sl, 4044.25)

    def test_manual_place_is_controlled_by_place_enabled_only(self):
        req = {"cmd": "place", "side": "buy", "lot": 0.1, "sl": 20, "tp": 40, "symbol": "XAUUSD",
               "ts": World.now.timestamp(), "user": 42}
        self.assertFalse(trade_mgmt.enabled())
        r = bot.handle_json_command({**req, "id": "p1"})
        self.assertTrue(r["ok"], r); self.assertIn("FILLED", r["text"])
        trade_mgmt.reset(True)
        C.PLACE_ENABLED = False
        r = bot.handle_json_command({**req, "id": "p2", "ts": World.now.timestamp()})
        self.assertFalse(r["ok"]); self.assertIn("PLACE_ENABLED", r["text"])


class TradeManagementDiscord(unittest.IsolatedAsyncioTestCase):
    """The Discord side: buttons and /trade_management go through the generic confirm view."""
    def setUp(self):
        try:
            import discord  # noqa: F401
        except ImportError:
            self.skipTest("discord.py not installed")
        self.calls = []

        def fake_request(cmd, payload=None, wait_s=8.0):
            self.calls.append((cmd, (payload or {}).get("action")))
            if (payload or {}).get("action") == "enable_request":
                return {"ok": True, "nonce": "n1", "text": "Enable?"}
            return {"ok": True, "text": "done"}
        p = mock.patch.object(alerts, "request", fake_request); p.start(); self.addCleanup(p.stop)
        self.client = alerts.build_discord_client()

    def inter(self, user=42, channel=777):
        i = mock.MagicMock(); i.user.id, i.channel_id = user, channel
        i.response.defer = mock.AsyncMock(); i.response.send_message = mock.AsyncMock()
        i.followup.send = mock.AsyncMock(); i.edit_original_response = mock.AsyncMock()
        return i

    async def test_unauthorised_button_sends_nothing(self):
        i = self.inter(user=99)
        await self.client.on_button(i, "aureon:tm:enable")
        self.assertEqual(self.calls, [])
        i.response.send_message.assert_awaited()

    async def test_enable_button_asks_for_confirmation_first(self):
        i = self.inter()
        await self.client.on_button(i, "aureon:tm:enable")
        self.assertEqual(self.calls, [("trade_management", "enable_request")])
        view = i.followup.send.call_args.kwargs["view"]
        self.assertIsInstance(view, self.client.ConfirmView)
        other = self.inter(user=43)
        if await view.interaction_check(other):
            await view.confirm.callback(other)
        self.assertEqual(len(self.calls), 1)                      # someone else cannot confirm
        a, b = self.inter(), self.inter()
        async def press(x):
            if await view.interaction_check(x):
                await view.confirm.callback(x)
        await asyncio.gather(press(a), press(b))
        self.assertEqual(self.calls[1:], [("trade_management", "enable_confirm")])   # once

    async def test_disable_button_is_immediate(self):
        await self.client.on_button(self.inter(), "aureon:tm:disable")
        self.assertEqual(self.calls, [("trade_management", "disable")])

    async def test_close_confirm_still_one_use(self):
        view = self.client.CloseConfirm(42, {"ticket": 1})
        await view.interaction_check(self.inter())
        await view.cancel.callback(self.inter())
        self.assertFalse(await view.interaction_check(self.inter()))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
