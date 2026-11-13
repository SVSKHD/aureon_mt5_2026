"""Manual order placement for /place, stop-loss tightening and confirmed closes. THE ONLY FILE THAT CAN SEND AN ORDER.
It acts on one explicit, confirmed command from you: a market order with a stop-loss and a take-profit.
Nothing in the signal loop calls this module - a cross never places a trade by itself.

    preview(req) -> what would be sent (levels, money at risk, margin), fully validated, no order
    place(req)   -> sends it, then reads the position back from MT5 and reports what really happened

req = {"side": "buy"|"sell", "lot": float, "sl": float, "tp": float, "filling": "auto"|"fok"|"ioc"|"return"}
sl / tp: a DISTANCE from the entry price (20 = $20 away), or an absolute PRICE if the number is at least half
the current price (4095.5). The reply always shows the exact levels.

Everything fails closed: if a check cannot be done, the order is not sent."""
import json, logging, os, time
from datetime import timedelta
import MetaTrader5 as mt5
import config as C
import signals

log = logging.getLogger("trader")
FILLING = {"fok": "ORDER_FILLING_FOK", "ioc": "ORDER_FILLING_IOC", "return": "ORDER_FILLING_RETURN"}
PLACED, DONE, PARTIAL = 10008, 10009, 10010
BAD_FILLING, REQUOTE, PRICE_CHANGED = 10030, 10004, 10020
ACCOUNT_KIND = {0: "DEMO", 1: "CONTEST", 2: "REAL"}


def _fail(text: str) -> dict:
    return {"ok": False, "text": f"❌ NOT placed — {text}"}


def _account_text(acc) -> str:
    return f"{ACCOUNT_KIND.get(getattr(acc, 'trade_mode', -1), '?')} · {acc.login} · {acc.server}"


def _fillings(info, wanted: str) -> list[str]:
    """Filling types to try, in order. 'auto' = what the broker lists for the symbol, then the rest."""
    if wanted != "auto":
        return [wanted]
    mask = getattr(info, "filling_mode", 0)
    listed = [n for bit, n in ((1, "fok"), (2, "ioc")) if mask & bit]
    return listed + [n for n in ("return", "ioc", "fok") if n not in listed]


def _closed_pnl_today() -> float | None:
    """Realised profit/loss of deals closed since server midnight (all symbols), or None if MT5 cannot say."""
    now = signals.server_now()
    deals = mt5.history_deals_get(signals._mt5_dt(now.replace(hour=0, minute=0, second=0, microsecond=0)),
                                  signals._mt5_dt(now + timedelta(days=1)))
    if deals is None:
        return None
    return sum(d.profit + getattr(d, "swap", 0) + getattr(d, "commission", 0) + getattr(d, "fee", 0)
               for d in deals if d.type in (0, 1))               # buy/sell deals only, not deposits


