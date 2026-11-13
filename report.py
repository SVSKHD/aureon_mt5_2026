"""/last-week (spec section 9): the previous completed Monday-Friday, per symbol.

Crosses are recomputed from MT5 candles (right even if the bot was offline); positions and P&L come from MT5
deal history (so trades placed from the MT5 app count). Runs in the bot loop (the only MT5 thread).
Week boundaries are broker server time: Monday 00:00 to Saturday 00:00.
"""
from __future__ import annotations
import logging
from collections import defaultdict
from datetime import datetime, timedelta
import numpy as np
import MetaTrader5 as mt5
import config as C
import signals, labels, chart, store

log = logging.getLogger("report")
REASONS = {0: "manual", 1: "manual (mobile)", 2: "manual (web)", 3: "expert", 4: "SL", 5: "TP", 6: "stop-out"}
MAX_LISTED = 25


def last_week(now_srv: datetime | None = None) -> tuple[datetime, datetime]:
    """(Monday 00:00, Saturday 00:00) of the most recent COMPLETED trading week, server time."""
    now = now_srv or signals.server_now()
    monday = datetime.combine(now.date() - timedelta(days=now.weekday()), datetime.min.time())
    if now < monday + timedelta(days=5):                       # this week's Friday is not over: the week before
        monday -= timedelta(days=7)
    return monday, monday + timedelta(days=5)


def _money(d) -> float:
    return float(d.profit + getattr(d, "swap", 0) + getattr(d, "commission", 0) + getattr(d, "fee", 0))


def crosses(df, start: datetime, end: datetime) -> dict:
    """8/32 crosses inside [start, end) with how far each ran, plus the EMA200 crossings."""
    bars = labels.prepare(df)
    res = labels.run(bars)
    times = bars["time"]
    xs = [e for e in res.events if e["label"] == labels.CROSS]
    out = []
    for k, e in enumerate(xs):
        t = e["time"].to_pydatetime()
        if not (start <= t < end):
            continue
        nxt = xs[k + 1] if k + 1 < len(xs) else None
        stop_i = nxt["i"] if nxt else len(bars) - 1
        seg = bars.iloc[e["i"] + 1:stop_i + 1]
        best = (seg["clean_high"].max() - e["price"]) if e["side"] > 0 else (e["price"] - seg["clean_low"].min())
        out.append({"time": t, "side": e["side"], "price": e["price"], "tag": e["tag"],
                    "ran": (e["side"] * (nxt["price"] - e["price"])) if nxt else None,
                    "best": float(best) if len(seg) else 0.0, "open": nxt is None,
                    "now": float(e["side"] * (bars["close"].iloc[-1] - e["price"])), "i": e["i"],
                    "next_i": nxt["i"] if nxt else None})
    # EMA200 crossings inside the week
    week = (times >= start) & (times < end)
    c200 = {}
    for name, col in (("EMA8", "ema_f"), ("EMA32", "ema_s")):
        sgn = np.sign(bars[col] - bars["ema_t"]).to_numpy()
        ups = [i for i in range(1, len(sgn)) if week.iloc[i] and sgn[i - 1] < 0 < sgn[i]]
        downs = [i for i in range(1, len(sgn)) if week.iloc[i] and sgn[i - 1] > 0 > sgn[i]]
        c200[name] = {"up": ups, "down": downs}
    followed = 0
    for x in out:                                               # a 200 cross the same way before the next 8/32 cross
        hi = x["next_i"] if x["next_i"] is not None else len(bars)
        same = "up" if x["side"] > 0 else "down"
        if any(x["i"] <= i < hi for name in c200 for i in c200[name][same]):
            followed += 1
    return {"list": out, "c200": {n: {k: len(v) for k, v in d.items()} for n, d in c200.items()},
            "followed": followed, "bars": bars, "res": res}


def trades(symbol: str, start: datetime, end: datetime) -> dict | None:
    deals = mt5.history_deals_get(signals._mt5_dt(start - timedelta(days=60)),
                                  signals._mt5_dt(signals.server_now() + timedelta(days=1)))
    if deals is None:
        return None
    broker = C.broker_symbol(symbol)
    t_of = lambda d: signals.stamp_to_dt(d.time)
    by_pos = defaultdict(list)
    for d in deals:
        if getattr(d, "symbol", broker) == broker and getattr(d, "type", 0) in (0, 1):
            by_pos[getattr(d, "position_id", None)].append(d)
    opened = closed = still = 0
    results, reasons, days, pts_total = [], defaultdict(int), defaultdict(float), 0.0
    week_money = 0.0
    for pid, ds in by_pos.items():
        ins = [d for d in ds if d.entry == 0]
        outs = [d for d in ds if d.entry in (1, 3)]
        for d in ds:
            if start <= t_of(d) < end:
                week_money += _money(d)
                days[t_of(d).strftime("%a %d %b")] += _money(d)
        if ins and start <= t_of(ins[0]) < end:
            opened += 1
            if not outs or t_of(outs[-1]) >= end:
                still += 1
        if outs and start <= t_of(outs[-1]) < end:
            closed += 1
            money = sum(_money(d) for d in ds)
            side = 1 if ins and ins[0].type == 0 else -1
            entry = ins[0].price if ins else outs[0].price
            exit_ = sum(d.price * d.volume for d in outs) / max(sum(d.volume for d in outs), 1e-12)
            pts = side * (exit_ - entry)
            pts_total += pts
            results.append({"ticket": pid, "money": money, "pts": pts, "side": side})
            reasons[REASONS.get(getattr(outs[-1], "reason", -1), "other")] += 1
    wins = [r for r in results if r["money"] > 0]
    losses = [r for r in results if r["money"] <= 0]
    return {"opened": opened, "closed": closed, "still_open": still, "wins": len(wins), "losses": len(losses),
            "win_rate": (len(wins) / len(results) * 100) if results else None, "reasons": dict(reasons),
            "money": week_money, "pts": pts_total, "days": dict(days),
            "best": max(results, key=lambda r: r["money"]) if results else None,
            "worst": min(results, key=lambda r: r["money"]) if results else None}


