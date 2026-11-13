"""Aureon EMA cross bot - main loop.
Signals only alert. An order is sent only when YOU run /place in Discord and press Confirm (trader.py); the
signal loop never trades. /place is off unless PLACE_ENABLED=1 is in .env.

Start:  python watchdog.py        (restarts the bot after a crash or freeze)
Check:  python bot.py --check-time   |   python -m unittest discover -s tests
Setup:  pip install -r requirements.txt, keys in .env (see .env.example), MT5 terminal open and logged in.
"""
import json, logging, os, sys, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import MetaTrader5 as mt5
import config as C
import signals, notifier, alerts, chart, trader, store, labels, positions, monitor, weekend

C.migrate_legacy_files()
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
_file = C.DailyFileHandler(C.RUNTIME_LOG_DIR, "bot"); _file.setFormatter(_fmt)
_con = logging.StreamHandler(); _con.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_con, _file])
log = logging.getLogger("bot")
TZ = ZoneInfo(C.TIMEZONE)
state = notifier.State()
REPLAY_CAP = C.MAX_REPLAY_BARS

GREEN, RESET = "\033[1;92m", "\033[0m"
BANNER = r"""
  █████╗ ██╗   ██╗██████╗ ███████╗ ██████╗ ███╗   ██╗
 ██╔══██╗██║   ██║██╔══██╗██╔════╝██╔═══██╗████╗  ██║
 ███████║██║   ██║██████╔╝█████╗  ██║   ██║██╔██╗ ██║
 ██╔══██║██║   ██║██╔══██╗██╔══╝  ██║   ██║██║╚██╗██║
 ██║  ██║╚██████╔╝██║  ██║███████╗╚██████╔╝██║ ╚████║
 ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚═╝  ╚═══╝
"""


def syms() -> str:
    """Title prefix for account-wide notices: every enabled symbol."""
    return "/".join(C.enabled_symbols())


def print_banner():
    os.system("")                                 # enable ANSI colours on Windows
    print(f"{GREEN}{BANNER}   v{C.VERSION} · {syms()} · EMA {C.EMA_FAST}/{C.EMA_SLOW}/{C.EMA_TREND} · "
          f"{'/'.join(C.TIMEFRAMES)}{RESET}\n")


def heartbeat(next_within: float):
    """Tell the watchdog we're alive and when to expect the next beat (seconds)."""
    try:
        tmp = C.HEARTBEAT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ts": time.time(), "next_within": next_within, "pid": os.getpid()}, f)
        os.replace(tmp, C.HEARTBEAT_FILE)
    except Exception:
        log.exception("heartbeat write failed")


def _week_minutes(weekday: int, hhmm: str) -> int:
    h, m = hhmm.split(":")
    return weekday * 1440 + int(h) * 60 + int(m)


def pause_seconds_left(now) -> float:
    """Seconds until the weekend pause ends, or 0 if the market week is on. Window: PAUSE_START -> PAUSE_END."""
    cur = now.weekday() * 1440 + now.hour * 60 + now.minute + now.second / 60
    start, end = _week_minutes(*C.PAUSE_START), _week_minutes(*C.PAUSE_END)
    inside = (start <= cur < end) if start < end else (cur >= start or cur < end)
    if not inside:
        return 0.0
    return ((end - cur) % (7 * 1440)) * 60


def weekend_pause() -> bool:
    now = signals.utc_now().astimezone(TZ)
    left = pause_seconds_left(now)
    if left <= 0:
        return False
    resume = now + timedelta(seconds=left)
    key = f"PAUSED:{resume:%Y-%m-%d}"                            # one notice per weekend
    if not state.seen(key):
        notifier.deliver(state, notifier.info("⏸️ paused for the weekend", f"Resumes {resume:%a %d %b %H:%M} IST",
                                              symbol=syms(), channel="updates", kind="PAUSE"))
        state.mark(key)
    mt5.shutdown()
    nap = min(left, 600)                                         # short naps so the heartbeat stays fresh
    heartbeat(nap)
    time.sleep(nap)
    return True


KEEP_CONNECTION = ("stale tick", "clock mismatch")
_stale_loops = 0


