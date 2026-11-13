"""Discord + Slack webhooks. send() returns per-service status: 'ok' | 'failed' | 'not_configured'.
State (dedupe keys, checkpoints, pending deliveries) lives in one JSON file owned by bot.py."""
import json, logging, os, time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import requests
import config as C
import signals
import store

log = logging.getLogger("notifier")
_last_fail_notice = {}


def load_json(path: str, default: dict) -> tuple[dict, str | None]:
    """Load path, falling back to path.bak. Returns (data, problem). A corrupt file is kept as *.corrupt-<ts>."""
    import copy
    default = copy.deepcopy(default)                             # callers mutate the result; never share the template
    name, corrupt = os.path.basename(path), []
    for p in (path, path + ".bak"):
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                if p != path:
                    why = f"{name} was corrupt" if corrupt else f"{name} was missing (interrupted write)"
                    return {**default, **d}, f"{why}; restored from .bak"
                return {**default, **d}, None
            except Exception:
                corrupt.append(p); os.replace(p, f"{p}.corrupt-{int(time.time())}")
    if corrupt:
        return default, f"{name} AND its .bak were corrupt — state reset"
    return default, None


def save_json(path: str, d: dict):
    """Atomic write; the previous good file is COPIED to .bak first so `path` always exists."""
    import shutil
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=0)
    if os.path.exists(path):
        shutil.copyfile(path, path + ".bak")
    os.replace(tmp, path)


def keys_from_log(limit=3000) -> list[str]:
    """Rebuild dedupe keys from logs/alerts/*.log (every sent event is logged with its KEY=...)."""
    import glob
    keys = []
    for path in sorted(glob.glob(os.path.join(C.ALERT_LOG_DIR, "alerts-*.log"))):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if " KEY=" in line:
                    keys.append(line.rsplit(" KEY=", 1)[1].strip())
    return keys[-limit:]


class State:
    def __init__(self, path=C.STATE_FILE):
        self.path = path
        self.d, self.problem = load_json(path, {"keys": [], "last_bar": {}, "pending": [], "last_price_check": None})
        self._migrate()
        if self.problem:
            log.error(self.problem)
            if not self.d["keys"]:                                 # total reset -> no duplicates thanks to the log
                self.d["keys"] = keys_from_log()
                self.problem += f"; {len(self.d['keys'])} dedupe keys rebuilt from alerts.log"
                self.save()

    def _migrate(self):
        """v2.1 kept one symbol's checkpoints un-namespaced ("M30"); 1.7.0 keys them "XAUUSD:M30"."""
        lb = self.d.get("last_bar") or {}
        for k in [k for k in lb if ":" not in k]:
            lb.setdefault(f"{C.SYMBOL}:{k}", lb.pop(k))
        lpc = self.d.get("last_price_check")
        if lpc is None or isinstance(lpc, str):
            self.d["last_price_check"] = {C.SYMBOL: lpc} if lpc else {}

    def save(self):
        save_json(self.path, self.d)

    def seen(self, key): return key in self.d["keys"]

    def mark(self, key):
        self.d["keys"] = (self.d["keys"] + [key])[-3000:]; self.save()

    def checkpoint(self, tf, bar_time, symbol: str | None = None):
        self.d["last_bar"][f"{symbol or C.SYMBOL}:{tf}"] = str(bar_time); self.save()

    def last_bar(self, tf, symbol: str | None = None):
        return self.d["last_bar"].get(f"{symbol or C.SYMBOL}:{tf}")

    def queue(self, msg, to: list[str]):
        sym = msg.get("symbol") if isinstance(msg, dict) else None
        p = self.d["pending"] + [{"text": msg, "ts": time.time(), "to": to, "symbol": sym}]
        if len(p) > C.MAX_PENDING:                               # never drop silently: fold the oldest into a summary
            cut = len(p) - C.MAX_PENDING
            p = [{"text": f"📦 {cut} older alerts could not be queued; full text is in logs/alerts/",
                  "ts": p[cut - 1]["ts"], "to": to}] + p[cut:]
        self.d["pending"] = p; self.save()


