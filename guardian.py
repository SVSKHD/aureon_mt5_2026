"""MT5 Connection Guardian: the single authority on the LIVE connection's state.

It does not replace the bot's reconnect loop (bot.ensure_mt5: 30 s grace, then 5/10/20/40 s backoff) - it watches
it, classifies what is wrong, decides what the loop may do next, and tells you.

States    CONNECTED · DEGRADED · DISCONNECTED · AUTH_FAILED · RECONNECTING · RESTARTING_TERMINAL · RECOVERED ·
          MANUAL_INTERVENTION_REQUIRED
Causes    broker_link_lost (MT5 lost the broker: network or broker) · terminal_gone (terminal crashed / not running)
          · api_init_failed · auth_failed (invalid credentials, account disabled, no session) · wrong_account ·
          wrong_server · stale_feed · frozen_terminal · clock_mismatch · symbol_unavailable · market_closed · ok

Recovery  1 detect (bot health check) · 2 confirm (a second look before acting) · 3 let transient failures heal
          (the bot's grace window) · 4 API reconnect with the bot's backoff · 5 restart the OWNED terminal, only if
          MT5_TERMINAL_RESTART_ENABLED and the cause is a dead/frozen terminal, at most MT5_RECOVERY_MAX_ATTEMPTS
          times per outage with MT5_RECOVERY_COOLDOWN_SECONDS between · 6 verify the expected login/server · 7
          reconcile positions and missed candles · 8 resume · 9 notify (off the loop).

Never:    a weekend / daily break / holiday is a failure; an auth failure or a wrong account triggers restarts
          (manual intervention instead); any order is allowed while the account is unverified (fail closed);
          credentials or the full account number appear in a log or a card.
"""
from __future__ import annotations
import logging, re, time
from datetime import datetime, timedelta, timezone
import config as C

log = logging.getLogger("guardian")
STATES = ("CONNECTED", "DEGRADED", "DISCONNECTED", "AUTH_FAILED", "RECONNECTING", "RESTARTING_TERMINAL", "RECOVERED",
          "MANUAL_INTERVENTION_REQUIRED")
RESTARTABLE = {"terminal_gone", "frozen_terminal", "api_init_failed"}
NO_RESTART = {"auth_failed", "wrong_account", "wrong_server", "market_closed", "broker_link_lost"}
ORDER_OK_STATES = {"CONNECTED", "DEGRADED"}
RESTART_AFTER_FAILED_ATTEMPTS = 3            # API reconnects that must fail before a terminal restart is considered
RESTART_SETTLE_SECONDS = 25                  # give a restarted terminal this long before the next API attempt
TIMELINE_KEEP = 300
# MetaTrader5.last_error() codes (MetaQuotes documentation)
E_AUTH_FAILED = -6
E_IPC = {-10001, -10002, -10003, -10004, -10005}   # send / receive / init / connect / timeout: no terminal to talk to


def mask(login) -> str:
    s = str(login or "")
    return ("•" * max(0, len(s) - 3) + s[-3:]) if s else "?"


def _int(v, default):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def market_closed(server_dt: datetime) -> bool:
    """The gold market's normal closures, in broker server time: the weekend, the daily break, 25 Dec and 1 Jan."""
    wd, hm = server_dt.weekday(), server_dt.hour * 60 + server_dt.minute
    if wd == 5 or wd == 6 or (wd == 4 and hm >= 23 * 60 + 55) or (wd == 0 and hm < 60 + 5):
        return True
    if (server_dt.month, server_dt.day) in ((12, 25), (1, 1)):
        return True
    a, b = (int(x[:2]) * 60 + int(x[3:]) for x in C.MT5_MARKET_BREAK.split("-"))
    return (a <= hm < b) if a < b else (hm >= a or hm < b)


