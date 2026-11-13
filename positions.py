"""Open positions: tracking, the stepped trailing stop, in-trade alerts and /positions (spec section 7).

The bot NEVER opens and NEVER closes a position. The only order action it takes without a tap is tightening a
stop-loss, and that goes through trader.modify_sl (the only module that sends order requests), which refuses to
widen or remove a stop and verifies the result by reading the position back. A failed move is reported once
and that step is not retried.

Runs in the bot loop (the only MT5 thread). Per-position memory lives in state.d["positions"][ticket]; the
trailing-stop levels themselves are recomputed from closed candles every time (labels.tsl_track), so a restart
in the middle of a pullback still fires the pending step.
"""
from __future__ import annotations
import logging, time
from datetime import datetime, timedelta
import MetaTrader5 as mt5
import config as C
import signals, notifier, store, trader, labels

log = logging.getLogger("positions")
REASONS = {0: "manual (terminal)", 1: "manual (mobile)", 2: "manual (web)", 3: "expert/script", 4: "SL hit",
           5: "TP hit", 6: "stop-out", 7: "rollover", 8: "variation margin", 9: "split"}
OPEN: dict = {}                                     # symbol -> [position objects] from the latest read
TSL_MODE_DEFAULT = "auto"


def tsl_mode() -> str:
    m = (C.env("TSL_MODE", TSL_MODE_DEFAULT) or TSL_MODE_DEFAULT).lower()
    return m if m in ("auto", "suggest") else "auto"


def _book(state) -> dict:
    return state.d.setdefault("positions", {})


def _side(pos) -> int:
    return 1 if pos.type == mt5.ORDER_TYPE_BUY else -1


def _pts(x: float) -> str:
    return f"{x:+.2f}"


def _sym(pos) -> str | None:
    return C.logical_symbol(pos.symbol)


def _atr(symbol: str, labs: dict) -> float | None:
    res = labs.get(symbol)
    return (res.state.last or {}).get("atr") if res else None


def _money(pos, price: float) -> float | None:
    try:
        typ = mt5.ORDER_TYPE_BUY if _side(pos) > 0 else mt5.ORDER_TYPE_SELL
        return mt5.order_calc_profit(typ, pos.symbol, pos.volume, pos.price_open, price)
    except Exception:
        return None


def _cur(pos) -> float:
    """The price the position would close at now."""
    tick = mt5.symbol_info_tick(pos.symbol)
    if tick is None:
        return float(getattr(pos, "price_current", pos.price_open))
    return tick.bid if _side(pos) > 0 else tick.ask


def _send(state, msg, key: str | None = None):
    notifier.deliver(state, msg, key=key)
    if key:
        state.mark(key)


# ------------------------------------------------------------------------------------------- tracking
def track(state, labs: dict) -> None:
    """Every loop: read the account's positions, announce new ones, report closed ones, watch MAX POINT giveback."""
    allpos = mt5.positions_get()
    if allpos is None:
        return                                          # cannot read: say nothing, decide nothing (fail closed)
    book = _book(state)
    seen = set()
    OPEN.clear()
    for pos in allpos:
        sym = _sym(pos)
        if sym is None or not C.is_enabled(sym):
            key = f"POS_DISABLED:{pos.ticket}"
            if not state.seen(key):
                _send(state, notifier.warn(f"position {pos.ticket} on a disabled symbol, NOT managed",
                                           f"{pos.symbol} {'BUY' if _side(pos) > 0 else 'SELL'} {pos.volume:g} lot. "
                                           f"No trailing stop, no in-trade alerts. Manage it in MT5.",
                                           symbol=sym or pos.symbol, channel="positions", ping=True,
                                           kind="POSITION_UNMANAGED"), key)
            continue
        OPEN.setdefault(sym, []).append(pos)
        t = str(pos.ticket)
        seen.add(t)
        if t not in book:
            _opened(state, pos, sym, labs)
        _watch_giveback(state, pos, sym, labs)
    for t in [t for t in book if t not in seen]:
        _closed(state, t)
    state.save()


