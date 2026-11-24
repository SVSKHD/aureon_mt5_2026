"""The candle -> events pipeline, shared by the live bot (bot.check_ema) and the historical replay (replay/).

Pure: no MT5 connection, no network, no files, no wall clock, no global state. Moved here unchanged from
signals.fetch() and bot.py so that both callers run literally the same code (Replay Lab, 1.7.3+).

    add_indicators(df)                  EMA 8/32/200 + the 14-bar ATR used by signals.detect()
    candle_events(df, todo, lab)        (bar_time, event) for the last `todo` closed candles, oldest first
    label_event(le)                     labels.py event -> the bot's event dict
    event_key(symbol, tf, bar_time, e)  the dedupe key (one alert per key)
"""
from __future__ import annotations
import pandas as pd
import config as C
import labels

LIVE_LABELS = {labels.GAP_WIDENING, labels.PARALLEL, labels.WIDE_GAP, labels.GAP_PEAKED, labels.PULLBACK,
               labels.RE_ENTRY, labels.PULLBACK_FAILED, labels.PRE_CROSS, labels.PRE_CROSS_FAILED}


def add_emas(df: pd.DataFrame, periods) -> pd.DataFrame:
    """In place (and returned): ema<p> for every period (EWM of the close, adjust=False: causal, the MT5 formula).
    THE one EMA implementation: the live 8/32/200 and any strategy's EMAs (e.g. 20/50) come from here."""
    for p in periods:
        df[f"ema{p}"] = df["close"].ewm(span=p, adjust=False).mean()
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """In place (and returned): ema8/ema32/ema200 (EWM, adjust=False: causal) and atr (14-bar mean true range)."""
    add_emas(df, (C.EMA_FAST, C.EMA_SLOW, C.EMA_TREND))
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    return df


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


def candle_events(df, todo: int, lab=None):
    """(bar_time, event) for every candle in the last `todo` closed candles, oldest first: the cross and EMA200
    events from signals.detect(), then the labels of that candle."""
    import signals
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


def event_key(symbol: str, tf: str, bar_time, e: dict) -> str:
    """One alert per key: episode-keyed events (pre-cross) once per episode, everything else once per candle."""
    return f"{symbol}:{tf}:{e['type']}:{e['dir']}:{e.get('episode', str(bar_time))}"
