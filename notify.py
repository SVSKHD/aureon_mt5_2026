"""Notification intelligence (1.7.3): modes, routing, grouping, digests, the monitor feed, audit and /notify.

    standard     exactly the delivery of earlier versions (the default; notifier.deliver bypasses everything here
                 except the audit line and secret redaction)
    intelligent  severity (notify_events), policy (notify_policy): critical at once everywhere, high at once,
                 normal grouped per symbol, low in a digest, rate limits, duplicate suppression, quiet hours,
                 per-service and per-symbol thresholds, context lines on the cards that do go out
    monitor      as intelligent, but normal/low events of a symbol update ONE live Discord card per symbol
                 ("feed", edited in place) instead of group/digest messages; critical/high still ping at once

Separation: this module never calls MT5 and never sends an order. It only decides about messages that already
exist. Its state (logs/state/notify.json) is written by the bot loop only. Network posts of group / digest /
test cards run on the "notify-dispatch" thread, never on the loop; results come back through a queue that the
loop drains in tick() (so the loop stays the only writer of every state file). Immediate cards use the same
synchronous parallel path as standard mode (no extra delay for an alert).

Idempotency: held items carry the event's dedupe key (or a content id) and are stored once; a group card is moved
to "flushing" on disk BEFORE it is posted and removed only after the post or its retry-queue entry, so a crash
re-posts it rather than loses it (at least once, like the rest of the bot).
"""
from __future__ import annotations
import json, logging, os, queue, re, threading, time, uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import config as C
import notifier, store
import notify_events as E
import notify_policy as P

log = logging.getLogger("notify")
DEFAULT = {"mode": None, "mode_by": None, "mode_at": None, "buffer": [], "flushing": {}, "feed": {},
           "memory": {"recent": {}, "rate": {}}, "history": [], "last_test": {}}
FEED_MIN_EDIT_SECONDS = 15
FEED_LINES = 12
HISTORY_KEEP = 200
CONTEXT_WINDOW_S = 3600
_ns: dict | None = None
_problem: str | None = None


# ------------------------------------------------------------------------------------------------ state
def _valid(d: dict) -> bool:
    types = {"buffer": list, "flushing": dict, "feed": dict, "memory": dict, "history": list, "last_test": dict}
    return isinstance(d, dict) and all(isinstance(d.get(k), t) for k, t in types.items()) and \
        (d.get("mode") is None or d.get("mode") in C.NOTIFY_MODES)


def ns() -> dict:
    """The persisted notification state. A corrupt file falls back to .bak, then to defaults (= standard mode)."""
    global _ns, _problem
    if _ns is None:
        d, _problem = notifier.load_json(C.NOTIFY_STATE_FILE, DEFAULT)
        if not _valid(d):
            _problem = (_problem + "; " if _problem else "") + "notify.json had an unexpected shape: reset"
            d = json.loads(json.dumps(DEFAULT))
        if _problem:
            log.error("notification state: %s", _problem)
        _ns = d
    return _ns


def save():
    try:
        notifier.save_json(C.NOTIFY_STATE_FILE, ns())
    except OSError:
        log.exception("notify.json write failed")


def reset():
    """Forget the in-memory state (tests; a restart does this)."""
    global _ns, _problem
    _ns, _problem = None, None
    _inflight.clear()
    while not _results.empty():
        _results.get_nowait()


def mode() -> str:
    m = ns().get("mode")
    if m in C.NOTIFY_MODES:
        return m
    return C.NOTIFY_MODE if C.NOTIFY_MODE in C.NOTIFY_MODES else "standard"


def settings() -> P.Settings:
    num = lambda v, d: float(v) if str(v).replace(".", "", 1).isdigit() and float(v) > 0 else d
    return P.Settings(mode=mode(), quiet=P.parse_quiet(C.NOTIFY_QUIET_HOURS) if _quiet_ok() else None,
                      digest_s=num(C.NOTIFY_DIGEST_MINUTES, 30) * 60, group_s=num(C.NOTIFY_GROUP_SECONDS, 60),
                      rate_max=int(num(C.NOTIFY_RATE_MAX, 8)), rate_window_s=num(C.NOTIFY_RATE_WINDOW_SECONDS, 600),
                      dup_window_s=num(C.NOTIFY_DUPLICATE_SECONDS, 600),
                      min_severity={k: v if v in E.SEVERITIES else "low" for k, v in C.NOTIFY_MIN_SEVERITY.items()},
                      disabled=set(C.NOTIFY_DISABLED_SERVICES), symbol_min=P.parse_symbol_prefs(C.NOTIFY_SYMBOL_PREFS))


