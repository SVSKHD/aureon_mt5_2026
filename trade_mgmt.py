"""Trade management toggle (1.7.3): may the bot itself send MANAGEMENT orders?

Management orders are the ones the bot sends about an existing position without a fresh order command: the stepped
trailing stop (TSL_MODE=auto) and the Apply button of a suggested step (TSL_MODE=suggest). All of them go through
trader.modify_sl, which only ever tightens a stop and calls check() immediately before order_send.

Independent controls (never changed by this toggle):
  PLACE_ENABLED   /place and the confirmed Close button (manual orders you type and confirm)
  TSL_MODE        auto (the bot moves the stop) | suggest (the bot shows an Apply button)
Enabling this toggle never enables automatic entry or automatic closing: the bot has neither.

OFF (the default): positions are still tracked and every notification still goes out; a trailing-stop step is
reported as "not applied" and that step is invalidated; broker-side SL/TP are never touched.

Safety:
  * the bot loop is the only writer (commands arrive through the signed inbox and run in the loop)
  * enabling takes two signed commands from the same explicitly allowed user: enable_request -> a one-time nonce
    (30 s) -> enable_confirm. "enable" alone, a malformed command or an unauthorised user can never enable it.
  * every change bumps a generation number. A management request carries the generation it was decided under;
    check(token) refuses it if management is off OR the generation moved, so a disable invalidates every pending
    request (suggestion buttons included) and a later re-enable does not revive them.
  * persisted atomically (logs/state/trade_management.json + .bak) with a history of every change. Any problem
    reading it = OFF. An ENABLED value is restored after a restart only with TRADE_MANAGEMENT_RESTORE_ENABLED=1
    or TRADE_MANAGEMENT_ENABLED=true in .env (explicit production policy); a persisted OFF always wins.
"""
from __future__ import annotations
import logging, secrets, threading, time
from datetime import datetime, timezone
import config as C

log = logging.getLogger("trade_mgmt")
_lock = threading.Lock()
_s = {"enabled": False, "generation": 0, "loaded": False, "problem": None, "history": [], "changed_at": None,
      "changed_by": None}
_nonces: dict = {}                                     # nonce -> (user, expires monotonic)
HISTORY_KEEP = 100


def _now() -> float:
    return time.time()