def configured() -> list[str]:
    return [n for n, (url, _) in SERVICES.items() if url]


def channel_of(msg) -> str:
    return msg.get("channel") or "alerts" if isinstance(msg, dict) else "alerts"


def _url(name: str, channel: str) -> str:
    if name == "Discord":
        return C.DISCORD_WEBHOOKS.get(channel) or C.DISCORD_WEBHOOK
    return SERVICES[name][0]


def _services_for(msg) -> list[str]:
    """Slack and ntfy only carry the #alerts set (section 11); Discord carries every channel."""
    return ["Discord"] if channel_of(msg) != "alerts" else list(SERVICES)


# ---- the bot-client path: cards with buttons, and cards edited in place, go to the Discord thread through
# logs/outbox/ (one writer each way, like the inbox). Set by bot.py once the Discord listener is running.
BOT_CLIENT = {"on": False}


def interactive(msg) -> bool:
    return (isinstance(msg, dict) and BOT_CLIENT["on"] and bool(C.DISCORD_CHANNEL_IDS.get(channel_of(msg)))
            and bool(msg.get("buttons") or msg.get("upsert")))


def outbox_put(msg) -> str:
    """Hand a card to the Discord thread. Returns 'ok' once it is durably on disk."""
    try:
        import uuid
        os.makedirs(C.OUTBOX_DIR, exist_ok=True)
        base = os.path.join(C.OUTBOX_DIR, f"{time.time():017.6f}-{uuid.uuid4().hex[:8]}")
        with open(base + ".tmp", "w", encoding="utf-8") as f:
            json.dump({**msg, "channel_id": C.DISCORD_CHANNEL_IDS.get(channel_of(msg))}, f, default=str)
        os.replace(base + ".tmp", base + ".json")
        return "ok"
    except OSError:
        log.exception("outbox write failed")
        return "failed"


def button(label: str, action: str, *args, style: str = "secondary") -> dict:
    """A button on a card. Pressing it sends a signed command back through the inbox (alerts.py)."""
    return {"label": label, "id": ":".join(["aureon", action, *[str(a) for a in args]]), "style": style}


def _post(url, payload, name, attempts: int = 3) -> str:
    if not url:
        return "not_configured"
    payload = dict(payload)
    image = payload.pop("_file", None)                           # set by _discord when the card has a chart
    data = None
    if image:
        try:
            with open(image, "rb") as f:
                data = f.read()
        except OSError:
            payload["embeds"][0].pop("image", None)              # chart file gone: send the card without it
    err = "unknown"
    for attempt in range(attempts):
        try:
            if data:
                r = requests.post(url, data={"payload_json": json.dumps(payload)},
                                  files={"files[0]": ("chart.png", data, "image/png")}, timeout=(5, 20))
            else:
                r = requests.post(url, json=payload, timeout=(5, 10))
            if r.status_code < 300:
                return "ok"
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2))); continue
            err = f"HTTP {r.status_code}: {r.text[:120]}"
        except requests.RequestException as e:
            err = str(e)[:120]
        if attempt + 1 < attempts:
            time.sleep(2 * (attempt + 1))
    log.error("%s webhook FAILED: %s", name, err)
    return "failed"


COLORS = {"BULL": 0x2ECC71, "BEAR": 0xE74C3C, "CAUTION": 0xF1C40F, "WARN": 0xE67E22, "INFO": 0x95A5A6, "HIT": 0x3498DB}


def card(title: str, desc: str = "", color: str = "INFO", fields: list | None = None, footer: str = "",
         ping: bool = False, image: str | None = None, channel: str = "alerts", symbol: str | None = None,
         kind: str = "", buttons: list | None = None, upsert: str | None = None) -> dict:
    """A notification card. fields = [(name, value, inline)]. Sent as a Discord embed / Slack attachment.
    ping=True: buzz the phone (Discord mention + readable push line, high-priority ntfy).
    image: path of a chart PNG, shown inside the Discord card.
    channel: alerts | updates | positions (section 11). kind: what it is, for the records.
    buttons: [button(...)] - shown when the Discord bot client is running. upsert: a stable key; the Discord thread
    EDITS the message it posted under that key instead of posting a new one (status / monitor cards)."""
    out = {"title": title, "desc": desc, "color": color, "fields": fields or [], "footer": footer, "ping": ping,
           "image": image, "channel": channel if channel in C.CHANNELS else "alerts", "symbol": symbol, "kind": kind}
    if buttons:
        out["buttons"] = buttons
    if upsert:
        out["upsert"] = upsert
    return out


