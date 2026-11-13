"""1.7.1: bar records, the Assembler uploader, /status line, backfill. Offline: a fake Assembler server sits in
front of the existing fake HTTP layer (webhooks still go to test_bot's fake)."""
import glob, gzip, io, json, logging, os, sys, unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bot import Http, titles, World                    # noqa: E402
from test_v170 import V170, records                          # noqa: E402
from test_labels import SAMPLE                               # noqa: E402
import config as C, signals, notifier, bot, store, labels, updater, uploader, backfill   # noqa: E402

URL, TOKEN = "https://assembler.test", "tok-SECRET-9f3a1c"


class Resp:
    def __init__(self, code, body=None, headers=None):
        self.status_code, self._body, self.headers, self.text = code, body or {}, headers or {}, ""

    def json(self):
        return self._body


class Server:
    """Scripted Assembler. script: list of answers for the next calls: an int status, an Exception to raise, or a
    callable(route, payload) -> Resp. When the script is empty: a real idempotent server."""
    def __init__(self, orig):
        self.orig, self.script, self.calls = orig, [], []
        self.ids, self.bars = [], {}                 # accepted record_ids (in order), (symbol, tf, ts) -> bar
        self.bad_ids = set()                         # rows this server refuses with 422 when in a batch

    def __call__(self, url, json=None, timeout=None, data=None, files=None, headers=None):
        if not url.startswith(URL):
            return self.orig(url, json=json, timeout=timeout, data=data, files=files)
        route = url[len(URL):]
        payload = __import__("json").loads(gzip.decompress(data))
        self.calls.append({"route": route, "payload": payload, "headers": headers or {}})
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            if callable(step):
                return step(route, payload)
            if step != 200:
                return Resp(step, headers={"Retry-After": "7"} if step == 429 else {})
        return self.real(route, payload)

    def real(self, route, payload):
        if route == uploader.RECORDS_ROUTE:
            rows = payload["rows"]
            if any(r["record_id"] in self.bad_ids for r in rows):
                return Resp(422)
            acc = dup = 0
            for r in rows:
                if r["record_id"] in self.ids:
                    dup += 1
                else:
                    self.ids.append(r["record_id"]); acc += 1
            return Resp(200, {"accepted": acc, "duplicate": dup, "rejected": []})
        ins = upd = 0
        for b in payload["bars"]:
            key = (payload["symbol"], payload["timeframe"], b["ts"])
            upd += key in self.bars; ins += key not in self.bars
            self.bars[key] = b
        return Resp(200, {"inserted": ins, "updated": upd, "rejected": []})


def file_rows():
    store.flush()
    out = []
    for p in uploader.record_files():
        with open(p, encoding="utf-8") as f:
            out += [json.loads(l) for l in f if l.strip()]
    return out


class Up(V170):
    def setUp(self):
        super().setUp()
        self.server = Server(sys.modules["requests"].post)
        for target, attr, val in ((uploader.requests, "post", self.server), (C, "RECORD_SINK", "api"),
                                  (C, "ASSEMBLER_URL", URL), (C, "ASSEMBLER_RECORDS_TOKEN", TOKEN)):
            p = mock.patch.object(target, attr, val); p.start(); self.addCleanup(p.stop)
        for p in glob.glob(os.path.join(updater.NOTICE_DIR, "*")):
            os.remove(p)
        self.addCleanup(self._stop_thread)

    def _stop_thread(self):
        if uploader._running:
            uploader._running.stop(); uploader._running = None

    def make_records(self, candles=30):
        bot.check_ema("M30")
        for _ in range(candles):
            self.to_close(); bot.check_ema("M30")
        return file_rows()

    def up(self, **kw):
        return uploader.Uploader(client=uploader.Client(URL, TOKEN, 5), **kw)

    def received_ids(self):
        return self.server.ids


