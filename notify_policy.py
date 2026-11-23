"""The notification policy engine: given one event, the settings and what was sent recently, decide what happens.

Pure and deterministic (no files, no network, no MT5, no LLM): decide() only reads its inputs, remember() is the
only function that changes the in-memory history, and the caller (notify.py) persists it.

Actions:
  send       post now (to `services`; quiet=True strips the phone buzz)
  group      hold for a short window, then one card per symbol (normal events, rate-limited high events)
  digest     hold for the low-priority digest
  feed       show on the symbol's live Discord feed card (monitor mode)
  duplicate  identical non-critical card already sent inside the duplicate window: not sent again

Safety rules (tested):
  * critical: always `send`, at once, to every enabled configured service; never quiet, grouped, rate-limited or
    suppressed as a duplicate (an unrelated or identical earlier card can never hold a new critical one back).
  * a card with buttons is never held (its buttons would be lost) and an edited-in-place card is passed through.
  * nothing is ever dropped except an exact non-critical duplicate inside its window; below-threshold events are
    digested, not discarded.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import notify_events as E

SERVICES = ("Discord", "Slack", "Ntfy")


@dataclass
class Settings:
    mode: str = "standard"
    quiet: tuple[int, int] | None = None              # (start, end) minutes of the day in the display timezone
    digest_s: float = 1800.0
    group_s: float = 60.0
    rate_max: int = 8
    rate_window_s: float = 600.0
    dup_window_s: float = 600.0
    min_severity: dict = field(default_factory=lambda: {"Discord": "low", "Slack": "high", "Ntfy": "high"})
    disabled: set = field(default_factory=set)
    symbol_min: dict = field(default_factory=dict)    # symbol -> lowest severity sent at once (below = digest)


@dataclass
class Decision:
    action: str
    services: list | None = None
    quiet: bool = False
    flush_at: float | None = None
    reason: str = ""


def parse_quiet(text: str) -> tuple[int, int] | None:
    if not text:
        return None
    a, b = text.split("-")
    mins = lambda hhmm: int(hhmm[:2]) * 60 + int(hhmm[3:5])
    return mins(a), mins(b)


def parse_symbol_prefs(text: str) -> dict:
    out = {}
    for part in (x.strip() for x in (text or "").replace(";", ",").split(",") if x.strip()):
        sym, _, rest = part.partition(":")
        if rest.startswith("min=") and rest[4:].lower() in E.SEVERITIES:
            out[sym.upper()] = rest[4:].lower()
    return out


def in_quiet(s: Settings, local: datetime) -> bool:
    if not s.quiet:
        return False
    start, end = s.quiet
    m = local.hour * 60 + local.minute
    return start <= m < end if start < end else (m >= start or m < end)


def quiet_end(s: Settings, local: datetime) -> datetime:
    """The next end of the quiet window after `local` (local = aware datetime in the display timezone)."""
    end = local.replace(hour=s.quiet[1] // 60, minute=s.quiet[1] % 60, second=0, microsecond=0)
    return end if end > local else end + timedelta(days=1)


def services_for(ev: E.NotificationEvent, s: Settings, configured: list[str], severity: str | None = None) -> list[str]:
    """Which configured, enabled services carry an event of this severity. Slack and ntfy only carry the #alerts set
    (as in standard mode) unless the event is critical; a critical event goes to every enabled service and, if the
    owner disabled them all, still to every configured one (a critical event is never routed nowhere)."""
    sev = severity or ev.severity
    on = [n for n in SERVICES if n in configured and n not in s.disabled]
    if sev == "critical":
        return on or [n for n in SERVICES if n in configured]
    out = []
    for n in on:
        if n != "Discord" and ev.channel != "alerts":
            continue
        if E.RANK[sev] >= E.RANK.get(s.min_severity.get(n, "low"), 0):
            out.append(n)
    return out


def _recent_sends(memory: dict, symbol, now: float, window: float) -> int:
    return sum(1 for t in memory.get("rate", {}).get(str(symbol), []) if now - t < window)


def decide(ev: E.NotificationEvent, s: Settings, memory: dict, now: float, configured: list[str],
           local: datetime, bot_client: bool) -> Decision:
    """local = `now` in the display timezone (quiet hours). memory = {"recent": {fp: ts}, "rate": {sym: [ts]}}."""
    if s.mode == "standard":
        return Decision("send", None, reason="standard mode")
    if ev.upsert or ev.kind in E.PASSTHROUGH:
        return Decision("send", None, reason="passthrough")
    if ev.severity == "critical":
        return Decision("send", services_for(ev, s, configured), reason="critical: always at once")
    last = memory.get("recent", {}).get(ev.fingerprint)
    if last is not None and now - last < s.dup_window_s:
        return Decision("duplicate", reason=f"identical card {now - last:.0f}s ago")
    actionable = ev.buttons
    quiet = in_quiet(s, local)
    sym_min = s.symbol_min.get(str(ev.symbol).upper()) if ev.symbol else None
    later = lambda delay: max(now + delay, quiet_end(s, local).timestamp()) if quiet else now + delay
    if sym_min and E.RANK[ev.severity] < E.RANK[sym_min] and not actionable:
        return Decision("digest", flush_at=later(s.digest_s), reason=f"below {ev.symbol} minimum {sym_min}")
    if ev.severity == "high":
        if not actionable and _recent_sends(memory, ev.symbol, now, s.rate_window_s) >= s.rate_max:
            return Decision("group", flush_at=later(s.group_s), reason="rate limit: grouped")
        return Decision("send", services_for(ev, s, configured), quiet=quiet,
                        reason="high" + (" (quiet hours: no buzz)" if quiet else ""))
    if actionable:                                                # (only reachable for a normal/low card with buttons)
        return Decision("send", services_for(ev, s, configured), quiet=quiet, reason="actionable")
    if s.mode == "monitor" and bot_client and ev.symbol:
        return Decision("feed", reason="monitor mode: live feed card")
    if ev.severity == "normal":
        return Decision("group", flush_at=later(s.group_s), reason="normal: grouped per symbol")
    return Decision("digest", flush_at=later(s.digest_s), reason="low: digest")


def remember(ev: E.NotificationEvent, d: Decision, memory: dict, now: float) -> None:
    """Record what was sent (for duplicate suppression and rate limits). Critical sends do not count against the
    rate limit, and nothing here can ever hold back a critical event."""
    if d.action not in ("send", "group", "digest", "feed") or ev.upsert or ev.kind in E.PASSTHROUGH:
        return
    memory.setdefault("recent", {})[ev.fingerprint] = now        # a held card counts too: no second copy is held
    if d.action == "send" and ev.severity != "critical":
        memory.setdefault("rate", {}).setdefault(str(ev.symbol), []).append(now)


def prune(memory: dict, s: Settings, now: float) -> None:
    memory["recent"] = {k: t for k, t in memory.get("recent", {}).items() if now - t < s.dup_window_s}
    memory["rate"] = {k: [t for t in v if now - t < s.rate_window_s] for k, v in memory.get("rate", {}).items()}
    memory["rate"] = {k: v for k, v in memory["rate"].items() if v}
