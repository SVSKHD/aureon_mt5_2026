"""Weekend rule (spec section 10): positions may be held overnight on weekdays, not over the weekend.

WARN ONLY. NEVER CLOSES. From Friday WEEKEND_WARN_FROM (default 20:00) in WEEKEND_WARN_TZ until the account is flat
or the market closes, and only while a position is open: hourly, then every 15 minutes in the final hour. Each
warning is a new ping listing every open position with profit, SL and time left. When the account goes flat: one
"flat for the weekend" confirmation, then silence. /place is refused in the last WEEKEND_BLOCK_PLACE_MIN minutes.
On Monday, anything carried over is reported once.

The Friday close: the MetaTrader5 Python API exposes no trading-session schedule, so it is LEARNED from the
broker's own data - the time of the last M1 candle of the previous Friday, plus one minute (broker server time,
which follows the broker's DST). Only if that history is missing is WEEKEND_CLOSE_FALLBACK used, and the card
says "estimated".
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import MetaTrader5 as mt5
import config as C
import signals, notifier, store

log = logging.getLogger("weekend")
WARN_TZ = C.env("WEEKEND_WARN_TZ", "Asia/Kolkata")
WARN_FROM = C.env("WEEKEND_WARN_FROM", "20:00")                 # Friday, in WARN_TZ
FINAL_MINUTES = 60                                              # 15-minute cadence inside this
try:
    BLOCK_PLACE_MIN = float(C.env("WEEKEND_BLOCK_PLACE_MIN", "60"))   # 0 disables
except ValueError:
    raise SystemExit("WEEKEND_BLOCK_PLACE_MIN in .env must be a number of minutes")
CLOSE_FALLBACK = C.env("WEEKEND_CLOSE_FALLBACK", "23:57")      # server time, used only without broker history


def _book(state) -> dict:
    return state.d.setdefault("weekend", {})


def _srv_to_utc(srv: datetime) -> datetime:
    return (srv - signals._server_offset).replace(tzinfo=timezone.utc)


def _this_friday(now_srv: datetime) -> datetime:
    """The Friday (server date) of the current trading week; on Sat/Sun the Friday just gone."""
    d = now_srv.date()
    back = (d.weekday() - 4) % 7 if d.weekday() >= 4 else -(4 - d.weekday())
    return datetime.combine(d - timedelta(days=back), datetime.min.time())


def friday_close(symbol: str, state=None, fri: datetime | None = None) -> tuple[datetime, bool]:
    """(close time in server time for this week's Friday - or the Friday `fri` - , learned_from_broker)."""
    fri = fri or _this_friday(signals.server_now())
    cache = _book(state).setdefault("close", {}) if state is not None else {}
    ck = f"{symbol}:{fri:%Y-%m-%d}"
    if ck in cache:
        return datetime.fromisoformat(cache[ck]["t"]), cache[ck]["learned"]
    prev = fri - timedelta(days=7)
    learned = False
    t = None
    try:
        rates = mt5.copy_rates_range(C.broker_symbol(symbol), mt5.TIMEFRAME_M1, signals._mt5_dt(prev),
                                     signals._mt5_dt(prev + timedelta(days=1, hours=6)))
        if rates is not None and len(rates):
            last = signals.stamp_to_dt(rates["time"][-1]) + timedelta(minutes=1)
            if last.date() in (prev.date(), (prev + timedelta(days=1)).date()):
                t = fri + (last - prev)                      # same time of day (may run past midnight)
                learned = True
    except Exception:
        log.exception("could not read last Friday's candles for %s", symbol)
    if t is None:
        h, m = (int(x) for x in CLOSE_FALLBACK.split(":"))
        t = fri.replace(hour=h, minute=m)
    if state is not None:
        cache[ck] = {"t": t.isoformat(), "learned": learned}
    return t, learned


def _window_start_utc(fri_srv: datetime) -> datetime:
    """Friday WARN_FROM in WARN_TZ, as real UTC, for the Friday of fri_srv's week."""
    tz = ZoneInfo(WARN_TZ)
    h, m = (int(x) for x in WARN_FROM.split(":"))
    local = datetime(fri_srv.year, fri_srv.month, fri_srv.day, h, m, tzinfo=tz)
    return local.astimezone(timezone.utc)


def _positions():
    allpos = mt5.positions_get()
    return list(allpos) if allpos is not None else None


def minutes_to_close(symbol: str, state=None) -> float | None:
    """Minutes until this Friday's close, or None outside Friday's warning window."""
    close_srv, _ = friday_close(symbol, state)
    now = signals.utc_now()
    start = _window_start_utc(close_srv)
    close = _srv_to_utc(close_srv)
    if not (start <= now < close):
        return None
    return (close - now).total_seconds() / 60


def check(state) -> None:
    """Every loop pass. Warns, confirms flat, reports Monday carry-over. Never sends an order."""
    pos = _positions()
    if pos is None:
        return
    _monday_report(state, pos)
    syms = C.enabled_symbols()
    if not syms:
        return
    close_srv, learned = min((friday_close(s, state) for s in syms), key=lambda x: x[0])
    now = signals.utc_now()
    start, close = _window_start_utc(close_srv), _srv_to_utc(close_srv)
    if not (start <= now < close):
        return
    wk = _book(state).setdefault(f"{close_srv:%Y-%m-%d}", {"warned": 0, "flat_sent": False})
    left = (close - now).total_seconds() / 60
    if not pos:
        if wk["warned"] and not wk["flat_sent"]:
            wk["flat_sent"] = True
            notifier.deliver(state, notifier.info("✅ flat for the weekend", "No open positions. Enjoy the weekend.",
                                                  symbol="/".join(syms), channel="alerts", kind="WEEKEND_FLAT"),
                             key=f"WEEKEND_FLAT:{close_srv:%Y-%m-%d}")
            state.save()
        return
    if left > FINAL_MINUTES:
        slot = f"H{int((now - start).total_seconds() // 3600)}"
    else:
        slot = f"Q{int((FINAL_MINUTES - left) // 15)}"
    key = f"WEEKEND:{close_srv:%Y-%m-%d}:{slot}"
    if state.seen(key):
        return
    lines = []
    for p in pos:
        side = 1 if p.type == mt5.ORDER_TYPE_BUY else -1
        sym = C.logical_symbol(p.symbol) or p.symbol
        tick = mt5.symbol_info_tick(p.symbol)
        cur = (tick.bid if side > 0 else tick.ask) if tick else p.price_current
        lines.append(f"**{sym} {'LONG' if side > 0 else 'SHORT'} {p.volume:g}** #{p.ticket} · "
                     f"P/L {side * (cur - p.price_open):+.2f} pts / {getattr(p, 'profit', 0):+,.2f} · "
                     f"SL {f'{p.sl:.2f}' if p.sl else 'NONE'}")
    when = signals.to_ist(close_srv)
    desc = "\n".join(lines) + (f"\n\nMarket closes {when:%a %H:%M} IST — **{_fmt_left(left)} left**"
                               + ("" if learned else " (close time estimated: no broker history)")
                               + "\nPositions are not held over the weekend. The bot will NOT close them.")
    wk["warned"] += 1
    notifier.deliver(state, notifier.warn(f"WEEKEND: {len(pos)} position(s) still open", desc, ping=True,
                                          symbol="/".join(syms), kind="WEEKEND_WARNING"), key=key)
    state.mark(key)
    store.record("system", {"event": "weekend_warning", "detail": f"{len(pos)} open, {left:.0f} min left"})


def _fmt_left(mins: float) -> str:
    mins = int(round(mins))
    return f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min"


def _monday_report(state, pos) -> None:
    """Once per week, after the market reopens: positions opened before last Friday's close."""
    now = signals.server_now()
    if now.weekday() != 0:
        return
    fri = _this_friday(now - timedelta(days=3))            # the Friday before this Monday
    key = f"WEEKEND_CARRIED:{fri:%Y-%m-%d}"
    if state.seen(key):
        return
    syms = C.enabled_symbols()
    close_srv = min(friday_close(s, state, fri)[0] for s in syms)
    if now < close_srv + timedelta(hours=24):
        return                                             # not reopened yet
    carried = [p for p in pos if signals.stamp_to_dt(p.time) < close_srv]
    state.mark(key)
    if not carried:
        return
    lines = [f"**{C.logical_symbol(p.symbol) or p.symbol} {'LONG' if p.type == mt5.ORDER_TYPE_BUY else 'SHORT'} "
             f"{p.volume:g}** #{p.ticket} · entry {p.price_open:.2f} · P/L {getattr(p, 'profit', 0):+,.2f} · "
             f"SL {f'{p.sl:.2f}' if p.sl else 'NONE'}" for p in carried]
    notifier.deliver(state, notifier.warn(f"{len(carried)} position(s) were carried over the weekend",
                                          "\n".join(lines), ping=True, symbol="/".join(syms), channel="positions",
                                          kind="WEEKEND_CARRIED"), key=key)


def place_gate(req: dict) -> str | None:
    """/place refusal in the last BLOCK_PLACE_MIN minutes before the Friday close (0 = off)."""
    if not BLOCK_PLACE_MIN:
        return None
    sym = str(req.get("symbol") or C.SYMBOL).upper()
    if sym not in C.SYMBOLS:
        return None
    left = minutes_to_close(sym)
    if left is not None and left <= BLOCK_PLACE_MIN:
        return (f"the market closes for the weekend in {_fmt_left(left)}; new orders are blocked in the last "
                f"{BLOCK_PLACE_MIN:g} min (WEEKEND_BLOCK_PLACE_MIN).")
    return None