class Off(Up):
    def test_jsonl_or_none_never_makes_a_request(self):
        for sink in ("jsonl", "none"):
            with mock.patch.object(C, "RECORD_SINK", sink):
                self.assertFalse(uploader.start())
                self.make_records(10)
                self.assertEqual(uploader.status_line(), "Record upload: off")
        self.assertEqual(self.server.calls, [])
        self.assertFalse(any(u.startswith(URL) for u, _ in Http.sent))

    def test_validate(self):
        with mock.patch.object(C, "ASSEMBLER_URL", ""):
            self.assertTrue(any("ASSEMBLER_URL" in p for p in C.validate()))
        with mock.patch.object(C, "ASSEMBLER_RECORDS_TOKEN", ""):
            self.assertTrue(any("ASSEMBLER_RECORDS_TOKEN" in p for p in C.validate()))
        with mock.patch.object(C, "RECORD_SINK", "s3"):
            self.assertTrue(any("jsonl, none or api" in p for p in C.validate()))


class HappyPath(Up):
    def test_every_row_arrives_once_and_a_restart_sends_nothing_twice(self):
        rows = self.make_records()
        self.assertTrue(any(r["kind"] == "bar" for r in rows)); self.assertTrue(any(r["kind"] == "event" for r in rows))
        u = self.up(batch=40)
        self.assertEqual(u.run_pass(), C.RECORD_API_INTERVAL_S)
        non_bar = [r["record_id"] for r in rows if r["kind"] != "bar"]
        self.assertEqual(sorted(self.received_ids()), sorted(non_bar))
        self.assertEqual(len(self.received_ids()), len(set(self.received_ids())))
        bars = [r for r in rows if r["kind"] == "bar"]
        self.assertEqual(len(self.server.bars), len(bars))
        call = self.server.calls[0]
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(call["headers"]["Content-Encoding"], "gzip")
        rec_call = next(c for c in self.server.calls if c["route"] == uploader.RECORDS_ROUTE)
        self.assertEqual(rec_call["payload"]["schema"], "aureon.mt5.records/v1")
        self.assertLessEqual(len(rec_call["payload"]["rows"]), 40)
        self.assertEqual(rec_call["payload"]["rows"][0], next(r for r in rows if r["record_id"] == rec_call["payload"]["rows"][0]["record_id"]))
        bar_call = next(c for c in self.server.calls if c["route"] == uploader.BARS_ROUTE)
        self.assertEqual((bar_call["payload"]["schema"], bar_call["payload"]["symbol"], bar_call["payload"]["timeframe"],
                          bar_call["payload"]["source"]), ("aureon.mt5.bars/v1", "XAUUSD", "M30", "bot"))
        self.assertEqual(uploader.backlog(), 0)
        n = len(self.server.calls)
        self.up().run_pass()                                        # restart: fresh object, cursor from disk
        self.assertEqual(len(self.server.calls), n)
        self.make_records(3)                                        # new rows: only those go
        before = len(self.received_ids())
        self.up().run_pass()
        self.assertEqual(len(self.received_ids()), len(set(self.received_ids())))
        self.assertGreater(len(self.received_ids()), before)

    def test_all_duplicates_is_success(self):
        self.make_records(5)
        dup = lambda route, p: Resp(200, {"accepted": 0, "duplicate": len(p.get("rows", p.get("bars", []))), "rejected": []})
        self.server.script = [dup] * 50
        self.up().run_pass()
        self.assertEqual(uploader.backlog(), 0)

    def test_half_written_last_line_waits(self):
        path = os.path.join(C.RECORD_DIR, "event-2026-11.jsonl")
        row = store.build("event", {"label": "X"}, "XAUUSD")
        with open(path, "a") as f:
            f.write(json.dumps(row)[:40])                           # the writer has not finished this line
        self.up().run_pass()
        self.assertEqual(self.server.calls, [])
        with open(path, "a") as f:
            f.write(json.dumps(row)[40:] + "\n")
        self.up().run_pass()
        self.assertEqual(self.received_ids(), [row["record_id"]])


