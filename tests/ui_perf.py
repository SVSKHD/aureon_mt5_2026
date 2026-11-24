"""Replay UI acceptance + performance: the built page (replay/app/web) driven by messages recorded from the real
Python engine on real XAUUSD candles, in headless Chromium (Playwright).

    python tests/ui_perf.py            # prints the measurements as JSON
    python -m unittest tests.ui_perf   # the same checks as assertions (skipped without Playwright/Chromium)

Checks: every revealed candle drawn incrementally (no setData after the start), no candle beyond the revealed one,
EMA values on the chart equal the engine's, the view does not jump while the user inspects older candles, frame
times during playback at several batch sizes, zoom and pan.
"""
import functools, http.server, json, os, shutil, socketserver, statistics, sys, tempfile, threading, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
SAMPLE = os.path.join(HERE, "data", "XAUUSD_M30_sample_20260901_20261030.csv")
WEB = os.path.join(ROOT, "replay", "app", "web")


def record(per_batch: int, start="2026-10-01", end="2026-10-31", strategy="ema_cross") -> tuple[list, dict]:
    """Messages exactly as the desktop host sends them, `per_batch` candles per frame."""
    os.environ.setdefault("AUREON_DATA", tempfile.mkdtemp(prefix="aureon_perf_"))
    import replay  # noqa: F401  (MT5 stub where MetaTrader5 is not installed)
    from replay import api
    from replay.app.host import Host, UiFeed
    from replay.app.main import _clean
    sent = []
    h = Host(send=sent.append)
    h.post(h.config_msg())
    p = api.prepare(api.Request(start=start, end=end, source="csv", csv_path=SAMPLE, csv_tz="utc",
                                strategy=strategy, out_dir=os.path.join(os.environ["AUREON_DATA"], "perf")))
    h.prepared = p
    h._session_msg(p.session)
    h.pump()
    feed = UiFeed()
    p.session.sinks.append(feed.push)
    truth = {}
    while not p.session.finished:
        for _ in range(per_batch):
            r = p.session.step()
            if r is None:
                break
            truth[r["bar"]["time"]] = r["bar"]
        sent.append(feed.drain())
    return [_clean(m) for m in sent if m], truth


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(folder):
    httpd = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=folder))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


MEASURE = """
async ([nBatches]) => {
  const c = window.__chart;
  const frames = [];
  let last = performance.now(), run = true;
  const loop = (t) => { frames.push(t - last); last = t; if (run) requestAnimationFrame(loop); };
  requestAnimationFrame(loop);
  // wait until every recorded batch has been played (one per frame)
  const t0 = performance.now();
  while (c.stats.batches < nBatches && performance.now() - t0 < 120000) await new Promise(r => setTimeout(r, 50));
  run = false;
  await new Promise(r => setTimeout(r, 50));
  return { frames, stats: { ...c.stats }, playMs: performance.now() - t0 };
}
"""


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 2) if xs else None