def ensure_mt5() -> str | None:
    """None when the feed is healthy, else the reason (so the loop can tell you it is blind)."""
    global _stale_loops
    problem = signals.health()
    if problem is None:
        _stale_loops = 0
        return None
    if problem.startswith(KEEP_CONNECTION):                        # quiet market, or the clock is still being measured:
        _stale_loops += 1                                          # reconnecting cannot help, so keep the connection
        if _stale_loops % 30:                                      # and only re-initialise every ~30th look
            return problem
    elif mt5.terminal_info() is not None:                             # was connected -> it's a real problem
        log.warning("MT5 health: %s — reconnecting", problem)
    mt5.shutdown()
    if mt5.initialize():
        for sym in C.enabled_symbols():                            # disabled symbols are never touched
            mt5.symbol_select(C.broker_symbol(sym), True)
        signals.sync_server_offset()
        acc = mt5.account_info()
        store.context["account_login"] = getattr(acc, "login", None)
        log.info("MT5 connected (server time %s)", signals.offset_text())
        return signals.health()
    err = f"MT5 init failed: {mt5.last_error()}"
    log.error(err)
    return err


# ---- "am I blind?" bookkeeping (in memory: a restart re-announces itself anyway) ----
_blind = {"since": None, "notified": False}


def report_blind(problem: str):
    now = time.time()
    if _blind["since"] is None:
        _blind["since"] = now
    limit = C.STALE_NOTICE_SECONDS if problem.startswith("stale tick") else C.BLIND_NOTICE_SECONDS
    if not _blind["notified"] and now - _blind["since"] >= limit:
        _blind["notified"] = True
        mins = (now - _blind["since"]) / 60
        notifier.deliver(state, notifier.warn("bot is BLIND — no alerts until this clears",
                                              f"{problem}\nSince {mins:.0f} min. Check the MT5 terminal / internet.",
                                              ping=True, symbol=syms(), kind="BLIND"))


def report_recovered():
    if _blind["notified"]:
        mins = (time.time() - _blind["since"]) / 60
        notifier.deliver(state, notifier.info("✅ feed is back", f"Was blind {mins:.0f} min — missed candles are being replayed.",
                                              symbol=syms(), kind="RECOVERED"))
    _blind["since"], _blind["notified"] = None, False


def _status_now(tf: str, symbol: str | None = None) -> str:
    df = signals.fetch(tf, symbol=symbol)
    price = signals.live_price(symbol)
    return signals.ema_status(df, price) if df is not None and price else "EMA status unavailable"


def _status_fields(tf: str) -> list:
    return [(sym, _status_now(tf, sym), False) for sym in C.enabled_symbols()]


def announce_start():
    """One 'online' card per (re)start, rate-limited so a crash loop cannot flood."""
    today = f"ALIVE:{signals.utc_now().astimezone(TZ):%Y-%m-%d}"
    store.record("system", {"event": "start", "detail": f"v{C.VERSION}", "sha": _git_sha()})
    if time.time() - state.d.get("last_start_notice", 0) >= C.START_NOTICE_COOLDOWN:
        state.d["last_start_notice"] = time.time(); state.save()
        off = [s for s in C.SYMBOLS if not C.is_enabled(s)]
        notifier.deliver(state, notifier.info(f"▶️ bot online · v{C.VERSION}", symbol=syms(), channel="updates", kind="START",
                                              fields=_status_fields(C.TIMEFRAMES[0]) +
                                              [("Server clock", signals.offset_text())] +
                                              ([("Disabled in config", ", ".join(off))] if off else [])))
    if not state.seen(today):
        state.mark(today)


def _git_sha() -> str | None:
    try:
        import subprocess
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=C.PROJECT_DIR, capture_output=True,
                              text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


def alive_ping():
    if not C.ALIVE_PING_IST:
        return
    now = signals.utc_now().astimezone(TZ)
    key = f"ALIVE:{now:%Y-%m-%d}"
    if f"{now:%H:%M}" >= C.ALIVE_PING_IST and not state.seen(key):
        notifier.deliver(state, notifier.info(f"✅ bot alive · v{C.VERSION}", symbol=syms(), channel="updates",
                                              kind="ALIVE", fields=_status_fields(C.TIMEFRAMES[0])))
        state.mark(key)