def plain(msg) -> str:
    """Text form of a message (for logs and as fallback)."""
    if isinstance(msg, str):
        return msg
    parts = [msg["title"]] + ([msg["desc"]] if msg.get("desc") else []) + \
            [f"{nm}: {val}" for nm, val, *_ in msg.get("fields", [])] + ([msg["footer"]] if msg.get("footer") else [])
    return "\n".join(parts)


def _discord(msg) -> dict:
    if isinstance(msg, str):
        return {"content": msg}
    emb = {"title": msg["title"], "description": msg.get("desc", ""), "color": COLORS[msg["color"]],
           "fields": [{"name": nm, "value": str(val), "inline": (f[2] if len(f) > 2 else True)}
                      for f in msg.get("fields", []) for nm, val in [f[:2]]]}
    if msg.get("footer"):
        emb["footer"] = {"text": msg["footer"]}
    out = {"embeds": [emb]}
    if msg.get("image"):
        emb["image"] = {"url": "attachment://chart.png"}
        out["_file"] = msg["image"]
    if msg.get("ping"):                                          # plain text line = what the phone push shows
        out["content"] = f"{C.DISCORD_MENTION} {msg['title']}".strip()
        out["allowed_mentions"] = {"parse": ["users", "roles", "everyone"]}
    return out


def _ntfy(msg) -> dict:
    if isinstance(msg, str):
        return {"topic": C.NTFY_TOPIC, "message": msg, "priority": 3}
    body = "\n".join(plain(msg).split("\n")[1:]) or msg["title"]
    return {"topic": C.NTFY_TOPIC, "title": msg["title"], "message": body, "priority": 5 if msg.get("ping") else 2}


def _slack(msg) -> dict:
    if isinstance(msg, str):
        return {"text": msg}
    hexc = f"#{COLORS[msg['color']]:06X}"
    att = {"color": hexc, "title": msg["title"], "text": msg.get("desc", ""), "mrkdwn_in": ["text", "fields"],
           "fields": [{"title": nm, "value": str(val), "short": (f[2] if len(f) > 2 else True)}
                      for f in msg.get("fields", []) for nm, val in [f[:2]]]}
    if msg.get("footer"):
        att["footer"] = msg["footer"]
    return {"text": msg["title"], "attachments": [att]}


SERVICES = {"Discord": (C.DISCORD_WEBHOOK, _discord),
            "Slack":   (C.SLACK_WEBHOOK,   _slack),
            "Ntfy":    (C.NTFY_SERVER if C.NTFY_TOPIC else "", _ntfy)}


def _logfile(msg, key: str | None = None):
    try:
        with open(C.alerts_log_path(), "a", encoding="utf-8") as f:
            tail = f" KEY={key}" if key else ""
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {plain(msg).replace(chr(10), ' | ')}{tail}\n")
    except Exception:
        log.exception("alerts.log write failed")


