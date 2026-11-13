"""Backfill candles into the Assembler API (bars route). Run by hand:

    python backfill.py --csv XAUUSD_M30_20250101_20261106.csv --symbol XAUUSD --timeframe M30
    python backfill.py --mt5 --symbol XAUUSD --timeframe M30 --from 2025-01-01

CSV columns (the owner's research files): time_utc, open, high, low, close, tick_volume, spread, ema8, ema32,
ema200, atr14. Missing ema/atr columns are computed with the bot's own functions (EMA = signals.fetch's ewm,
ATR14 = the label engine's Wilder ATR on bad-tick-cleaned bars); missing tick_volume / spread are sent empty.

--mt5 pulls closed bars from the terminal and needs it; it refuses to run while the bot's heartbeat is fresh, so
two processes never share the terminal. Uploads go in chunks of 5,000 (source = "csv" or "mt5"). The server
upserts on symbol + timeframe + ts, so running it twice is safe. Uses ASSEMBLER_URL and ASSEMBLER_RECORDS_TOKEN.
"""
from __future__ import annotations
import argparse, json, os, sys, time
from datetime import datetime, timedelta, timezone
import pandas as pd
import config as C
import uploader as U

CHUNK = 5000
HEARTBEAT_FRESH_S = 180


def _num(v):
    return None if v is None or v != v else float(v)


def complete(df: pd.DataFrame) -> pd.DataFrame:
    """Fill ema8/32/200 and atr14 when the file does not have them, with the bot's own math."""
    import labels
    df = df.copy()
    for p, col in ((C.EMA_FAST, "ema8"), (C.EMA_SLOW, "ema32"), (C.EMA_TREND, "ema200")):
        if col not in df:
            df[col] = df["close"].ewm(span=p, adjust=False).mean()
    if "atr14" not in df:
        df["atr14"] = labels.prepare(df[["time", "open", "high", "low", "close"]])["atr"].to_numpy()
    return df


def to_bars(df: pd.DataFrame, server_offset: timedelta | None) -> list[dict]:
    out = []
    for r in df.itertuples(index=False):
        t = pd.Timestamp(r.time).to_pydatetime().replace(tzinfo=timezone.utc)      # df["time"] is UTC here
        row = r._asdict()
        out.append({"ts": t.isoformat(),
                    "server_ts": (t + server_offset).replace(tzinfo=None).isoformat() if server_offset is not None else None,
                    "open": _num(row["open"]), "high": _num(row["high"]), "low": _num(row["low"]), "close": _num(row["close"]),
                    "tick_volume": int(row["tick_volume"]) if row.get("tick_volume") == row.get("tick_volume") and row.get("tick_volume") is not None else None,
                    "spread": int(row["spread"]) if row.get("spread") == row.get("spread") and row.get("spread") is not None else None,
                    "ema8": _num(row["ema8"]), "ema32": _num(row["ema32"]), "ema200": _num(row["ema200"]),
                    "atr14": _num(row["atr14"])})
    return out


def send(bars: list[dict], symbol: str, timeframe: str, source: str, client: U.Client | None = None,
         chunk: int = CHUNK, out=print) -> dict:
    client = client or U.Client(C.ASSEMBLER_URL, C.ASSEMBLER_RECORDS_TOKEN, max(C.RECORD_API_TIMEOUT_S, 60))
    total = {"sent": 0, "inserted": 0, "updated": 0, "rejected": 0, "requests": 0}
    for k in range(0, len(bars), chunk):
        part = bars[k:k + chunk]
        body = {"schema": U.BARS_SCHEMA, "symbol": symbol, "timeframe": timeframe, "source": source, "bars": part}
        for attempt in range(5):
            try:
                res = client.post(U.BARS_ROUTE, body)
                break
            except U.Paused as e:                               # 401 / 415: retrying cannot help
                raise SystemExit(f"stopped at bar {k}: {e} (check ASSEMBLER_RECORDS_TOKEN; a 415 is a bug)")
            except U.Transient as e:
                wait = e.wait or min(15 * 2 ** attempt, 300)
                out(f"  {e.why}; retrying in {wait:.0f} s")
                time.sleep(wait)
        else:
            raise SystemExit(f"giving up after 5 tries at bar {k}: the API keeps failing")
        total["sent"] += len(part); total["requests"] += 1
        total["inserted"] += int(res.get("inserted", 0)); total["updated"] += int(res.get("updated", 0))
        total["rejected"] += len(res.get("rejected") or [])
        out(f"  {total['sent']}/{len(bars)} sent")
    return total