def next_sleep() -> float:
    """Sleep POLL_SECONDS, but wake exactly BAR_CLOSE_DELAY seconds after the next candle close."""
    now = signals.server_now()
    secs = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()
    waits = [float(C.POLL_SECONDS)]
    for tf in C.TIMEFRAMES:
        to_close = C.BAR_MINUTES[tf] * 60 - secs % (C.BAR_MINUTES[tf] * 60)
        if to_close <= C.POLL_SECONDS:                             # a candle closes before the next poll: land right on it
            return max(1.0, to_close + C.BAR_CLOSE_DELAY)
    return max(1.0, min(waits))


def check_ema(tf: str, symbol: str | None = None):
    """New closed candles on one symbol: detect, deliver once, then move that symbol's checkpoint."""
    symbol = symbol or C.SYMBOL
    if not C.is_enabled(symbol):
        return                                                     # disabled: no data reads, no alerts
    last_done = state.last_bar(tf, symbol)
    since = pd.Timestamp(last_done).to_pydatetime() if last_done else None
    if since and signals.server_now() < since + timedelta(minutes=2 * C.BAR_MINUTES[tf]):
        return                                                     # the next candle has not closed yet
    df = signals.fetch(tf, since=since, symbol=symbol)
    if df is None:
        log.warning("%s %s: no data", symbol, tf); return
    newest = df["time"].iloc[-1]
    if since and since >= newest.to_pydatetime():
        return                                                     # nothing new since checkpoint

    todo = int((df["time"] > since).sum()) if since else C.DEFAULT_LOOKBACK_BARS + 1
    todo = min(todo, len(df) - C.EMA_TREND, REPLAY_CAP)
    first_eval = df["time"].iloc[len(df) - todo].to_pydatetime()     # first candle actually evaluated

    if since:
        clamped = df.attrs.get("clamped_from")
        if clamped:                                                # we chose not to fetch that far back
            notifier.deliver(state, notifier.warn(f"{tf}: outage longer than replay limit",
                                                  f"NOT replayed: {signals.fmt_times(clamped)} → {signals.fmt_times(first_eval)}\nReplaying from there.",
                                                  symbol=symbol))
        elif first_eval > since + timedelta(minutes=C.BAR_MINUTES[tf]):   # broker history too short
            notifier.deliver(state, notifier.warn(f"{tf}: MT5 history too short",
                                                  f"NOT replayed: {signals.fmt_times(since)} → {signals.fmt_times(first_eval)}\nRaise 'Max bars in chart' in MT5.",
                                                  symbol=symbol))

    # ---- pass 1: detect everything, nothing persisted yet ----
    bar_min, now_srv = C.BAR_MINUTES[tf], signals.server_now()
    live, missed, missed_bars = [], [], 0                        # (key, bar_time, event)
    seen_now = set()                                               # episode-keyed events: once per replay too
    eval_from = df["time"].iloc[len(df) - todo]
    lab = update_labels(symbol, df)
    for bar_time, e in candle_events(df, todo, tf, symbol, lab):
        bar_close = bar_time.to_pydatetime() + timedelta(minutes=bar_min)
        is_missed = (now_srv - bar_close).total_seconds() > C.MISSED_GRACE_SECONDS
        key = f"{symbol}:{tf}:{e['type']}:{e['dir']}:{e.get('episode', str(bar_time))}"
        if state.seen(key) or key in seen_now:
            continue
        seen_now.add(key)
        (missed if is_missed and todo > 2 else live).append((key, bar_time, e))
    missed_bars = sum((now_srv - (t.to_pydatetime() + timedelta(minutes=bar_min))).total_seconds() > C.MISSED_GRACE_SECONDS
                      for t in df["time"][df["time"] >= eval_from])

    # ---- pass 2: deliver (or durably queue) FIRST, then mark keys and advance checkpoint ----
    if missed:
        lines = [notifier.digest_line(tf, bt, e) for _, bt, e in missed]
        for key, bt, e in missed:
            notifier._logfile(notifier.format_event(tf, bt, e, missed=True, symbol=symbol), key)
            record_event(symbol, tf, bt, e)
        price = signals.live_price(symbol) or float(df["close"].iloc[-1])
        last_missed_bar = missed[-1][1]
        notifier.deliver(state, notifier.format_digest(tf, lines, df["time"].iloc[len(df) - todo], last_missed_bar,
                                                       missed_bars, signals.ema_status(df, price), symbol=symbol))
        for key, _, _ in missed:                                   # digest is sent or in the retry queue -> safe to mark
            state.mark(key)
    elif missed_bars and todo > 2:
        log.info("%s %s: %d missed bars, no signals in them", symbol, tf, missed_bars)

    charts = {}                                                    # one picture per candle, shared by its events
    for bt, group in _fold_quiet(live):
        for key, e, folded in group:
            image = None
            if C.CHART_ENABLED and e["type"] in notifier.PINGS:
                if bt not in charts:
                    charts[bt] = chart.render(df[df["time"] <= bt], tf, notifier.event_caption(tf, bt, e, symbol),
                                              signals.to_ist, mark_last=e["dir"] if e["dir"] in ("BULL", "BEAR") else None,
                                              symbol=symbol)
                image = charts[bt]
            for _, fe in folded:
                record_event(symbol, tf, bt, fe)
            record_event(symbol, tf, bt, e)
            msg = format_live(tf, bt, e, symbol, missed=(todo <= 2 and bt != newest), image=image)
            if folded:
                msg["fields"].append(("Also on this candle", " · ".join(
                    f"{notifier.TITLES.get(fe['type'], fe['type'])} {notifier.ARROW.get(fe['dir'], fe['dir'])}"
                    for _, fe in folded), False))
            notifier.deliver(state, msg, key=key)
            for fk, _ in folded:
                state.mark(fk)
            state.mark(key)
    state.checkpoint(tf, newest, symbol)
    fresh = [(bt, e) for _, bt, e in live]
    for hook in CANDLE_HOOKS:                                      # positions (TSL, in-trade alerts), monitor, ...
        hook(symbol, tf, df, lab, fresh)


