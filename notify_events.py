"""The structured notification event and its severity (notification intelligence, 1.7.3).

Pure: no MT5, no network, no files. Severity is decided ONLY by the fixed rules below (never by an LLM), so the
same card always gets the same severity and a critical card can never be classified away.

Severity, lowest first: low < normal < high < critical.
  critical  something is wrong or money/safety is at stake: always sent at once, to every enabled service,
            bypasses quiet hours, digests, grouping, rate limits and duplicate suppression.
  high      a trading signal or a position event: sent at once (in quiet hours without the phone buzz).
  normal    context labels (gap widening, pullback started, ...): grouped per symbol for a short window.
  low       housekeeping (online, alive, paused, ...): low-priority digest.
"""
from __future__ import annotations
import hashlib, json, time
from dataclasses import dataclass, field, asdict

SEVERITIES = ("low", "normal", "high", "critical")
RANK = {s: i for i, s in enumerate(SEVERITIES)}

# kind -> base severity. Anything not listed is HIGH (an unknown card is never quietly digested).
SEVERITY = {
    # critical: the bot is blind, broken, or a position is unprotected / unmanaged
    "BLIND": "critical", "SCRIPT_FAILURE": "critical", "TSL_FAILED": "critical", "POSITION_UNMANAGED": "critical",
    "SYSTEM_ROLLED_BACK": "critical", "SYSTEM_CRASHED": "critical", "WEEKEND_WARNING": "critical",
    "WEEKEND_CARRIED": "critical", "TRADE_MANAGEMENT": "critical", "WARN": "high",
    # high: signals and position events
    "CROSS": "high", "CROSS_AFTER_TREND": "high", "FAST_X_TREND": "high", "SLOW_X_TREND": "high",
    "PARALLEL_TREND": "high", "PRE-CROSS": "high", "PRE-CROSS FAILED": "high", "PRE_CROSS": "high",
    "PRE_CROSS_FAILED": "high", "GAP PEAKED": "high", "PULLBACK FAILED": "high", "MAX_POINT": "high",
    "PRICE_ALERT": "high", "POSITION_OPENED": "high", "POSITION_CLOSED": "high", "TSL_MOVED": "high",
    "TREND_STRENGTHENING_SL": "high", "TSL_SUGGEST": "high", "TSL_BLOCKED": "high",
    "POSITION_AGAINST_TREND": "high", "DIGEST": "high", "SYSTEM_UPLOAD_FAILING": "high",
    # normal: context labels
    "GAP WIDENING": "normal", "PARALLEL RUNNING": "normal", "WIDE GAP": "normal", "PULLBACK STARTED": "normal",
    "RE-ENTRY": "normal", "NARROW_ZONE_START": "normal", "NARROW_ZONE_END": "normal", "TREND_RUNNING": "normal",
    "RECOVERED": "normal", "SYSTEM_UPDATED": "normal", "SYSTEM_UPDATE_SKIPPED": "normal",
    "SYSTEM_UPLOAD_RECOVERED": "normal", "WEEKEND_FLAT": "normal",
    # low: housekeeping
    "START": "low", "ALIVE": "low", "PAUSE": "low", "INFO": "low", "NOTIFY_TEST": "low",
}
# Never folded into a digest / feed: edited-in-place cards, charts (they follow their alert), digests themselves.
PASSTHROUGH = {"MONITOR", "CHART", "NOTIFY_DIGEST", "NOTIFY_FEED", "CONTROLS"}


def at_least(a: str, b: str) -> str:
    return a if RANK[a] >= RANK[b] else b


def classify(msg) -> str:
    """Deterministic severity of a card (dict) or plain text notice (str)."""
    if not isinstance(msg, dict):
        return "high"                                             # plain-text notices: service failing etc.
    kind = msg.get("kind") or ""
    sev = SEVERITY.get(kind, "high")
    if msg.get("ping"):                                           # it buzzes the phone in standard mode: >= high
        sev = at_least(sev, "high")
        if kind == "POSITION_OPENED":                             # opened WITHOUT a stop-loss
            sev = "critical"
    if msg.get("buttons"):                                        # actionable: cannot wait in a digest
        sev = at_least(sev, "high")
    return sev


def fingerprint(msg) -> str:
    """Content hash used for duplicate suppression (title, text and fields; not the footer/time)."""
    if isinstance(msg, dict):
        body = [msg.get("symbol"), msg.get("kind"), msg.get("title"), msg.get("desc"),
                [list(f[:2]) for f in msg.get("fields", [])]]
    else:
        body = str(msg)
    return hashlib.sha1(json.dumps(body, default=str, sort_keys=True).encode()).hexdigest()[:16]


@dataclass
class NotificationEvent:
    """One notification, as the policy engine and the audit trail see it."""
    event_id: str
    kind: str
    symbol: str | None
    channel: str
    severity: str
    title: str
    fingerprint: str
    dedupe_key: str | None = None
    ts: float = field(default_factory=time.time)
    buttons: bool = False
    upsert: bool = False
    ping: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def from_card(msg, key: str | None = None, now: float | None = None) -> NotificationEvent:
    now = time.time() if now is None else now
    fp = fingerprint(msg)
    if isinstance(msg, dict):
        kind, sym, chan, title = msg.get("kind") or "INFO", msg.get("symbol"), msg.get("channel") or "alerts", msg.get("title", "")
        buttons, upsert, ping = bool(msg.get("buttons")), bool(msg.get("upsert")), bool(msg.get("ping"))
    else:
        kind, sym, chan, title, buttons, upsert, ping = "TEXT", None, "alerts", str(msg)[:120], False, False, False
    # the dedupe key IS the identity when there is one (stable across restarts); else content + time
    eid = key or f"{fp}:{int(now * 1000)}"
    return NotificationEvent(event_id=eid, kind=kind, symbol=sym, channel=chan, severity=classify(msg), title=title,
                             fingerprint=fp, dedupe_key=key, ts=now, buttons=buttons, upsert=upsert, ping=ping)