def _opened(state, pos, sym: str, labs: dict):
    side = _side(pos)
    rec = {"ticket": pos.ticket, "symbol": sym, "side": side, "volume": pos.volume, "entry": float(pos.price_open),
           "open_time": int(pos.time), "sl": float(pos.sl or 0), "tp": float(pos.tp or 0), "steps": 0,
           "peak": float(pos.price_open), "maxpoint_peak": None, "against": False, "close_tries": 0,
           "status_at_entry": _status(sym, labs)}
    _book(state)[str(pos.ticket)] = rec
    risk_pts = side * (pos.price_open - pos.sl) if pos.sl else None
    risk_money = _money(pos, pos.sl) if pos.sl else None
    fields = [("Ticket", str(pos.ticket)), ("Lot", f"{pos.volume:g}"), ("Entry", f"{pos.price_open:.2f}"),
              ("SL", f"{pos.sl:.2f}" if pos.sl else "🚨 NONE"), ("TP", f"{pos.tp:.2f}" if pos.tp else "none")]
    if risk_pts is not None:
        fields.append(("Risk", f"{risk_pts:.2f} pts" + (f" · {risk_money:,.2f}" if risk_money is not None else "")))
    fields.append(("Market at entry", rec["status_at_entry"] or "-", False))
    title = f"📥 POSITION OPENED {'▲ LONG' if side > 0 else '▼ SHORT'}"
    desc = "🚨 **NO STOP-LOSS on this position.** The trailing stop will set one at its first step." if not pos.sl else ""
    _send(state, notifier.info(title, desc, symbol=sym, channel="positions", fields=fields, kind="POSITION_OPENED",
                               ping=not pos.sl, color="BULL" if side > 0 else "BEAR"),
          f"POS_OPEN:{pos.ticket}")
    store.record("position", {"event": "opened", "ticket": pos.ticket, "side": "long" if side > 0 else "short",
                              "lot": pos.volume, "entry": float(pos.price_open), "exit": None, "sl": float(pos.sl or 0),
                              "tp": float(pos.tp or 0), "result": None, "close_reason": None,
                              "magic": getattr(pos, "magic", None), "market_status_at_entry": rec["status_at_entry"]},
                 symbol=sym)


def _deals_of(ticket: int):
    now = signals.server_now()
    try:
        return mt5.history_deals_get(signals._mt5_dt(now - timedelta(days=60)), signals._mt5_dt(now + timedelta(days=1)),
                                     position=ticket)
    except TypeError:
        return None


def _closed(state, t: str):
    book = _book(state)
    rec = book[t]
    deals = _deals_of(int(t))
    outs = [d for d in (deals or ()) if getattr(d, "entry", None) in (1, 3)]          # OUT / OUT_BY
    if not outs and rec.get("close_tries", 0) < 5:                                   # history may lag a moment
        rec["close_tries"] = rec.get("close_tries", 0) + 1
        return
    side, entry = rec["side"], rec["entry"]
    if outs:
        exit_price = sum(d.price * d.volume for d in outs) / max(sum(d.volume for d in outs), 1e-12)
        money = sum(d.profit + getattr(d, "swap", 0) + getattr(d, "commission", 0) + getattr(d, "fee", 0)
                    for d in (deals or ()))
        reason = REASONS.get(getattr(outs[-1], "reason", -1), "unknown")
        pts = side * (exit_price - entry)
        fields = [("Ticket", t), ("Entry → exit", f"{entry:.2f} → {exit_price:.2f}"), ("Result", f"{_pts(pts)} pts · {money:+,.2f}"),
                  ("Closed by", reason), ("TSL steps", str(rec.get("steps", 0)))]
        color = "BULL" if money >= 0 else "BEAR"
    else:
        exit_price = pts = money = None
        reason = "unknown (no deal history from MT5)"
        fields = [("Ticket", t), ("Result", "MT5 gave no deal history — check the terminal")]
        color = "WARN"
    _send(state, notifier.info(f"📤 POSITION CLOSED {'▲ LONG' if side > 0 else '▼ SHORT'}", symbol=rec["symbol"],
                               channel="positions", fields=fields, kind="POSITION_CLOSED", color=color),
          f"POS_CLOSE:{t}")
    store.record("position", {"event": "closed", "ticket": int(t), "side": "long" if side > 0 else "short",
                              "lot": rec["volume"], "entry": entry, "exit": exit_price, "sl": rec.get("sl"),
                              "tp": rec.get("tp"), "result": money, "result_points": pts, "close_reason": reason,
                              "market_status_at_entry": rec.get("status_at_entry")}, symbol=rec["symbol"])
    del book[t]


