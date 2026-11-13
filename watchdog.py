"""Aureon process watchdog: run `python watchdog.py` instead of `python bot.py`.

Supervises one bot process: restarts it on unexpected exit, and on a FREEZE (bot alive but its
heartbeat.json stops updating). The bot itself owns MT5 reconnects, weekend pauses, replay and delivery.
For restart after a machine reboot, add `python watchdog.py` to Task Scheduler / systemd.
"""
from __future__ import annotations
import json, logging, os, signal, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import config as C                                    # paths + DailyFileHandler
import updater as U
C.migrate_legacy_files()

LOCK_PATH = Path(C.WATCHDOG_LOCK)
RESTART_INITIAL_SECONDS = 5
RESTART_MAX_SECONDS = 300
STABLE_AFTER_SECONDS = 300
STOP_GRACE_SECONDS = 15
HEARTBEAT_GRACE = 120                                 # seconds past the bot's own "next_within" before we call it frozen
STARTUP_GRACE = 180                                   # time allowed for the first heartbeat after launch

log = logging.getLogger("aureon.watchdog")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
for h in (logging.StreamHandler(sys.stdout), C.DailyFileHandler(C.RUNTIME_LOG_DIR, "watchdog")):
    h.setFormatter(_fmt); log.addHandler(h)


class SingleInstance:
    """OS-level lock held for the watchdog's lifetime; released by the OS even after an abrupt kill."""
    def __init__(self, path: Path): self.path, self.fh = path, None

    def __enter__(self):
        self.fh = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                if not self.fh.read(1):
                    self.fh.write(b" "); self.fh.flush()
                self.fh.seek(0); msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close(); raise RuntimeError("Aureon watchdog is already running (lock busy)")
        return self

    def __exit__(self, *_):
        if self.fh:
            try:
                if os.name == "nt":
                    import msvcrt; self.fh.seek(0); msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl; fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            finally:
                self.fh.close()


def heartbeat_age() -> tuple[float, float] | None:
    """(seconds since last beat, seconds the bot said to expect before the next) or None if no file."""
    try:
        with open(C.HEARTBEAT_FILE) as f:
            d = json.load(f)
        return time.time() - d["ts"], float(d.get("next_within", 60))
    except Exception:
        return None


