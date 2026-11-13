"""Optional uploader: ships the bot's JSONL records (and closed candles) to the Assembler API.

Started only when RECORD_SINK=api. It runs on its own daemon thread, never on the MT5 thread, never calls MT5,
and nothing in the bot waits for it: alerts, labels, stop-loss moves and commands are identical whether the API is
up, down, slow or answering errors.

    JSONL stays the source of truth. store.py writes logs/records/<kind>-YYYY-MM.jsonl exactly as before; this
    module only READS those files and remembers, per file, the byte offset already accepted by the server
    (logs/state/upload_cursor.json, written atomically). The cursor moves only after a 200. A half-written last line
    waits for the next pass. Files are never deleted.

Routes: kind "bar" rows -> POST /api/trading/mt5/bars (contiguous runs per symbol + timeframe), everything else ->
POST /api/trading/mt5/records. Delivery is at-least-once; the server is idempotent (record_id / symbol+tf+ts).

Failures: timeout / connection error / 5xx / 503 -> keep the cursor, back off 15 s doubling to 5 min. 429 -> wait
Retry-After. 401 -> stop, retry every 5 min only, notice at once. 413 / 422 for a whole batch -> halve it; a single
row that is still refused goes to logs/records/rejected-YYYY-MM.jsonl and is skipped, so one bad row can never
block the queue. Rows the server lists as rejected are written there too.

The token is only ever put in the Authorization header: never logged, recorded or shown.
"""
from __future__ import annotations
import glob, gzip, json, logging, os, threading, time, uuid
from datetime import datetime, timezone
import requests
import config as C

log = logging.getLogger("uploader")
RECORDS_ROUTE, BARS_ROUTE = "/api/trading/mt5/records", "/api/trading/mt5/bars"
RECORDS_SCHEMA, BARS_SCHEMA = "aureon.mt5.records/v1", "aureon.mt5.bars/v1"
MAX_BARS_PER_REQUEST = 5000
CURSOR_FILE = os.path.join(C.STATE_DIR, "upload_cursor.json")
BACKOFF_START, BACKOFF_MAX, AUTH_RETRY = 15.0, 300.0, 300.0
NOTICE_AFTER = 30 * 60                       # failing this long -> one notice (401: at once)
MAX_LINES_PER_PASS = 20000                   # keeps one pass bounded; the rest goes next pass


class Transient(Exception):
    """Try the same rows again later (timeout, connection error, 5xx, 429)."""
    def __init__(self, why: str, wait: float | None = None):
        super().__init__(why)
        self.why, self.wait = why, wait


class AuthFailed(Exception):
    pass


# ------------------------------------------------------------------------------------------- HTTP client
class Client:
    def __init__(self, url: str, token: str, timeout: float):
        self.url, self._token, self.timeout = url.rstrip("/"), token, timeout

    def __repr__(self):                                       # never show the token, even in a traceback
        return f"Client({self.url!r})"

    def post(self, route: str, payload: dict) -> dict:
        """POST gzip JSON. Returns the 200 body. Raises Transient, AuthFailed, or Refused (413/422)."""
        body = gzip.compress(json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8"))
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json",
                   "Content-Encoding": "gzip"}
        try:
            r = requests.post(self.url + route, data=body, headers=headers, timeout=self.timeout)
        except Exception as e:                                # timeout, refused, DNS, TLS ...
            raise Transient(f"{type(e).__name__} on {route}") from None
        code = r.status_code
        if code == 200:
            try:
                return r.json() or {}
            except ValueError:
                return {}
        if code == 401:
            raise AuthFailed(f"401 on {route}")
        if code in (413, 422):
            raise Refused(code)
        if code == 429:
            try:
                wait = float(r.headers.get("Retry-After", BACKOFF_START))
            except (TypeError, ValueError):
                wait = BACKOFF_START
            raise Transient(f"429 on {route}", wait=max(wait, 1.0))
        raise Transient(f"HTTP {code} on {route}")