def _quiet_ok() -> bool:
    return bool(re.fullmatch(r"\d\d:\d\d-\d\d:\d\d", C.NOTIFY_QUIET_HOURS or ""))


def _now() -> float:
    return time.time()


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(ZoneInfo(C.TIMEZONE))


# ------------------------------------------------------------------------------------------- redaction
def _secrets() -> list[str]:
    vals = [C.DISCORD_BOT_TOKEN, C.DISCORD_WEBHOOK, C.SLACK_WEBHOOK, C.ASSEMBLER_RECORDS_TOKEN, C.NTFY_TOPIC,
            *C.DISCORD_WEBHOOKS.values()]
    return sorted({v for v in vals if v and len(v) >= 6}, key=len, reverse=True)


def _scrub(text):
    if not isinstance(text, str):
        return text
    for sec in _secrets():
        if sec in text:
            text = text.replace(sec, "[redacted]")
    return text


def redact(msg):
    """Never let a token, webhook URL or the ntfy topic reach a notification (every mode)."""
    if isinstance(msg, str):
        return _scrub(msg)
    if not isinstance(msg, dict):
        return msg
    sec = _secrets()
    if not sec or not any(s in json.dumps(msg, default=str) for s in sec):
        return msg                                                # nothing to do: the card is passed through as is
    out = dict(msg)
    for k in ("title", "desc", "footer"):
        out[k] = _scrub(out.get(k))
    out["fields"] = [tuple([_scrub(f[0]), _scrub(str(f[1]))] + list(f[2:])) for f in msg.get("fields", [])]
    return out


# ---------------------------------------------------------------------------------------------- audit
_audit_lock = threading.Lock()


def audit(row: dict) -> None:
    """One JSON line per notification decision / delivery in logs/notify/audit-YYYY-MM.jsonl. Never raises.
    NOTIFY_ASSEMBLER_EXPORT=1 also writes it as a "notification" record (uploaded with RECORD_SINK=api)."""
    try:
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **row}
        os.makedirs(C.NOTIFY_AUDIT_DIR, exist_ok=True)
        line = json.dumps(row, default=str, ensure_ascii=False)
        with _audit_lock, open(os.path.join(C.NOTIFY_AUDIT_DIR, f"audit-{row['ts'][:7]}.jsonl"), "a",
                               encoding="utf-8") as f:
            f.write(line + "\n")
        if C.NOTIFY_ASSEMBLER_EXPORT:
            store.record("notification", {k: v for k, v in row.items() if k != "ts"}, symbol=row.get("symbol"))
    except Exception:
        log.exception("notification audit write failed")


def _status_row(status: dict) -> dict:
    return {"delivered": [n for n, s in status.items() if s == "ok"], "failed": notifier.failed_services(status)}


def audit_standard(msg, key, status: dict):
    ev = E.from_card(msg, key)
    audit({"event_id": ev.event_id, "kind": ev.kind, "symbol": ev.symbol, "severity": ev.severity,
           "mode": "standard", "action": "send", "reason": "standard mode", **_status_row(status)})