class Watchdog:
    def __init__(self):
        self.stopping = False
        self.child: subprocess.Popen | None = None
        self.next_check = time.monotonic() + 60          # first update check a minute after start
        self.awaiting: dict | None = None                # update waiting for the bot's safe moment
        self.probation: dict | None = None               # {"prev","new","started"}: new version on trial

    # ---------------------------------------------------------------- auto update (spec section 14)
    def maybe_update(self, now: float | None = None):
        """Every UPDATE_CHECK_MIN: is there a clean fast-forward? Then ask the bot for a safe moment."""
        now = time.monotonic() if now is None else now
        if not U.UPDATE_ENABLED or self.awaiting or self.probation or now < self.next_check:
            return
        self.next_check = now + U.UPDATE_CHECK_MIN * 60
        st = U.load_state()
        action, reason, info = U.plan(str(ROOT))
        remote = info.get("remote")
        if action == "refuse":
            if remote != st.get("last_failed_remote"):        # report once per remote head
                files = info.get("conflicts") or info.get("dirty") or info.get("local_commits") or []
                U.notice("update_skipped", f"Update to {remote[:7]} NOT applied — still running {U.version_at(str(ROOT))}",
                         reason + (("\nFiles: " + ", ".join(files[:15])) if files else ""))
                st["last_failed_remote"] = remote; U.save_state(st)
                log.warning("update not applied: %s %s", reason, files)
            return
        if action != "apply" or remote == st.get("last_failed_remote"):
            return
        log.info("update available (%s) — asking the bot for a safe moment", reason)
        with open(U.REQUEST_FILE, "w") as f:
            f.write(remote)
        self.awaiting = {"remote": remote, "deadline": now + 15 * 60}

    def apply_update(self) -> None:
        """The bot stopped itself at a safe moment: pull, then start the new version on probation."""
        for p in (U.REQUEST_FILE, U.READY_FILE):
            if os.path.exists(p):
                os.remove(p)
        want = self.awaiting or {}
        self.awaiting = None
        st = U.load_state()
        ok, reason, res = U.apply(str(ROOT))
        if not ok:
            U.notice("update_skipped", "Update NOT applied — still running the previous version",
                     reason + (("\nConflicts: " + ", ".join(res.get("conflicts", []))) if res.get("conflicts") else ""))
            st["last_failed_remote"] = want.get("remote"); U.save_state(st)
            log.error("update failed: %s", reason)
            return
        self.probation = {"prev": res["prev"], "new": res["new"], "started": None}
        st["pending"] = self.probation; U.save_state(st)
        log.info("updated %s — new version on probation for %ss", reason, U.HEALTH_SECONDS)

    def probation_verdict(self, alive: bool, runtime: float, beat: bool) -> str | None:
        """None while undecided, "ok" or a failure reason."""
        if self.probation is None:
            return None
        if not alive and runtime < U.HEALTH_SECONDS:
            return f"it crashed on start (after {runtime:.0f} s)"
        if runtime >= U.HEALTH_SECONDS:
            return "ok" if (alive and beat) else f"no heartbeat within {U.HEALTH_SECONDS} s"
        return None

    def finish_probation(self, verdict: str):
        p, self.probation = self.probation, None
        st = U.load_state(); st.pop("pending", None)
        if verdict == "ok":
            _, sha = U.git(str(ROOT), "rev-parse", "--short", "HEAD")
            U.notice("updated", f"Updated to {U.version_at(str(ROOT))} ({sha})", f"{p['prev'][:7]} → {p['new'][:7]}")
            U.save_state(st)
            return
        self._terminate()
        U.rollback(str(ROOT), p["prev"])
        st["last_failed_remote"] = p["new"]; U.save_state(st)
        U.notice("rolled_back", f"Update {p['new'][:7]} ROLLED BACK to {p['prev'][:7]} ({U.version_at(str(ROOT))})",
                 f"The new version failed its health check: {verdict}.")
        log.error("rolled back to %s: %s", p["prev"][:7], verdict)

    def stop(self, *_):
        self.stopping = True
        log.info("Shutdown requested")
        self._terminate()

    def _terminate(self):
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                log.warning("Bot did not stop within %ss; force-killing", STOP_GRACE_SECONDS)
                self.child.kill(); self.child.wait()

    def frozen(self, started: float) -> str | None:
        hb = heartbeat_age()
        if hb is None:
            return f"no heartbeat {STARTUP_GRACE}s after start" if time.monotonic() - started > STARTUP_GRACE else None
        age, expect = hb
        if age > expect + HEARTBEAT_GRACE:
            return f"heartbeat stale: {age:.0f}s old, expected within {expect:.0f}s"
        return None

    def run(self):
        if not (ROOT / "bot.py").is_file():
            raise FileNotFoundError(f"Missing {ROOT / 'bot.py'}")
        if not (C.DISCORD_WEBHOOK or C.SLACK_WEBHOOK or C.NTFY_TOPIC):
            raise RuntimeError(f"Set DISCORD_WEBHOOK, SLACK_WEBHOOK and/or NTFY_TOPIC in {C.ENV_FILE}")
        delay = RESTART_INITIAL_SECONDS
        log.info("Starting Aureon watchdog (%s, %s)", C.SYMBOL, "/".join(C.TIMEFRAMES))
        while not self.stopping:
            started = time.monotonic()
            try:
                if os.path.exists(C.HEARTBEAT_FILE):
                    os.remove(C.HEARTBEAT_FILE)                   # stale beat from a previous run must not count
                self.child = subprocess.Popen([sys.executable, "-u", str(ROOT / "bot.py")], cwd=ROOT)
                log.info("Bot started PID=%s", self.child.pid)
                reason = None
                while self.child.poll() is None and not self.stopping:
                    time.sleep(2)
                    runtime = time.monotonic() - started
                    verdict = self.probation_verdict(True, runtime, heartbeat_age() is not None)
                    if verdict:
                        self.finish_probation(verdict)
                        if verdict != "ok":
                            reason = "rolled back"; break
                    reason = self.frozen(started)
                    if reason:
                        log.error("Bot appears FROZEN (%s) — killing PID=%s", reason, self.child.pid)
                        U.notice("crashed", "bot FROZE — restarting it", f"{reason}. Killed and restarted by the watchdog.")
                        self._terminate(); break
                    self.maybe_update()
                    if self.awaiting and time.monotonic() > self.awaiting["deadline"]:
                        log.warning("bot never reached a safe moment for the update; trying again later")
                        if os.path.exists(U.REQUEST_FILE):
                            os.remove(U.REQUEST_FILE)
                        self.awaiting = None
                if self.stopping:
                    break
                runtime = time.monotonic() - started
                if os.path.exists(U.READY_FILE):                  # the bot stopped itself for an update
                    self.apply_update(); delay = RESTART_INITIAL_SECONDS
                    continue
                if reason == "rolled back":
                    delay = RESTART_INITIAL_SECONDS
                    continue
                verdict = self.probation_verdict(False, runtime, heartbeat_age() is not None)
                if verdict:
                    self.finish_probation(verdict); delay = RESTART_INITIAL_SECONDS
                    continue
                if not reason:
                    log.error("Bot exited (code=%s, uptime=%.0fs)", self.child.returncode, runtime)
                    U.notice("crashed", "bot STOPPED unexpectedly — restarting it",
                             f"exit code {self.child.returncode} after {runtime:.0f} s. Restarting in {delay} s.")
                if runtime >= STABLE_AFTER_SECONDS:
                    delay = RESTART_INITIAL_SECONDS
            except OSError:
                log.exception("Failed to start bot")
            finally:
                self.child = None
            if self.stopping:
                break
            log.warning("Restarting in %ds", delay)
            deadline = time.monotonic() + delay
            while not self.stopping and time.monotonic() < deadline:
                time.sleep(min(1, deadline - time.monotonic()))
            delay = min(delay * 2, RESTART_MAX_SECONDS)
        log.info("Watchdog stopped")


def main():
    wd = Watchdog()
    signal.signal(signal.SIGINT, wd.stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, wd.stop)
    try:
        with SingleInstance(LOCK_PATH):
            wd.run()
    except (RuntimeError, FileNotFoundError) as exc:
        log.error("Cannot start: %s", exc); return 1
    finally:
        wd._terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
