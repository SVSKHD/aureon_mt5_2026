"""Historical candles for the Replay Lab: MT5 download, CSV import, validation, gaps, warm-up, cache.

Time convention (the same as the live bot): `time` = the candle OPEN in broker SERVER time, naive. Every dataset
also carries `utc` (tz-aware UTC) and the server timezone it was converted with (`server_tz` in the metadata).
Server <-> UTC uses zoneinfo for the server timezone at THAT candle's date, so history across DST changes is right
(the live bot's measured offset is never used for history).

Nothing is fabricated: invalid rows are dropped and reported, missing candles are reported as gaps (classified as
weekend / daily break / holiday / unexpected), and a range the data does not cover is reported, not filled.
MT5 is reached only through ReadOnlyMT5, which exposes candle and symbol reads and nothing that can trade.
"""
from __future__ import annotations
import gzip, hashlib, json, logging, os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import config as C

log = logging.getLogger("replay.data")
TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240}
REPLAY_TFS = ("M15", "M30", "H1", "H4")
MIN_WARMUP = 3 * C.EMA_TREND                     # the same rule config.validate() applies to the live bot
CACHE_DIR = os.path.join(C.DATA_DIR, "replay", "cache")


class DataError(Exception):
    """The data cannot honestly answer the request (missing, too short, wrong timeframe)."""


# ------------------------------------------------------------------------------------------- timezones
def server_tz() -> str:
    """'UTC+2' style fixed offset when pinned in config, else the IANA zone (Europe/Athens by default)."""
    if C.SERVER_UTC_OFFSET_HOURS is not None:
        return f"UTC{C.SERVER_UTC_OFFSET_HOURS:+g}"
    return C.SERVER_TZ or "UTC"


def _tzinfo(name: str):
    if name.startswith("UTC") and len(name) > 3:
        return timezone(timedelta(hours=float(name[3:])))
    return ZoneInfo(name if name != "UTC" else "UTC")


def server_to_utc(times: pd.Series, tz_name: str) -> pd.Series:
    """Naive server times -> tz-aware UTC (DST per candle; a repeated autumn hour is read as the first one)."""
    t = pd.to_datetime(times)
    tz = _tzinfo(tz_name)
    if isinstance(tz, timezone):
        return (t - pd.Timedelta(tz.utcoffset(None))).dt.tz_localize("UTC")
    return t.dt.tz_localize(tz, ambiguous=np.ones(len(t), bool), nonexistent="shift_forward").dt.tz_convert("UTC")


def utc_to_server(utc: pd.Series, tz_name: str) -> pd.Series:
    u = pd.to_datetime(utc, utc=True)
    tz = _tzinfo(tz_name)
    return u.dt.tz_convert(tz).dt.tz_localize(None)


# -------------------------------------------------------------------------------------------- results
@dataclass
class Gap:
    start: str                    # last candle before the hole (server time)
    end: str                      # first candle after it
    missing: int                  # candles that would fit in between
    kind: str                     # weekend | daily_break | holiday | unexpected


@dataclass
class Dataset:
    symbol: str
    tf: str
    df: pd.DataFrame              # time (server, naive), utc, open, high, low, close, tick_volume?, partial?
    source: str
    server_tz: str
    requested: tuple[str, str] | None = None
    first_index: int = 0          # first candle inside the requested range (everything before = warm-up)
    last_index: int = -1
    gaps: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    dropped: int = 0

    @property
    def warmup_bars(self) -> int:
        return self.first_index

    def coverage(self) -> dict:
        r = self.df.iloc[self.first_index:self.last_index + 1]
        rng = (str(r["time"].iloc[0]), str(r["time"].iloc[-1])) if len(r) else (None, None)
        in_range = [g for g in self.gaps if self.requested and g.end >= self.requested[0] and g.start <= self.requested[1]]
        return {"symbol": self.symbol, "tf": self.tf, "source": self.source, "server_tz": self.server_tz,
                "requested": list(self.requested) if self.requested else None, "actual": list(rng),
                "candles": int(len(r)), "warmup_candles": int(self.warmup_bars),
                "gaps": [asdict(g) for g in in_range],
                "unexpected_gaps": sum(g.kind == "unexpected" for g in in_range),
                "missing_candles_unexpected": sum(g.missing for g in in_range if g.kind == "unexpected"),
                "dropped_rows": self.dropped, "problems": list(self.problems)}


