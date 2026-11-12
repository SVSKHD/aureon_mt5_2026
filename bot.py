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
import signals, notifier, alerts, chart, trader

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


def print_banner():
    os.system("")                                 # enable ANSI colours on Windows
    print(f"{GREEN}{BANNER}   {C.SYMBOL} · EMA {C.EMA_FAST}/{C.EMA_SLOW}/{C.EMA_TREND} · {'/'.join(C.TIMEFRAMES)}{RESET}\n")


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
        notifier.deliver(state, notifier.card(f"⏸️ {C.SYMBOL} paused for the weekend",
                                              f"Resumes {resume:%a %d %b %H:%M} IST", "INFO"))
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
        mt5.symbol_select(C.SYMBOL, True)
        signals.sync_server_offset()
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
        notifier.deliver(state, notifier.warn(f"{C.SYMBOL} bot is BLIND — no alerts until this clears",
                                              f"{problem}\nSince {mins:.0f} min. Check the MT5 terminal / internet.", ping=True))


def report_recovered():
    if _blind["notified"]:
        mins = (time.time() - _blind["since"]) / 60
        notifier.deliver(state, notifier.card(f"✅ {C.SYMBOL} feed is back", f"Was blind {mins:.0f} min — missed candles are being replayed.", "INFO"))
    _blind["since"], _blind["notified"] = None, False


def _status_now(tf: str) -> str:
    df = signals.fetch(tf)
    price = signals.live_price()
    return signals.ema_status(df, price) if df is not None and price else "EMA status unavailable"


def announce_start():
    """One 'online' card per (re)start, rate-limited so a crash loop cannot flood."""
    today = f"ALIVE:{signals.utc_now().astimezone(TZ):%Y-%m-%d}"
    if time.time() - state.d.get("last_start_notice", 0) >= C.START_NOTICE_COOLDOWN:
        state.d["last_start_notice"] = time.time(); state.save()
        notifier.deliver(state, notifier.card(f"▶️ {C.SYMBOL} bot online", color="INFO",
                                              fields=[("Now", _status_now(C.TIMEFRAMES[0]), False),
                                                      ("Server clock", signals.offset_text())]))
    if not state.seen(today):
        state.mark(today)


def alive_ping():
    if not C.ALIVE_PING_IST:
        return
    now = signals.utc_now().astimezone(TZ)
    key = f"ALIVE:{now:%Y-%m-%d}"
    if f"{now:%H:%M}" >= C.ALIVE_PING_IST and not state.seen(key):
        notifier.deliver(state, notifier.card(f"✅ {C.SYMBOL} bot alive", color="INFO",
                                              fields=[("Now", _status_now(C.TIMEFRAMES[0]), False)]))
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