class Failures(Up):
    def test_timeout_then_500_then_200_delivers_in_order_and_alerts_are_unchanged(self):
        rows = self.make_records(10)
        u = self.up(batch=500)
        self.server.script = [ConnectionError("timed out")]
        self.assertEqual(u.run_pass(), 15); self.assertEqual(self.received_ids(), [])
        self.server.script = [500]
        self.assertEqual(u.run_pass(), 30)                          # backoff doubles
        u.run_pass()
        want = [r["record_id"] for r in rows if r["kind"] != "bar"]
        self.assertEqual(sorted(self.received_ids()), sorted(want))
        by_file = {}
        for r in rows:
            if r["kind"] != "bar":
                by_file.setdefault(r["kind"], []).append(r["record_id"])
        for ids in by_file.values():                                # within a file: exactly the file order
            pos = [self.received_ids().index(i) for i in ids]
            self.assertEqual(pos, sorted(pos))
        self.assertEqual(u.backoff, 15)

    def test_alerts_do_not_change_while_the_api_is_down(self):
        def run():
            World.reset(); Http.reset()
            for name in os.listdir(C.STATE_DIR):
                os.remove(os.path.join(C.STATE_DIR, name))
            bot.state = notifier.State(); bot.ensure_mt5(); bot.check_ema("M30")
            for _ in range(40):
                self.to_close(); bot.check_ema("M30")
            return [t for t in titles(Http.to("discord"))]
        with mock.patch.object(C, "RECORD_SINK", "jsonl"):
            normal = run()
        self.server.script = [ConnectionError("down")] * 1000
        self.assertTrue(uploader.start())
        down = run()
        self.assertEqual(normal, down); self.assertGreater(len(normal), 0)

    def test_429_waits_for_retry_after(self):
        self.make_records(3)
        self.server.script = [429]
        self.assertEqual(self.up().run_pass(), 7)

    def test_401_stops_and_raises_one_notice(self):
        self.make_records(3)
        u = self.up()
        self.server.script = [401, 401]
        self.assertEqual(u.run_pass(), uploader.AUTH_RETRY)
        self.assertEqual(u.run_pass(), uploader.AUTH_RETRY)
        self.assertEqual(len(self.server.calls), 2)                 # one try per pass: no retry storm
        bot.deliver_notices()
        t = [x for x in titles(Http.to("discord")) if "Record upload FAILING" in x]
        self.assertEqual(len(t), 1)
        p = next(p for p in Http.to("discord") if "Record upload FAILING" in p["embeds"][0]["title"])
        self.assertIn("401", p["embeds"][0]["description"])
        self.assertEqual(len([r for r in records("system") if r["event"] == "upload_failing"]), 1)
        u.run_pass()                                                # token fixed: one recovered notice
        bot.deliver_notices()
        self.assertEqual(len([x for x in titles(Http.to("discord")) if "recovered" in x]), 1)
        store.flush(); u.run_pass()                                 # the "recovered" record itself goes next pass
        self.assertEqual(uploader.backlog(), 0)

    def test_failing_for_30_minutes_gives_one_notice_and_one_recovery(self):
        self.make_records(3)
        u = self.up()
        self.server.script = [503] * 3
        u.run_pass(); u.run_pass()
        self.assertEqual(updater.pop_notices(), [])                 # not yet 30 min
        u.failing_since -= 31 * 60
        u.run_pass()
        n = updater.pop_notices()
        self.assertEqual([x["kind"] for x in n], ["upload_failing"])
        u.run_pass()
        self.assertEqual([x["kind"] for x in updater.pop_notices()], ["upload_recovered"])
        self.assertEqual([r["event"] for r in records("system") if r["event"].startswith("upload")],
                         ["upload_failing", "upload_recovered"])

    def test_422_halves_down_to_the_bad_row(self):
        rows = [store.build("event", {"label": f"L{k}"}, "XAUUSD") for k in range(16)]
        path = os.path.join(C.RECORD_DIR, "event-2026-11.jsonl")
        with open(path, "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))
        bad = rows[11]["record_id"]
        self.server.bad_ids = {bad}
        self.up(batch=16).run_pass()
        self.assertEqual(sorted(self.received_ids()), sorted(r["record_id"] for r in rows if r["record_id"] != bad))
        rej = glob.glob(os.path.join(C.RECORD_DIR, "rejected-*.jsonl"))
        with open(rej[0]) as f:
            lines = [json.loads(l) for l in f]
        self.assertEqual([l["row"]["record_id"] for l in lines], [bad])
        self.assertLessEqual(len(self.server.calls), 1 + 2 * 4)    # halving, not one request per row
        self.assertEqual(uploader.backlog(), 0)

    def test_killed_between_send_and_cursor_write_resends_as_duplicates(self):
        rows = self.make_records(5)
        real_save = uploader.save_cursor
        with mock.patch.object(uploader, "save_cursor", side_effect=SystemExit("killed")):
            with self.assertRaises(SystemExit):
                self.up().run_pass()                                # the server has the rows, the cursor does not
        sent_once = list(self.received_ids())
        self.assertTrue(sent_once)
        dups = []
        orig_real = self.server.real

        def count(route, payload):
            r = orig_real(route, payload); dups.append(r._body.get("duplicate", 0)); return r
        self.server.real = count
        self.up().run_pass()
        self.assertEqual(sorted(self.received_ids()), sorted(r["record_id"] for r in rows if r["kind"] != "bar"))
        self.assertGreater(sum(dups), 0)
        self.assertEqual(uploader.backlog(), 0)

    def test_token_never_appears_in_logs_records_or_discord(self):
        buf = io.StringIO()
        h = logging.StreamHandler(buf); h.setLevel(logging.DEBUG)
        root = logging.getLogger(); root.addHandler(h); old = root.level; root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(h), root.setLevel(old)))
        self.make_records(5)
        u = self.up()
        self.server.script = [401, ConnectionError("x"), 500, 422, 422, 200]
        for _ in range(6):
            u.failing_since = (u.failing_since or 0) - 3600 if u.failing_since else None
            u.run_pass()
        bot.deliver_notices()
        self.assertTrue(uploader.start())
        r = bot.handle_json_command({"cmd": "status", "ts": World.now.timestamp(), "id": "s", "user": 42})
        store.flush()
        blobs = [buf.getvalue(), json.dumps(Http.sent, default=str), r["text"], repr(u.client), repr(u.__dict__)]
        for p in glob.glob(os.path.join(C.RECORD_DIR, "*")) + glob.glob(os.path.join(C.STATE_DIR, "*")):
            if os.path.isfile(p):
                with open(p, errors="replace") as f:
                    blobs.append(f.read())
        for b in blobs:
            self.assertNotIn(TOKEN, b)