def send(msg, only: list[str] | None = None, key: str | None = None, attempts: int = 2) -> dict:
    """Post a str or card to configured services (or just `only`) IN PARALLEL, so one slow or dead service
    never delays the others. Two quick tries live; anything still failing goes to the retry queue. Returns {service: 'ok'|'failed'|'not_configured'}."""
    log.info("ALERT %s", plain(msg).replace("\n", " | "))
    if only is None:
        _logfile(msg, key)
    chan = channel_of(msg)
    names = [n for n in _services_for(msg) if only is None or n in only]
    if not names:
        return {}

    def one(n):
        if n == "Discord" and interactive(msg):
            return outbox_put(msg)                               # buttons / edit-in-place: the bot client posts it
        return _post(_url(n, chan), SERVICES[n][1](msg), n, attempts)
    with ThreadPoolExecutor(max_workers=len(names)) as ex:
        results = list(ex.map(one, names))
    status = dict(zip(names, results))
    if only is None:
        store.record("alert", {"channel": chan, "alert_kind": msg.get("kind", "") if isinstance(msg, dict) else "text",
                               "title": msg.get("title") if isinstance(msg, dict) else str(msg)[:200],
                               "dedupe_key": key, "delivered": [n for n, st in status.items() if st == "ok"],
                               "failed": failed_services(status)},
                     symbol=msg.get("symbol") if isinstance(msg, dict) else None)
    delivered = [n for n, s in status.items() if s == "ok"]
    for name in (n for n, s in status.items() if s == "failed"):
        if time.time() - _last_fail_notice.get(name, 0) < C.FAIL_NOTICE_COOLDOWN:
            continue
        _last_fail_notice[name] = time.time()
        notice = f"⚠️ {name} webhook is failing — alerts still arrive here. Check the {name} URL."
        for other in delivered:
            _post(_url(other, chan), SERVICES[other][1](notice), other)
    return status


def failed_services(status: dict) -> list[str]:
    return [n for n, s in status.items() if s == "failed"]


def flush_pending(state: State):
    """Retry each pending message to the services that still owe it; keep what still fails."""
    still, down = [], set()                                      # a service that fails once is skipped for this pass
    for item in state.d["pending"]:
        targets = [s for s in item["to"] if s not in down]
        if not targets:
            still.append(item); continue
        age_min = (time.time() - item["ts"]) / 60
        msg = item["text"]
        if age_min >= 2:
            if isinstance(msg, dict):
                msg = {**msg, "footer": (f"⏳ delayed {age_min:.0f} min · " + msg.get("footer", "")).rstrip(" ·")}
            else:
                msg = f"⏳ (delayed {age_min:.0f} min)\n{msg}"
        failed = failed_services(send(msg, only=targets, attempts=1))   # one quick try: never stall the loop
        down.update(failed)
        owed = [s for s in item["to"] if s in failed or s not in targets]
        if owed:
            still.append({**item, "to": owed})
    if len(still) != len(state.d["pending"]) or still != state.d["pending"]:
        state.d["pending"] = still; state.save()


def deliver(state: State, msg, key: str | None = None) -> bool:
    """Send now (str or card); any service that fails gets it queued for retry. True if >=1 delivered."""
    status = send(msg, key=key)
    owed = failed_services(status)
    if owed:
        state.queue(msg, owed)
    return any(s == "ok" for s in status.values())


TITLES = {
    "CROSS":              f"EMA {C.EMA_FAST}/{C.EMA_SLOW} CROSS",
    "CROSS_AFTER_TREND":  f"EMA {C.EMA_FAST}/{C.EMA_SLOW} CROSS ✔ with EMA{C.EMA_TREND}",
    "FAST_X_TREND":       f"EMA {C.EMA_FAST} × EMA{C.EMA_TREND}",
    "SLOW_X_TREND":       f"EMA {C.EMA_SLOW} × EMA{C.EMA_TREND}",
    "PARALLEL_TREND":     f"BOTH × EMA{C.EMA_TREND} (EMA {C.EMA_FAST}+{C.EMA_SLOW} together)",
    "PRE_CROSS":          f"PRE-CROSS {C.EMA_FAST}/{C.EMA_SLOW}",
    "PRE_CROSS_FAILED":   f"PRE-CROSS FAILED",
    "NARROW_ZONE_START":  f"NARROW ZONE — chop",
    "NARROW_ZONE_END":    f"NARROW ZONE ENDED",
    # label engine (labels.py)
    "GAP WIDENING":       "GAP WIDENING",
    "PARALLEL RUNNING":   "PARALLEL RUNNING",
    "WIDE GAP":           "WIDE GAP",
    "GAP PEAKED":         "GAP PEAKED",
    "PULLBACK STARTED":   "PULLBACK STARTED",
    "RE-ENTRY":           "RE-ENTRY",
    "PULLBACK FAILED":    "PULLBACK FAILED",
    "PRE-CROSS":          "PRE-CROSS",
    "PRE-CROSS FAILED":   "PRE-CROSS FAILED",
}
ARROW = {"BULL": "▲ BULL", "BEAR": "▼ BEAR", "UNCLEAR": "◆ UNCLEAR", "LONG": "▲ LONG", "SHORT": "▼ SHORT"}
# where each event goes (section 11). Anything not listed: #alerts.
CHANNEL = {"GAP WIDENING": "updates", "PARALLEL RUNNING": "updates", "WIDE GAP": "updates",
           "PULLBACK STARTED": "updates", "RE-ENTRY": "updates", "NARROW_ZONE_START": "updates",
           "NARROW_ZONE_END": "updates"}