# ---------------------------------------------------------------------------------------- routing
def route(state, msg, key=None, timing=None) -> bool | None:
    """notifier.deliver's policy step. None = standard delivery (standard mode). Otherwise the message has been
    sent, queued for retry, or durably held; returns True when it was delivered or held."""
    m = mode()
    if m == "standard":
        return None
    s, now = settings(), _now()
    ev = E.from_card(msg, key, now)
    mem = ns()["memory"]
    d = P.decide(ev, s, mem, now, notifier.configured(), _local(now), bool(notifier.BOT_CLIENT["on"]))
    row = {"event_id": ev.event_id, "kind": ev.kind, "symbol": ev.symbol, "severity": ev.severity, "mode": m,
           "action": d.action, "reason": d.reason}
    if d.action == "send":
        if d.services is None:                                    # passthrough (edited-in-place cards, charts)
            status = notifier.send(msg, key=key, timing=timing)
            out, riders = msg, []
        else:
            out, riders = _enrich(msg, ev, d, now)
            status = notifier.send(out, key=key, timing=timing, route=d.services)
        owed = notifier.failed_services(status)
        if owed:
            state.queue(out, owed)                                # same retry queue as standard mode
        if riders:                                                # they rode along on this card: delivered or queued
            ids = {r["id"] for r in riders}
            ns()["buffer"] = [b for b in ns()["buffer"] if b["id"] not in ids]
        P.remember(ev, d, mem, now)
        _history(ev, now)
        save()
        audit({**row, "services": d.services, "quiet": d.quiet, "riders": len(riders), **_status_row(status)})
        return any(v == "ok" for v in status.values()) or (not status and not d.services)
    if d.action == "duplicate":
        audit(row)
        return True
    if d.action == "feed":
        P.remember(ev, d, mem, now)
        _feed_add(ev, msg, now)
        _history(ev, now)
        save()
        audit(row)
        _feed_push(ev.symbol, now)
        return True
    _hold(ev, msg, d, now)                                        # group / digest
    P.remember(ev, d, mem, now)
    _history(ev, now)
    save()
    audit({**row, "flush_at": d.flush_at})
    return True


def _history(ev, now):
    h = ns()["history"]
    h.append({"ts": now, "symbol": ev.symbol, "title": ev.title, "severity": ev.severity, "fp": ev.fingerprint})
    del h[:-HISTORY_KEEP]


def _line(item: dict) -> str:
    sev = {"critical": "🚨", "high": "❗", "normal": "•", "low": "·"}.get(item["severity"], "•")
    return f"`{_local(item['ts']):%H:%M}` {sev} {item['title']}"


def _hold(ev, msg, d, now):
    buf = ns()["buffer"]
    if any(b["id"] == ev.event_id for b in buf):                  # already held (replay after a restart)
        return
    buf.append({"id": ev.event_id, "ts": now, "symbol": ev.symbol, "kind": ev.kind, "severity": ev.severity,
                "channel": ev.channel, "title": ev.title, "flush_at": d.flush_at, "action": d.action})


def _enrich(msg, ev, d, now):
    """Context-rich copy of a card that goes out now: recent events of the same symbol, held items of that symbol
    riding along (so they are not repeated later), the severity, and no buzz in quiet hours."""
    if not isinstance(msg, dict):
        return msg, []
    out = {**msg, "fields": list(msg.get("fields", []))}
    if d.quiet:
        out["ping"] = False
    riders = [b for b in ns()["buffer"] if ev.symbol and b["symbol"] == ev.symbol][:5]
    if riders:
        out["fields"].append(("Also since the last card", "\n".join(_line(b) for b in riders), False))
    recent = [h for h in ns()["history"] if h["symbol"] == ev.symbol and ev.symbol and now - h["ts"] < CONTEXT_WINDOW_S
              and h["fp"] != ev.fingerprint and h["severity"] in ("critical", "high")][-3:]
    if recent:
        out["fields"].append(("Earlier this hour", "\n".join(_line(h) for h in recent), False))
    tag = ev.severity.upper() + (" · 🌙 quiet hours" if d.quiet else "")
    out["footer"] = f"{msg.get('footer', '')} · {tag}".strip(" ·")
    return out, riders


# ------------------------------------------------------------------------------------------ monitor feed
def _feed_add(ev, msg, now):
    f = ns()["feed"].setdefault(ev.symbol, {"lines": [], "dirty": False, "last_edit": 0.0})
    f["lines"] = (f["lines"] + [_line({"ts": now, "severity": ev.severity, "title": ev.title})])[-FEED_LINES:]
    f["dirty"] = True


def feed_card(symbol: str) -> dict:
    f = ns()["feed"].get(symbol, {"lines": []})
    return notifier.info("📡 FEED", "\n".join(reversed(f["lines"])) or "nothing yet", symbol=symbol,
                         channel="updates", kind="NOTIFY_FEED", upsert=f"feed:{symbol}",
                         fields=[("Mode", "monitor · critical and high events still arrive as their own cards", False)])


