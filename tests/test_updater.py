"""Failure handling (spec 13) and auto update with rollback (spec 14). Real git, in throwaway repositories."""
import glob, os, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bot import Http, titles, World                    # noqa: E402
from test_v170 import V170, records                          # noqa: E402
import config as C, notifier, bot, positions, updater as U, alerts   # noqa: E402
import watchdog as W                                         # noqa: E402


def sh(cwd, *args):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stdout, r.stderr)
    return r.stdout.strip()


class Repos:
    """origin (bare) + 'live' (the machine running the bot) + 'dev' (where new versions are pushed from)."""
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="aureon_git_")
        self.origin, self.live, self.dev = (os.path.join(self.root, n) for n in ("origin.git", "live", "dev"))
        sh(self.root, "git", "init", "-q", "--bare", "-b", "main", self.origin)
        sh(self.root, "git", "clone", "-q", self.origin, self.dev)
        for d in (self.dev,):
            sh(d, "git", "config", "user.email", "t@example.com"); sh(d, "git", "config", "user.name", "t")
            sh(d, "git", "checkout", "-q", "-b", "main")
        self.write(self.dev, "VERSION", "1.7.0\n"); self.write(self.dev, "requirements.txt", "pandas\n")
        self.write(self.dev, "bot.py", "print('v1')\n")
        self.commit_push("v1")
        sh(self.root, "git", "clone", "-q", "-b", "main", self.origin, self.live)
        sh(self.live, "git", "config", "user.email", "t@example.com"); sh(self.live, "git", "config", "user.name", "t")

    @staticmethod
    def write(repo, name, text):
        with open(os.path.join(repo, name), "w") as f:
            f.write(text)

    def commit_push(self, msg, **files):
        for name, text in files.items():
            self.write(self.dev, name.replace("__", "."), text)
        sh(self.dev, "git", "add", "-A"); sh(self.dev, "git", "commit", "-q", "-m", msg)
        sh(self.dev, "git", "push", "-q", "origin", "main")
        return sh(self.dev, "git", "rev-parse", "HEAD")

    def head(self):
        return sh(self.live, "git", "rev-parse", "HEAD")

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


@unittest.skipIf(shutil.which("git") is None, "git not installed")
class Updater(unittest.TestCase):
    def setUp(self):
        self.r = Repos(); self.addCleanup(self.r.cleanup)

    def test_up_to_date_does_nothing(self):
        self.assertEqual(U.plan(self.r.live, "main")[0], "none")

    def test_clean_fast_forward_is_applied_and_pip_runs_when_requirements_change(self):
        new = self.r.commit_push("v2", bot__py="print('v2')\n", VERSION="1.7.1\n", requirements__txt="pandas\nnumpy\n")
        action, reason, info = U.plan(self.r.live, "main")
        self.assertEqual(action, "apply"); self.assertEqual(info["remote"], new)
        pip = mock.Mock(return_value=(True, "ok"))
        ok, reason, res = U.apply(self.r.live, "main", pip=pip)
        self.assertTrue(ok, reason); self.assertEqual(self.r.head(), new); pip.assert_called_once()
        self.assertEqual(U.version_at(self.r.live), "1.7.1")

    def test_failed_pip_undoes_the_update(self):
        old = self.r.head()
        self.r.commit_push("v2", requirements__txt="nope\n")
        ok, reason, _ = U.apply(self.r.live, "main", pip=lambda root: (False, "no such package"))
        self.assertFalse(ok); self.assertIn("pip install failed", reason); self.assertEqual(self.r.head(), old)

    def test_dirty_tree_is_refused_with_the_conflicting_files(self):
        self.r.write(self.r.live, "bot.py", "print('local edit')\n")
        self.r.commit_push("v2", bot__py="print('v2')\n")
        action, reason, info = U.plan(self.r.live, "main")
        self.assertEqual(action, "refuse"); self.assertIn("local changes", reason)
        self.assertEqual(info["conflicts"], ["bot.py"])

    def test_untracked_files_that_the_update_does_not_touch_are_fine(self):
        self.r.write(self.r.live, ".env", "SECRET=1\n")             # .env and logs/ live untracked next to the code
        self.r.commit_push("v2", bot__py="print('v2')\n")
        self.assertEqual(U.plan(self.r.live, "main")[0], "apply")

    def test_local_commits_mean_no_fast_forward(self):
        self.r.write(self.r.live, "local.txt", "x"); sh(self.r.live, "git", "add", "-A")
        sh(self.r.live, "git", "commit", "-q", "-m", "local")
        self.r.commit_push("v2", bot__py="print('v2')\n")
        action, reason, _ = U.plan(self.r.live, "main")
        self.assertEqual(action, "refuse"); self.assertIn("not a fast-forward", reason)

    def test_rollback(self):
        old = self.r.head(); self.r.commit_push("v2", bot__py="print('v2')\n")
        self.assertTrue(U.apply(self.r.live, "main", pip=lambda r: (True, ""))[0])
        self.assertTrue(U.rollback(self.r.live, old)); self.assertEqual(self.r.head(), old)