def from_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    col = "time_utc" if "time_utc" in df else "time"
    df["time"] = pd.to_datetime(df[col])
    return complete(df.sort_values("time").reset_index(drop=True))


def heartbeat_fresh() -> bool:
    try:
        with open(C.HEARTBEAT_FILE) as f:
            return time.time() - json.load(f)["ts"] < HEARTBEAT_FRESH_S
    except Exception:
        return False


def from_mt5(symbol: str, timeframe: str, start: datetime) -> tuple[pd.DataFrame, timedelta]:
    if heartbeat_fresh():
        raise SystemExit("The bot is running (its heartbeat is fresh). Stop it first: two processes must never "
                         "share the MT5 terminal.")
    import MetaTrader5 as mt5
    import signals
    if not mt5.initialize():
        raise SystemExit(f"MT5 init failed: {mt5.last_error()}")
    try:
        mt5.symbol_select(C.broker_symbol(symbol), True)
        off = signals.sync_server_offset()                    # the configured broker offset (SERVER_TZ)
        now = signals.server_now()
        rates = mt5.copy_rates_range(C.broker_symbol(symbol), signals.TF[timeframe], signals._mt5_dt(start),
                                     signals._mt5_dt(now + timedelta(hours=1)))
    finally:
        mt5.shutdown()
    if rates is None or not len(rates):
        raise SystemExit("MT5 returned no candles for that range (raise 'Max bars in chart' in MT5?)")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")          # server time
    bar = timedelta(minutes=C.BAR_MINUTES[timeframe])
    df = df[df["time"] + bar <= now].reset_index(drop=True)   # closed bars only
    df = complete(df)                                          # EMA/ATR on server-time bars (order is all that matters)
    df["time"] = df["time"] - off                              # -> UTC for "ts"
    return df, off


def main(argv=None, client=None, out=print) -> dict:
    ap = argparse.ArgumentParser(description="Backfill candles into the Assembler API")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv"); src.add_argument("--mt5", action="store_true")
    ap.add_argument("--symbol", required=True); ap.add_argument("--timeframe", default="M30")
    ap.add_argument("--from", dest="start", help="YYYY-MM-DD (with --mt5)")
    a = ap.parse_args(argv)
    if client is None and not (C.ASSEMBLER_URL and C.ASSEMBLER_RECORDS_TOKEN):
        raise SystemExit("Set ASSEMBLER_URL and ASSEMBLER_RECORDS_TOKEN in .env first.")
    symbol, tf = a.symbol.upper(), a.timeframe.upper()
    if tf not in C.BAR_MINUTES:
        raise SystemExit(f"timeframe must be one of {', '.join(C.BAR_MINUTES)}")
    if a.csv:
        df, off, source = from_csv(a.csv), None, "csv"
    else:
        if not a.start:
            raise SystemExit("--mt5 needs --from YYYY-MM-DD")
        df, off = from_mt5(symbol, tf, datetime.strptime(a.start, "%Y-%m-%d"))
        source = "mt5"
    bars = to_bars(df, off)
    out(f"{symbol} {tf}: {len(bars)} bars ({bars[0]['ts'] if bars else '-'} → {bars[-1]['ts'] if bars else '-'}), "
        f"source {source}")
    total = send(bars, symbol, tf, source, client=client, out=out)
    out(f"done: sent {total['sent']} · inserted {total['inserted']} · updated {total['updated']} · "
        f"rejected {total['rejected']} · {total['requests']} request(s)")
    return total


if __name__ == "__main__":
    main()
