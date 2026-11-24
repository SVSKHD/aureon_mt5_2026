"""The desktop app's host (no Qt needed) and, when PySide6 + Qt WebEngine are installed, a real offscreen window."""
import json, os, subprocess, sys, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_replay import ReplayBase, SAMPLE, REAL_SLEEP, ROOT   # noqa: E402
from test_bot import World                                   # noqa: E402
import config as C                                           # noqa: E402
from replay.app.host import Host, UiFeed                     # noqa: E402
from replay.app.main import to_json                          # noqa: E402

REQ = {"symbol": "XAUUSD", "tf": "M30", "start": "2026-10-20", "end": "2026-10-23", "source": "csv",
       "csv_path": SAMPLE, "csv_tz": "utc", "strategy": "ema_cross", "speed": None}


class HostTests(ReplayBase):
    def run_host(self, req, until=("finished",), timeout=120):
        sent = []
        h = Host(send=sent.append)
        h.handle({"cmd": "hello"}); h.handle({"cmd": "start", "request": req})
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            h.pump()
            if any(m["type"] == "report" for m in sent) or any(m["type"] == "error" for m in sent):
                break
            REAL_SLEEP(0.02)
        h.pump()
        self.addCleanup(h.shutdown)
        return h, sent

    def test_full_replay_through_the_host_in_order(self):
        h, sent = self.run_host(REQ)
        types = [m["type"] for m in sent]
        self.assertEqual(types[0], "config")
        self.assertLess(types.index("session"), types.index("batch"))
        bars = [b for m in sent if m["type"] == "batch" for b in m["bars"]]
        self.assertEqual(len(bars), h.ctl.session.total)                   # every candle, none skipped
        self.assertEqual([b["i"] for b in bars], sorted(b["i"] for b in bars))
        evs = [e for m in sent if m["type"] == "batch" for e in m["events"]]
        self.assertEqual([e["seq"] for e in evs], list(range(1, len(evs) + 1)))
        self.assertIn("finished", [m.get("state") for m in sent if m["type"] == "state"])
        rep = next(m for m in sent if m["type"] == "report")
        self.assertIn("HISTORICAL REPLAY", rep["summary"])
        sess = next(m for m in sent if m["type"] == "session")
        self.assertEqual(len(sess["warmup"]), h.ctl.session.ds.first_index)
        self.assertTrue(all(b["time"] < bars[0]["time"] for b in sess["warmup"]))   # context only, all in the past
        json.loads(to_json(sent[-1]))                                       # serialisable for the bridge
        self.assertEqual(World.orders, [])

    def test_restart_sends_a_new_session_and_replays_from_the_start(self):
        h, sent = self.run_host(REQ)
        first = next(m for m in sent if m["type"] == "session")["id"]
        sent.clear()
        h.handle({"cmd": "restart"}); h.pump()
        new = next(m for m in sent if m["type"] == "session")
        self.assertNotEqual(new["id"], first)
        self.assertEqual(h.ctl.session.id, new["id"])

    def test_bad_request_reports_an_error_and_starts_nothing(self):
        h, sent = self.run_host({**REQ, "start": "2026-12-01", "end": "2026-12-05"})
        self.assertTrue(any(m["type"] == "error" and "no XAUUSD M30 candles" in m["text"] for m in sent))
        self.assertIsNone(h.ctl)

    def test_feed_batches_everything_once(self):
        f = UiFeed()
        mk = lambda i: {"bar": {"i": i}, "events": [{"seq": i}], "times": {"close_time": "t", "close_ist": "x"},
                        "done": i, "total": 9, "open_trades": [], "equity": 1, "balance": 1, "status": ""}
        for i in range(5):
            f.push(mk(i))
        f.push_forming({"i": 5, "forming": True})
        b = f.drain()
        self.assertEqual([x["i"] for x in b["bars"]], [0, 1, 2, 3, 4]); self.assertEqual(b["forming"]["i"], 5)
        self.assertIsNone(f.drain())
        f.push(mk(5))
        self.assertIsNone(f.drain()["forming"])                            # a closed candle replaces its forming state

    def test_nan_travels_as_null(self):
        self.assertEqual(json.loads(to_json({"a": float("nan"), "b": [float("inf"), 1.0]})), {"a": None, "b": [None, 1.0]})


class QtWindowSmoke(unittest.TestCase):
    def test_real_window_offscreen(self):
        try:
            import PySide6.QtWebEngineWidgets  # noqa: F401
        except Exception:
            self.skipTest("PySide6 with Qt WebEngine not installed")
        code = f"""
import os, sys, json; sys.path.insert(0, {ROOT!r})
os.environ['AUREON_DATA'] = {os.path.join(C.DATA_DIR, 'qt')!r}
import replay
from replay.app import main as M
seen = []
def ready(win, host, app):
    from PySide6.QtCore import QTimer
    send = host.send
    host.send = lambda m: (seen.append(m['type']), send(m))
    QTimer.singleShot(2000, lambda: host.handle({{'cmd': 'start', 'request': {REQ!r}}}))
    def done():
        print(json.dumps({{'state': host.ctl.state if host.ctl else None, 'types': sorted(set(seen))}})); app.quit()
    QTimer.singleShot(15000, done)
M.main([], on_ready=ready)
"""
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox --disable-gpu"}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=env)
        line = [x for x in out.stdout.splitlines() if x.startswith("{")]
        self.assertTrue(line, out.stderr[-2000:])
        res = json.loads(line[-1])
        self.assertEqual(res["state"], "finished")
        for t in ("config", "session", "batch", "report"):
            self.assertIn(t, res["types"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