def _feed_push(symbol, now) -> bool:
    f = ns()["feed"].get(symbol)
    if not f or not f["dirty"] or now - f["last_edit"] < FEED_MIN_EDIT_SECONDS:
        return False
    notifier.send(feed_card(symbol))                              # bot client: an outbox file, no network here
    f["dirty"], f["last_edit"] = False, now
    save()
    return True


# ------------------------------------------------------------------------------------------- dispatcher
_jobs: queue.Queue = queue.Queue()
_results: queue.Queue = queue.Queue()
_inflight: set = set()
_thread = None


def _worker():
    while True:
        job = _jobs.get()
        try:
            status = notifier.send(job["msg"], route=job["route"], only=job.get("only"), attempts=2)
        except Exception as e:                                    # never kill the thread
            log.exception("notify dispatch failed")
            status = {n: "failed" for n in (job["route"] or notifier.configured())} or {"?": f"failed: {type(e).__name__}"}
        _results.put((job, status))
        _jobs.task_done()


def dispatch(job: dict):
    """Post a card off the loop. The result is applied by tick() on the loop."""
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_worker, daemon=True, name="notify-dispatch")
        _thread.start()
    _inflight.add(job["id"])
    _jobs.put(job)


def send_async(msg, by: str = "async") -> str:
    """Post a card from the dispatch thread (the caller never waits on the network). Failures land in the standard
    retry queue when tick() applies the result. Used for Guardian cards (never block MT5 polling)."""
    jid = f"{by}:{uuid.uuid4().hex[:10]}"
    dispatch({"id": jid, "purpose": "async", "msg": redact(msg), "route": None})
    return jid


def wait_idle(timeout: float = 10.0) -> bool:
    """Wait until every dispatched post has an answer (tests, shutdown)."""
    end = time.monotonic() + timeout
    while _jobs.unfinished_tasks and time.monotonic() < end:
        time.sleep(0.01)
    return not _jobs.unfinished_tasks


def _apply_results(state):
    while True:
        try:
            job, status = _results.get_nowait()
        except queue.Empty:
            return
        _inflight.discard(job["id"])
        if job["purpose"] == "test":
            ns()["last_test"] = {"at": _now(), "by": job.get("by"), "results": status}
            save()
            audit({"event_id": job["id"], "kind": "NOTIFY_TEST", "action": "test", "mode": mode(), **_status_row(status)})
            continue
        owed = notifier.failed_services(status)
        if owed:
            state.queue(job["msg"], owed)                         # the standard retry queue takes it from here
        ns()["flushing"].pop(job["id"], None)
        save()
        audit({"event_id": job["id"], "kind": job["msg"].get("kind", "NOTIFY_DIGEST") if isinstance(job["msg"], dict) else "TEXT",
               "symbol": job["msg"].get("symbol") if isinstance(job["msg"], dict) else None,
               "action": "flushed" if job["purpose"] == "digest" else "sent_async",
               "mode": mode(), "items": job.get("items"), "services": job["route"], **_status_row(status)})


# ------------------------------------------------------------------------------------------------ tick
def _group_card(symbol, items: list[dict], s: P.Settings) -> tuple[dict, list[str]]:
    items = sorted(items, key=lambda b: b["ts"])
    shown = items[-C.DIGEST_MAX_LINES:]
    more = len(items) - len(shown)
    worst = max((b["severity"] for b in items), key=lambda x: E.RANK[x])
    digest = all(b["action"] == "digest" for b in items)
    chan = "alerts" if any(b["channel"] == "alerts" for b in items) else "updates"
    title = f"{'🗂️ DIGEST' if digest else '📋 UPDATES'} · {len(items)} event{'s' if len(items) != 1 else ''}"
    desc = (f"… {more} earlier in logs/notify/\n" if more else "") + "\n".join(_line(b) for b in shown)
    span = f"{_local(items[0]['ts']):%d %b %H:%M} → {_local(items[-1]['ts']):%H:%M} IST"
    card = notifier.info(title, desc, symbol=symbol, channel=chan, kind="NOTIFY_DIGEST",
                         fields=[("Window", span, False)], color="INFO")
    card["footer"] = f"{worst.upper()} · {mode()} mode"
    probe = E.NotificationEvent("", "NOTIFY_DIGEST", symbol, chan, worst, title, "")
    return card, P.services_for(probe, s, notifier.configured())


