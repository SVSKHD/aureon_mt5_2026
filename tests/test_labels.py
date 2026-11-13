"""Label engine: golden test on real XAUUSD M30 data (spec section 18) and unit tests on built bars.

The owner's file tests/data/XAUUSD_M30_20250101_20261106.csv covers both golden trends; until it is added that
test is skipped. tests/data/XAUUSD_M30_sample_20260901_20261030.csv (same feed, same UTC times) covers the short
trend of 29-30 Oct 2026 and always runs. labels.py is pure, so none of this touches MT5."""
import os, sys, unittest
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import labels as L                                     # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
FULL = os.path.join(DATA, "XAUUSD_M30_20250101_20261106.csv")
SAMPLE = os.path.join(DATA, "XAUUSD_M30_sample_20260901_20261030.csv")
XAU_BUFFER = 5.0                                       # TSL_BUFFER for XAUUSD (price units)


def load(path):
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time_utc"])
    return df[["time", "open", "high", "low", "close"]]


class Golden:
    """Mixin: the assertions. self.bars and self.res are set by setUpClass."""
    path = None

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(cls.path):
            raise unittest.SkipTest(f"{os.path.basename(cls.path)} not in tests/data/ yet")
        cls.bars = L.prepare(load(cls.path))
        cls.res = L.run(cls.bars)

    def at(self, label, when):
        t = pd.Timestamp(when)
        return [e for e in self.res.events if e["label"] == label and e["time"] == t]

    def one(self, label, when, price=None, side=None):
        found = self.at(label, when)
        self.assertEqual(len(found), 1, f"{label} at {when}: {found}")
        if price is not None:
            self.assertAlmostEqual(found[0]["price"], price, delta=0.1, msg=f"{label} at {when}")
        if side is not None:
            self.assertEqual(found[0]["side"], side, f"{label} at {when}")
        return found[0]

    def steps_from_cross(self, when):
        cross = self.one(L.CROSS, when)
        return L.tsl_steps(self.bars, cross["i"], cross["side"], lambda i: XAU_BUFFER)

    def check_short_29_30_oct(self):
        c = self.one(L.CROSS, "2026-10-29 05:30", 4038.1, side=-1)
        self.assertEqual(c["tag"], "below 200")
        steps = [s for s in self.steps_from_cross("2026-10-29 05:30") if s["time"] <= pd.Timestamp("2026-10-30 14:00")]
        self.assertEqual([str(s["time"]) for s in steps],
                         ["2026-10-29 18:00:00", "2026-10-30 02:30:00", "2026-10-30 08:30:00", "2026-10-30 14:00:00"])
        for s, want in zip(steps, (4044.2, 4037.4, 4016.4, 4004.2)):
            self.assertAlmostEqual(s["sl"], want, delta=0.1)
        self.one(L.GAP_WIDENING, "2026-10-30 08:30", side=-1)          # same candle as the 3rd step -> alert C
        self.assertIn(pd.Timestamp("2026-10-30 08:30"), [s["time"] for s in steps])
        self.one(L.WIDE_GAP, "2026-10-30 17:00", 3907.2, side=-1)
        self.one(L.GAP_PEAKED, "2026-10-30 21:00", 3875.7, side=-1)
        for a, b in zip(steps, steps[1:]):                             # a step never loosens the stop (short: lower)
            self.assertLess(b["sl"], a["sl"])


class GoldenSample(Golden, unittest.TestCase):
    path = SAMPLE

    def test_short_trend_29_30_oct_2026(self):
        self.check_short_29_30_oct()


class GoldenFull(Golden, unittest.TestCase):
    path = FULL

    def test_short_trend_29_30_oct_2026(self):
        self.check_short_29_30_oct()

    def test_long_trend_4_5_nov_2026(self):
        self.one(L.CROSS, "2026-11-04 01:30", 3938.2, side=1)
        self.assertEqual(self.one(L.CROSS, "2026-11-04 01:30")["tag"], "below 200")
        self.one(L.GAP_WIDENING, "2026-11-04 07:30", side=1)
        self.one(L.WIDE_GAP, "2026-11-04 09:30", 4022.5)
        self.one(L.GAP_PEAKED, "2026-11-04 13:00", 4041.9)
        self.one(L.PULLBACK, "2026-11-04 15:00", 4039.2)
        self.one(L.RE_ENTRY, "2026-11-04 17:30", 4048.9)
        steps = self.steps_from_cross("2026-11-04 01:30")
        first = [s for s in steps if s["time"] == pd.Timestamp("2026-11-04 18:30")]
        self.assertEqual(len(first), 1); self.assertAlmostEqual(first[0]["sl"], 4024.2, delta=0.1)
        self.one(L.GAP_PEAKED, "2026-11-05 07:30", 4114.6)
        c = self.one(L.CROSS, "2026-11-05 20:30", 4105.1, side=-1)
        self.assertEqual(c["tag"], "above 200")

    def test_bad_ticks_5_and_6_nov_are_cleaned(self):
        b = self.bars
        for day in ("2026-11-05 01:00", "2026-11-06 01:00"):
            row = b[b["time"] == pd.Timestamp(day)].iloc[0]
            self.assertAlmostEqual(row["low"], 3914.40, delta=0.01)
            self.assertTrue(row["cleaned"]); self.assertGreater(row["clean_low"], 3914.40 + 100)
            for label in (L.PULLBACK, L.STEP, L.PULLBACK_FAILED):
                self.assertEqual(self.at(label, day), [], f"{label} on the bad tick {day}")
        steps = self.steps_from_cross("2026-11-04 01:30")              # the long's stop is never "hit" by them
        for day in ("2026-11-05 01:00", "2026-11-06 01:00"):
            t = pd.Timestamp(day)
            active = [s["sl"] for s in steps if s["time"] < t]
            row = b[b["time"] == t].iloc[0]
            if active:
                self.assertGreater(row["clean_low"], active[-1])
            self.assertNotIn(t, [s["time"] for s in steps])