# ----------------------------------------------------------------------------------------- validation
def normalise(df: pd.DataFrame, tz_name: str) -> tuple[pd.DataFrame, list[str], int]:
    """Sort, de-duplicate, drop rows that are not valid candles. Returns (df, problems, dropped rows)."""
    problems, n0 = [], len(df)
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"])
    for k in ("open", "high", "low", "close"):
        df[k] = pd.to_numeric(df[k], errors="coerce")
    bad_nan = df[["time", "open", "high", "low", "close"]].isna().any(axis=1)
    bad_ohlc = (df["high"] < df[["open", "close"]].max(axis=1)) | (df["low"] > df[["open", "close"]].min(axis=1)) | \
               (df["low"] <= 0)
    bad = bad_nan | bad_ohlc
    if bad.any():
        problems.append(f"{int(bad.sum())} invalid candle(s) dropped (missing values or high/low outside the body)")
    df = df[~bad].sort_values("time")
    dup = df["time"].duplicated(keep="last")
    if dup.any():
        problems.append(f"{int(dup.sum())} duplicate candle time(s): the last copy kept")
    df = df[~dup].reset_index(drop=True)
    df["utc"] = server_to_utc(df["time"], tz_name)
    return df, problems, n0 - len(df)


HOLIDAYS = {(12, 25), (1, 1)}             # the only full-day closures of gold every year; anything else is a hole


def classify_gap(a: datetime, b: datetime, minutes: int) -> str:
    """Why candles are missing between a (last before) and b (first after), server time. Only the weekend, the
    daily break and Christmas / New Year are expected; every other hole is "unexpected" and reported."""
    span = b - a
    if a.weekday() >= 4 and b.weekday() in (0, 6) and span <= timedelta(days=3, hours=12):
        return "weekend"
    if span <= timedelta(hours=2, minutes=minutes):
        return "daily_break"
    days = {(a + timedelta(days=k)).date() for k in range(0, span.days + 2)}
    if span <= timedelta(days=1, hours=12) and any((d.month, d.day) in HOLIDAYS for d in days):
        return "holiday"
    return "unexpected"


def find_gaps(df: pd.DataFrame, tf: str) -> list[Gap]:
    step = pd.Timedelta(minutes=TF_MINUTES[tf])
    t = df["time"]
    d = t.diff()
    out = []
    for i in np.flatnonzero((d > step).to_numpy()):
        a, b = t.iloc[i - 1].to_pydatetime(), t.iloc[i].to_pydatetime()
        out.append(Gap(str(a), str(b), int((b - a) / step) - 1, classify_gap(a, b, TF_MINUTES[tf])))
    return out