def _build(req: dict) -> tuple[dict | None, dict]:
    """Validate and turn a request into an MT5 order request. Returns (order, context) or (None, error reply)."""
    # ---- the request itself ----
    if not C.PLACE_ENABLED:
        return None, _fail("trading is switched off. Set PLACE_ENABLED=1 in .env and restart.")
    side = str(req.get("side", "")).lower()
    filling = str(req.get("filling") or "auto").lower()
    if side not in ("buy", "sell"):
        return None, _fail("side must be buy or sell.")
    if filling != "auto" and filling not in FILLING:
        return None, _fail("filling must be auto, fok, ioc or return.")
    try:
        lot, sl_in, tp_in = float(req["lot"]), float(req["sl"]), float(req["tp"])
    except (KeyError, TypeError, ValueError):
        return None, _fail("lot, sl and tp are all required numbers.")
    if lot <= 0 or sl_in <= 0 or tp_in <= 0:
        return None, _fail("lot, sl and tp must be greater than zero (stop-loss and take-profit are mandatory).")
    name = str(req.get("symbol") or C.SYMBOL).upper()
    if name not in C.SYMBOLS:
        return None, _fail(f"unknown symbol {name}.")
    if not C.is_enabled(name):
        return None, _fail(f"{name} is disabled in config.")
    SYM = C.broker_symbol(name)

    # ---- terminal and account ----
    term, acc = mt5.terminal_info(), mt5.account_info()
    info, tick = mt5.symbol_info(SYM), mt5.symbol_info_tick(SYM)
    if term is None or acc is None or info is None or tick is None:
        return None, _fail("MT5 is not connected.")
    if not getattr(term, "trade_allowed", True):
        return None, _fail("Algo Trading is OFF in the MT5 terminal (toolbar button).")
    if not getattr(acc, "trade_allowed", True):
        return None, _fail("this account is not allowed to trade (investor password?).")
    if getattr(acc, "trade_mode", -1) != 0 and not C.PLACE_ALLOW_REAL:
        return None, _fail(f"MT5 is logged in to a {ACCOUNT_KIND.get(acc.trade_mode, 'non-demo')} account ({acc.login}). "
                           f"Add PLACE_ALLOW_REAL=1 to .env and restart to allow real-money orders.")
    if C.PLACE_ACCOUNT_LOGINS and acc.login not in C.PLACE_ACCOUNT_LOGINS:
        return None, _fail(f"account {acc.login} is not in PLACE_ACCOUNT_LOGINS.")

    # ---- volume ----
    step, vmin, vmax = info.volume_step, info.volume_min, info.volume_max
    if lot < vmin or lot > vmax:
        return None, _fail(f"lot {lot:g} is outside the broker's {vmin:g}–{vmax:g}.")
    if abs(round(lot / step) * step - lot) > 1e-9:
        return None, _fail(f"lot must be a multiple of {step:g}.")
    if C.PLACE_MAX_LOT is not None and lot > C.PLACE_MAX_LOT:
        return None, _fail(f"lot {lot:g} is above your PLACE_MAX_LOT {C.PLACE_MAX_LOT:g}.")

    # ---- price levels ----
    buy = side == "buy"
    price = tick.ask if buy else tick.bid
    digits, point = info.digits, info.point

    def level(value: float, is_sl: bool) -> float:
        if value >= price * 0.5:                                  # an absolute price
            return round(value, digits)
        below = (buy and is_sl) or (not buy and not is_sl)
        return round(price - value if below else price + value, digits)

    sl, tp = level(sl_in, True), level(tp_in, False)
    if buy and not (sl < tick.bid and tp > tick.ask):
        return None, _fail(f"for a BUY the stop must be below {tick.bid:.{digits}f} and the target above {tick.ask:.{digits}f} "
                           f"(got SL {sl}, TP {tp}).")
    if not buy and not (sl > tick.ask and tp < tick.bid):
        return None, _fail(f"for a SELL the stop must be above {tick.ask:.{digits}f} and the target below {tick.bid:.{digits}f} "
                           f"(got SL {sl}, TP {tp}).")
    min_dist = getattr(info, "trade_stops_level", 0) * point
    if min(abs(price - sl), abs(tp - price)) < min_dist:
        return None, _fail(f"SL/TP must be at least {min_dist:.{digits}f} away from the price (broker rule).")

    # ---- money: what this order can lose, win and lock up ----
    otype = mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL
    loss = mt5.order_calc_profit(otype, SYM, lot, price, sl)
    gain = mt5.order_calc_profit(otype, SYM, lot, price, tp)
    margin = mt5.order_calc_margin(otype, SYM, lot, price)
    if loss is None or gain is None:
        return None, _fail("MT5 could not calculate the risk of this order.")
    if C.PLACE_MAX_RISK is not None and -loss > C.PLACE_MAX_RISK:
        return None, _fail(f"loss at the stop would be {-loss:,.2f} {acc.currency}, above your PLACE_MAX_RISK {C.PLACE_MAX_RISK:,.2f}.")
    if C.PLACE_MAX_POSITIONS is not None:
        open_now = mt5.positions_get(symbol=SYM)
        if open_now is None:
            return None, _fail("MT5 could not list open positions (needed for PLACE_MAX_POSITIONS).")
        if len(open_now) >= C.PLACE_MAX_POSITIONS:
            return None, _fail(f"{len(open_now)} position(s) already open on {name}; PLACE_MAX_POSITIONS is {C.PLACE_MAX_POSITIONS:g}.")
    if C.PLACE_DAILY_LOSS_LIMIT is not None:
        pnl = _closed_pnl_today()
        if pnl is None:
            return None, _fail("MT5 could not read today's deals (needed for PLACE_DAILY_LOSS_LIMIT).")
        if -pnl >= C.PLACE_DAILY_LOSS_LIMIT:
            return None, _fail(f"today's closed loss is {-pnl:,.2f} {acc.currency}; PLACE_DAILY_LOSS_LIMIT is {C.PLACE_DAILY_LOSS_LIMIT:,.2f}.")

    order = {"action": mt5.TRADE_ACTION_DEAL, "symbol": SYM, "volume": lot, "type": otype, "price": price,
             "sl": sl, "tp": tp, "deviation": C.PLACE_DEVIATION_POINTS, "magic": C.PLACE_MAGIC,
             "comment": "aureon /place", "type_time": mt5.ORDER_TIME_GTC}
    return order, {"info": info, "acc": acc, "buy": buy, "filling": filling, "digits": digits, "name": name,
                   "loss": loss, "gain": gain, "margin": margin}