def trend_bars(closes, start="2026-01-05 00:00", wick=0.5):
    df = pd.DataFrame({"time": pd.date_range(start, periods=len(closes), freq="30min"), "close": closes})
    df["open"] = df["close"].shift(fill_value=closes[0])
    df["high"] = df[["open", "close"]].max(axis=1) + wick
    df["low"] = df[["open", "close"]].min(axis=1) - wick
    return df


class Units(unittest.TestCase):
    def test_bad_tick_wicks_are_cut_at_the_body_and_logged(self):
        closes = [4000 + (k % 3) for k in range(60)]
        df = trend_bars(closes)
        df.loc[50, "low"] = df.loc[50, "low"] - 150                   # an impossible wick
        df.loc[52, "high"] = df.loc[52, "high"] + 150
        with self.assertLogs("labels", "INFO") as logs:
            b = L.prepare(df)
        self.assertTrue(b.loc[50, "cleaned"]); self.assertEqual(b.loc[50, "clean_low"], min(df.loc[50, ["open", "close"]]))
        self.assertTrue(b.loc[52, "cleaned"]); self.assertEqual(b.loc[52, "clean_high"], max(df.loc[52, ["open", "close"]]))
        self.assertFalse(b.loc[51, "cleaned"])
        self.assertEqual(sum("bad tick cleaned" in m for m in logs.output), 2)
        self.assertLess(b["atr"].iloc[55], 5)                          # ATR is not blown up by the bad tick

    def test_phases_fire_once_per_episode_and_rearm(self):
        closes = [4000.0] * 260 + [4000 + 2.5 * k for k in range(1, 80)] + [4197.5 - 1.5 * k for k in range(1, 60)]
        res = L.run(L.prepare(trend_bars(closes)))
        labels_seen = [e["label"] for e in res.events]
        self.assertIn(L.CROSS, labels_seen); self.assertIn(L.GAP_WIDENING, labels_seen)
        for side in (1, -1):                                           # once per trend (the rally, then the drop)
            self.assertEqual(sum(1 for e in res.events if e["label"] == L.WIDE_GAP and e["side"] == side), 1)
        self.assertIn(L.GAP_PEAKED, labels_seen)
        # the rally starts from a flat line, so it is the replay's first trend (never seen crossing): no CROSS for it;
        # the turn into the drop is a real cross, and it reports the rally as finished
        crosses = [e for e in res.events if e["label"] == L.CROSS]
        self.assertEqual([e["side"] for e in crosses], [-1])
        self.assertEqual(crosses[0]["finished"]["side"], 1)
        self.assertIn(L.TREND_FINISHED, labels_seen)

    def test_replay_from_a_later_start_gives_the_same_recent_labels(self):
        """Restart safety: the state is rebuilt from candles, so a replay window that starts before the last
        cross gives the same labels for that trend."""
        df = load(SAMPLE)
        full = L.run(L.prepare(df))
        tail = L.run(L.prepare(df.iloc[-1200:].reset_index(drop=True)))
        since = pd.Timestamp("2026-10-29 05:30")
        pick = lambda r: [(e["label"], str(e["time"])) for e in r.events
                          if e["time"] >= since and e["label"] not in (L.PRE_CROSS, L.PRE_CROSS_FAILED)]
        self.assertEqual(pick(full), pick(tail))
        self.assertEqual(L.status_line(full.state), L.status_line(tail.state))

    def test_status_line(self):
        res = L.run(L.prepare(load(SAMPLE)))
        line = L.status_line(res.state)
        self.assertIn("SHORT", line); self.assertIn("ATR", line); self.assertIn("candles since the cross", line)

    def test_pre_cross_is_a_watch_signal_with_failure(self):
        res = L.run(L.prepare(load(SAMPLE)))
        pre = [e for e in res.events if e["label"] == L.PRE_CROSS]
        self.assertTrue(pre)
        for e in pre:
            self.assertIn(e["pair"], ("8/32", "8/200", "32/200"))
            arm, fire, _, _ = L.DEFAULTS[{"8/32": "pre_8_32", "8/200": "pre_8_200", "32/200": "pre_32_200"}[e["pair"]]]
            self.assertLessEqual(e["dist_atr"], fire + 1e-9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