def resample(df: pd.DataFrame, src_tf: str, tf: str) -> pd.DataFrame:
    """Aggregate genuine candles to a higher timeframe in SERVER time (MT5's own bar boundaries). A candle built
    from fewer source candles than it spans is kept and flagged `partial` (never padded)."""
    if TF_MINUTES[tf] % TF_MINUTES[src_tf]:
        raise DataError(f"cannot build {tf} from {src_tf} (only higher timeframes can be derived)")
    if tf == src_tf:
        return df
    s = df.set_index("time")
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "tick_volume" in s:
        agg["tick_volume"] = "sum"
    rule = f"{TF_MINUTES[tf]}min"
    out = s.resample(rule, label="left", closed="left", origin="start_day").agg(agg)
    cnt = s["close"].resample(rule, label="left", closed="left", origin="start_day").count()
    out = out[cnt > 0]
    out["partial"] = (cnt[cnt > 0] < TF_MINUTES[tf] // TF_MINUTES[src_tf]).to_numpy()
    return out.reset_index()


# ----------------------------------------------------------------------------------------------- MT5
class ReadOnlyMT5:
    """The only MT5 surface the Replay Lab uses: candle and symbol reads. There is deliberately no attribute that
    can send, check or modify an order; asking for one raises."""
    ALLOWED = ("initialize", "shutdown", "symbol_select", "symbol_info", "copy_rates_range", "copy_rates_from",
               "last_error", "TIMEFRAME_M1", "TIMEFRAME_M5", "TIMEFRAME_M15", "TIMEFRAME_M30", "TIMEFRAME_H1",
               "TIMEFRAME_H4")

    def __init__(self, module=None):
        if module is None:
            import MetaTrader5 as module                           # the terminal must be installed and running
        object.__setattr__(self, "_m", module)

    def __getattr__(self, name):
        if name not in ReadOnlyMT5.ALLOWED:
            raise PermissionError(f"the Replay Lab may not use MetaTrader5.{name}")
        return getattr(self._m, name)

    def __setattr__(self, name, value):
        raise PermissionError("read-only")


def _rates_df(rates) -> pd.DataFrame:
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")             # naive = MT5 server time (repo convention)
    keep = ["time", "open", "high", "low", "close"] + (["tick_volume"] if "tick_volume" in df else [])
    return df[keep]


def from_mt5(symbol: str, tf: str, start: datetime, end: datetime, warmup: int = C.WARMUP_BARS, mt5=None,
             now_server: datetime | None = None) -> pd.DataFrame:
    """Closed candles of [start, end) in server time, plus `warmup` candles before start (by count, so weekends
    do not shorten it). The candle still forming at `now_server` is never included."""
    mt5 = mt5 or ReadOnlyMT5()
    if not mt5.initialize():
        raise DataError(f"MT5 is not available: {mt5.last_error()}")
    name = C.broker_symbol(symbol)
    mt5.symbol_select(name, True)
    code = getattr(mt5, f"TIMEFRAME_{tf}")
    aware = lambda d: d.replace(tzinfo=timezone.utc)                # MT5 shifts naive datetimes by the PC timezone
    before = mt5.copy_rates_from(name, code, aware(start - timedelta(seconds=1)), warmup)
    after = mt5.copy_rates_range(name, code, aware(start), aware(end - timedelta(seconds=1)))
    parts = [_rates_df(r) for r in (before, after) if r is not None and len(r)]
    if not parts:
        raise DataError(f"MT5 returned no {symbol} {tf} candles for {start} → {end} (raise 'Max bars in chart'?)")
    df = pd.concat(parts).drop_duplicates("time").sort_values("time").reset_index(drop=True)
    if now_server is not None:
        df = df[df["time"] + pd.Timedelta(minutes=TF_MINUTES[tf]) <= now_server]
    return df


def symbol_spec(symbol: str, mt5=None) -> dict | None:
    """Contract size, tick size, tick value, digits and volume rules from the broker (None if MT5 is not there)."""
    try:
        mt5 = mt5 or ReadOnlyMT5()
        if not mt5.initialize():
            return None
        info = mt5.symbol_info(C.broker_symbol(symbol))
    except Exception:
        return None
    if info is None:
        return None
    g = lambda k, d=None: getattr(info, k, d)
    return {"contract_size": g("trade_contract_size", 100.0), "tick_size": g("trade_tick_size", g("point", 0.01)),
            "tick_value": g("trade_tick_value", 1.0), "digits": g("digits", 2), "point": g("point", 0.01),
            "volume_min": g("volume_min", 0.01), "volume_step": g("volume_step", 0.01),
            "volume_max": g("volume_max", 100.0), "currency": g("currency_profit", "USD"), "source": "mt5"}


# ----------------------------------------------------------------------------------------------- CSV
TIME_COLS = ("time", "time_utc", "datetime", "date_time", "timestamp")


def read_csv(path: str, csv_tz: str | None = None) -> tuple[pd.DataFrame, str]:
    """A candle CSV: a time column (time_utc = UTC; otherwise `csv_tz`: 'utc' or 'server'), open/high/low/close,
    optional tick_volume/volume. Returns (df with server-time `time`, how the times were read)."""
    raw = pd.read_csv(path)
    cols = {c.lower().strip(): c for c in raw.columns}
    tcol = next((cols[c] for c in TIME_COLS if c in cols), None)
    if tcol is None and "date" in cols and "time" in cols:
        raw["_t"] = raw[cols["date"]].astype(str) + " " + raw[cols["time"]].astype(str); tcol = "_t"
    if tcol is None:
        raise DataError(f"{os.path.basename(path)}: no time column (one of {', '.join(TIME_COLS)})")
    missing = [k for k in ("open", "high", "low", "close") if k not in cols]
    if missing:
        raise DataError(f"{os.path.basename(path)}: missing column(s) {', '.join(missing)}")
    df = pd.DataFrame({"time": pd.to_datetime(raw[tcol]), **{k: raw[cols[k]] for k in ("open", "high", "low", "close")}})
    for v in ("tick_volume", "volume", "tickvol"):
        if v in cols:
            df["tick_volume"] = pd.to_numeric(raw[cols[v]], errors="coerce"); break
    kind = "utc" if tcol.lower() == "time_utc" else (csv_tz or "server").lower()
    if kind not in ("utc", "server"):
        raise DataError("csv_tz must be utc or server")
    if df["time"].dt.tz is not None:
        df["time"] = df["time"].dt.tz_convert("UTC").dt.tz_localize(None); kind = "utc"
    if kind == "utc":
        df["time"] = utc_to_server(df["time"].dt.tz_localize("UTC"), server_tz())
    return df, kind


def detect_tf(df: pd.DataFrame) -> str:
    d = df["time"].diff().dropna()
    if d.empty:
        raise DataError("not enough candles to tell the timeframe")
    m = int(d.mode().iloc[0].total_seconds() // 60)
    for tf, mins in TF_MINUTES.items():
        if mins == m:
            return tf
    raise DataError(f"unsupported candle spacing of {m} minutes")


# --------------------------------------------------------------------------------------------- cache
def _cache_path(symbol: str, tf: str, source: str) -> str:
    return os.path.join(CACHE_DIR, f"{symbol}_{tf}_{source}.csv.gz")


def save_cache(symbol: str, tf: str, source: str, df: pd.DataFrame, tz_name: str) -> str:
    """Merge into the persistent cache (atomic). Newer rows win on equal times."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(symbol, tf, source)
    old = load_cache(symbol, tf, source, tz_name)
    cols = [c for c in ("time", "open", "high", "low", "close", "tick_volume") if c in df]
    merged = pd.concat([old[cols]] + [df[cols]] if old is not None else [df[cols]])
    merged = merged.drop_duplicates("time", keep="last").sort_values("time")
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        merged.to_csv(f, index=False)
    os.replace(tmp, path)
    meta = {"symbol": symbol, "tf": tf, "source": source, "server_tz": tz_name, "rows": int(len(merged)),
            "first": str(merged["time"].iloc[0]), "last": str(merged["time"].iloc[-1]),
            "saved_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "time": "server, naive"}
    with open(path + ".json.tmp", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    os.replace(path + ".json.tmp", path + ".json")
    return path


def load_cache(symbol: str, tf: str, source: str, tz_name: str) -> pd.DataFrame | None:
    path = _cache_path(symbol, tf, source)
    if not os.path.exists(path):
        return None
    try:
        with open(path + ".json", encoding="utf-8") as f:
            meta = json.load(f)
        if meta.get("server_tz") != tz_name:                       # times were written for another server clock
            log.warning("cache %s was written for %s, not %s: ignored", path, meta.get("server_tz"), tz_name)
            return None
        df = pd.read_csv(path, compression="gzip")
        df["time"] = pd.to_datetime(df["time"])
        return df
    except Exception:
        log.exception("cache %s unreadable: ignored", path)
        return None


def file_id(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "csv-" + h.hexdigest()[:10]


# ---------------------------------------------------------------------------------------------- load
def load(symbol: str, tf: str, start: datetime, end: datetime, source: str = "mt5", csv_path: str | None = None,
         csv_tz: str | None = None, warmup: int = C.WARMUP_BARS, min_warmup: int = MIN_WARMUP, use_cache: bool = True,
         mt5=None, now_server: datetime | None = None) -> Dataset:
    """The candles for one replay: `warmup` candles before `start` (server time) and every closed candle in
    [start, end). source: mt5 | csv | cache. Raises DataError when the request cannot be met honestly."""
    symbol, tf = symbol.upper(), tf.upper()
    if symbol not in C.SYMBOLS:
        raise DataError(f"unknown symbol {symbol} (known: {', '.join(C.SYMBOLS)})")
    if tf not in REPLAY_TFS:
        raise DataError(f"timeframe {tf} is not supported (use {', '.join(REPLAY_TFS)})")
    if end <= start:
        raise DataError("the end date must be after the start date")
    tz_name = server_tz()
    label = source
    if source == "csv":
        if not csv_path:
            raise DataError("source csv needs a file")
        raw, kind = read_csv(csv_path, csv_tz)
        src_tf = detect_tf(raw)
        raw = resample(raw, src_tf, tf)
        label = file_id(csv_path)
        if use_cache:
            save_cache(symbol, tf, label, raw, tz_name)
    elif source == "mt5":
        raw = from_mt5(symbol, tf, start, end, warmup, mt5=mt5, now_server=now_server)
        if use_cache:
            save_cache(symbol, tf, "mt5", raw, tz_name)
    elif source == "cache":
        raw = load_cache(symbol, tf, "mt5", tz_name)
        if raw is None:
            raise DataError(f"no cached {symbol} {tf} candles (download from MT5 or import a CSV first)")
        label = "cache(mt5)"
    else:
        raise DataError("source must be mt5, csv or cache")
    df, problems, dropped = normalise(raw, tz_name)
    in_range = (df["time"] >= start) & (df["time"] < end)
    if not in_range.any():
        have = f"{df['time'].iloc[0]} → {df['time'].iloc[-1]}" if len(df) else "nothing"
        raise DataError(f"no {symbol} {tf} candles in {start} → {end} (the data covers {have})")
    first = int(np.argmax(in_range.to_numpy()))
    last = int(len(in_range) - 1 - np.argmax(in_range.to_numpy()[::-1]))
    lo = max(0, first - warmup)
    df = df.iloc[lo:last + 1].reset_index(drop=True)
    first, last = first - lo, last - lo
    if first < min_warmup:
        raise DataError(f"only {first} warm-up candles before {start}; at least {min_warmup} are needed for an "
                        f"EMA{C.EMA_TREND} that matches the chart (start later or load more history)")
    if first < warmup:
        problems.append(f"warm-up has {first} candles (wanted {warmup})")
    step = pd.Timedelta(minutes=TF_MINUTES[tf])
    if df["time"].iloc[first] - pd.Timestamp(start) > step * 3 and \
            classify_gap(pd.Timestamp(start).to_pydatetime(), df["time"].iloc[first].to_pydatetime(), TF_MINUTES[tf]) == "unexpected":
        problems.append(f"data starts at {df['time'].iloc[first]}, later than the requested {start}")
    if pd.Timestamp(end) - (df["time"].iloc[last] + step) > step * 3 and \
            classify_gap((df["time"].iloc[last] + step).to_pydatetime(), pd.Timestamp(end).to_pydatetime(), TF_MINUTES[tf]) == "unexpected":
        problems.append(f"data ends at {df['time'].iloc[last]}, earlier than the requested {end}")
    if "partial" in df and df["partial"].iloc[first:].any():
        problems.append(f"{int(df['partial'].iloc[first:].sum())} {tf} candle(s) built from incomplete source candles")
    ds = Dataset(symbol, tf, df, label, tz_name, (str(pd.Timestamp(start)), str(pd.Timestamp(end))), first, last,
                 find_gaps(df, tf), problems, dropped)
    for g in ds.gaps:
        if g.kind == "unexpected":
            problems.append(f"unexpected gap {g.start} → {g.end} ({g.missing} candles)")
    return ds


def load_intrabar(symbol: str, start: datetime, end: datetime, source: str = "mt5", csv_path: str | None = None,
                  csv_tz: str | None = None, mt5=None) -> pd.DataFrame | None:
    """Genuine M1 candles for the enhanced animation (None if unavailable; the replay then reveals whole candles)."""
    try:
        if source == "csv" and csv_path:
            raw, _ = read_csv(csv_path, csv_tz)
            if detect_tf(raw) != "M1":
                return None
        elif source == "mt5":
            mt5 = mt5 or ReadOnlyMT5()
            if not mt5.initialize():
                return None
            r = mt5.copy_rates_range(C.broker_symbol(symbol), mt5.TIMEFRAME_M1, start.replace(tzinfo=timezone.utc),
                                     end.replace(tzinfo=timezone.utc))
            if r is None or not len(r):
                return None
            raw = _rates_df(r)
        else:
            return None
        df, _, _ = normalise(raw, server_tz())
        return df[(df["time"] >= start) & (df["time"] < end)].reset_index(drop=True)
    except Exception:
        log.exception("M1 history for the animation is unavailable")
        return None