def _status(sym: str, labs: dict) -> str:
    res = labs.get(sym)
    return labels.status_line(res.state) if res else ""


# -------------------------------------------------------------------------------------------- MAX POINT
def _watch_giveback(state, pos, sym: str, labs: dict):
    """Profit has fallen MAXPOINT_GIVEBACK from its peak after reaching MAXPOINT_MIN_PROFIT -> MAX POINT, once per peak."""
    rec = _book(state)[str(pos.ticket)]
    side, cur = rec["side"], _cur(pos)
    if side * (cur - rec["peak"]) > 0:
        rec["peak"] = cur
    atr = _atr(sym, labs)
    min_profit = C.sym_value(sym, "maxpoint_min_profit", atr)
    giveback = C.sym_value(sym, "maxpoint_giveback", atr)
    if min_profit is None or giveback is None:
        return                                              # ATR not known yet: cannot judge, say nothing
    peak_profit, now_profit = side * (rec["peak"] - rec["entry"]), side * (cur - rec["entry"])
    if peak_profit >= min_profit and peak_profit - now_profit >= giveback:
        max_point(state, pos, sym, labs, f"profit gave back {peak_profit - now_profit:.2f} from its peak")


def max_point(state, pos, sym: str, labs: dict, why: str, df=None):
    rec = _book(state)[str(pos.ticket)]
    if rec.get("maxpoint_peak") is not None and rec["side"] * (rec["peak"] - rec["maxpoint_peak"]) <= 0:
        return                                              # already said for this peak; re-arms on a new extreme
    rec["maxpoint_peak"] = rec["peak"]
    side, cur, sl = rec["side"], _cur(pos), float(pos.sl or 0)
    peak_p, now_p = side * (rec["peak"] - rec["entry"]), side * (cur - rec["entry"])
    kept = side * (sl - rec["entry"]) if sl else None
    fields = [("Ticket", str(pos.ticket)), ("Peak profit", f"{peak_p:.2f} pts"), ("Now", f"{now_p:.2f} pts"),
              ("Given back", f"{peak_p - now_p:.2f} pts"), ("SL", f"{sl:.2f}" if sl else "none"),
              ("Kept if SL hits", f"{kept:+.2f} pts" + (f" · {_money(pos, sl):+,.2f}" if sl and _money(pos, sl) is not None else "")
               if kept is not None else "nothing — no SL"),
              ("Why", why, False), ("Market now", _status(sym, labs) or "-", False)]
    image = None
    if df is not None and C.CHART_ENABLED:
        import chart
        image = chart.render(df, C.TIMEFRAMES[0], f"{sym} MAX POINT · ticket {pos.ticket}", signals.to_ist,
                             price_line=cur, symbol=sym, lines=[(rec["entry"], "entry")] + ([(sl, "SL")] if sl else []))
    buttons = [notifier.button("Close position", "close", pos.ticket, style="danger"),
               notifier.button("Keep", "keep", pos.ticket)]
    _send(state, notifier.info("🏔️ MAX POINT — take it or keep it?", symbol=sym, channel="alerts", fields=fields,
                               kind="MAX_POINT", ping=True, buttons=buttons, image=image, color="CAUTION"),
          f"MAXPOINT:{pos.ticket}:{rec['peak']:.5f}")


def keep(state, req: dict) -> dict:
    rec = _book(state).get(str(req.get("ticket")))
    if rec is None:
        return {"ok": False, "text": "That position is not open any more."}
    return {"ok": True, "text": f"Kept {req.get('ticket')}. The next MAX POINT comes only after price makes a new extreme."}