def _iso(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else "-"


def load() -> list[str]:
    """Read the persisted value at startup (fail closed). Returns notes worth telling the owner."""
    from notifier import load_json
    notes = []
    with _lock:
        d, problem = load_json(C.TRADE_MANAGEMENT_FILE, {})
        enabled = False
        if C.TRADE_MANAGEMENT_MALFORMED:
            notes.append(f"TRADE_MANAGEMENT_ENABLED={C.TRADE_MANAGEMENT_RAW!r} is not true/false: trade management is OFF")
        if problem:
            notes.append(f"trade management state: {problem}: trade management is OFF")
        elif d and not (isinstance(d.get("enabled"), bool) and isinstance(d.get("generation", 0), int)
                        and isinstance(d.get("history", []), list)):
            notes.append("trade management state file is unreadable: trade management is OFF")
            problem = "bad shape"
        if not problem and not C.TRADE_MANAGEMENT_MALFORMED:
            if not d:                                          # first start: the .env value
                enabled = C.TRADE_MANAGEMENT_ENABLED
            elif d["enabled"]:
                enabled = C.TRADE_MANAGEMENT_RESTORE_ENABLED or C.TRADE_MANAGEMENT_ENABLED
                if not enabled:
                    notes.append("trade management was ON before the restart; it starts OFF "
                                 "(set TRADE_MANAGEMENT_RESTORE_ENABLED=1 to restore it). /trade_management enable")
        hist = d.get("history", []) if not problem and isinstance(d.get("history", []), list) else []
        gen = d.get("generation", 0) if not problem and isinstance(d.get("generation", 0), int) else 0
        prev = bool(d.get("enabled")) if not problem and isinstance(d.get("enabled"), bool) else None
        _s.update(enabled=enabled, generation=gen + (1 if prev is not None and prev != enabled else 0),
                  loaded=True, problem=problem, history=hist[-HISTORY_KEEP:], changed_at=d.get("changed_at"),
                  changed_by=d.get("changed_by"))
        _nonces.clear()
        if prev is not None and prev != enabled:
            _s["history"].append({"ts": _iso(_now()), "user": None, "previous": prev, "new": enabled,
                                  "reason": "restart policy"})
        if problem or prev != enabled:
            _persist()
    for n in notes:
        log.warning(n)
    return notes


def _ensure():
    if not _s["loaded"]:
        load()


def enabled() -> bool:
    _ensure()
    return bool(_s["enabled"])


def token() -> int:
    """The generation a management request is decided under. Pass it to check() / trader.modify_sl."""
    _ensure()
    return int(_s["generation"])


def check(tok) -> str | None:
    """Called immediately before every management order is sent. None = allowed, else the refusal."""
    _ensure()
    import guardian
    why = guardian.order_block()                                   # wrong account / recovering: fail closed
    if why:
        return why
    with _lock:
        if not _s["enabled"]:
            return "trade management is OFF (/trade_management enable)"
        try:
            ok = tok is not None and int(tok) == _s["generation"]
        except (TypeError, ValueError):
            ok = False
        if not ok:
            return "this request was made before trade management was last switched; it is no longer valid"
        return None


def _persist() -> bool:
    from notifier import save_json
    try:
        save_json(C.TRADE_MANAGEMENT_FILE, {"enabled": _s["enabled"], "generation": _s["generation"],
                                            "changed_at": _s["changed_at"], "changed_by": _s["changed_by"],
                                            "history": _s["history"][-HISTORY_KEEP:]})
        return True
    except OSError:
        log.exception("trade management state write failed")
        return False


def _set(value: bool, user, reason: str) -> tuple[bool, str | None]:
    """Change the value (bot loop only). Returns (previous, error). Enabling that cannot be persisted is undone."""
    with _lock:
        prev = _s["enabled"]
        _s["enabled"], _s["generation"] = value, _s["generation"] + 1
        _s["changed_at"], _s["changed_by"] = _iso(_now()), user
        _s["history"].append({"ts": _s["changed_at"], "user": user, "previous": prev, "new": value, "reason": reason})
        _nonces.clear()                                    # any open confirmation dies with a change
        if not _persist():
            if value:                                      # fail closed: never ON without a durable record
                _s["enabled"], _s["generation"] = False, _s["generation"] + 1
                return prev, "could not save the setting, so trade management stays OFF"
            return prev, "could not save the setting; it is OFF now but may come back as before after a restart"
    log.warning("TRADE MANAGEMENT %s -> %s by user %s at %s (%s)", "ON" if prev else "OFF", "ON" if value else "OFF",
                user, _s["changed_at"], reason)
    import store
    store.record("system", {"event": "trade_management", "previous": prev, "new": value, "user": user,
                            "reason": reason, "changed_at": _s["changed_at"]})
    return prev, None


def disable(user, reason: str = "command") -> tuple[bool, str | None]:
    _ensure()
    return _set(False, user, reason)


def authorized(req: dict) -> str | None:
    """None if the user may change it, else the refusal. Re-checked in the bot loop on every command."""
    import alerts
    if not alerts.may(req.get("user"), req.get("channel"), "manage"):
        return "⛔ NOT changed — this user may not change trade management."
    return None


def status_line(_symbol=None) -> str:
    _ensure()
    return f"Trade management: {'ON' if _s['enabled'] else 'OFF'}"


def status_text() -> str:
    _ensure()
    on = _s["enabled"]
    lines = [f"**Trade management: {'🟢 ON' if on else '⚪ OFF'}**",
             ("The bot may send its management orders (trailing-stop steps, Apply)." if on else
              "No automatic stop moves and no Apply. Positions are still tracked and reported."),
             f"Trailing stop mode: {_tsl_mode()} · manual orders (/place, Close): "
             f"{'on' if C.PLACE_ENABLED else 'off'} (PLACE_ENABLED, separate)",
             "This switch never opens or closes a position."]
    if _s["changed_at"]:
        lines.append(f"Last change {_s['changed_at']} by {_s['changed_by'] or 'restart policy'}")
    if _s["problem"]:
        lines.append(f"⚠️ state problem at startup: {_s['problem']}")
    import guardian
    g = guardian.current()
    if g is not None:
        snap = g.snapshot()
        lines.append(f"**MT5 connection: {snap['state']}**" + (f" ({snap['cause']})" if snap["cause"] != "ok" else "")
                     + f" · account {snap['account']} · orders {snap['orders']}")
    return "\n".join(lines)


def _tsl_mode() -> str:
    try:
        import positions
        return positions.tsl_mode()
    except Exception:
        return "?"


def buttons() -> list:
    from notifier import button
    return [button("Enable trade management", "tm", "enable", style="success"),
            button("Disable trade management", "tm", "disable", style="danger")]


def controls_card() -> dict:
    """The persistent controls card (edited in place, buttons keep working after a restart)."""
    import notifier
    return notifier.info("⚙️ CONTROLS", status_text(), channel="updates", kind="CONTROLS", upsert="controls",
                         buttons=buttons(), color="BULL" if enabled() else "INFO")


def change_card(prev: bool, new: bool, user) -> dict:
    import notifier
    return notifier.info(f"⚙️ TRADE MANAGEMENT {'ON' if new else 'OFF'}",
                         ("The bot may now send its trailing-stop orders." if new else
                          "No automatic stop moves. Pending suggestions are void. Positions are still tracked."),
                         channel="alerts", kind="TRADE_MANAGEMENT", color="WARN" if new else "INFO",
                         fields=[("Previous", "ON" if prev else "OFF"), ("Now", "ON" if new else "OFF"),
                                 ("By", f"<@{user}>" if user else "restart policy"),
                                 ("At", _s["changed_at"] or "-")])


def handle(req: dict) -> tuple[dict, tuple | None]:
    """/trade_management in the bot loop. Returns (reply, change) where change = (previous, new, user) or None.
    actions: status | disable | enable_request | enable_confirm (with the nonce). Anything else is refused."""
    _ensure()
    action = str(req.get("action") or "status").lower()
    user = req.get("user")
    if action == "status":
        return {"ok": True, "text": status_text(), "buttons": buttons()}, None
    if action not in ("disable", "enable_request", "enable_confirm"):
        return {"ok": False, "text": "Usage: /trade_management status | enable | disable"}, None
    why = authorized(req)
    if why:
        log.warning("trade management %s refused for user %r", action, user)
        return {"ok": False, "text": why}, None
    if action == "disable":
        if not _s["enabled"]:
            _nonces.clear()
            return {"ok": True, "text": "Trade management is already OFF."}, None
        prev, err = disable(user)
        return {"ok": err is None, "text": "⚪ Trade management is OFF. No automatic stop moves; pending suggestions "
                                           "are void." + (f"\n⚠️ {err}" if err else "")}, (prev, False, user)
    if action == "enable_request":
        if _s["enabled"]:
            return {"ok": True, "text": "Trade management is already ON."}, None
        if C.TRADE_MANAGEMENT_MALFORMED:
            return {"ok": False, "text": "⛔ TRADE_MANAGEMENT_ENABLED in .env is malformed: fix it first."}, None
        n = secrets.token_hex(12)
        _nonces.clear()
        _nonces[n] = (user, time.monotonic() + C.TRADE_MANAGEMENT_CONFIRM_SECONDS)
        return {"ok": True, "nonce": n,
                "text": f"**Enable trade management?** The bot will move stops by itself (TSL mode: {_tsl_mode()}). "
                        f"It never opens or closes positions. Confirm within {C.TRADE_MANAGEMENT_CONFIRM_SECONDS} s."}, None
    nonce = str(req.get("nonce") or "")
    held = _nonces.pop(nonce, None)                         # one use
    if held is None or held[0] != user or time.monotonic() > held[1]:
        return {"ok": False, "text": "⛔ NOT enabled — the confirmation is missing, expired or not yours."}, None
    if _s["enabled"]:
        return {"ok": True, "text": "Trade management is already ON."}, None
    prev, err = _set(True, user, "confirmed command")
    if err:
        return {"ok": False, "text": f"❌ NOT enabled — {err}."}, None
    return {"ok": True, "text": "🟢 Trade management is ON."}, (prev, True, user)


def reset(enabled_: bool = False):
    """Tests: forget the in-memory value (the file is left as it is)."""
    with _lock:
        _s.update(enabled=enabled_, generation=_s["generation"] + 1, loaded=True, problem=None, history=[],
                  changed_at=None, changed_by=None)
        _nonces.clear()