def _levels_text(order: dict, ctx: dict) -> str:
    d, cur = ctx["digits"], ctx["acc"].currency
    margin = f"{ctx['margin']:,.2f} {cur}" if ctx["margin"] is not None else "n/a"
    return (f"SL {order['sl']:.{d}f} → {ctx['loss']:+,.2f} {cur} · TP {order['tp']:.{d}f} → {ctx['gain']:+,.2f} {cur}\n"
            f"Margin {margin} · Filling {ctx['filling'].upper()} · {_account_text(ctx['acc'])}")


def preview(req: dict) -> dict:
    order, ctx = _build(req)
    if order is None:
        return ctx
    return {"ok": True, "text": f"{'BUY' if ctx['buy'] else 'SELL'} {order['volume']:g} {ctx['name']} @ ~{order['price']:.{ctx['digits']}f}\n"
                                + _levels_text(order, ctx)}


def _find_position(ticket, symbol: str | None = None):
    """The position this order opened, read back from MT5 (a few quick tries), or None. symbol = broker name."""
    for _ in range(5):
        found = mt5.positions_get(symbol=symbol or C.broker_symbol(C.SYMBOL)) or ()
        for p in found:
            if ticket in (getattr(p, "ticket", None), getattr(p, "identifier", None)):
                return p
        time.sleep(0.2)
    return None


def _protect(pos, order: dict) -> bool:
    """Some brokers open market orders without the SL/TP that was sent. Put them on the position now."""
    res = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": order["symbol"], "position": pos.ticket,
                          "sl": order["sl"], "tp": order["tp"]})
    _journal({"action": "SLTP", "position": pos.ticket, "sl": order["sl"], "tp": order["tp"]}, res, "-")
    return res is not None and res.retcode in (PLACED, DONE)


def _report(res, order: dict, ctx: dict, filling: str) -> dict:
    """Say what MT5 actually holds after the broker accepted the order - not what we hoped for."""
    d, side = ctx["digits"], "BUY" if ctx["buy"] else "SELL"
    acct = _account_text(ctx["acc"])
    pos = _find_position(res.order, order["symbol"])
    if pos is None:
        if res.retcode == PLACED:
            return {"ok": True, "text": f"⏳ ACCEPTED, not filled yet · order {res.order}\n{side} {order['volume']:g} {ctx['name']}\n"
                                        f"The broker took the order but no position shows yet — CHECK MT5.\n{acct}"}
        return {"ok": True, "text": f"⚠️ Broker answered 'done' (order {res.order}) but no position is visible — CHECK MT5 "
                                    f"before placing again.\n{acct}"}
    lines = []
    if pos.volume + 1e-9 < order["volume"]:
        lines.append(f"⚠️ PARTLY FILLED · {pos.volume:g} of {order['volume']:g} lot · position {pos.ticket}")
    else:
        lines.append(f"✅ FILLED · position {pos.ticket}")
    lines.append(f"{side} {pos.volume:g} {ctx['name']} @ {pos.price_open:.{d}f} · {filling.upper()}")
    protected = abs(pos.sl - order["sl"]) < 10 ** -d and abs(pos.tp - order["tp"]) < 10 ** -d
    if not protected and _protect(pos, order):
        pos = _find_position(res.order, order["symbol"]) or pos
        protected = abs(pos.sl - order["sl"]) < 10 ** -d and abs(pos.tp - order["tp"]) < 10 ** -d
    if protected:
        lines.append(f"SL {pos.sl:.{d}f} · TP {pos.tp:.{d}f} — both set on the position")
    else:
        lines.append(f"🚨 POSITION IS OPEN WITHOUT ITS SL/TP (has SL {pos.sl:.{d}f}, TP {pos.tp:.{d}f}). "
                     f"Set SL {order['sl']:.{d}f} and TP {order['tp']:.{d}f} in MT5 NOW.")
    lines.append(acct)
    return {"ok": True, "text": "\n".join(lines)}