# ------------------------------------------------------------------------------ per closed candle: TSL + alerts
def on_candle(state, symbol: str, df, lab, fresh: list) -> None:
    """After a symbol's new closed candle(s): trailing stop and in-trade alerts for its open positions.
    fresh = the events delivered live on this pass (bar_time, event)."""
    if not C.is_enabled(symbol) or lab is None:
        return
    track(state, {symbol: lab})
    bars = labels.prepare(df)
    newest = bars["time"].iloc[-1]
    new_labels = {e["type"] for bt, e in fresh if bt == newest}
    for pos in OPEN.get(symbol, []):
        rec = _book(state).get(str(pos.ticket))
        if rec is None:
            continue
        side = rec["side"]
        if lab.state.side != side:
            if not rec.get("against"):
                rec["against"] = True
                _send(state, notifier.warn(f"position {pos.ticket} is AGAINST the trend — no stop moves",
                                           f"{'LONG' if side > 0 else 'SHORT'} position, EMA {C.EMA_FAST}/{C.EMA_SLOW} "
                                           f"says {'LONG' if lab.state.side > 0 else 'SHORT'}. The bot will not move "
                                           f"its stop while this lasts.", symbol=symbol, channel="positions",
                                           kind="POSITION_AGAINST_TREND"),
                      f"AGAINST:{pos.ticket}:{lab.state.cross_i and str(bars['time'].iloc[lab.state.cross_i])}")
            continue
        rec["against"] = False
        moved = _trail(state, pos, symbol, bars, lab, rec, newest)
        widening = labels.GAP_WIDENING in new_labels
        if moved and widening:
            _send(state, notifier.info("💪 TREND STRENGTHENING, SL TIGHTENED", symbol=symbol, channel="positions",
                                       fields=moved + [("Market now", _status(symbol, {symbol: lab}), False)],
                                       kind="TREND_STRENGTHENING_SL", color="BULL" if side > 0 else "BEAR"),
                  f"INTRADE_C:{pos.ticket}:{newest}")
        elif moved:
            _send(state, notifier.info("🔒 TSL MOVED", symbol=symbol, channel="positions", fields=moved,
                                       kind="TSL_MOVED", color="BULL" if side > 0 else "BEAR"),
                  f"INTRADE_A:{pos.ticket}:{newest}")
        elif widening:
            cur = _cur(pos)
            _send(state, notifier.info(f"🏃 TREND RUNNING with your {'LONG' if side > 0 else 'SHORT'}", symbol=symbol,
                                       channel="positions", kind="TREND_RUNNING", color="BULL" if side > 0 else "BEAR",
                                       fields=[("Ticket", str(pos.ticket)), ("Profit", f"{side * (cur - rec['entry']):.2f} pts"),
                                               ("SL", f"{pos.sl:.2f}" if pos.sl else "none"),
                                               ("Market now", _status(symbol, {symbol: lab}), False)]),
                  f"INTRADE_B:{pos.ticket}:{newest}")
        if labels.GAP_PEAKED in new_labels:
            max_point(state, pos, symbol, {symbol: lab}, "GAP PEAKED on the last candle", df=df)
    state.save()


def _open_index(bars, pos) -> int:
    """The last candle that had CLOSED when the position opened: tracking starts at its close (the entry), so a
    position opened right after a cross candle is tracked from that cross."""
    opened = signals.stamp_to_dt(pos.time) - timedelta(minutes=C.BAR_MINUTES[C.TIMEFRAMES[0]])
    idx = bars.index[bars["time"] <= opened]
    return int(idx[-1]) if len(idx) else 0


def tsl_plan(bars, lab, pos, symbol: str):
    """(steps, pullback machine, start index) for this position, from candles only."""
    side = _side(pos)
    start_open = _open_index(bars, pos)
    start = max(start_open, lab.state.cross_i or 0)
    atr_now = _atr(symbol, {symbol: lab})
    kind, x = C.SYMBOLS[symbol]["tsl_buffer"]
    atrs = bars["atr"].to_numpy()
    buffer_at = (lambda i: float(x)) if kind == "price" else (lambda i: float(x) * float(atrs[i] if atrs[i] == atrs[i] else atr_now))
    entry = float(pos.price_open) if start == start_open else None
    steps, pb = labels.tsl_track(bars, start, side, buffer_at, entry=entry)
    return steps, pb, start