CANDLE_HOOKS: list = [lambda symbol, tf, df, lab, fresh: positions.on_candle(state, symbol, df, lab, fresh),
                      lambda symbol, tf, df, lab, fresh: monitor.refresh(state, symbol, lab, df, candle_close=True)]


def _fold_quiet(live):
    """Per candle: when an #alerts event fires, the quiet (#updates) labels of the same candle ride along on its card
    instead of being a second message. Yields (bar_time, [(key, event, [(folded_key, folded_event)])])."""
    order, by_bar = [], {}
    for key, bt, e in live:
        if bt not in by_bar:
            order.append(bt); by_bar[bt] = []
        by_bar[bt].append((key, e))
    for bt in order:
        items = by_bar[bt]
        loud = [(k, e) for k, e in items if notifier.CHANNEL.get(e["type"], "alerts") == "alerts"]
        quiet = [(k, e) for k, e in items if notifier.CHANNEL.get(e["type"], "alerts") != "alerts"]
        if loud and quiet:
            yield bt, [(loud[0][0], loud[0][1], quiet)] + [(k, e, []) for k, e in loud[1:]]
        else:
            yield bt, [(k, e, []) for k, e in items]


LABELS: dict = {}                                                  # symbol -> labels.Result for the newest closed bar
LIVE_LABELS = {labels.GAP_WIDENING, labels.PARALLEL, labels.WIDE_GAP, labels.GAP_PEAKED, labels.PULLBACK,
               labels.RE_ENTRY, labels.PULLBACK_FAILED, labels.PRE_CROSS, labels.PRE_CROSS_FAILED}
def _pre_cross_button(symbol, tf, bt, e):
    """A pre-cross is a WATCH signal: its only button is Monitor (no Place, no entry, no SL)."""
    if e["type"] == labels.PRE_CROSS:
        return {"buttons": [notifier.button("Monitor", "monitor", symbol, style="primary")]}
    return {}


EVENT_HOOKS: list = [lambda symbol, tf, bt, e: positions.event_lines(state, symbol, e), _pre_cross_button]
#                        callables (symbol, tf, bar_time, event) -> {"fields": [...], "buttons": [...]}


def update_labels(symbol: str, df) -> "labels.Result":
    """Re-run the label engine over every closed candle we hold. Pure and deterministic, so a restart rebuilds
    exactly the same label, pullback and TSL state."""
    res = labels.run(labels.prepare(df))
    LABELS[symbol], LAST_DF[symbol] = res, df
    return res


