"""One replay, start to finish: shared by the command line (python -m replay) and the desktop app."""
from __future__ import annotations
import json, logging, os, time
from dataclasses import dataclass, field
from datetime import datetime
import config as C
from replay import data as D, report as R
from replay.broker import Costs, spec_for
from replay.discord import ReplayDiscord, EventLog, summary_card
from replay.engine import ReplaySession, ReplaySettings
from replay import strategies as S

log = logging.getLogger("replay")


@dataclass
class Request:
    symbol: str = "XAUUSD"
    tf: str = "M30"
    start: str = ""
    end: str = ""
    source: str = "mt5"                    # mt5 | csv | cache
    csv_path: str | None = None
    csv_tz: str | None = None
    strategy: str = "indicators"
    params: dict = field(default_factory=dict)
    protective: dict = field(default_factory=dict)
    sizing: dict = field(default_factory=dict)
    trade_management: bool = C.REPLAY_TRADE_MANAGEMENT_ENABLED
    balance: float = float(C.REPLAY_ACCOUNT_BALANCE)
    spread: float = float(C.REPLAY_SPREAD)
    commission: float = float(C.REPLAY_COMMISSION_PER_LOT)
    slippage: float = float(C.REPLAY_SLIPPAGE)
    discord: bool = C.REPLAY_DISCORD_ENABLED
    animate: bool = False                  # forming candle from genuine M1 data (if available)
    out_dir: str | None = None

    @staticmethod
    def parse_time(s: str) -> datetime:
        return datetime.fromisoformat(s.strip().replace("T", " "))


@dataclass
class Prepared:
    """The loaded data and settings of one request. fresh() builds a new, fully isolated session from them (the
    first run and every restart), with its own event log folder and its own replay Discord queue."""
    req: Request
    ds: D.Dataset
    spec: object
    intrabar: object
    animate_note: str
    session: ReplaySession | None = None
    discord: ReplayDiscord | None = None
    log: EventLog | None = None
    folder: str = ""

    def fresh(self) -> ReplaySession:
        req = self.req
        strat = S.make(req.strategy, protective=req.protective or None, sizing=req.sizing or None, **(req.params or {}))
        settings = ReplaySettings(strategy=strat, trade_management=req.trade_management, balance=req.balance,
                                  costs=Costs(req.spread, req.commission, req.slippage), spec=self.spec)
        s = ReplaySession(self.ds, settings, intrabar=self.intrabar)
        self.folder = req.out_dir or os.path.join(C.DATA_DIR, "replay", s.id)
        if self.log:
            self.log.close()
        self.log = EventLog(self.folder)
        self.discord = ReplayDiscord(enabled=req.discord)
        s.sinks += [self.log, self.discord]
        with open(os.path.join(self.folder, "request.json"), "w", encoding="utf-8") as f:
            json.dump({**vars(req), "session": s.id}, f, indent=1, default=str)
        self.session = s
        return s


def prepare(req: Request, mt5=None) -> Prepared:
    """Load and validate the data (raises DataError with the reason), then build the first session."""
    bad = C.replay_problems()
    if bad and any("EXECUTION_MODE" in b for b in bad):
        raise D.DataError("; ".join(bad))
    start, end = Request.parse_time(req.start), Request.parse_time(req.end)
    ds = D.load(req.symbol, req.tf, start, end, source=req.source, csv_path=req.csv_path, csv_tz=req.csv_tz, mt5=mt5)
    S.make(req.strategy, protective=req.protective or None, sizing=req.sizing or None, **(req.params or {}))  # validate
    spec = spec_for(ds.symbol, D.symbol_spec(ds.symbol, mt5=mt5) if req.source == "mt5" else None)
    intrabar, note = None, "closed-candle replay (no lower-timeframe history: intrabar movement is not invented)"
    if req.animate:
        intrabar = D.load_intrabar(ds.symbol, start, end, source=req.source, mt5=mt5)
        if intrabar is not None and len(intrabar):
            note = f"enhanced animation from {len(intrabar)} genuine M1 candles"
    p = Prepared(req, ds, spec, intrabar, note)
    p.fresh()
    return p


def finish(p: Prepared, wall_seconds: float | None = None) -> tuple[dict, dict]:
    """Write the report (always), send the Discord summary (if enabled) and wait for the replay queue."""
    rep = R.build(p.session, p.discord.status(), wall_seconds)
    status = p.discord.finish(summary_card(rep) if p.discord.enabled else None)
    rep["discord"] = status
    rep["animation"] = p.animate_note
    files = R.write(rep, p.session, p.folder)
    p.log.close()
    return rep, files