class Refused(Exception):
    def __init__(self, code: int):
        super().__init__(f"HTTP {code}")
        self.code = code


# -------------------------------------------------------------------------------------------- the cursor
def load_cursor() -> dict:
    for p in (CURSOR_FILE, CURSOR_FILE + ".bak"):
        try:
            with open(p, encoding="utf-8") as f:
                return {k: int(v) for k, v in json.load(f).items()}
        except (OSError, ValueError, AttributeError):
            continue
    return {}


def save_cursor(cur: dict) -> None:
    """Atomic, with the previous good file kept as .bak (as the other state files)."""
    tmp = CURSOR_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f)
        f.flush(); os.fsync(f.fileno())
    if os.path.exists(CURSOR_FILE):
        import shutil
        shutil.copyfile(CURSOR_FILE, CURSOR_FILE + ".bak")
    os.replace(tmp, CURSOR_FILE)


def record_files() -> list[str]:
    """Record files oldest month first; the rejected log is not a record file."""
    paths = [p for p in glob.glob(os.path.join(C.RECORD_DIR, "*.jsonl"))
             if not os.path.basename(p).startswith("rejected-")]
    month = lambda p: os.path.basename(p).rsplit("-", 2)[-2] + os.path.basename(p).rsplit("-", 1)[-1]
    return sorted(paths, key=lambda p: (month(p), os.path.basename(p)))


def read_new(path: str, offset: int, limit: int) -> list[tuple[int, bytes]]:
    """[(end_offset, line_bytes)] for complete lines after offset. A last line without its newline is left."""
    out = []
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return out
    pos = offset
    for chunk in data.split(b"\n")[:-1]:                     # the piece after the last \n is incomplete (or empty)
        pos += len(chunk) + 1
        if chunk.strip():
            out.append((pos, chunk))
        if len(out) >= limit:
            break
    if not out and data.count(b"\n"):                        # only blank lines: still move past them
        out.append((offset + data.rfind(b"\n") + 1, b""))
    return out


def backlog() -> int:
    cur = load_cursor()
    n = 0
    for p in record_files():
        try:
            with open(p, "rb") as f:
                f.seek(cur.get(os.path.basename(p), 0))
                n += sum(1 for line in f.read().split(b"\n")[:-1] if line.strip())
        except OSError:
            pass
    return n


# ----------------------------------------------------------------------------------------- row mapping
def bar_payload(rows: list[dict]) -> dict:
    """bar records (one symbol + timeframe) -> the bars route body."""
    first = rows[0]
    return {"schema": BARS_SCHEMA, "symbol": first.get("symbol"), "timeframe": first.get("tf"), "source": "bot",
            "bars": [{"ts": r.get("bar_time_utc"), "server_ts": r.get("bar_time_server"),
                      **{k: r.get(k) for k in ("open", "high", "low", "close", "tick_volume", "spread",
                                               "ema8", "ema32", "ema200", "atr14")}} for r in rows]}