def place(req: dict) -> dict:
    order, ctx = _build(req)
    if order is None:
        return ctx
    last = "no answer from MT5"
    for name in _fillings(ctx["info"], ctx["filling"]):
        order["type_filling"] = getattr(mt5, FILLING[name])
        for attempt in range(2):                                  # one retry on a requote / price change
            check = mt5.order_check(order)
            if check is None:                                     # cannot pre-check -> do not send
                return _fail(f"MT5 could not pre-check the order ({mt5.last_error()}).")
            if check.retcode != 0:
                last = f"{check.retcode} {check.comment}"
                break
            res = mt5.order_send(order)
            _journal(order, res, name)
            if res is None:
                return _fail(f"MT5 gave no answer to the order ({mt5.last_error()}). CHECK MT5 before trying again.")
            if res.retcode in (PLACED, DONE, PARTIAL):
                return _report(res, order, ctx, name)
            last = f"{res.retcode} {res.comment}"
            if res.retcode in (REQUOTE, PRICE_CHANGED) and attempt == 0:
                fresh, fresh_ctx = _build(req)                    # new price, same distances, all checks again
                if fresh is None:
                    return fresh_ctx
                fresh["type_filling"] = order["type_filling"]; order, ctx = fresh, fresh_ctx
                continue
            break
        if not (ctx["filling"] == "auto" and str(last).startswith(str(BAD_FILLING))):
            break                                                  # only "unsupported filling" moves on to the next type
    return _fail(f"broker said: {last}")