LAST_DF: dict = {}                                                 # symbol -> the candles LABELS was computed on


def warm_start():
    """After a (re)start: rebuild labels from candles, re-read positions, apply any pending trailing-stop step.
    Nothing here sends a market order; the stop can only be tightened."""
    for sym in C.enabled_symbols():
        df = signals.fetch(C.TIMEFRAMES[0], symbol=sym)
        if df is not None:
            lab = update_labels(sym, df)
            positions.on_candle(state, sym, df, lab, [])


def _dir(side: int) -> str:
    return "BULL" if side > 0 else "BEAR"


def label_event(le: dict) -> dict:
    """labels.py event -> the bot's event dict (type/dir/price + details)."""
    e = {k: v for k, v in le.items() if k not in ("i", "time", "label", "side")}
    e["type"] = le["label"]
    if le["label"] in (labels.PRE_CROSS, labels.PRE_CROSS_FAILED):
        e["dir"] = _dir(le.get("expect", -le["side"]))               # the cross it is waiting for
        e["episode"] = f"{le['pair']}:{le['time']}"
    else:
        e["dir"] = "LONG" if le["side"] > 0 else "SHORT"
    return e


def candle_events(df, todo: int, tf: str, symbol: str, lab=None):
    """(bar_time, event) for every candle in the last `todo` closed candles, oldest first: the cross and EMA200
    events from signals.detect(), then the labels of that candle."""
    by_time: dict = {}
    for le in (lab.events if lab else []):
        by_time.setdefault(le["time"], []).append(le)
    for back in range(todo - 1, -1, -1):                           # oldest -> newest
        sub = df.iloc[:len(df) - back]
        bar_time = sub["time"].iloc[-1]
        here = by_time.get(bar_time, [])
        cross = next((le for le in here if le["label"] == labels.CROSS), None)
        for e in signals.detect(sub):
            if e["type"].startswith("CROSS") and cross:          # the label engine adds the tag and what finished
                e.update(tag=cross["tag"], lesser_gap=cross["lesser_gap"], finished=cross["finished"])
            yield bar_time, e
        for le in here:
            if le["label"] in LIVE_LABELS:
                yield bar_time, label_event(le)


def market_status(symbol: str) -> str:
    res = LABELS.get(symbol)
    return labels.status_line(res.state) if res else ""


def format_live(tf, bt, e, symbol, missed=False, image=None):
    extra, buttons = [], []
    for hook in EVENT_HOOKS:
        out = hook(symbol, tf, bt, e) or {}
        extra += out.get("fields", [])
        buttons += out.get("buttons", [])
    return notifier.format_event(tf, bt, e, missed=missed, image=image, symbol=symbol, status=market_status(symbol),
                                 extra=extra, buttons=buttons or None)


def record_event(symbol, tf, bt, e):
    store.record("event", {"tf": tf, "bar_time": str(bt), "label": e["type"], "side": e["dir"], "tag": e.get("tag"),
                           "price": float(e["price"]), "gap": e.get("gap"), "atr": e.get("atr"),
                           "pair": e.get("pair")}, symbol=symbol)


def _symbol_arg(req: dict) -> tuple[str | None, str | None]:
    """(symbol, None) or (None, refusal text). No symbol given = the primary symbol."""
    sym = str(req.get("symbol") or C.SYMBOL).upper()
    if sym not in C.SYMBOLS:
        return None, f"unknown symbol {sym!r} (known: {', '.join(C.SYMBOLS)})"
    if not C.is_enabled(sym):
        return None, f"{sym} is disabled in config"
    return sym, None


def chart_reply(symbol: str) -> dict:
    tf = C.TIMEFRAMES[0]
    df, price = signals.fetch(tf, symbol=symbol), signals.live_price(symbol)
    if df is None or price is None:
        return {"ok": False, "text": f"{symbol}: no market data right now."}
    title = f"{symbol} {tf} · now {price:.2f} · {signals.to_ist(signals.server_now()):%d %b %H:%M} IST"
    return {"ok": True, "text": f"**{symbol}** · {signals.ema_status(df, price)}",
            "image": chart.render(df, tf, title, signals.to_ist, price_line=price, symbol=symbol)}


STATUS_LINES: list = [lambda sym: f"Market: {market_status(sym)}" if market_status(sym) else "",
                      lambda sym: monitor.status_line(state, sym)]