def reject(rows: list[dict], reason: str) -> None:
    """Log refused rows once and move on."""
    if not rows:
        return
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    path = os.path.join(C.RECORD_DIR, f"rejected-{month}.jsonl")
    try:
        with open(path, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({"rejected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    "reason": str(reason)[:300], "row": r}, default=str, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("could not write the rejected log")
    log.warning("%d row(s) rejected by the server: %s", len(rows), str(reason)[:200])


# ------------------------------------------------------------------------------------------------ engine
class Uploader:
    def __init__(self, client: Client | None = None, batch: int | None = None):
        self.client = client or Client(C.ASSEMBLER_URL, C.ASSEMBLER_RECORDS_TOKEN, C.RECORD_API_TIMEOUT_S)
        self.batch = max(1, min(int(batch or C.RECORD_API_BATCH), 500))
        self.backoff = BACKOFF_START
        self.last_ok: float | None = None
        self.failing_since: float | None = None
        self.notified = False
        self.auth_failed = False
        self.last_error = ""
        self._stop = threading.Event()

    # -- one batch -------------------------------------------------------------------------------------
    def _send_records(self, rows: list[dict]) -> None:
        res = self.client.post(RECORDS_ROUTE, {"schema": RECORDS_SCHEMA, "batch_id": str(uuid.uuid4()), "rows": rows})
        bad = res.get("rejected") or []
        if bad:
            by_id = {r.get("record_id"): r for r in rows}
            reject_rows = []
            for b in bad:
                row = by_id.get(b.get("record_id"), {"record_id": b.get("record_id")})
                reject_rows.append({**row, "_reason": b.get("reason")})
            reject([{k: v for k, v in r.items() if k != "_reason"} for r in reject_rows],
                   "; ".join(str(b.get("reason")) for b in bad))

    def _send_bars(self, rows: list[dict]) -> None:
        for k in range(0, len(rows), MAX_BARS_PER_REQUEST):
            part = rows[k:k + MAX_BARS_PER_REQUEST]
            res = self.client.post(BARS_ROUTE, bar_payload(part))
            for b in res.get("rejected") or []:                  # match by ts when the server says which bar
                ts = b.get("ts") if isinstance(b, dict) else None
                row = next((r for r in part if r.get("bar_time_utc") == ts), {"symbol": part[0].get("symbol"),
                                                                                 "tf": part[0].get("tf"), "ts": ts})
                reject([row], b.get("reason", b) if isinstance(b, dict) else b)

    def _deliver(self, rows: list[dict], send) -> tuple[int, Exception | None]:
        """Send rows in order, halving on 413/422 down to single rows (a refused single row is logged as rejected
        and skipped). Returns (rows done - always a prefix -, the transient/auth error that stopped it or None)."""
        try:
            send(rows)
            return len(rows), None
        except Refused as e:
            if len(rows) == 1:
                reject(rows, f"HTTP {e.code} for a single row")
                return 1, None
            half = len(rows) // 2
            d1, err = self._deliver(rows[:half], send)
            if err:
                return d1, err
            d2, err = self._deliver(rows[half:], send)
            return half + d2, err
        except (Transient, AuthFailed) as e:
            return 0, e

    # -- one pass -------------------------------------------------------------------------------------
    def run_pass(self) -> float:
        """Upload what is waiting. Returns seconds until the next pass."""
        cur = load_cursor()
        sent_lines = 0
        try:
            for path in record_files():
                name = os.path.basename(path)
                while sent_lines < MAX_LINES_PER_PASS and not self._stop.is_set():
                    lines = read_new(path, cur.get(name, 0), self.batch)
                    if not lines:
                        break
                    rows, ends = [], []
                    for end, raw in lines:
                        ends.append(end)
                        if not raw:
                            rows.append(None); continue
                        try:
                            rows.append(json.loads(raw))
                        except ValueError:
                            reject([{"raw": raw.decode("utf-8", "replace")[:2000], "file": name}], "not valid JSON")
                            rows.append(None)
                    done, err = self._ship(rows)
                    if done:                                     # the cursor moves only past delivered rows
                        cur[name] = ends[done - 1]
                        save_cursor(cur)
                        sent_lines += done
                    if err:
                        raise err
        except AuthFailed as e:
            self._failed(str(e), auth=True)
            return AUTH_RETRY
        except Transient as e:
            self._failed(e.why)
            wait = e.wait if e.wait else self.backoff
            if not e.wait:
                self.backoff = min(self.backoff * 2, BACKOFF_MAX)
            return wait
        self._ok()
        return C.RECORD_API_INTERVAL_S

    def _ship(self, rows: list) -> tuple[int, Exception | None]:
        """One read batch, in order: runs of records -> records route, runs of bars (same symbol + timeframe)
        -> bars route. Returns (rows done, error)."""
        i = 0
        while i < len(rows):
            if rows[i] is None:                                  # blank or bad line, already handled
                i += 1; continue
            j = i
            if rows[i].get("kind") == "bar":
                key = (rows[i].get("symbol"), rows[i].get("tf"))
                while j < len(rows) and rows[j] is not None and rows[j].get("kind") == "bar" \
                        and (rows[j].get("symbol"), rows[j].get("tf")) == key:
                    j += 1
                send = self._send_bars
            else:
                while j < len(rows) and rows[j] is not None and rows[j].get("kind") != "bar":
                    j += 1
                send = self._send_records
            k, err = self._deliver(rows[i:j], send)
            i += k
            if err:
                return i, err
        return i, None

    def _ok(self):
        self.backoff = BACKOFF_START
        self.last_ok = time.time()
        self.auth_failed = False
        if self.notified:
            _notice("upload_recovered", "Record upload recovered",
                    f"Uploads to the Assembler API work again (backlog {backlog()} rows).")
            _record("upload_recovered", f"after {(time.time() - (self.failing_since or time.time())) / 60:.0f} min")
        self.failing_since, self.notified, self.last_error = None, False, ""

    def _failed(self, why: str, auth: bool = False):
        now = time.time()
        self.last_error = why
        if self.failing_since is None:
            self.failing_since = now
            log.warning("record upload failing: %s (retrying)", why)
        self.auth_failed = auth
        if not self.notified and (auth or now - self.failing_since >= NOTICE_AFTER):
            self.notified = True
            what = ("the server refused the token (401). Sending is paused and retried every 5 minutes; "
                    "check ASSEMBLER_RECORDS_TOKEN") if auth else f"{why}, for {(now - self.failing_since) / 60:.0f} min"
            _notice("upload_failing", "Record upload FAILING",
                    f"Uploads to the Assembler API are failing: {what}. Nothing else is affected: records keep "
                    f"being written to logs/records/ and will be sent when it recovers (backlog {backlog()} rows).")
            _record("upload_failing", why)

    # -- thread ---------------------------------------------------------------------------------------
    def run(self):
        while not self._stop.is_set():
            try:
                wait = self.run_pass()
            except Exception:                                    # never die: log and back off
                log.exception("upload pass crashed")
                wait = self.backoff
                self.backoff = min(self.backoff * 2, BACKOFF_MAX)
            self._stop.wait(wait)                                # an Event, not time.sleep

    def stop(self):
        self._stop.set()


def _notice(kind: str, title: str, desc: str):
    try:
        import updater
        updater.notice(kind, title, desc)                     # the bot loop delivers it (#updates)
    except Exception:
        log.exception("could not queue the upload notice")


def _record(event: str, detail: str):
    try:
        import store
        store.record("system", {"event": event, "detail": detail[:300]})
    except Exception:
        pass


# ----------------------------------------------------------------------------------------- public API
_running: Uploader | None = None


def start() -> bool:
    """Start the daemon thread when RECORD_SINK=api. Returns True if started."""
    global _running
    if C.RECORD_SINK != "api" or not C.ASSEMBLER_URL or not C.ASSEMBLER_RECORDS_TOKEN:
        return False
    if _running is None:
        _running = Uploader()
        threading.Thread(target=_running.run, daemon=True, name="record-upload").start()
    return True


def status_line() -> str:
    """For /status: on/off, last successful upload, backlog."""
    if C.RECORD_SINK != "api":
        return "Record upload: off"
    u = _running
    if u is None:
        return "Record upload: on (not started)"
    last = "never" if u.last_ok is None else datetime.fromtimestamp(u.last_ok, timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo(C.TIMEZONE)).strftime("%d %b %H:%M IST")
    state = " · FAILING: " + u.last_error if u.failing_since else ""
    return f"Record upload: on · last success {last} · backlog {backlog()} rows{state}"