def balances(start: datetime, end: datetime) -> tuple[float, float] | None:
    acc = mt5.account_info()
    deals = mt5.history_deals_get(signals._mt5_dt(start), signals._mt5_dt(signals.server_now() + timedelta(days=1)))
    if acc is None or deals is None:
        return None
    t_of = lambda d: signals.stamp_to_dt(d.time)
    after = sum(_money(d) for d in deals if t_of(d) >= end)
    during = sum(_money(d) for d in deals if start <= t_of(d) < end)
    end_bal = float(acc.balance) - after
    return end_bal - during, end_bal


def build(symbol: str, now_srv: datetime | None = None) -> dict:
    start, end = last_week(now_srv)
    df = signals.fetch(C.TIMEFRAMES[0], since=start, symbol=symbol)
    if df is None:
        return {"ok": False, "text": f"{symbol}: MT5 has no candles for {start:%d %b} → {end:%d %b}."}
    cx = crosses(df, start, end)
    tr = trades(symbol, start, end)
    bal = balances(start, end)
    cur = getattr(mt5.account_info(), "currency", "")
    L = cx["list"]
    longs, shorts = sum(x["side"] > 0 for x in L), sum(x["side"] < 0 for x in L)
    lines = [f"**{symbol} · week {start:%a %d %b} → {(end - timedelta(days=1)):%a %d %b} (server time)**",
             f"**1. EMA {C.EMA_FAST}/{C.EMA_SLOW} crosses: {len(L)}** ({longs} long, {shorts} short)"]
    for x in L[:MAX_LISTED]:
        ran = (f"ran {x['ran']:+.2f} to the opposite cross" if not x["open"] else f"still running ({x['now']:+.2f} now)")
        lines.append(f"`{signals.to_ist(x['time'] + timedelta(minutes=C.BAR_MINUTES[C.TIMEFRAMES[0]])):%a %H:%M}` "
                     f"{'▲ LONG' if x['side'] > 0 else '▼ SHORT'} @ {x['price']:.2f} · {x['tag']} · {ran} · best {x['best']:+.2f}")
    if len(L) > MAX_LISTED:
        lines.append(f"… and {len(L) - MAX_LISTED} more")
    c = cx["c200"]
    lines.append(f"**2. EMA200 crosses:** EMA8 ↑{c['EMA8']['up']} ↓{c['EMA8']['down']} · EMA32 ↑{c['EMA32']['up']} "
                 f"↓{c['EMA32']['down']} · {cx['followed']} of {len(L)} 8/32 crosses were followed by a 200 cross")
    if tr is None:
        lines.append("**3-4. Positions / P&L:** MT5 gave no deal history.")
    else:
        wr = f"{tr['win_rate']:.0f}%" if tr["win_rate"] is not None else "-"
        how = ", ".join(f"{k} {v}" for k, v in tr["reasons"].items()) or "-"
        lines.append(f"**3. Positions:** opened {tr['opened']} · closed {tr['closed']} · still open {tr['still_open']} · "
                     f"wins {tr['wins']} · losses {tr['losses']} · win rate {wr} · closed by: {how}")
        lines.append(f"**4. P&L:** {tr['money']:+,.2f} {cur} · {tr['pts']:+.2f} pts"
                     + (f" · best #{tr['best']['ticket']} {tr['best']['money']:+,.2f}" if tr["best"] else "")
                     + (f" · worst #{tr['worst']['ticket']} {tr['worst']['money']:+,.2f}" if tr["worst"] else ""))
        if tr["days"]:
            lines.append("By day: " + " · ".join(f"{d} {v:+,.2f}" for d, v in tr["days"].items()))
    if bal:
        lines.append(f"Balance {bal[0]:,.2f} → {bal[1]:,.2f} {cur} (account)")
    week_df = df[(df["time"] >= start) & (df["time"] < end)]
    image = None
    if len(week_df):
        image = chart.render(df[df["time"] < end], C.TIMEFRAMES[0], f"{symbol} · week of {start:%d %b %Y} · 8/32 crosses",
                             signals.to_ist, symbol=symbol, bars=len(week_df),
                             marks=[(x["time"], "BULL" if x["side"] > 0 else "BEAR") for x in L])
    store.record("weekly", {"week": f"{start:%Y-%m-%d}", "crosses": len(L), "long": longs, "short": shorts,
                            "ema200": c, "followed_by_200": cx["followed"],
                            "positions": {k: v for k, v in (tr or {}).items() if k not in ("best", "worst", "days")},
                            "pnl": (tr or {}).get("money"), "pnl_points": (tr or {}).get("pts"),
                            "balance_start": bal[0] if bal else None, "balance_end": bal[1] if bal else None},
                 symbol=symbol)
    return {"ok": True, "text": "\n".join(lines), "image": image}


def last_week_reply(symbols: list[str]) -> dict:
    parts = [build(s) for s in symbols]
    return {"ok": all(p["ok"] for p in parts), "text": "\n\n".join(p["text"] for p in parts)[:3900],
            "images": [p["image"] for p in parts if p.get("image")]}