def status_reply(symbol: str) -> dict:
    """/status: the /chart output plus version and the state lines features add (labels, monitor)."""
    r = chart_reply(symbol)
    extra = [f"v{C.VERSION} · server clock {signals.offset_text()}"] + [f(symbol) for f in STATUS_LINES]
    r["text"] = r["text"] + "\n" + "\n".join(x for x in extra if x)
    return r


def _bars_of(symbol: str):
    df = signals.fetch(C.TIMEFRAMES[0], symbol=symbol)
    return labels.prepare(df) if df is not None else None


def _trade_user(req: dict) -> str | None:
    if req.get("user") not in C.DISCORD_ALLOWED_USER_IDS:
        return "⛔ NOT done — this user is not in DISCORD_ALLOWED_USER_IDS."
    return None


def _close_cmd(req: dict) -> dict:
    why = _trade_user(req)
    if why:
        return {"ok": False, "text": why}
    reply = trader.close_position(req)
    log.info("CLOSE %s by %s -> %s", req.get("ticket"), req.get("user"), reply["text"].replace("\n", " | "))
    return reply


def _apply_cmd(req: dict) -> dict:
    why = _trade_user(req)
    return {"ok": False, "text": why} if why else positions.apply(state, req)


JSON_COMMANDS: dict = {                                            # name -> handler(req); features add more
    "positions": lambda req: positions.report(state, LABELS, _bars_of),
    "keep": lambda req: positions.keep(state, req),
    "apply": _apply_cmd,
    "close": _close_cmd,
    "monitor": lambda req: _with_symbol(req, lambda s: monitor.start(state, s, req.get("user"))),
    "unmonitor": lambda req: _with_symbol(req, lambda s: monitor.stop(state, s)),
}


def _with_symbol(req: dict, fn) -> dict:
    sym, why = _symbol_arg(req)
    return {"ok": False, "text": why} if why else fn(sym)


def refresh_monitors():
    for sym in C.enabled_symbols():
        if monitor.active(state, sym):
            monitor.refresh(state, sym, LABELS.get(sym), LAST_DF.get(sym))
PLACE_GATES: list = [weekend.place_gate]                           # callables req -> refusal text or None


def handle_json_command(req: dict) -> dict:
    """Commands from the Discord slash commands and buttons. Runs in the bot loop (the only MT5 thread)."""
    reply = _handle_json_command(req)
    store.record("command", {"name": req.get("cmd"), "arguments": {k: v for k, v in req.items()
                                                                   if k not in ("cmd", "ts", "id", "user")},
                             "user": req.get("user"), "outcome": "ok" if reply.get("ok") else "refused",
                             "reply": str(reply.get("text", ""))[:300]},
                 symbol=req.get("symbol"))
    return reply


def _handle_json_command(req: dict) -> dict:
    cmd = req.get("cmd")
    age = signals.utc_now().timestamp() - float(req.get("ts", 0))
    if age > C.COMMAND_MAX_AGE:
        return {"ok": False, "text": f"⌛ NOT done — the command was {age:.0f} s old when the bot saw it."}
    if cmd in ("chart", "status"):
        if cmd == "status" and not req.get("symbol"):           # one card per enabled symbol
            cards = [status_reply(s) for s in C.enabled_symbols()]
            return {"ok": all(c["ok"] for c in cards), "text": "\n\n".join(c["text"] for c in cards),
                    "images": [c["image"] for c in cards if c.get("image")],
                    "image": next((c["image"] for c in cards if c.get("image")), None)}
        sym, why = _symbol_arg(req)
        if why:
            return {"ok": False, "text": why}
        return chart_reply(sym) if cmd == "chart" else status_reply(sym)
    if cmd in JSON_COMMANDS:
        return JSON_COMMANDS[cmd](req)
    if cmd in ("quote", "place"):
        if req.get("user") not in C.DISCORD_ALLOWED_USER_IDS:      # checked again here, not only in Discord
            log.warning("order command from a user who is not allowed: %r", req.get("user"))
            return {"ok": False, "text": "⛔ NOT placed — this user is not in DISCORD_ALLOWED_USER_IDS."}
        if req.get("symbol"):
            sym, why = _symbol_arg(req)
            if why:
                return {"ok": False, "text": f"❌ NOT placed — {why}."}
        for gate in PLACE_GATES:                                    # e.g. the Friday-close block (weekend.py)
            why = gate(req)
            if why:
                return {"ok": False, "text": f"❌ NOT placed — {why}"}
    if cmd == "quote":
        return trader.preview(req)
    if cmd == "place":
        reply = trader.place(req)
        log.info("PLACE by %s -> %s", req.get("user"), reply["text"].replace("\n", " | "))
        return reply
    return {"ok": False, "text": f"unknown command {cmd!r}"}