PINGS = C.PING_TYPES | {"PRE-CROSS", "PRE-CROSS FAILED", "GAP PEAKED", "PULLBACK FAILED"}


def _color(e) -> str:
    if e["type"] in ("NARROW_ZONE_START", "PRE_CROSS", "PRE_CROSS_FAILED", "PRE-CROSS", "PRE-CROSS FAILED",
                     "GAP PEAKED", "PULLBACK STARTED", "PULLBACK FAILED") or e["dir"] == "UNCLEAR":
        return "CAUTION"
    return "BULL" if e["dir"] in ("BULL", "LONG") else "BEAR"


def _emoji(e) -> str:
    return {"BULL": "🟢", "BEAR": "🔴", "CAUTION": "🟡"}[_color(e)]


def _context(e) -> str:
    t = e["type"]
    if t == "NARROW_ZONE_START":
        return f"gap {e['gap']:.2f} · wait, chop"
    if t == "NARROW_ZONE_END":
        box = f"box {e['zone_lo']:.0f}–{e['zone_hi']:.0f} · {e['bars']} bars"
        return box + (" · price confirms" if e["dir"] != "UNCLEAR" else " · price still inside")
    if t in ("PRE_CROSS_FAILED", "PRE-CROSS FAILED"):
        return f"{e.get('pair', '8/32')} · diverging again · watch only"
    if t in ("PRE_CROSS", "PRE-CROSS"):
        return (f"{e.get('pair', '8/32')} · {e.get('dist', 0):.2f} apart ({e.get('dist_atr', 0):.2f} ATR) · "
                f"converging · WATCH SIGNAL, not a trade signal")
    if t.startswith("CROSS"):
        out = f"tag {e['tag']}" if e.get("tag") else ""
        if e.get("lesser_gap"):
            out += " · LESSER GAP"
        fin = e.get("finished")
        if fin:
            side = "long" if fin["side"] > 0 else "short"
            out += f" · TREND FINISHED: the {side} ran {fin['ran']:+.2f} in {fin['bars']} candles"
        return out.strip(" ·")
    if t in ("PULLBACK STARTED",):
        return f"pre-pullback extreme {e.get('pre_extreme', 0):.2f}"
    if t == "RE-ENTRY":
        return f"pullback extreme {e.get('pullback_extreme', 0):.2f}"
    return ""


def _times(tf, bar_time) -> tuple[str, str]:
    close_time = bar_time.to_pydatetime() + timedelta(minutes=C.BAR_MINUTES[tf])
    return f"{signals.to_ist(close_time):%d %b %H:%M} IST", f"{close_time:%H:%M} server"