def check_ema(tf: str):
    last_done = state.d["last_bar"].get(tf)
    since = pd.Timestamp(last_done).to_pydatetime() if last_done else None
    if since and signals.server_now() < since + timedelta(minutes=2 * C.BAR_MINUTES[tf]):
        return                                                     # the next candle has not closed yet
    df = signals.fetch(tf, since=since)
    if df is None:
        log.warning("%s: no data", tf); return
    newest = df["time"].iloc[-1]
    if since and since >= newest.to_pydatetime():
        return                                                     # nothing new since checkpoint

    todo = int((df["time"] > since).sum()) if since else C.DEFAULT_LOOKBACK_BARS + 1
    todo = min(todo, len(df) - C.EMA_TREND, REPLAY_CAP)
    first_eval = df["time"].iloc[len(df) - todo].to_pydatetime()     # first candle actually evaluated

    if since:
        clamped = df.attrs.get("clamped_from")
        if clamped:                                                # we chose not to fetch that far back
            notifier.deliver(state, notifier.warn(f"{C.SYMBOL} {tf}: outage longer than replay limit",
                                                  f"NOT replayed: {signals.fmt_times(clamped)} → {signals.fmt_times(first_eval)}\nReplaying from there."))
        elif first_eval > since + timedelta(minutes=C.BAR_MINUTES[tf]):   # broker history too short
            notifier.deliver(state, notifier.warn(f"{C.SYMBOL} {tf}: MT5 history too short",
                                                  f"NOT replayed: {signals.fmt_times(since)} → {signals.fmt_times(first_eval)}\nRaise 'Max bars in chart' in MT5."))

    # ---- pass 1: detect everything, nothing persisted yet ----
    bar_min, now_srv = C.BAR_MINUTES[tf], signals.server_now()
    live, missed, missed_bars = [], [], 0                        # (key, bar_time, event)
    seen_now = set()                                               # episode-keyed events: once per replay too
    for back in range(todo - 1, -1, -1):                           # oldest -> newest
        sub = df.iloc[:len(df) - back]
        bar_time = sub["time"].iloc[-1]
        bar_close = bar_time.to_pydatetime() + timedelta(minutes=bar_min)
        is_missed = (now_srv - bar_close).total_seconds() > C.MISSED_GRACE_SECONDS
        missed_bars += is_missed
        for e in signals.detect(sub):
            key = f"{C.SYMBOL}:{tf}:{e['type']}:{e['dir']}:{e.get('episode', str(bar_time))}"
            if state.seen(key) or key in seen_now:
                continue
            seen_now.add(key)
            (missed if is_missed and todo > 2 else live).append((key, bar_time, e))

    # ---- pass 2: deliver (or durably queue) FIRST, then mark keys and advance checkpoint ----
    if missed:
        lines = [notifier.digest_line(tf, bt, e) for _, bt, e in missed]
        for key, bt, e in missed:
            notifier._logfile(notifier.format_event(tf, bt, e, missed=True), key)
        price = signals.live_price() or float(df["close"].iloc[-1])
        last_missed_bar = missed[-1][1]
        notifier.deliver(state, notifier.format_digest(tf, lines, df["time"].iloc[len(df) - todo], last_missed_bar,
                                                       missed_bars, signals.ema_status(df, price)))
        for key, _, _ in missed:                                   # digest is sent or in the retry queue -> safe to mark
            state.mark(key)
    elif missed_bars and todo > 2:
        log.info("%s: %d missed bars, no signals in them", tf, missed_bars)

    charts = {}                                                    # one picture per candle, shared by its events
    for key, bt, e in live:
        image = None
        if C.CHART_ENABLED and e["type"] in C.PING_TYPES:
            if bt not in charts:
                charts[bt] = chart.render(df[df["time"] <= bt], tf, notifier.event_caption(tf, bt, e), signals.to_ist,
                                          mark_last=e["dir"])
            image = charts[bt]
        notifier.deliver(state, notifier.format_event(tf, bt, e, missed=(todo <= 2 and bt != newest), image=image), key=key)
        state.mark(key)
    state.checkpoint(tf, newest)


def handle_json_command(req: dict) -> dict:
    """chart / quote / place, sent by the Discord slash commands. Runs in the bot loop (the only MT5 thread)."""
    cmd = req.get("cmd")
    age = signals.utc_now().timestamp() - float(req.get("ts", 0))
    if age > C.COMMAND_MAX_AGE:
        return {"ok": False, "text": f"⌛ NOT done — the command was {age:.0f} s old when the bot saw it."}
    if cmd == "chart":
        tf = C.TIMEFRAMES[0]
        df, price = signals.fetch(tf), signals.live_price()
        if df is None or price is None:
            return {"ok": False, "text": "No market data right now."}
        title = f"{C.SYMBOL} {tf} · now {price:.2f} · {signals.to_ist(signals.server_now()):%d %b %H:%M} IST"
        return {"ok": True, "text": signals.ema_status(df, price),
                "image": chart.render(df, tf, title, signals.to_ist, price_line=price)}
    if cmd in ("quote", "place"):
        if req.get("user") not in C.DISCORD_ALLOWED_USER_IDS:      # checked again here, not only in Discord
            log.warning("order command from a user who is not allowed: %r", req.get("user"))
            return {"ok": False, "text": "⛔ NOT placed — this user is not in DISCORD_ALLOWED_USER_IDS."}
    if cmd == "quote":
        return trader.preview(req)
    if cmd == "place":
        reply = trader.place(req)
        log.info("PLACE by %s -> %s", req.get("user"), reply["text"].replace("\n", " | "))
        return reply
    return {"ok": False, "text": f"unknown command {cmd!r}"}


def process_commands():
    for reply in alerts.process_inbox(price=signals.live_price(), json_handler=handle_json_command):
        log.info("command -> %s", reply[:200].replace("\n", " | "))    # new alerts are armed at the current price