def process_commands():
    for reply in alerts.process_inbox(json_handler=handle_json_command,
                                      price_of=lambda sym: signals.live_price(sym)):
        log.info("command -> %s", reply[:200].replace("\n", " | "))    # new alerts are armed at the current price


def nap(seconds: float):
    """Sleep, but wake at once when a command (/place, /chart, /alert) is waiting."""
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0 or alerts.inbox_waiting():
            return
        time.sleep(min(0.25, left))


def check_price_alerts(tf: str, symbol: str | None = None):
    """Price alerts on every enabled symbol (or just `symbol`)."""
    for sym in ([symbol] if symbol else C.enabled_symbols()):
        if C.is_enabled(sym):
            _check_price_alerts(tf, sym)


def _per_symbol(name: str) -> dict:
    d = state.d.get(name)
    if not isinstance(d, dict):
        d = state.d[name] = {}
    return d


def _check_price_alerts(tf: str, symbol: str):
    now_srv = signals.server_now()
    since = state.d["last_price_check"].get(symbol)
    since_dt = pd.Timestamp(since).to_pydatetime() if since else None
    rng = signals.price_range_since(since_dt, symbol)
    if rng is None:
        return
    last, gap = rng["last"], rng["gap"]
    retries = _per_symbol("price_hist_retries")
    if gap:
        tries = retries.get(symbol, 0) + 1
        if tries < C.PRICE_HISTORY_RETRIES:                      # keep the checkpoint, try again next loop
            retries[symbol] = tries; state.save()
            log.warning("%s price history incomplete (%s), retry %d/%d", symbol, gap, tries, C.PRICE_HISTORY_RETRIES)
            return
        notices = _per_symbol("price_gap_notice_ts")
        if time.time() - notices.get(symbol, 0) > 3600:          # at most once an hour
            notices[symbol] = time.time()
            notifier.deliver(state, notifier.warn("price alerts: coverage incomplete",
                                                  f"{gap}\nA touch in that window may have been missed — check the chart.",
                                                  symbol=symbol))
    retries[symbol] = 0
    offset_s = signals._server_offset.total_seconds()
    now_real = signals.utc_now().timestamp()
    fresh = {}                                                     # new alerts: exact tick range since creation
    for a in alerts.pending(symbol):
        created = a.get("created")
        if created and now_real - created < C.NEW_ALERT_TICK_SECONDS:
            rng_ticks = signals.bid_range_since(signals.stamp_to_dt(created + offset_s), symbol)
            if rng_ticks:
                fresh[a["level"]] = rng_ticks
    triggered = alerts.check(last, m1=rng["m1"], server_offset_s=offset_s, tick_ranges=fresh, symbol=symbol)
    state.d["last_price_check"][symbol] = str(now_srv); state.save()   # ... THEN move the checkpoint
    if not triggered:
        return
    df = signals.fetch(tf, symbol=symbol)
    status = signals.ema_status(df, last) if df is not None else "EMA status unavailable"
    for t in triggered:
        text = notifier.format_price_alert(t["level"], t["price"], status, signals.server_now(), symbol=symbol)
        st = notifier.send(text)
        if any(v == "ok" for v in st.values()):
            owed = notifier.failed_services(st)
            if owed:
                state.queue(text, owed)                            # the service that missed it gets a retry
            alerts.ack(t["level"], symbol)
        # all failed: do NOT queue — the 'triggered' record itself is the retry, so no duplicates