class Guardian:
    def __init__(self, mt5, clock=time.monotonic, wall=time.time, server_now=None, notify=None, terminal=None,
                 reconcile=None, path: str | None = None):
        self.mt5, self.clock, self.wall = mt5, clock, wall
        self.server_now = server_now or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))
        self.notify = notify or (lambda card: None)
        self.reconcile = reconcile                    # () -> dict summary (bot: positions + missed candles)
        self.path = path or C.GUARDIAN_FILE
        self.terminal = terminal
        if self.terminal is None and C.MT5_TERMINAL_PATH:
            import terminal as T
            self.terminal = T.TerminalProcess(C.MT5_TERMINAL_PATH, C.MT5_TERMINAL_DATA_DIR)
        self.state, self.cause, self.detail = "CONNECTED", "ok", ""
        self.since = self.clock()
        self.outage = None                            # {"since", "cause", "attempts", "restarts", "notified": set()}
        self.confirm = {}                             # cause -> first seen (confirmation before acting)
        self.account_ok: bool | None = None
        self.account_reason = ""
        self.last_init_error = None
        self.settle_until = 0.0
        self.timeline: list = []
        self.cooldown_until_wall = 0.0
        self.manual = None                            # {"cause", "reason"} until a healthy, verified look
        self.notified: set = set()
        self.on_change = None                         # () -> None, after every state change (the bot's status card)
        self._load()

    # ------------------------------------------------------------------------------------- persistence
    def _load(self):
        from notifier import load_json
        d, problem = load_json(self.path, {"timeline": [], "cooldown_until": 0.0, "manual": None})
        self.timeline = d.get("timeline", [])[-TIMELINE_KEEP:] if isinstance(d.get("timeline"), list) else []
        self.cooldown_until_wall = float(d.get("cooldown_until") or 0.0)
        self.manual = d.get("manual") if isinstance(d.get("manual"), dict) else None
        if self.manual:                               # survives a bot restart: still needs a person
            self.state, self.cause = "MANUAL_INTERVENTION_REQUIRED", self.manual.get("cause", "unknown")
        if problem:
            log.warning("guardian state: %s", problem)

    def _save(self):
        from notifier import save_json
        try:
            save_json(self.path, {"timeline": self.timeline[-TIMELINE_KEEP:], "cooldown_until": self.cooldown_until_wall,
                                  "manual": self.manual, "state": self.state, "cause": self.cause})
        except OSError:
            log.exception("guardian state write failed")

    @property
    def enabled(self) -> bool:
        return C.MT5_GUARDIAN_ENABLED

    # ----------------------------------------------------------------------------------- transitions
    def _set(self, state: str, cause: str, detail: str = ""):
        if state == self.state and cause == self.cause:
            self.detail = detail or self.detail
            return
        prev = self.state
        self.state, self.cause, self.detail, self.since = state, cause, detail, self.clock()
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "from": prev, "state": state,
               "cause": cause, "detail": detail[:200]}
        self.timeline.append(row)
        del self.timeline[:-TIMELINE_KEEP]
        self._save()
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                log.exception("on_change failed")
        lvl = logging.WARNING if state not in ("CONNECTED", "RECOVERED") else logging.INFO
        log.log(lvl, "MT5 connection %s -> %s (%s) %s", prev, state, cause, detail)
        try:
            import ops
            ops.emit_live("guardian", f"MT5_{state}", severity=self._severity(state), connection_state=state,
                          symbol=C.SYMBOL, detail={"cause": cause, "from": prev, "detail": detail[:200]})
        except Exception:
            log.exception("ops event failed")

    @staticmethod
    def _severity(state):
        return {"AUTH_FAILED": "critical", "MANUAL_INTERVENTION_REQUIRED": "critical", "RESTARTING_TERMINAL": "high",
                "DISCONNECTED": "high", "RECONNECTING": "high", "DEGRADED": "normal"}.get(state, "normal")

    def _card_once(self, key: str, card: dict):
        """One card per key until the connection is healthy again (no repeat on every health check)."""
        if key in self.notified:
            return
        self.notified.add(key)
        if self.outage is not None:
            self.outage["notified"].add(key)
        try:
            self.notify(card)
        except Exception:
            log.exception("guardian notification failed")

    # ------------------------------------------------------------------------------------ classification
    def classify(self, problem: str | None) -> str:
        if problem is None:
            return "ok"
        p = problem.lower()
        if p.startswith("mt5 init failed"):
            code = self._code(problem)
            if code == E_AUTH_FAILED:
                return "auth_failed"
            return "terminal_gone" if code in E_IPC else "api_init_failed"
        if "terminal not connected" in p:
            return "terminal_gone"
        if p.startswith("mt5 terminal has no connection"):
            return "broker_link_lost"
        if p.startswith("clock mismatch"):
            return "clock_mismatch"
        if p.startswith("symbol"):
            return "symbol_unavailable"
        if p.startswith("stale tick") or p == "no tick":
            if market_closed(self.server_now()):
                return "market_closed"
            age = _int((re.findall(r"\((\d+)s\)", problem) or [0])[0], 0)
            return "frozen_terminal" if age >= C.MT5_FROZEN_SECONDS else "stale_feed"
        return "api_init_failed"

    @staticmethod
    def _code(text: str):
        m = re.search(r"\((-?\d+)", text)
        return int(m.group(1)) if m else None

    # --------------------------------------------------------------------------------------- account
    def verify_account(self) -> str | None:
        """None if the terminal is logged into the expected account/server (or nothing is expected)."""
        if not (C.MT5_EXPECTED_LOGIN or C.MT5_EXPECTED_SERVER):
            self.account_ok, self.account_reason = True, ""
            return None
        try:
            acc = self.mt5.account_info()
        except Exception:
            acc = None
        if acc is None:
            why, cause = "the account cannot be read (not logged in / session invalid)", "auth_failed"
        elif C.MT5_EXPECTED_LOGIN and str(getattr(acc, "login", "")) != C.MT5_EXPECTED_LOGIN:
            why, cause = (f"logged into account {mask(getattr(acc, 'login', ''))}, expected {mask(C.MT5_EXPECTED_LOGIN)}",
                          "wrong_account")
        elif C.MT5_EXPECTED_SERVER and str(getattr(acc, "server", "")).lower() != C.MT5_EXPECTED_SERVER.lower():
            why, cause = f"connected to server {getattr(acc, 'server', '?')}, expected {C.MT5_EXPECTED_SERVER}", "wrong_server"
        else:
            self.account_ok, self.account_reason = True, ""
            return None
        self.account_ok, self.account_reason = False, why
        self._manual(cause, why)
        return why

    def _manual(self, cause: str, why: str):
        self.manual = {"cause": cause, "reason": why}
        state = "AUTH_FAILED" if cause == "auth_failed" else "MANUAL_INTERVENTION_REQUIRED"
        self._set(state, cause, why)
        self._card_once(f"manual:{cause}", self._card(
            "🛑 MT5 needs you — " + {"auth_failed": "login FAILED", "wrong_account": "WRONG ACCOUNT",
                                    "wrong_server": "WRONG SERVER"}.get(cause, "recovery FAILED"),
            f"{why}.\nEvery order (trade management, /place, Close) is BLOCKED until this is fixed. "
            f"The bot will not restart the terminal for this. Fix it in MT5; the bot re-checks every "
            f"{_int(C.MT5_RECOVERY_COOLDOWN_SECONDS, 300) // 60} min and resumes by itself.", "critical"))

    def order_block(self) -> str | None:
        """Why no order may be sent right now (None = allowed). Fail closed."""
        if not self.enabled:
            return None
        if self.manual:
            return f"MT5 needs manual intervention: {self.manual.get('reason')}"
        if self.account_ok is False:
            return f"MT5 account not verified: {self.account_reason}"
        if (C.MT5_EXPECTED_LOGIN or C.MT5_EXPECTED_SERVER) and self.account_ok is None:
            return "MT5 account not verified yet"
        if self.state not in ORDER_OK_STATES:
            return f"MT5 connection is {self.state}"
        return None

    # --------------------------------------------------------------------------------------- the loop
    def observe(self, problem: str | None, attempts: int = 0):
        """Every bot health check. problem = the bot's health result (None = healthy)."""
        if not self.enabled:
            return
        cause = self.classify(problem)
        if cause == "ok":
            if self.manual or self.account_ok is not True or self.outage:
                why = self.verify_account()
                if why:
                    return
            if self.outage or self.manual:
                self._recovered()
            elif (self.state, self.cause) != ("CONNECTED", "ok"):
                self._set("CONNECTED", "ok", "")
            return
        if cause == "market_closed":                     # normal closure: not a failure
            self.confirm.clear()
            self._set("CONNECTED", "market_closed", "market closed (weekend / daily break / holiday)")
            return
        if self.outage is None:
            self.outage = {"since": self.clock(), "cause": cause, "attempts": 0, "restarts": 0, "notified": set()}
        if self.manual:                                   # a person must act: no re-classification, no restarts
            self.outage["attempts"] = attempts
            return
        self.outage["cause"], self.outage["attempts"] = cause, attempts
        if cause in ("auth_failed",):
            self._manual(cause, problem or "authorization failed")
            return
        if cause in ("stale_feed", "clock_mismatch", "symbol_unavailable"):
            self._set("DEGRADED", cause, problem or "")
            return
        if cause == "frozen_terminal":
            self._set("DEGRADED", cause, problem or "")
            self.try_restart(cause)
            return
        self._set("RECONNECTING" if attempts else "DISCONNECTED", cause, problem or "")
        if attempts >= RESTART_AFTER_FAILED_ATTEMPTS and cause in RESTARTABLE:
            self.try_restart(cause)

    def init_failed(self, err):
        """initialize() failed: an authorization failure is final until a person fixes it (classified at once, so the
        very next retry already waits for the cooldown)."""
        self.last_init_error = err
        if self.enabled and isinstance(err, tuple) and err and err[0] == E_AUTH_FAILED:
            if self.outage is None:
                self.outage = {"since": self.clock(), "cause": "auth_failed", "attempts": 0, "restarts": 0,
                               "notified": set()}
            self._manual("auth_failed", f"MT5 refused the login ({err[1] if len(err) > 1 else 'authorization failed'})")

    def retry_delay(self, default: float) -> float:
        """How long the bot waits before its next initialize(): the cooldown for causes a retry cannot fix fast."""
        if self.enabled and (self.manual or (self.outage and self.outage["cause"] == "auth_failed")):
            return max(default, float(C.MT5_RECOVERY_COOLDOWN_SECONDS))
        return default

    def hold(self) -> bool:
        """True while a restarted terminal is still starting (no API attempt yet)."""
        return self.clock() < self.settle_until

    def try_restart(self, cause: str) -> bool:
        """Restart the OWNED terminal if everything allows it. Bounded per outage, cooldown between restarts."""
        o = self.outage
        if not (C.MT5_AUTO_RECOVERY_ENABLED and C.MT5_TERMINAL_RESTART_ENABLED) or self.terminal is None:
            return False
        if cause not in RESTARTABLE or cause in NO_RESTART:
            return False
        # confirmation: the same cause seen on two looks before acting (a single bad read never restarts)
        first = self.confirm.setdefault(cause, self.clock())
        if self.clock() - first < 2 * _int(C.MT5_HEALTH_CHECK_SECONDS, 10) and cause != "frozen_terminal":
            return False
        if self.wall() < self.cooldown_until_wall:
            return False
        if o["restarts"] >= _int(C.MT5_RECOVERY_MAX_ATTEMPTS, 3):
            self._manual(cause, f"{o['restarts']} terminal restart(s) did not bring MT5 back ({cause})")
            return False
        o["restarts"] += 1
        self._set("RESTARTING_TERMINAL", cause, f"restart {o['restarts']}/{C.MT5_RECOVERY_MAX_ATTEMPTS}")
        self._card_once(f"restart:{o['restarts']}", self._card(
            f"🔁 restarting the MT5 terminal ({o['restarts']}/{C.MT5_RECOVERY_MAX_ATTEMPTS})",
            f"Cause: {cause}. Only the terminal at MT5_TERMINAL_PATH is restarted; no order is sent and "
            f"broker-side SL/TP stay on the broker's server.", "high"))
        try:
            ok, detail = self.terminal.restart()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        self.cooldown_until_wall = self.wall() + float(C.MT5_RECOVERY_COOLDOWN_SECONDS)
        self.settle_until = self.clock() + RESTART_SETTLE_SECONDS
        self.timeline.append({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "from": self.state,
                              "state": "RESTARTING_TERMINAL", "cause": cause, "detail": f"terminal {detail}"})
        self._save()
        return ok

    def _recovered(self):
        o = self.outage
        summary = {}
        if self.reconcile:
            try:
                summary = self.reconcile() or {}
            except Exception:
                log.exception("reconcile after recovery failed")
                summary = {"error": "reconcile failed (see log)"}
        was_manual = bool(self.manual) or bool(self.notified)
        self.manual = None
        self.notified = set()
        self._set("RECOVERED", "ok", "verified and reconciled")
        if (o and (o["restarts"] or o["notified"])) or was_manual:
            o = o or {"since": self.since, "cause": "manual", "restarts": 0}
            mins = (self.clock() - o["since"]) / 60
            fields = [("Down", f"{mins:.1f} min"), ("Cause", o["cause"]), ("Terminal restarts", str(o["restarts"])),
                      ("Account", "verified" if self.account_ok else "not checked (MT5_EXPECTED_LOGIN empty)")]
            if summary:
                fields.append(("Reconciled", ", ".join(f"{k} {v}" for k, v in summary.items()), False))
            self.notify(self._card("✅ MT5 recovered", "Monitoring resumed. No order was sent during recovery.",
                                   "normal", fields))
        self.outage = None
        self.confirm.clear()
        self.cooldown_until_wall = self.cooldown_until_wall if self.cooldown_until_wall > self.wall() else 0.0
        self._set("CONNECTED", "ok", "")

    # ---------------------------------------------------------------------------------------- status
    def _card(self, title, desc, severity, fields=None):
        import notifier
        kind = {"critical": "MT5_MANUAL", "high": "MT5_RESTART", "normal": "MT5_RECOVERED"}[severity]
        if severity == "critical":
            return notifier.warn(title, desc, ping=True, symbol="/".join(C.enabled_symbols()), kind=kind)
        return notifier.info(title, desc, symbol="/".join(C.enabled_symbols()), kind=kind, fields=fields,
                             channel="alerts" if severity == "high" else "updates",
                             color="WARN" if severity == "high" else "INFO")

    def snapshot(self) -> dict:
        tick_age = None
        try:
            tick = self.mt5.symbol_info_tick(C.broker_symbol(C.SYMBOL))
            if tick is not None:
                import signals
                tick_age = round((signals.server_now() - signals.stamp_to_dt(tick.time)).total_seconds())
        except Exception:
            pass
        o = self.outage or {}
        return {"enabled": self.enabled, "state": self.state, "cause": self.cause, "detail": self.detail,
                "for_seconds": round(self.clock() - self.since), "outage_attempts": o.get("attempts", 0),
                "outage_restarts": o.get("restarts", 0), "account": "verified" if self.account_ok else (
                    "not checked" if self.account_ok is None and not C.MT5_EXPECTED_LOGIN else self.account_reason or "unverified"),
                "orders": self.order_block() or "allowed", "tick_age_s": tick_age,
                "restart": "enabled" if (C.MT5_TERMINAL_RESTART_ENABLED and self.terminal) else "disabled",
                "cooldown_s": max(0, round(self.cooldown_until_wall - self.wall()))}

    def status_line(self, _symbol=None) -> str:
        return f"MT5: {self.state}" + (f" ({self.cause})" if self.cause not in ("ok",) else "")


# ---------------------------------------------------------------------------- the bot's single instance
_current: Guardian | None = None


def install(g: Guardian) -> Guardian:
    global _current
    _current = g
    return g


def current() -> Guardian | None:
    return _current


def order_block() -> str | None:
    """For trader.py / trade_mgmt.py: why orders are blocked by the connection, or None (no Guardian = no block)."""
    g = _current
    return g.order_block() if g is not None else None