def _trail(state, pos, symbol, bars, lab, rec, newest) -> list | None:
    """Apply (or suggest) the latest trailing-stop step if it tightens the stop. Returns the card fields of a
    successful move, else None. Each step is tried once."""
    steps, _, _ = tsl_plan(bars, lab, pos, symbol)
    if not steps:
        return None
    step = steps[-1]
    side, cur_sl = rec["side"], float(pos.sl or 0)
    if not trader.tighter(side, step["sl"], cur_sl):
        return None
    key = f"TSL:{pos.ticket}:{step['time']}"
    if state.seen(key):
        return None                                          # tried before (moved, suggested or failed): never loop
    late = step["time"] != newest
    info = mt5.symbol_info(pos.symbol)
    digits = getattr(info, "digits", 2)
    new_sl = round(step["sl"], digits)
    trigger = f"close beyond {step['pre_extreme']:.2f} after RE-ENTRY · pullback extreme {step['pullback_extreme']:.2f}"
    mode = tsl_mode()
    if mode == "suggest":
        state.mark(key)
        store.record("sl_change", {"ticket": pos.ticket, "old_sl": cur_sl, "new_sl": new_sl, "trigger": trigger,
                                   "mode": "suggest", "verified": False}, symbol=symbol)
        _send(state, notifier.info("🔒 TSL STEP suggested", symbol=symbol, channel="positions", kind="TSL_SUGGEST",
                                   fields=[("Ticket", str(pos.ticket)), ("SL now", f"{cur_sl:.2f}" if cur_sl else "none"),
                                           ("Suggested SL", f"{new_sl:.{digits}f}"), ("Trigger", trigger, False)],
                                   buttons=[notifier.button("Apply", "apply", pos.ticket, new_sl, style="primary")]))
        return None
    state.mark(key)                                          # claimed before sending: a crash cannot repeat it
    res = trader.modify_sl(pos.ticket, new_sl, why="tsl")
    store.record("sl_change", {"ticket": pos.ticket, "old_sl": cur_sl, "new_sl": new_sl, "trigger": trigger,
                               "mode": "auto", "verified": res["verified"], "detail": res["text"]}, symbol=symbol)
    if not res["ok"]:
        _send(state, notifier.warn(f"TSL step NOT applied on {pos.ticket}",
                                   f"Wanted SL {new_sl:.{digits}f} (now {cur_sl or 'none'}): {res['text']}.\n"
                                   f"The bot will not retry this step. Check the position in MT5.",
                                   symbol=symbol, channel="positions", ping=True, kind="TSL_FAILED"))
        return None
    rec["steps"] = rec.get("steps", 0) + 1
    rec["sl"] = new_sl
    locked = side * (new_sl - rec["entry"])
    money = _money(pos, new_sl)
    return [("Ticket", str(pos.ticket)), ("Old SL", f"{cur_sl:.{digits}f}" if cur_sl else "none"),
            ("New SL", f"{new_sl:.{digits}f}"), ("Step", str(rec["steps"])),
            ("Profit locked", f"{locked:+.2f} pts" + (f" · {money:+,.2f}" if money is not None else "")),
            ("Trigger", trigger + (" (caught up after a restart)" if late else ""), False)]


