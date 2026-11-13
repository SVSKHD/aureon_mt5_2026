"""Records for a future database (spec section 12). ONE interface: record(kind, payload).

The default sink appends one JSON line per record to logs/records/<kind>-YYYY-MM.jsonl. RECORD_SINK = "none"
turns it off; "api" writes the same JSONL files and uploader.py ships them to the Assembler API. There is no database driver, connection string or schema here, and nothing in the bot ever reads
these files back: live state stays in the state files. A later database sink only has to implement write(rows)
and can replay the JSONL files to backfill.

Guarantees: record() never raises and never blocks the main loop (rows go on an in-memory queue that a daemon
thread writes); a sink failure is logged once and otherwise ignored, so trading logic cannot be affected.
Every row carries: record_id, kind, utc_time, server_time, version, account_login, symbol. Field names are
stable snake_case so they map straight to table columns.
"""
from __future__ import annotations
import json, logging, os, queue, threading, time, uuid
from datetime import datetime, timezone
import config as C

log = logging.getLogger("store")
KINDS = ("event", "alert", "position", "sl_change", "order", "command", "weekly", "system", "bar")
context = {"account_login": None}          # set by the bot after it connects (this module never calls MT5)
_q: queue.Queue = queue.Queue(maxsize=50000)
_thread = None
_lock = threading.Lock()
_warned = set()
_inflight = [0]                             # rows queued but not yet handed to the sink


class JsonlSink:
    def __init__(self, folder: str):
        self.folder = folder

    def write(self, rows: list[dict]):
        by_file: dict[str, list[str]] = {}
        for r in rows:
            name = f"{r['kind']}-{r['utc_time'][:7]}.jsonl"
            by_file.setdefault(name, []).append(json.dumps(r, default=str, ensure_ascii=False))
        os.makedirs(self.folder, exist_ok=True)
        for name, lines in by_file.items():
            with open(os.path.join(self.folder, name), "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")


class NoSink:
    def write(self, rows):
        pass


sink = None                                 # resolved lazily from config (tests may replace it)


def _sink():
    global sink
    if sink is None:
        sink = NoSink() if C.RECORD_SINK == "none" else JsonlSink(C.RECORD_DIR)
    return sink


def _warn_once(what: str, exc: Exception):
    if what not in _warned:
        _warned.add(what)
        log.error("record sink %s failed (logged once, the bot carries on): %r", what, exc)


def _server_time() -> str | None:
    try:
        import signals
        return signals.server_now().isoformat(sep=" ", timespec="seconds")
    except Exception:
        return None


def _utc_now() -> datetime:
    try:
        import signals
        return signals.utc_now()
    except Exception:
        return datetime.now(timezone.utc)


def build(kind: str, payload: dict, symbol: str | None = None) -> dict:
    row = {"record_id": uuid.uuid4().hex, "kind": kind,
           "utc_time": _utc_now().astimezone(timezone.utc).isoformat(timespec="seconds"),
           "server_time": _server_time(), "version": C.VERSION, "account_login": context.get("account_login"),
           "symbol": symbol if symbol is not None else (payload or {}).get("symbol")}
    for k, v in (payload or {}).items():
        if k not in row:
            row[k] = v
    return row


def record(kind: str, payload: dict | None = None, symbol: str | None = None) -> None:
    """Queue one record. Never raises, never blocks."""
    try:
        if C.RECORD_SINK == "none" and sink is None:
            return
        _q.put_nowait(build(kind, payload or {}, symbol))
        with _lock:
            _inflight[0] += 1
        _ensure_thread()
    except Exception as e:                              # queue full, bad payload, ... : the bot carries on
        _warn_once("enqueue", e)


def flush(timeout: float = 5.0) -> None:
    """Write everything queued so far (tests, shutdown)."""
    deadline = time.time() + timeout
    while _inflight[0] > 0 and time.time() < deadline:
        _drain()
        time.sleep(0.005)


def _drain(first: dict | None = None):
    rows = [first] if first is not None else []
    with _lock:
        while True:
            try:
                rows.append(_q.get_nowait())
            except queue.Empty:
                break
        if rows:
            try:
                _sink().write(rows)
            except Exception as e:
                _warn_once("write", e)
            _inflight[0] -= len(rows)


def _run():
    while True:
        try:
            _drain(_q.get(timeout=1.0))
        except queue.Empty:
            pass
        except Exception as e:
            _warn_once("thread", e)


def _ensure_thread():
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_run, daemon=True, name="record-sink")
        _thread.start()
