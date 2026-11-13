"""A stand-in for the MetaTrader5 module: deterministic XAUUSD bars, a clock you control, no network.
`World.stamp_offset` is how the fake broker stamps time (2 h = server time, 0 = plain UTC)."""
import math, sys, types
from datetime import datetime, timedelta, timezone
import numpy as np

STEP = {"M1": 60, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400}
DT = np.dtype([("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"), ("tick_volume", "i8")])


class World:
    now = datetime(2026, 11, 10, 10, 29, 50, tzinfo=timezone.utc)   # real UTC
    stamp_offset = timedelta(hours=2)
    connected = True
    initialised = False
    symbols = {"XAUUSD": True, "XAGUSD": True}   # broker symbols that exist -> visible in Market Watch
    freeze_level = 0
    frozen_tick = None        # real time of the last tick when the market is closed (None = live market)
    m1_override = {}          # {bar open stamp: (low, high)}
    ticks = []                # [(stamp, bid)]
    history_bars = 4000
    orders = []               # every request passed to order_send
    retcodes = []             # scripted broker answers for the next order_send calls (default 10009 = done)
    algo_trading = True
    filling_mode = 1          # symbol allows FOK only
    spread = 0.30
    account_mode = 0          # 0 demo, 2 real
    positions = []            # open positions (SimpleNamespace)
    deals = []                # today's closed deals
    calls = []                # (function, symbol) for every MT5 call that names a symbol
    check_unavailable = False # order_check returns None
    drop_sltp = False         # broker opens the position without the SL/TP that was sent
    sltp_fails = False        # ... and also refuses to add them afterwards

    @classmethod
    def reset(cls):
        cls.now = datetime(2026, 11, 10, 10, 29, 50, tzinfo=timezone.utc)
        cls.stamp_offset, cls.connected, cls.initialised, cls.frozen_tick = timedelta(hours=2), True, False, None
        cls.m1_override, cls.ticks, cls.history_bars = {}, [], 4000
        cls.symbols, cls.freeze_level = {"XAUUSD": True, "XAGUSD": True}, 0
        cls.orders, cls.retcodes, cls.algo_trading, cls.filling_mode = [], [], True, 1
        cls.account_mode, cls.positions, cls.deals = 0, [], []
        cls.check_unavailable = cls.drop_sltp = cls.sltp_fails = False
        cls.calls = []

    @classmethod
    def stamp(cls, real=None) -> float:
        return ((real or cls.now) + cls.stamp_offset).replace(tzinfo=timezone.utc).timestamp()

    @classmethod
    def advance(cls, **kw):
        cls.now += timedelta(**kw)


def price(stamp: float, symbol: str = "XAUUSD") -> float:
    i = stamp / 1800.0                                             # in M30 bars, continuous
    if symbol.startswith("XAG"):                                   # silver: its own scale and rhythm
        return 50 + 0.9 * math.sin(i / 7.0) + 0.35 * math.sin(i / 1.9)
    return 4100 + 60 * math.sin(i / 9.0) + 25 * math.sin(i / 2.3)


def _bars(tf, symbol="XAUUSD"):
    step = STEP[tf]
    n = World.history_bars if tf != "M1" else 900
    end = int(World.stamp()) // step * step                        # open of the forming bar
    t = end - step * np.arange(n)[::-1]
    a = np.zeros(n, DT)
    a["time"] = t
    a["open"] = [price(x, symbol) for x in t]
    a["close"] = [price(x + step, symbol) for x in t]
    wick = 0.5 if not symbol.startswith("XAG") else 0.01
    a["high"] = np.maximum(a["open"], a["close"]) + wick
    a["low"] = np.minimum(a["open"], a["close"]) - wick
    a["tick_volume"] = 100
    if tf == "M1":
        for k, (lo, hi) in World.m1_override.items():
            a["low"][a["time"] == k] = lo; a["high"][a["time"] == k] = hi
    return a


class _Tick:
    pass


