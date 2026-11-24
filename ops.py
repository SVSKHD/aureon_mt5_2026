"""The unified operational event: one schema for everything that happens, LIVE or REPLAY.

    event_id, ts (UTC ISO), source, mode (LIVE | REPLAY), symbol, strategy, version, type, severity,
    correlation_id, connection_state, replay_session (REPLAY only), detail

Stores are SEPARATE by mode and never shared:
    LIVE    logs/ops/live-YYYY-MM.jsonl                  (the bot: Guardian, notifications, trade management)
    REPLAY  <the replay session folder>/ops.jsonl        (the Replay Lab, one file per session)
A store never raises and never blocks on the network (local append only); severity follows notify_events.
"""
from __future__ import annotations
import json, logging, os, threading, uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import config as C

log = logging.getLogger("ops")
MODES = ("LIVE", "REPLAY")
_lock = threading.Lock()


@dataclass
class OpsEvent:
    source: str
    type: str
    mode: str = "LIVE"
    severity: str = "normal"
    symbol: str | None = None
    strategy: str | None = None
    version: str | None = None
    correlation_id: str | None = None
    connection_state: str | None = None
    replay_session: str | None = None
    detail: dict = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"mode must be LIVE or REPLAY, not {self.mode!r}")
        if self.mode == "REPLAY" and not self.replay_session:
            raise ValueError("a REPLAY event needs its replay_session")
        if self.mode == "LIVE" and self.replay_session:
            raise ValueError("a LIVE event cannot carry a replay_session")

    def to_dict(self) -> dict:
        return asdict(self)


class Store:
    """Append-only JSONL. LIVE: monthly files in logs/ops/. REPLAY: one file in the session folder."""

    def __init__(self, mode: str, folder: str | None = None):
        if mode not in MODES:
            raise ValueError(mode)
        if mode == "REPLAY" and not folder:
            raise ValueError("a REPLAY store needs its session folder")
        self.mode = mode
        self.folder = folder or C.OPS_DIR

    def path(self, ev: OpsEvent) -> str:
        if self.mode == "LIVE":
            return os.path.join(self.folder, f"live-{ev.ts[:7]}.jsonl")
        return os.path.join(self.folder, "ops.jsonl")

    def write(self, ev: OpsEvent) -> bool:
        if ev.mode != self.mode:                                   # the two stores never mix
            raise ValueError(f"{ev.mode} event sent to the {self.mode} store")
        try:
            os.makedirs(self.folder, exist_ok=True)
            line = json.dumps(ev.to_dict(), default=str, ensure_ascii=False)
            with _lock, open(self.path(ev), "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return True
        except Exception:
            log.exception("ops event write failed")
            return False

    def read(self, limit: int = 200) -> list[dict]:
        import glob
        files = sorted(glob.glob(os.path.join(self.folder, "live-*.jsonl" if self.mode == "LIVE" else "ops.jsonl")))
        out: list[dict] = []
        for p in reversed(files):
            with open(p, encoding="utf-8") as f:
                rows = [json.loads(x) for x in f if x.strip()]
            out = rows + out
            if len(out) >= limit:
                break
        return out[-limit:]


_live: Store | None = None


def live() -> Store:
    global _live
    if _live is None or _live.folder != C.OPS_DIR:
        _live = Store("LIVE")
    return _live


def emit_live(source: str, type_: str, severity: str = "normal", **kw) -> OpsEvent:
    ev = OpsEvent(source=source, type=type_, mode="LIVE", severity=severity, **kw)
    live().write(ev)
    return ev