def format_event(tf, bar_time, e, missed=False, image=None, symbol: str | None = None, status: str = "",
                 extra: list | None = None, buttons: list | None = None) -> dict:
    """Live alert card. Title starts with the symbol. status = the present market status line."""
    symbol = symbol or C.SYMBOL
    ist, srv = _times(tf, bar_time)
    title = f"{symbol} {_emoji(e)} {tf} · {TITLES.get(e['type'], e['type'])} {ARROW.get(e['dir'], e['dir'])}"
    if missed:
        title = f"{symbol} ⚠️ MISSED · " + title[len(symbol) + 1:]
    fields = [("Price", f"{e['price']:.2f}"), ("Candle closed", ist)]
    ctx = _context(e)
    if ctx:
        fields.append(("Note", ctx, False))
    if status:
        fields.append(("Market now", status, False))
    for f in extra or []:
        fields.append(f)
    return card(title, color=_color(e), fields=fields, footer=srv, ping=e["type"] in PINGS and not missed, image=image,
                channel=CHANNEL.get(e["type"], "alerts"), symbol=symbol, kind=e["type"], buttons=buttons)


def event_caption(tf, bar_time, e, symbol: str | None = None) -> str:
    """Plain-text chart title (no emoji: chart fonts do not have them)."""
    ist, _ = _times(tf, bar_time)
    arrow = {"BULL": "BULL", "BEAR": "BEAR", "UNCLEAR": "UNCLEAR", "LONG": "LONG", "SHORT": "SHORT"}.get(e["dir"], e["dir"])
    return f"{symbol or C.SYMBOL} {tf} · {TITLES.get(e['type'], e['type']).replace('✔ ', '')} · {arrow} · {e['price']:.2f} · {ist}"


def digest_line(tf, bar_time, e) -> str:
    close_time = bar_time.to_pydatetime() + timedelta(minutes=C.BAR_MINUTES[tf])
    return (f"{_emoji(e)} `{signals.to_ist(close_time):%d %b %H:%M}` {TITLES.get(e['type'], e['type'])} "
            f"{ARROW.get(e['dir'], e['dir'])} @ **{e['price']:.2f}**")


def format_digest(tf, lines: list[str], first_bar, last_bar, n_bars: int, status: str, symbol: str | None = None) -> dict:
    symbol = symbol or C.SYMBOL
    shown = lines[-C.DIGEST_MAX_LINES:]
    earlier = len(lines) - len(shown)
    f_ist = signals.to_ist(first_bar.to_pydatetime())
    l_ist = signals.to_ist(last_bar.to_pydatetime() + timedelta(minutes=C.BAR_MINUTES[tf]))
    desc = (f"… {earlier} earlier events in logs/alerts/\n" if earlier else "") + \
           ("\n".join(shown) if shown else "no signals in that window")
    return card(f"{symbol} ⚠️ {tf} · MISSED WHILE OFFLINE", desc, "WARN",
                fields=[("Window", f"{n_bars} bars · {f_ist:%d %b %H:%M} → {l_ist:%d %b %H:%M} IST", False),
                        ("Now", status, False)], symbol=symbol, kind="DIGEST")


def format_price_alert(level, price, status, now_server, symbol: str | None = None) -> dict:
    symbol = symbol or C.SYMBOL
    return card(f"{symbol} 🎯 HIT {level:.2f}", color="HIT", ping=True,
                fields=[("Now", f"{price:.2f}"), ("Time", f"{signals.to_ist(now_server):%H:%M} IST"),
                        ("EMA state", status, False)], symbol=symbol, kind="PRICE_ALERT")


def warn(title: str, desc: str = "", ping: bool = False, symbol: str | None = None, channel: str = "alerts",
         kind: str = "WARN") -> dict:
    """A warning card. With a symbol, the title starts with it."""
    return card(f"{symbol} ⚠️ {title}" if symbol else f"⚠️ {title}", desc, "WARN", ping=ping, symbol=symbol,
                channel=channel, kind=kind)


def info(title: str, desc: str = "", symbol: str | None = None, channel: str = "alerts", fields: list | None = None,
         kind: str = "INFO", ping: bool = False, buttons: list | None = None, upsert: str | None = None,
         image: str | None = None, color: str = "INFO") -> dict:
    """An information card; title starts with the symbol when there is one."""
    return card(f"{symbol} {title}" if symbol else title, desc, color, fields=fields, symbol=symbol, channel=channel,
                kind=kind, ping=ping, buttons=buttons, upsert=upsert, image=image)