def install():
    m = types.ModuleType("MetaTrader5")
    for n in STEP:
        setattr(m, "TIMEFRAME_" + n, n)
    m.COPY_TICKS_ALL = -1

    def tick(_s):
        World.calls.append(("symbol_info_tick", _s))
        if not World.initialised or _s not in World.symbols:
            return None
        t = _Tick()
        st = World.stamp(World.frozen_tick or World.now)
        t.time, t.time_msc, t.bid = int(st), int(st * 1000), round(price(st, _s), 2)   # real ticks come at 2 digits
        t.ask = round(t.bid + World.spread, 2)
        return t

    class _TI:
        def __init__(self): self.connected, self.trade_allowed = World.connected, World.algo_trading
    m.symbol_info_tick = tick
    m.terminal_info = lambda: _TI() if World.initialised else None
    def symbol_info(_s):
        World.calls.append(("symbol_info", _s))
        if _s not in World.symbols:
            return None
        return types.SimpleNamespace(visible=World.symbols[_s], volume_min=0.01, volume_max=100.0, volume_step=0.01,
                                     digits=2 if not _s.startswith("XAG") else 3, point=0.01 if not _s.startswith("XAG") else 0.001,
                                     trade_stops_level=50, trade_freeze_level=World.freeze_level,
                                     filling_mode=World.filling_mode)
    m.symbol_info = symbol_info
    m.account_info = lambda: types.SimpleNamespace(login=555, server="Fake-Server", trade_mode=World.account_mode,
                                                   trade_allowed=True, currency="USD")
    m.TRADE_ACTION_DEAL, m.TRADE_ACTION_SLTP, m.ORDER_TYPE_BUY, m.ORDER_TYPE_SELL, m.ORDER_TIME_GTC = 1, 6, 0, 1, 0
    m.ORDER_FILLING_FOK, m.ORDER_FILLING_IOC, m.ORDER_FILLING_RETURN = 0, 1, 2
    m.order_check = lambda req: None if World.check_unavailable else types.SimpleNamespace(retcode=0, comment="ok")
    m.order_calc_profit = lambda typ, _s, vol, p_open, p_close: round((p_close - p_open) * (1 if typ == 0 else -1) * vol * 100, 2)
    m.order_calc_margin = lambda typ, _s, vol, p: round(vol * p * 100 / 100, 2)      # 100 oz contract, 1:100
    def positions_get(symbol=None, ticket=None):
        World.calls.append(("positions_get", symbol))
        return tuple(p for p in World.positions if (symbol is None or getattr(p, "symbol", symbol) == symbol)
                     and (ticket is None or p.ticket == ticket))
    m.positions_get = positions_get
    m.history_deals_get = lambda a, b: tuple(World.deals)

    def order_send(req):
        World.orders.append(dict(req))
        if req["action"] == m.TRADE_ACTION_SLTP:
            if World.sltp_fails:
                return types.SimpleNamespace(retcode=10016, comment="invalid stops", order=0, price=0, volume=0)
            for p in World.positions:
                if p.ticket == req["position"]:
                    p.sl, p.tp = req["sl"], req["tp"]
            return types.SimpleNamespace(retcode=10009, comment="done", order=0, price=0, volume=0)
        code = World.retcodes.pop(0) if World.retcodes else 10009
        ticket = 1000 + len(World.orders)
        if code in (10009, 10010):
            vol = req["volume"] if code == 10009 else round(req["volume"] * 0.6, 2)
            keep = not World.drop_sltp
            World.positions.append(types.SimpleNamespace(ticket=ticket, identifier=ticket, volume=vol, price_open=req["price"],
                                                         sl=req["sl"] if keep else 0.0, tp=req["tp"] if keep else 0.0,
                                                         symbol=req["symbol"], type=req["type"], magic=req.get("magic", 0),
                                                         time=int(World.stamp()), price_current=req["price"],
                                                         profit=0.0, swap=0.0))
        return types.SimpleNamespace(retcode=code, comment="done" if code == 10009 else f"code {code}",
                                     order=ticket, price=req["price"], volume=req["volume"])
    m.order_send = order_send
    def symbol_select(_s, _b):
        World.calls.append(("symbol_select", _s))
        return _s in World.symbols
    m.symbol_select = symbol_select
    m.last_error = lambda: (-1, "fake")

    def initialize():
        World.initialised = World.connected
        return World.initialised

    def shutdown():
        World.initialised = False
    m.initialize, m.shutdown = initialize, shutdown
    def copy_rates_from_pos(_s, tf, pos, cnt):
        World.calls.append(("copy_rates_from_pos", _s))
        return _bars(tf, _s)[:-pos][-cnt:]
    m.copy_rates_from_pos = copy_rates_from_pos

    def copy_rates_from(_s, tf, d, cnt):
        World.calls.append(("copy_rates_from", _s))
        b = _bars(tf, _s); return b[b["time"] <= d.timestamp()][-cnt:]

    def copy_rates_range(_s, tf, a, b_):
        World.calls.append(("copy_rates_range", _s))
        b = _bars(tf, _s); return b[(b["time"] >= a.timestamp()) & (b["time"] <= b_.timestamp())]

    def copy_ticks_range(_s, a, b_, _flags):
        World.calls.append(("copy_ticks_range", _s))
        rows = [(int(t), bid) for t, bid in World.ticks if a.timestamp() <= t <= b_.timestamp()]
        return np.array(rows, dtype=[("time", "i8"), ("bid", "f8")])
    m.copy_rates_from, m.copy_rates_range, m.copy_ticks_range = copy_rates_from, copy_rates_range, copy_ticks_range
    sys.modules["MetaTrader5"] = m
    return m