def tick(state) -> None:
    """Once per loop pass: apply dispatch results, post due group/digest cards (off the loop), push dirty feed
    cards, re-post anything a crash left in "flushing", and forget old duplicate/rate history. No network here."""
    _apply_results(state)
    d = ns()
    s, now = settings(), _now()
    for jid, job in list(d["flushing"].items()):                  # left over from a crash / restart
        if jid not in _inflight:
            dispatch({"id": jid, "purpose": "digest", "msg": job["msg"], "route": job["route"], "items": job["items"]})
    by_sym: dict = {}
    for b in d["buffer"]:
        by_sym.setdefault(b["symbol"], []).append(b)
    changed = False
    for sym, items in by_sym.items():
        due = s.mode == "standard" or min(b["flush_at"] or 0 for b in items) <= now
        if not due:
            continue
        card, svcs = _group_card(sym, items, s)
        jid = f"digest:{sym}:{uuid.uuid4().hex[:10]}"
        d["flushing"][jid] = {"msg": card, "route": svcs, "items": [b["id"] for b in items]}
        ids = {b["id"] for b in items}
        d["buffer"] = [b for b in d["buffer"] if b["id"] not in ids]
        save()                                                    # on disk BEFORE it is posted
        dispatch({"id": jid, "purpose": "digest", "msg": card, "route": svcs, "items": [b["id"] for b in items]})
        changed = True
    for sym in list(d["feed"]):
        if s.mode != "monitor" or not notifier.BOT_CLIENT["on"]:
            continue
        _feed_push(sym, now)
    P.prune(d["memory"], s, now)
    if changed:
        save()


# ------------------------------------------------------------------------------------------- commands
def _may_change(req: dict) -> str | None:
    import alerts
    if not alerts.may(req.get("user"), req.get("channel"), "trade"):
        return "⛔ NOT changed — this user is not explicitly listed in DISCORD_ALLOWED_USER_IDS."
    return None


def set_mode(state, req: dict) -> dict:
    """/notify_mode mode: standard | intelligent | monitor. Persisted. Authorised users only (re-checked here)."""
    why = _may_change(req)
    if why:
        return {"ok": False, "text": why}
    new = str(req.get("mode") or "").lower()
    if new not in C.NOTIFY_MODES:
        return {"ok": False, "text": f"Unknown mode {new!r}. Use one of: {', '.join(C.NOTIFY_MODES)}."}
    old = mode()
    d = ns()
    d["mode"], d["mode_by"], d["mode_at"] = new, req.get("user"), _now()
    save()
    log.warning("NOTIFY MODE %s -> %s by %s", old, new, req.get("user"))
    audit({"event_id": f"mode:{int(_now())}", "kind": "NOTIFY_MODE", "action": "mode", "previous": old, "new": new,
           "user": req.get("user")})
    store.record("system", {"event": "notify_mode", "previous": old, "new": new, "user": req.get("user")})
    extra = " Held cards are sent at the next pass." if new == "standard" and d["buffer"] else ""
    return {"ok": True, "text": f"🔔 Notification mode: **{old}** → **{new}**.{extra}"}


def _svc_line(s: P.Settings) -> str:
    out = []
    conf = notifier.configured()
    for n in P.SERVICES:
        if n not in conf:
            out.append(f"{n}: not configured")
        elif n in s.disabled:
            out.append(f"{n}: ⛔ disabled")
        else:
            out.append(f"{n}: ✅ on (from {s.min_severity.get(n, 'low')})")
    return " · ".join(out)