def apply(state, req: dict) -> dict:
    """The Apply button of a suggested step (TSL_MODE=suggest). Still only ever tightens."""
    try:
        ticket, sl = int(req["ticket"]), float(req["sl"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "text": "Apply needs a ticket and a stop level."}
    rec = _book(state).get(str(ticket))
    res = trader.modify_sl(ticket, sl, why="tsl apply")
    store.record("sl_change", {"ticket": ticket, "old_sl": res["old"], "new_sl": res["new"], "trigger": "Apply button",
                               "mode": "suggest", "verified": res["verified"], "detail": res["text"]},
                 symbol=rec["symbol"] if rec else None)
    if res["ok"] and rec:
        rec["steps"] = rec.get("steps", 0) + 1; rec["sl"] = res["new"]; state.save()
    return {"ok": res["ok"], "text": ("✅ " if res["ok"] else "❌ NOT applied — ") + res["text"]}


# ------------------------------------------------------------------------------------- 200-cross lines
CROSS_200 = {"FAST_X_TREND", "SLOW_X_TREND", "PARALLEL_TREND"}


def event_lines(state, symbol: str, e: dict) -> dict:
    """For 200-crosses and 200 pre-crosses while a position is open: with/against, profit and SL on the same card."""
    is_200 = e["type"] in CROSS_200 or (e["type"] in (labels.PRE_CROSS, labels.PRE_CROSS_FAILED)
                                        and e.get("pair") in ("8/200", "32/200"))
    if not is_200:
        return {}
    out = []
    for pos in OPEN.get(symbol, []):
        side = _side(pos)
        with_ = (e["dir"] == "BULL") == (side > 0)
        cur = _cur(pos)
        sl_txt = f"SL {pos.sl:.2f}" if pos.sl else "no SL"
        out.append((f"Your {'LONG' if side > 0 else 'SHORT'} {pos.ticket}",
                    f"{'WITH' if with_ else 'AGAINST'} the position · profit {side * (cur - pos.price_open):+.2f} pts · "
                    + sl_txt, False))
    return {"fields": out}


# --------------------------------------------------------------------------------------------- /positions
def report(state, labs: dict, bars_of=None) -> dict:
    """/positions text. bars_of(symbol) -> labels.prepare()d bars (for the next-step line)."""
    acc = mt5.account_info()
    allpos = mt5.positions_get()
    if acc is None or allpos is None:
        return {"ok": False, "text": "MT5 cannot list positions right now."}
    cur = getattr(acc, "currency", "")
    lines = []
    total = 0.0
    now = signals.server_now()
    for pos in allpos:
        sym = _sym(pos) or pos.symbol
        side = _side(pos)
        price = _cur(pos)
        pts = side * (price - pos.price_open)
        money = float(getattr(pos, "profit", 0.0) or 0.0)
        total += money
        opened = signals.stamp_to_dt(pos.time)
        held = now - opened
        rec = _book(state).get(str(pos.ticket), {})
        managed = C.is_enabled(sym) if sym in C.SYMBOLS else False
        nxt = "-"
        if managed and bars_of:
            try:
                bars = bars_of(sym)                          # labels re-run on THESE candles: indexes must match
                _, pb, _ = tsl_plan(bars, labels.run(bars), pos, sym)
                nxt = labels.next_step_text(pb, side) if pb else "none — the trend is against the position"
            except Exception:
                log.exception("next-step text")
        to_sl = f"{side * (price - pos.sl):.2f} pts" if pos.sl else "no SL"
        sl_txt = f"{pos.sl:.2f}" if pos.sl else "🚨 none"
        tp_txt = f"{pos.tp:.2f}" if pos.tp else "none"
        lines.append(
            f"**{sym} {'▲ LONG' if side > 0 else '▼ SHORT'} {pos.volume:g}** · #{pos.ticket}"
            + ("" if managed else " · NOT managed (disabled)") + "\n"
            + f"Entry {pos.price_open:.2f} at {signals.to_ist(opened):%d %b %H:%M} IST · open {_dur(held)}\n"
            + f"Now {price:.2f} · P/L {pts:+.2f} pts / {money:+,.2f} {cur} · swap {getattr(pos, 'swap', 0):+,.2f}\n"
            + f"SL {sl_txt} · TP {tp_txt} · to SL {to_sl}\n")
        if managed:
            lines[-1] += (f"TSL step {rec.get('steps', 0)} ({tsl_mode()}) · next: {nxt}\n"
                          f"Market: {_status(sym, labs) or '-'}")
    today = trader._closed_pnl_today()
    acct = (f"Balance {acc.balance:,.2f} · Equity {acc.equity:,.2f} {cur} · free margin {getattr(acc, 'margin_free', 0):,.2f}"
            + (f" · today's closed {today:+,.2f}" if today is not None else ""))
    if not allpos:
        text = f"No open positions.\n{acct}"
    else:
        text = "\n\n".join(lines) + f"\n\n**Open P/L {total:+,.2f} {cur}** · {acct}"
    return {"ok": True, "text": text, "buttons": [notifier.button("Refresh", "refresh", "positions", style="secondary")]}


def _dur(td: timedelta) -> str:
    mins = int(td.total_seconds() // 60)
    return f"{mins // 1440}d {mins % 1440 // 60}h {mins % 60}m" if mins >= 1440 else f"{mins // 60}h {mins % 60}m"