class Headers(Up):
    def test_every_request_carries_the_json_content_type_and_the_bearer(self):
        self.make_records(3)
        self.up().run_pass()
        self.assertTrue(self.server.calls)
        routes = {c["route"] for c in self.server.calls}
        self.assertEqual(routes, {uploader.RECORDS_ROUTE, uploader.BARS_ROUTE})
        for c in self.server.calls:
            h = c["headers"]
            self.assertEqual(h["Content-Type"], "application/json")    # the server answers 415 without it
            self.assertEqual(h["Content-Encoding"], "gzip")
            self.assertEqual(h["Authorization"], f"Bearer {TOKEN}")

    def test_415_is_logged_once_and_not_retried_in_a_loop(self):
        self.make_records(3)
        u = self.up()
        self.server.script = [415, 415]
        with self.assertLogs("uploader", level="WARNING") as logs:
            self.assertEqual(u.run_pass(), uploader.AUTH_RETRY)    # paused: next try in 5 minutes
            self.assertEqual(u.run_pass(), uploader.AUTH_RETRY)
        self.assertEqual(len(self.server.calls), 2)                 # one try per pass, no halving storm
        self.assertEqual(len([m for m in logs.output if "415" in m]), 1)
        self.assertEqual(uploader.load_cursor(), {})                # nothing skipped ...
        self.assertEqual(glob.glob(os.path.join(C.RECORD_DIR, "rejected-*.jsonl")), [])   # ... or rejected
        bot.deliver_notices()
        failing = [p for p in Http.to("discord") if "Record upload FAILING" in p["embeds"][0]["title"]]
        self.assertEqual(len(failing), 1)
        self.assertIn("415", failing[0]["embeds"][0]["description"])
        u.run_pass()                                                # fixed: everything goes through
        store.flush(); u.run_pass()                                 # incl. the failing/recovered records
        self.assertEqual(uploader.backlog(), 0)
        self.assertEqual(len(self.received_ids()), len(set(self.received_ids())))

    def test_backfill_stops_at_once_on_415_and_401(self):
        bars = backfill.to_bars(backfill.from_csv(SAMPLE), None)[:10]
        for code in (415, 401):
            self.server.calls.clear()
            self.server.script = [code]
            with mock.patch.object(backfill.time, "sleep") as nap, self.assertRaises(SystemExit) as e:
                backfill.send(bars, "XAUUSD", "M30", "csv", client=uploader.Client(URL, TOKEN, 5),
                              out=lambda *a: None)
            self.assertIn(str(code), str(e.exception))
            self.assertEqual(len(self.server.calls), 1)
            nap.assert_not_called()