def status_text(state) -> str:
    s, d = settings(), ns()
    m = mode()
    src = (f"set by <@{d['mode_by']}> {_local(d['mode_at']):%d %b %H:%M} IST" if d.get("mode") and d.get("mode_at")
           else f".env default ({C.NOTIFY_MODE if C.NOTIFY_MODE in C.NOTIFY_MODES else 'standard'})")
    lines = [f"**Notification mode: {m}** ({src})",
             "Services: " + _svc_line(s) + ("" if m != "standard" else "  _(thresholds apply in intelligent/monitor)_")]
    if s.quiet:
        lines.append(f"Quiet hours: {C.NOTIFY_QUIET_HOURS} IST ({'now quiet' if P.in_quiet(s, _local(_now())) else 'not now'})")
    held = d["buffer"]
    if held:
        nxt = min(b["flush_at"] or 0 for b in held)
        lines.append(f"Held: {sum(b['action'] == 'group' for b in held)} grouped · "
                     f"{sum(b['action'] == 'digest' for b in held)} for the digest · next card {_local(nxt):%H:%M} IST")
    if d["flushing"]:
        lines.append(f"Group cards being posted: {len(d['flushing'])}")
    lines.append(f"Retry queue: {len(state.d.get('pending', []))} message(s)")
    t = d.get("last_test") or {}
    if t.get("results"):
        lines.append(f"Last test {_local(t['at']):%d %b %H:%M} IST: " +
                     " · ".join(f"{n} {v}" for n, v in t["results"].items()))
    if _problem:
        lines.append(f"⚠️ {_problem}")
    import trade_mgmt
    lines.append(trade_mgmt.status_line())
    return "\n".join(lines)


def prefs_text() -> str:
    s = settings()
    by = {}
    for k, v in E.SEVERITY.items():
        by.setdefault(v, []).append(k)
    lines = [f"**Notification preferences** (mode {s.mode})",
             f"Group window {s.group_s:.0f} s · digest every {s.digest_s / 60:.0f} min · "
             f"at most {s.rate_max} immediate cards per symbol per {s.rate_window_s / 60:.0f} min · "
             f"identical cards suppressed for {s.dup_window_s / 60:.0f} min (never critical)",
             f"Quiet hours: {C.NOTIFY_QUIET_HOURS or 'off'} (critical always sent; high sent without the buzz)",
             "Services: " + _svc_line(s),
             "Per symbol: " + (", ".join(f"{k} from {v}" for k, v in s.symbol_min.items()) or "no overrides")]
    for sev in reversed(E.SEVERITIES):
        lines.append(f"**{sev}**: " + ", ".join(sorted(by.get(sev, []))))
    lines.append("Not listed: high. A card with buttons is at least high; a card that pings is at least high.")
    return "\n".join(lines)


def test_channels(state, req: dict) -> dict:
    """Post a test card to every configured, enabled service, off the loop. No order, no MT5 call."""
    why = _may_change(req)
    if why:
        return {"ok": False, "text": why}
    s = settings()
    conf = notifier.configured()
    targets = [n for n in P.SERVICES if n in conf and n not in s.disabled]
    skipped = [n for n in P.SERVICES if n in conf and n in s.disabled]
    if not targets:
        return {"ok": False, "text": "No enabled service to test." + (f" Disabled: {', '.join(skipped)}." if skipped else "")}
    card = notifier.info("🧪 notification test", "Delivery check only. No trade was placed and nothing was changed.",
                         kind="NOTIFY_TEST", fields=[("Mode", mode())])
    dispatch({"id": f"test:{uuid.uuid4().hex[:10]}", "purpose": "test", "msg": card, "route": targets,
              "only": targets, "by": req.get("user")})
    return {"ok": True, "text": f"🧪 Test card sent to {', '.join(targets)}"
                                + (f" (disabled, not tested: {', '.join(skipped)})" if skipped else "")
                                + ". Results in /notify status in a few seconds."}


def command(state, req: dict) -> dict:
    """/notify action: status (default) | prefs | test."""
    action = str(req.get("action") or "status").lower()
    if action == "status":
        return {"ok": True, "text": status_text(state)}
    if action == "prefs":
        return {"ok": True, "text": prefs_text()}
    if action == "test":
        return test_channels(state, req)
    return {"ok": False, "text": "Usage: /notify status | prefs | test"}


def status_line(_symbol=None) -> str:
    held = len(ns()["buffer"])
    return f"Notifications: {mode()}" + (f" · {held} held" if held else "")