def run(per_batches=(1, 4, 16), strategies=("ema_cross", "ema_20_50_basic")) -> dict:
    from playwright.sync_api import sync_playwright
    out = {"chromium": None, "cases": []}
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception:                                 # a pre-installed Chromium of another Playwright build
            browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium"))
        out["chromium"] = browser.version
        for strategy, n in [(st, n) for st in strategies for n in per_batches]:
            msgs, truth = record(n, strategy=strategy)
            tmp = tempfile.mkdtemp(prefix="aureon_ui_")
            site = os.path.join(tmp, "web")
            shutil.copytree(WEB, site)
            with open(os.path.join(site, "fixture.js"), "w", encoding="utf-8") as f:
                f.write("window.__FIXTURE = " + json.dumps(msgs) + ";")
            httpd = serve(site)
            page = browser.new_page(viewport={"width": 1600, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{httpd.server_address[1]}/index.html")
            page.wait_for_function("window.__chart !== undefined")
            batches = sum(1 for m in msgs if m["type"] == "batch")
            res = page.evaluate(MEASURE, [batches])
            frames = res["frames"][2:]
            check = page.evaluate("""() => {
              const c = window.__chart;
              const d = c.candles.data();
              const e200 = c.ema.ema200.data();
              const cl = c.cloud.data();
              const e20 = c.cloudLines ? c.cloudLines.ema20.data() : [];
              return { n: d.length, last: d[d.length - 1].time, e200last: e200[e200.length - 1].value,
                       cloud: cl.length, e20last: e20.length ? e20[e20.length - 1].value : null,
                       resets: c.stats.resets, markers: c.markers.markers().length,
                       bodyText: document.body.innerText.includes('TradingView') };
            }""")
            revealed = sorted(truth)
            last_bar = truth[revealed[-1]]
            # inspect history, then reveal more (a fresh page at a smaller batch): the view must not move
            page.mouse.move(800, 400)
            t0 = time.perf_counter()
            zoom_frames = page.evaluate("""async () => {
              const fr = []; let last = performance.now(), run = true;
              const loop = (t) => { fr.push(t - last); last = t; if (run) requestAnimationFrame(loop); };
              requestAnimationFrame(loop);
              const el = document.querySelector('.chart');
              const r = el.getBoundingClientRect();
              for (let k = 0; k < 60; k++) {
                el.dispatchEvent(new WheelEvent('wheel', { deltaY: k < 30 ? -120 : 120, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2, bubbles: true, cancelable: true }));
                await new Promise(r => requestAnimationFrame(r));
              }
              run = false; return fr.slice(2);
            }""")
            page.mouse.down()
            for k in range(40):
                page.mouse.move(800 + k * 12, 400)
            page.mouse.up()
            pan_ms = (time.perf_counter() - t0) * 1000
            before = page.evaluate("() => window.__chart.chart.timeScale().getVisibleLogicalRange()")
            following = page.evaluate("() => window.__chart.following")
            # reveal 20 more synthetic-but-continuous candles through the same code path while scrolled back
            page.evaluate("""() => {
              const c = window.__chart; const d = c.candles.data(); let t = d[d.length - 1].time; let p = d[d.length - 1].close;
              for (let k = 0; k < 20; k++) { t += 1800; const iso = new Date(t * 1000).toISOString().slice(0, 19);
                c.apply([{ i: 0, time: iso, utc: iso + '+00:00', open: p, high: p + 1, low: p - 1, close: p }], [], null); }
            }""")
            after = page.evaluate("() => window.__chart.chart.timeScale().getVisibleLogicalRange()")
            shot = os.path.join(tmp, f"replay_ui_{strategy}_{n}.png")
            page.screenshot(path=shot)
            page.close(); httpd.shutdown()
            out["cases"].append({
                "strategy": strategy, "candles_per_frame": n,
                "cloud_points": check["cloud"],
                "ema20_matches_engine": (check["e20last"] is None and "ema20" not in last_bar) or
                                        (check["e20last"] is not None and abs(check["e20last"] - last_bar.get("ema20", 1e18)) < 1e-9), "batches": batches, "frames": len(frames),
                "frame_ms_p50": pct(frames, 0.5), "frame_ms_p95": pct(frames, 0.95), "frame_ms_max": pct(frames, 1.0),
                "fps_mean": round(1000 / statistics.mean(frames), 1) if frames else None,
                "batch_apply_ms_max": round(res["stats"]["maxBatchMs"], 2), "candle_updates": res["stats"]["updates"],
                "zoom_frame_ms_p95": pct(zoom_frames, 0.95), "zoom_frame_ms_max": pct(zoom_frames, 1.0),
                "pan_ms_total": round(pan_ms, 1),
                "chart_candles": check["n"], "warmup_plus_revealed": None,
                "last_chart_time_is_last_revealed": check["last"] == int(time.mktime(time.strptime(last_bar["time"], "%Y-%m-%dT%H:%M:%S"))) - time.timezone,
                "ema200_matches_engine": abs(check["e200last"] - last_bar["ema200"]) < 1e-9,
                "full_redraws": check["resets"] - 1, "markers": check["markers"],
                "view_kept_while_inspecting": (not following) and abs(before["from"] - after["from"]) < 1e-6,
                "attribution_visible": check["bodyText"], "page_errors": errors, "screenshot": shot,
                "revealed": len(truth)})
            out["cases"][-1]["warmup_plus_revealed"] = sum(len(m["warmup"]) for m in msgs if m["type"] == "session") + len(truth)
        browser.close()
    return out


class UiPerformance(unittest.TestCase):
    def test_replay_ui(self):
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright not installed")
        if not os.path.exists(os.path.join(WEB, "index.html")):
            self.skipTest("UI not built")
        res = run(per_batches=(1, 16))
        for c in res["cases"]:
            self.assertTrue(c["ema20_matches_engine"])
            if c["strategy"] == "ema_20_50_basic":
                self.assertEqual(c["cloud_points"], c["chart_candles"])           # a cloud point on every candle
            else:
                self.assertEqual(c["cloud_points"], 0)                            # EMA 8/32 strategies: no cloud
            self.assertEqual(c["page_errors"], [])
            self.assertEqual(c["chart_candles"], c["warmup_plus_revealed"])      # nothing beyond the revealed candle
            self.assertTrue(c["last_chart_time_is_last_revealed"])
            self.assertTrue(c["ema200_matches_engine"])
            self.assertEqual(c["full_redraws"], 0)                                # incremental, no full redraw
            self.assertEqual(c["candle_updates"], c["revealed"])
            self.assertTrue(c["view_kept_while_inspecting"])
            self.assertTrue(c["attribution_visible"])
            self.assertLess(c["batch_apply_ms_max"], 50)


if __name__ == "__main__":
    print(json.dumps(run(), indent=1))