def nap(seconds: float):
    """Sleep, but wake at once when a command (/place, /chart, /alert) is waiting."""
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0 or alerts.inbox_waiting():
            return
        time.sleep(min(0.25, left))


def check_price_alerts(tf: str):
    now_srv = signals.server_now()
    since = state.d.get("last_price_check")
    since_dt = pd.Timestamp(since).to_pydatetime() if since else None
    rng = signals.price_range_since(since_dt)
    if rng is None:
        return
    last, gap = rng["last"], rng["gap"]
    if gap:
        tries = state.d.get("price_hist_retries", 0) + 1
        if tries < C.PRICE_HISTORY_RETRIES:                      # keep the checkpoint, try again next loop
            state.d["price_hist_retries"] = tries; state.save()
            log.warning("price history incomplete (%s), retry %d/%d", gap, tries, C.PRICE_HISTORY_RETRIES)
            return
        if time.time() - state.d.get("price_gap_notice_ts", 0) > 3600:      # at most once an hour
            state.d["price_gap_notice_ts"] = time.time()
            notifier.deliver(state, notifier.warn(f"{C.SYMBOL} price alerts: coverage incomplete",
                                                  f"{gap}\nA touch in that window may have been missed — check the chart."))
    state.d["price_hist_retries"] = 0
    offset_s = signals._server_offset.total_seconds()
    now_real = signals.utc_now().timestamp()
    fresh = {}                                                     # new alerts: exact tick range since creation
    for a in alerts.pending():
        created = a.get("created")
        if created and now_real - created < C.NEW_ALERT_TICK_SECONDS:
            rng_ticks = signals.bid_range_since(signals.stamp_to_dt(created + offset_s))
            if rng_ticks:
                fresh[a["level"]] = rng_ticks
    triggered = alerts.check(last, m1=rng["m1"], server_offset_s=offset_s, tick_ranges=fresh)   # persists 'triggered' first ...
    state.d["last_price_check"] = str(now_srv); state.save()           # ... THEN move the checkpoint
    if not triggered:
        return
    df = signals.fetch(tf)
    status = signals.ema_status(df, last) if df is not None else "EMA status unavailable"
    for t in triggered:
        text = notifier.format_price_alert(t["level"], t["price"], status, signals.server_now())
        st = notifier.send(text)
        if any(v == "ok" for v in st.values()):
            owed = notifier.failed_services(st)
            if owed:
                state.queue(text, owed)                            # the service that missed it gets a retry
            alerts.ack(t["level"])
        # all failed: do NOT queue — the 'triggered' record itself is the retry, so no duplicates


def check_time():
    """`python bot.py --check-time`: compare inferred broker offset with your terminal, then pin it in config."""
    if not mt5.initialize():
        sys.exit(f"MT5 init failed: {mt5.last_error()}")
    mt5.symbol_select(C.SYMBOL, True)
    tick = mt5.symbol_info_tick(C.SYMBOL)
    if tick is None:
        sys.exit(f"No tick for {C.SYMBOL}")
    signals.sync_server_offset()
    configured = signals.offset_text()
    print("\nWatching the feed for ~40 s ...")
    for _ in range(8):                                             # let observe_clock measure from live ticks
        time.sleep(5)
        tick = mt5.symbol_info_tick(C.SYMBOL) or tick
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
    if problems:
        sys.exit("Config problems:\n  - " + "\n  - ".join(problems))
    log.info("Starting %s EMA %d/%d/%d on %s | delivering to %s | replay up to %d bars",
             C.SYMBOL, C.EMA_FAST, C.EMA_SLOW, C.EMA_TREND, C.TIMEFRAMES, notifier.configured(), REPLAY_CAP)
    if state.problem:
        notifier.deliver(state, notifier.warn("State recovered", state.problem))
    log.info("Data folder: %s", C.DATA_DIR)
    chart.prune()
    if alerts.start_discord_listener():
        log.info("Discord commands starting: /chart /alert /alerts and /place (%s)", "ON" if C.PLACE_ENABLED else "off")
    backoff, announced, last_flush = 5, False, 0.0
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
            note = signals.pop_clock_note()
            if note:
                notifier.deliver(state, notifier.warn(f"{C.SYMBOL} MT5 clock corrected", note))
            for tf in C.TIMEFRAMES:                                # signals first: nothing may delay a fresh candle
                check_ema(tf)
            process_commands()
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