class Bars(Up):
    def test_one_bar_record_per_closed_candle_with_the_bot_values(self):
        self.enable("XAGUSD", False)
        bot.check_ema("M30")
        first = {r["bar_time_server"] for r in records("bar")}
        self.assertEqual(len(first), C.DEFAULT_LOOKBACK_BARS + 1)
        for _ in range(6):
            self.to_close(); bot.check_ema("M30"); bot.check_ema("M30")   # second call: nothing new
        bars = records("bar")
        self.assertEqual(len(bars), len({b["bar_time_server"] for b in bars}))          # once each
        self.assertEqual(len(bars), C.DEFAULT_LOOKBACK_BARS + 1 + 6)
        self.assertEqual({b["symbol"] for b in bars}, {"XAUUSD"})                        # none for disabled XAGUSD
        forming = datetime.fromtimestamp(int(World.stamp()) // 1800 * 1800, timezone.utc).replace(tzinfo=None)
        self.assertTrue(all(datetime.fromisoformat(b["bar_time_server"]) < forming for b in bars))
        df, lb = bot.LAST_DF["XAUUSD"], bot.LAST_BARS["XAUUSD"]
        last = bars[-1]
        i = df.index[df["time"] == datetime.fromisoformat(last["bar_time_server"])][0]
        self.assertAlmostEqual(last["ema8"], df["ema8"].iloc[i]); self.assertAlmostEqual(last["ema200"], df["ema200"].iloc[i])
        self.assertAlmostEqual(last["atr14"], lb["atr"].iloc[i]); self.assertAlmostEqual(last["close"], df["close"].iloc[i])
        self.assertEqual(datetime.fromisoformat(last["bar_time_utc"]),
                         (datetime.fromisoformat(last["bar_time_server"]) - timedelta(hours=2)).replace(tzinfo=timezone.utc))
        self.assertEqual(last["tf"], "M30"); self.assertEqual(last["tick_volume"], 100)

    def test_missed_bars_after_an_outage_are_recorded(self):
        bot.check_ema("M30"); n = len(records("bar"))
        World.advance(hours=10)                                     # 20 candles while the bot was down
        bot.check_ema("M30")
        self.assertEqual(len(records("bar")) - n, 20)

    def test_enabled_silver_records_its_own_bars_and_record_bars_off(self):
        self.enable("XAGUSD")
        bot.check_ema("M30", "XAGUSD")
        self.assertTrue(all(b["symbol"] == "XAGUSD" for b in records("bar")))
        with mock.patch.object(C, "RECORD_BARS", False):
            self.to_close(); bot.check_ema("M30", "XAGUSD")
        self.assertEqual(len(records("bar")), C.DEFAULT_LOOKBACK_BARS + 1)


class Status(Up):
    def test_status_shows_the_upload_line(self):
        self.make_records(3)
        uploader._running = self.up()                              # no thread: this test drives the passes
        r = bot.handle_json_command({"cmd": "status", "symbol": "XAUUSD", "ts": World.now.timestamp(), "id": "s", "user": 42})
        self.assertIn("Record upload: on · last success", r["text"]); self.assertIn("backlog", r["text"])
        store.flush(); uploader._running.run_pass()
        self.assertIn("backlog 0 rows", uploader.status_line()); self.assertNotIn("never", uploader.status_line())


class Backfill(Up):
    def test_sample_csv_goes_in_chunks_of_5000(self):
        out = []
        total = backfill.main(["--csv", SAMPLE, "--symbol", "xauusd", "--timeframe", "M30"],
                              client=uploader.Client(URL, TOKEN, 5), out=out.append)
        self.assertEqual(total["sent"], 1943); self.assertEqual(total["requests"], 1)
        self.assertEqual(total["inserted"], 1943); self.assertIn("inserted 1943", out[-1])
        call = self.server.calls[-1]["payload"]
        self.assertEqual((call["source"], call["symbol"], call["timeframe"]), ("csv", "XAUUSD", "M30"))
        b = call["bars"][-1]
        self.assertEqual(b["ts"], "2026-10-30T22:30:00+00:00")
        self.assertIsNone(b["tick_volume"])                        # not in the sample file
        self.assertIsNotNone(b["ema200"]); self.assertIsNotNone(b["atr14"])   # computed with the bot's math
        with mock.patch.object(backfill, "CHUNK", 500):
            self.server.calls.clear()
            total = backfill.send(backfill.to_bars(backfill.from_csv(SAMPLE), None), "XAUUSD", "M30", "csv",
                                  client=uploader.Client(URL, TOKEN, 5), chunk=500, out=lambda *a: None)
        self.assertEqual([len(c["payload"]["bars"]) for c in self.server.calls], [500, 500, 500, 443])
        self.assertEqual((total["updated"], total["inserted"]), (1943, 0))   # second run: upserts

    def test_mt5_mode_refuses_while_the_bot_runs(self):
        bot.heartbeat(30)
        with self.assertRaises(SystemExit) as e:
            backfill.main(["--mt5", "--symbol", "XAUUSD", "--timeframe", "M30", "--from", "2026-11-01"],
                          client=uploader.Client(URL, TOKEN, 5))
        self.assertIn("heartbeat is fresh", str(e.exception))
        os.remove(C.HEARTBEAT_FILE)
        out = []
        total = backfill.main(["--mt5", "--symbol", "XAUUSD", "--timeframe", "M30", "--from", "2026-11-09"],
                              client=uploader.Client(URL, TOKEN, 5), out=out.append)
        self.assertGreater(total["sent"], 20)
        bars = self.server.calls[-1]["payload"]["bars"]
        self.assertEqual(self.server.calls[-1]["payload"]["source"], "mt5")
        last_open_utc = datetime.fromisoformat(bars[-1]["ts"])
        self.assertLessEqual(last_open_utc + timedelta(minutes=30), World.now)   # closed bars only


if __name__ == "__main__":
    unittest.main(verbosity=2)