def check_time():
    """`python bot.py --check-time`: compare inferred broker offset with your terminal, then pin it in config."""
    if not mt5.initialize():
        sys.exit(f"MT5 init failed: {mt5.last_error()}")
    mt5.symbol_select(C.broker_symbol(C.SYMBOL), True)
    tick = mt5.symbol_info_tick(C.broker_symbol(C.SYMBOL))
    if tick is None:
        sys.exit(f"No tick for {C.SYMBOL}")
    signals.sync_server_offset()
    configured = signals.offset_text()
    print("\nWatching the feed for ~40 s ...")
    for _ in range(8):                                             # let observe_clock measure from live ticks
        time.sleep(5)
        tick = mt5.symbol_info_tick(C.broker_symbol(C.SYMBOL)) or tick
        signals.observe_clock(tick)
    raw = signals.stamp_to_dt(tick.time)
    feed = (tick.time - signals.utc_now().timestamp()) / 3600
    print(f"Tick time as MT5 stamps it : {raw:%Y-%m-%d %H:%M:%S}   <- compare with MT5 Market Watch")
    print(f"Real UTC now               : {signals.utc_now():%Y-%m-%d %H:%M:%S}")
    print(f"Config says                : {configured}")
    print(f"Feed says (raw)            : UTC{feed:+.2f} h   (only meaningful while the market is open)")
    print(f"Bot is using               : {signals.offset_text()}")
    print(f"IST conversion of tick     : {signals.to_ist(raw):%H:%M}   <- must equal your wall clock now")
    print("\n'measured' above = the feed disagreed with config and the bot corrected itself; update config to match.\n")
    mt5.shutdown()


def main():
    if "--check-time" in sys.argv:
        check_time(); return
    print_banner()
    problems = C.validate()
    if not problems and mt5.initialize():                          # symbols must exist and be in Market Watch
        problems = C.validate(symbol_check=signals.symbol_problem)
        mt5.shutdown()
    if problems:
        sys.exit("Config problems:\n  - " + "\n  - ".join(problems))
    log.info("Starting v%s %s EMA %d/%d/%d on %s | delivering to %s | replay up to %d bars",
             C.VERSION, syms(), C.EMA_FAST, C.EMA_SLOW, C.EMA_TREND, C.TIMEFRAMES, notifier.configured(), REPLAY_CAP)
    if state.problem:
        notifier.deliver(state, notifier.warn("State recovered", state.problem))
    log.info("Data folder: %s", C.DATA_DIR)
    chart.prune()
    if alerts.start_discord_listener():
        notifier.BOT_CLIENT["on"] = True
        log.info("Discord commands starting: /chart /alert /alerts and /place (%s)", "ON" if C.PLACE_ENABLED else "off")
    backoff, announced, last_flush, warmed = 5, False, 0.0, False
    while True:
        try:
            if weekend_pause():
                notifier.flush_pending(state)
                continue
            problem = ensure_mt5()
            if problem:
                report_blind(problem)
                notifier.flush_pending(state)
                wait = C.POLL_SECONDS if problem.startswith(KEEP_CONNECTION) else backoff
                if wait == backoff:
                    backoff = min(backoff * 2, 60)                 # never more than 1 min from noticing the feed is back
                heartbeat(wait); time.sleep(wait); continue
            backoff = 5
            report_recovered()
            if not warmed:
                warm_start(); warmed = True
            note = signals.pop_clock_note()
            if note:
                notifier.deliver(state, notifier.warn("MT5 clock corrected", note, symbol=syms()))
            for sym in C.enabled_symbols():                        # signals first: nothing may delay a fresh candle
                for tf in C.TIMEFRAMES:
                    check_ema(tf, sym)
            process_commands()
            positions.track(state, LABELS)                         # opened / closed / MAX POINT giveback
            refresh_monitors()
            weekend.check(state)                                   # Friday warnings (warn only, never closes)
            check_price_alerts(C.TIMEFRAMES[0])
            if not announced:
                announce_start(); announced = True
            alive_ping()
            if state.d["pending"] and time.time() - last_flush >= 30:    # retries after the live work, twice a minute
                last_flush = time.time(); notifier.flush_pending(state)
            wait = next_sleep()
            heartbeat(wait + 30)
            nap(wait)
        except KeyboardInterrupt:
            log.info("Stopped"); mt5.shutdown(); break
        except Exception:
            log.exception("Loop error, retrying")
            mt5.shutdown()
            heartbeat(backoff)
            time.sleep(backoff); backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
