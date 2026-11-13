"""Monitor mode (spec section 6): one live card per symbol, edited in place.

Started by the Monitor button on a pre-cross alert or /monitor [symbol]; stopped ONLY by /unmonitor [symbol]
(it does not stop when the cross confirms or fails). The on/off state is persisted in the bot state, so restarts
and auto-updates keep it. The card is refreshed on any change and at each candle close, never more often than
once a minute. Real events still go out as normal pings; the card is extra.

Before the cross: how far apart each pre-cross pair still is (closed candles), plus LIVE values from the forming
candle, marked "live, not confirmed". After the cross: direction, tag, time since the cross and a checklist.
"""
from __future__ import annotations
import hashlib, logging, time
from datetime import timezone
import config as C
import signals, notifier, labels

log = logging.getLogger("monitor")
MIN_EDIT_SECONDS = 60
NO_FOLLOW_THROUGH_BARS = 9                 # study: failed crosses typically died within ~4.5 h


def _book(state) -> dict:
    return state.d.setdefault("monitor", {})


def active(state, symbol: str) -> bool:
    return bool(_book(state).get(symbol, {}).get("on"))


def status_line(state, symbol: str) -> str:
    m = _book(state).get(symbol)
    if not m or not m.get("on"):
        return "Monitor: off"
    return f"Monitor: ON since {signals.to_ist(signals.stamp_to_dt(m['since_srv'])):%d %b %H:%M} IST"


def start(state, symbol: str, user=None) -> dict:
    if active(state, symbol):
        return {"ok": True, "text": f"{symbol} is already monitored. /unmonitor {symbol} stops it."}
    _book(state)[symbol] = {"on": True, "since": time.time(),
                            "since_srv": int(signals.server_now().replace(tzinfo=timezone.utc).timestamp()),
                            "last_edit": 0.0, "last_hash": "", "by": user}
    state.save()
    if not notifier.BOT_CLIENT["on"]:
        return {"ok": True, "text": f"👀 Monitoring {symbol}. (The live card needs the Discord bot client; "
                                    f"alerts still arrive as usual.)"}
    return {"ok": True, "text": f"👀 Monitoring {symbol} — a live card in the updates channel. /unmonitor {symbol} stops it."}


def stop(state, symbol: str) -> dict:
    m = _book(state).get(symbol)
    if not m or not m.get("on"):
        return {"ok": True, "text": f"{symbol} was not being monitored."}
    m["on"] = False
    state.save()
    if notifier.BOT_CLIENT["on"]:                       # leave the card, marked stopped
        notifier.send(notifier.info("👀 MONITOR stopped", "Stopped with /unmonitor.", symbol=symbol,
                                    channel="updates", kind="MONITOR", upsert=f"monitor:{symbol}"))
    return {"ok": True, "text": f"Stopped monitoring {symbol}."}


def _live_ema(prev: float, span: int, price: float) -> float:
    a = 2.0 / (span + 1.0)
    return prev + a * (price - prev)


def card(state, symbol: str, lab, df, price: float | None) -> dict:
    st = lab.state
    m = _book(state)[symbol]
    since = signals.stamp_to_dt(m["since_srv"])
    times = df["time"].tolist()
    crossed_after = st.cross_i is not None and st.cross_i < len(times) and times[st.cross_i].to_pydatetime() >= since
    fields = []
    atr = (st.last or {}).get("atr") or 0
    if not crossed_after:
        desc = "**Before the cross** — distances between the lines (closed candles):"
        last = df.iloc[-1]
        live = {}
        if price is not None:
            e8 = _live_ema(float(last[f"ema{C.EMA_FAST}"]), C.EMA_FAST, price)
            e32 = _live_ema(float(last[f"ema{C.EMA_SLOW}"]), C.EMA_SLOW, price)
            e200 = _live_ema(float(last[f"ema{C.EMA_TREND}"]), C.EMA_TREND, price)
            live = {"8/32": abs(e8 - e32), "8/200": abs(e8 - e200), "32/200": abs(e32 - e200)}
        for pair, d in (st.last or {}).get("dist", {}).items():
            p = st.pre.get(pair, {})
            flag = "🟡 PRE-CROSS fired" if p.get("fired") else ("armed" if p.get("armed") else "not armed")
            txt = f"{d:.2f} pts" + (f" ({d / atr:.2f} ATR)" if atr else "") + f" · {flag}"
            if pair in live:
                txt += f"\nlive {live[pair]:.2f} — *live, not confirmed*"
            fields.append((f"EMA {pair}", txt))
    else:
        side = "▲ LONG" if st.side > 0 else "▼ SHORT"
        bars = st.bars_since_cross
        desc = f"**After the cross** — {side} · {st.tag} · {bars} candles ({bars * C.BAR_MINUTES[C.TIMEFRAMES[0]] / 60:.1f} h) since the cross"
        seen = st.seen
        fields += [("Gap widening", "✅ seen" if seen.get("widening") else "⬜ not yet"),
                   ("Parallel running", "✅ **seen — no failed cross in the study ever showed it**"
                    if seen.get("parallel") else "⬜ not yet"),
                   ("Wide gap", "✅ reached" if seen.get("wide") else "⬜ not yet")]
        if bars >= NO_FOLLOW_THROUGH_BARS and not seen.get("widening"):
            fields.append(("⚠️", f"no follow-through yet ({bars} candles, no GAP WIDENING)", False))
    fields.append(("Market (closed candles)", labels.status_line(st), False))
    if price is not None:
        fields.append(("Price", f"{price:.2f} — live, not confirmed"))
    color = "CAUTION" if not crossed_after else ("BULL" if st.side > 0 else "BEAR")
    return notifier.info("👀 MONITOR", desc, symbol=symbol, channel="updates", fields=fields, kind="MONITOR",
                         upsert=f"monitor:{symbol}", color=color)


def refresh(state, symbol: str, lab, df, candle_close: bool = False) -> bool:
    """Edit the card if something changed (or a candle closed), at most once a minute. True if an edit was sent."""
    if not active(state, symbol) or lab is None or df is None or not C.is_enabled(symbol):
        return False
    if not notifier.BOT_CLIENT["on"]:
        return False                                    # an edit-in-place card needs the bot client
    m = _book(state)[symbol]
    msg = card(state, symbol, lab, df, signals.live_price(symbol))
    h = hashlib.sha1(repr((msg["desc"], msg["fields"])).encode()).hexdigest()
    if h == m.get("last_hash") and not candle_close:
        return False
    if time.time() - m.get("last_edit", 0) < MIN_EDIT_SECONDS:
        return False
    notifier.send(msg)
    m["last_hash"], m["last_edit"] = h, time.time()
    state.save()
    return True