def _journal(order: dict, res, filling: str):
    """Every request sent and the broker's answer, one JSON line each, in logs/trades/."""
    try:
        os.makedirs(C.TRADE_LOG_DIR, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "request": {**order, "filling": filling},
               "retcode": getattr(res, "retcode", None), "comment": getattr(res, "comment", None),
               "ticket": getattr(res, "order", None), "price": getattr(res, "price", None)}
        with open(os.path.join(C.TRADE_LOG_DIR, f"trades-{time.strftime('%Y-%m')}.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
        log.info("ORDER %s", row)
        import store
        store.record("order", {"request": row["request"], "result": row["comment"], "retcode": row["retcode"],
                               "ticket": row["ticket"], "price": row["price"]},
                     symbol=C.logical_symbol(order.get("symbol")) or order.get("symbol"))
    except Exception:
        log.exception("trade journal write failed")


# ------------------------------------------------------------------------------ stop-loss moves and closes
def _position(ticket: int):
    found = mt5.positions_get(ticket=ticket)
    return found[0] if found else None


def tighter(side: int, new_sl: float, cur_sl: float) -> bool:
    """True if new_sl protects more than cur_sl. A missing stop (0) is always loosest."""
    if not cur_sl:
        return True
    return new_sl > cur_sl if side > 0 else new_sl < cur_sl


def modify_sl(ticket: int, new_sl: float, why: str = "tsl") -> dict:
    """Move a position's stop-loss. ONLY EVER TIGHTENS it, never widens, never removes (TP is kept as it is).
    Checks the broker's stops and freeze levels, sends TRADE_ACTION_SLTP, then reads the position back.
    Returns {"ok", "verified", "old", "new", "text"}. Fails closed: if anything cannot be checked, nothing is sent."""
    pos = _position(ticket)
    if pos is None:
        return {"ok": False, "verified": False, "old": None, "new": new_sl, "text": f"position {ticket} not found"}
    side = 1 if pos.type == mt5.ORDER_TYPE_BUY else -1
    info, tick = mt5.symbol_info(pos.symbol), mt5.symbol_info_tick(pos.symbol)
    old = float(pos.sl or 0.0)
    base = {"ok": False, "verified": False, "old": old, "new": new_sl}
    if info is None or tick is None:
        return {**base, "text": "MT5 cannot give the symbol's price or rules right now"}
    new_sl = round(float(new_sl), info.digits)
    base["new"] = new_sl
    if new_sl <= 0:
        return {**base, "text": "a stop-loss is never removed"}
    if not tighter(side, new_sl, old):
        return {**base, "text": f"{new_sl} would not tighten the current stop {old}"}
    price = tick.bid if side > 0 else tick.ask
    room = max(getattr(info, "trade_stops_level", 0), getattr(info, "trade_freeze_level", 0)) * info.point
    if side * (price - new_sl) <= room:
        return {**base, "text": f"{new_sl} is within the broker's stop/freeze distance ({room:.{info.digits}f}) of "
                                f"the price {price:.{info.digits}f}"}
    order = {"action": mt5.TRADE_ACTION_SLTP, "symbol": pos.symbol, "position": ticket, "sl": new_sl,
             "tp": float(pos.tp or 0.0), "magic": C.PLACE_MAGIC, "comment": f"aureon {why}"}
    res = mt5.order_send(order)
    _journal(order, res, "-")
    back = _position(ticket)
    verified = back is not None and abs(float(back.sl or 0) - new_sl) < info.point / 2 + 1e-9
    if res is None or res.retcode not in (PLACED, DONE) or not verified:
        code = getattr(res, "retcode", "no answer")
        return {**base, "text": f"broker said {code} {getattr(res, 'comment', '')}; the position shows SL "
                                f"{getattr(back, 'sl', '?')}, not {new_sl}"}
    return {"ok": True, "verified": True, "old": old, "new": new_sl, "text": f"SL {old} → {new_sl}"}


def close_position(req: dict) -> dict:
    """Close one position, after the user pressed Close and then Confirm on a card. Never called by the bot itself.
    Sends the opposite market deal for the whole volume, then reads back that the position is gone."""
    if not C.PLACE_ENABLED:
        return {"ok": False, "text": "❌ NOT closed — trading is switched off (PLACE_ENABLED). Close it in MT5."}
    try:
        ticket = int(req["ticket"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "text": "❌ NOT closed — no position ticket."}
    pos = _position(ticket)
    if pos is None:
        return {"ok": False, "text": f"❌ NOT closed — position {ticket} is not open (already closed?)."}
    acc, term = mt5.account_info(), mt5.terminal_info()
    if acc is None or term is None:
        return {"ok": False, "text": "❌ NOT closed — MT5 is not connected. Close it in MT5."}
    if not getattr(term, "trade_allowed", True):
        return {"ok": False, "text": "❌ NOT closed — Algo Trading is OFF in the MT5 terminal. Close it in MT5."}
    if getattr(acc, "trade_mode", -1) != 0 and not C.PLACE_ALLOW_REAL:
        return {"ok": False, "text": "❌ NOT closed — real account and PLACE_ALLOW_REAL is not set. Close it in MT5."}
    info, tick = mt5.symbol_info(pos.symbol), mt5.symbol_info_tick(pos.symbol)
    if info is None or tick is None:
        return {"ok": False, "text": "❌ NOT closed — no price for the symbol. Close it in MT5."}
    long_ = pos.type == mt5.ORDER_TYPE_BUY
    order = {"action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume, "position": ticket,
             "type": mt5.ORDER_TYPE_SELL if long_ else mt5.ORDER_TYPE_BUY, "price": tick.bid if long_ else tick.ask,
             "deviation": C.PLACE_DEVIATION_POINTS, "magic": C.PLACE_MAGIC, "comment": "aureon close",
             "type_time": mt5.ORDER_TIME_GTC}
    last = "no answer from MT5"
    for name in _fillings(info, "auto"):
        order["type_filling"] = getattr(mt5, FILLING[name])
        check = mt5.order_check(order)
        if check is None:
            return {"ok": False, "text": f"❌ NOT closed — MT5 could not pre-check it ({mt5.last_error()})."}
        if check.retcode != 0:
            last = f"{check.retcode} {check.comment}"
            continue
        res = mt5.order_send(order)
        _journal(order, res, name)
        if res is not None and res.retcode in (PLACED, DONE, PARTIAL):
            left = _position(ticket)
            if left is None:
                return {"ok": True, "text": f"✅ CLOSED position {ticket} · {pos.volume:g} {C.logical_symbol(pos.symbol) or pos.symbol} "
                                            f"@ ~{order['price']:.{info.digits}f}"}
            return {"ok": True, "text": f"⚠️ Broker answered but position {ticket} still shows {left.volume:g} lot — CHECK MT5."}
        last = f"{getattr(res, 'retcode', '?')} {getattr(res, 'comment', '')}"
        if not str(last).startswith(str(BAD_FILLING)):
            break
    return {"ok": False, "text": f"❌ NOT closed — broker said: {last}. Close it in MT5."}