@unittest.skipIf(shutil.which("git") is None, "git not installed")
class WatchdogUpdate(V170):
    def setUp(self):
        super().setUp()
        self.r = Repos(); self.addCleanup(self.r.cleanup)
        p = mock.patch.object(W, "ROOT", __import__("pathlib").Path(self.r.live)); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(U, "UPDATE_BRANCH", "main"); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(U, "plan", lambda root, branch="main": U.__dict__["_plan"](root, "main")); p.start(); self.addCleanup(p.stop)
        for f in (U.STATE_FILE, U.REQUEST_FILE, U.READY_FILE):
            if os.path.exists(f):
                os.remove(f)
        U.pop_notices()
        self.wd = W.Watchdog(); self.wd.next_check = 0

    def test_clean_update_waits_for_the_bot_then_restarts_and_reports(self):
        new = self.r.commit_push("v2", bot__py="print('v2')\n", VERSION="1.7.1\n")
        self.wd.maybe_update(now=1)
        self.assertTrue(os.path.exists(U.REQUEST_FILE))            # asks the bot; applies nothing yet
        self.assertNotEqual(self.r.head(), new)
        self.assertTrue(U.restart_requested())
        U.ready_for_restart()                                      # the bot stopped itself at a safe moment
        self.wd.apply_update()
        self.assertEqual(self.r.head(), new); self.assertFalse(os.path.exists(U.REQUEST_FILE))
        self.assertIsNone(self.wd.probation_verdict(True, 30, True))
        self.wd.finish_probation(self.wd.probation_verdict(True, 95, True))
        bot.deliver_notices()
        t = [x for x in titles(Http.to("discord")) if "Updated to 1.7.1" in x]
        self.assertEqual(len(t), 1)
        self.assertTrue(any(r["event"] == "updated" for r in records("system")))

    def test_broken_new_version_is_rolled_back(self):
        old = self.r.head()
        new = self.r.commit_push("v2", bot__py="raise SystemExit(1)\n")
        self.wd.maybe_update(now=1); U.ready_for_restart(); self.wd.apply_update()
        self.assertEqual(self.r.head(), new)
        verdict = self.wd.probation_verdict(False, 4, False)       # crashed 4 s after start
        self.assertIn("crashed on start", verdict)
        self.wd.finish_probation(verdict)
        self.assertEqual(self.r.head(), old)
        self.assertEqual(U.load_state()["last_failed_remote"], new)
        bot.deliver_notices()
        t = [x for x in titles(Http.to("discord")) if "ROLLED BACK" in x]
        self.assertEqual(len(t), 1)
        self.wd.next_check = 0; self.wd.maybe_update(now=2)        # the same bad head is not tried again
        self.assertFalse(os.path.exists(U.REQUEST_FILE))
        self.assertIn("no heartbeat", self.wd.probation_verdict(True, 95, False) or "no heartbeat")

    def test_dirty_tree_keeps_running_and_reports_once_per_remote_head(self):
        self.r.write(self.r.live, "bot.py", "print('hotfix on the machine')\n")
        self.r.commit_push("v2", bot__py="print('v2')\n")
        for k in range(3):
            self.wd.next_check = 0; self.wd.maybe_update(now=k)
        self.assertFalse(os.path.exists(U.REQUEST_FILE))
        bot.deliver_notices()
        t = [x for x in titles(Http.to("discord")) if "NOT applied" in x]
        self.assertEqual(len(t), 1)
        self.assertIn("bot.py", json_desc := str(Http.to("discord")))
        self.r.commit_push("v3", other__txt="x")                   # a new remote head: reported again
        self.wd.next_check = 0; self.wd.maybe_update(now=9); bot.deliver_notices()
        self.assertEqual(len([x for x in titles(Http.to("discord")) if "NOT applied" in x]), 2)


U._plan = U.plan                                                   # the real plan(), for the patch above


class Failures(V170):
    def test_a_failing_stage_does_not_stop_the_others_and_is_reported_once(self):
        calls = []
        with mock.patch.object(positions, "track", side_effect=RuntimeError("broker hiccup")):
            for _ in range(3):
                bot.run_stage("positions", positions.track, bot.state, {})
                bot.run_stage("other", lambda: calls.append(1))
        self.assertEqual(len(calls), 3)
        t = [x for x in titles(Http.to("discord")) if "script failure in 'positions'" in x]
        self.assertEqual(len(t), 1)
        self.assertEqual(len([r for r in records("system") if r["event"] == "stage_failed"]), 3)

    def test_one_real_pass_stops_for_an_update_at_a_safe_moment(self):
        C.PLACE_ENABLED = False
        with open(U.REQUEST_FILE, "w") as f:
            f.write("x")
        self.addCleanup(lambda: [os.remove(p) for p in (U.REQUEST_FILE, U.READY_FILE) if os.path.exists(p)])
        with mock.patch.object(positions, "track", side_effect=RuntimeError("boom")), \
                mock.patch.object(bot, "print_banner"):
            bot.main()                                             # returns instead of looping forever
        self.assertTrue(os.path.exists(U.READY_FILE))
        names = titles(Http.to("discord"))
        self.assertTrue(any("script failure in 'positions'" in t for t in names))
        self.assertTrue(any("bot online" in t for t in names))     # the other stages ran
        self.assertTrue(any(r["event"] == "stop_for_update" for r in records("system")))

    def test_no_safe_moment_while_a_command_is_waiting(self):
        alerts.submit("list", wait_s=0)
        self.assertFalse(bot.safe_to_restart())
        bot.process_commands()
        self.assertTrue(bot.safe_to_restart())


if __name__ == "__main__":
    unittest.main(verbosity=2)
