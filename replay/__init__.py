"""Aureon Replay Lab: historical replay, virtual trades and backtests. SIMULATION ONLY.

This package never imports bot, trader, alerts or positions, never sends an order, never writes the live state
files and never posts to the live alert channels (tests/test_replay.py enforces all four).

The MetaTrader5 package only exists on Windows. Where it is missing (Linux/macOS, CI), a stub is registered so CSV
and cached replays still work: it has the timeframe constants and every call raises (it cannot trade either).
"""
import sys
import types

try:
    import MetaTrader5  # noqa: F401
except ImportError:                                     # not Windows: CSV / cache replays only
    _stub = types.ModuleType("MetaTrader5")
    for _i, _n in enumerate(("M1", "M5", "M15", "M30", "H1", "H4")):
        setattr(_stub, f"TIMEFRAME_{_n}", _n)

    def _missing(name):
        def f(*a, **k):
            raise RuntimeError(f"MetaTrader5 is not installed here (MetaTrader5.{name}); use --source csv or cache")
        return f

    _stub.__getattr__ = lambda name: _missing(name)
    _stub.AUREON_REPLAY_STUB = True
    sys.modules["MetaTrader5"] = _stub
