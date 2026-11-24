"""The replay report: what was asked, what the data really covered, what happened, how it was delivered.

build(session, ...) -> dict ; write(rep, session, folder) -> {"json", "summary", "events", "trades", ...} files.

An indicator-only replay (strategy "indicators") reports events and data quality only: no profit, loss,
drawdown or win rate is computed for it, because no trading rule was tested.
"""
from __future__ import annotations
import csv, json, os
from collections import Counter
import pandas as pd
import config as C


def performance(session) -> dict | None:
    """Virtual-trade statistics (None for an indicator-only replay)."""
    if not session.strategy.trades:
        return None
    b = session.broker
    trades = b.closed
    nets = [t.net for t in trades]
    wins, losses = [x for x in nets if x > 0], [x for x in nets if x <= 0]
    eq = pd.DataFrame(b.equity_curve)
    dd = dd_pct = 0.0
    if len(eq):
        peak = eq["equity"].cummax()
        draw = peak - eq["equity"]
        k = int(draw.idxmax())
        dd = float(draw.max())
        dd_pct = float(draw.iloc[k] / peak.iloc[k] * 100) if peak.iloc[k] else 0.0
    monthly = []
    if trades:
        df = pd.DataFrame([{"month": str(t.exit_time)[:7], "net": t.net, "win": t.net > 0} for t in trades])
        bal = b.start_balance
        for m, g in df.groupby("month", sort=True):
            net = float(g["net"].sum())
            monthly.append({"month": m, "trades": int(len(g)), "net": round(net, 2),
                            "win_rate": round(float(g["win"].mean()) * 100, 1),
                            "return_pct": round(net / bal * 100, 2) if bal else None, "start_balance": round(bal, 2)})
            bal += net
    by_event: dict = {}
    for t in trades:
        k = t.event_type or t.reason
        a = by_event.setdefault(k, {"trades": 0, "net": 0.0, "wins": 0})
        a["trades"] += 1; a["net"] = round(a["net"] + t.net, 2); a["wins"] += t.net > 0
    bins = [-1e18, -1000, -500, -250, 0, 250, 500, 1000, 1e18]
    names = ["< -1000", "-1000..-500", "-500..-250", "-250..0", "0..250", "250..500", "500..1000", "> 1000"]
    dist = Counter(names[max(0, min(len(names) - 1, next(i for i in range(len(bins) - 1) if bins[i] < x <= bins[i + 1])))]
                   for x in nets)
    gp, gl = sum(wins), -sum(losses)
    return {"strategy": session.strategy.describe(), "trades": len(trades), "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else None,
            "net": round(sum(nets), 2), "gross_profit": round(gp, 2), "gross_loss": round(gl, 2),
            "profit_factor": round(gp / gl, 3) if gl else None,
            "avg_win": round(gp / len(wins), 2) if wins else None,
            "avg_loss": round(-gl / len(losses), 2) if losses else None,
            "expectancy": round(sum(nets) / len(trades), 2) if trades else None,
            "start_balance": b.start_balance, "end_balance": round(b.balance, 2),
            "max_drawdown": round(dd, 2), "max_drawdown_pct": round(dd_pct, 2),
            "commission": round(sum(t.commission for t in trades), 2),
            "ambiguous_bars": sum(t.ambiguous for t in trades),
            "sl_moves": sum(len(t.sl_moves) for t in trades), "blocked_sl_steps": sum(len(t.blocked_steps) for t in trades),
            "monthly": monthly, "by_signal": by_event, "distribution": {n: dist.get(n, 0) for n in names},
            "trade_management": b.trade_management}


def build(session, discord_status: dict | None = None, wall_seconds: float | None = None) -> dict:
    ds = session.ds
    perf = performance(session)
    started, finished = session.started_at, session.finished_at
    dur = wall_seconds if wall_seconds is not None else ((finished or started or 0) - (started or 0))
    return {
        "label": "HISTORICAL REPLAY", "session": session.id, "symbol": ds.symbol, "tf": ds.tf,
        "data": ds.coverage(), "candles_processed": session.done, "candles_in_range": session.total,
        "complete": session.finished, "bad_ticks_cleaned": session.cleaned,
        "events_by_type": dict(sorted(session.counts.items())),
        "signal_events": sum(1 for e in session.events if e["kind"] == "signal"),
        "discord": discord_status or {"enabled": False},
        "replay_seconds": round(dur, 3),
        "candles_per_second": round(session.done / dur, 1) if dur else None,
        "execution": {"mode": "simulation", "spec": vars(session.spec), "costs": vars(session.broker.costs),
                      "fills": "next candle open after the deciding close; bid prices; buys at ask = bid + spread; "
                               "stop before target when one candle touches both; gaps through the stop fill at the open",
                      "trade_management": session.broker.trade_management},
        "performance": perf,
        "notice": ("Indicator replay only: events are reported, no trading rule was tested, so no profit or loss "
                   "is implied." if perf is None else
                   "Simulated results on historical candles with the stated costs and fill policy. Not live results."),
        "evaluation": f"per candle, the live bot's window: the {session.s.window} candles before it plus itself "
                      f"(signal_engine + labels); no later candle is used",
    }


def _csv(path: str, rows: list[dict]):
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys or ["empty"])
        w.writeheader()
        for r in rows:
            w.writerow({k: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for k, v in r.items()})


def write(rep: dict, session, folder: str | None = None) -> dict:
    folder = folder or os.path.join(C.DATA_DIR, "replay", session.id)
    os.makedirs(folder, exist_ok=True)
    out = {"json": os.path.join(folder, "report.json"), "summary": os.path.join(folder, "summary.md"),
           "events": os.path.join(folder, "events.csv"), "gaps": os.path.join(folder, "gaps.csv")}
    with open(out["json"], "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1, default=str)
    _csv(out["events"], [{k: e[k] for k in ("seq", "kind", "type", "dir", "price", "bar_time", "close_time",
                                            "close_utc", "close_ist", "severity", "key", "status")} for e in session.events])
    _csv(out["gaps"], rep["data"]["gaps"])
    if rep["performance"] is not None:
        out["trades"] = os.path.join(folder, "trades.csv")
        out["equity"] = os.path.join(folder, "equity.csv")
        out["monthly"] = os.path.join(folder, "monthly.csv")
        _csv(out["trades"], [t.to_dict() for t in session.broker.closed])
        _csv(out["equity"], session.broker.equity_curve)
        _csv(out["monthly"], rep["performance"]["monthly"])
    with open(out["summary"], "w", encoding="utf-8") as f:
        f.write(summary_md(rep))
    return out


def summary_md(rep: dict) -> str:
    d = rep["data"]
    L = [f"# HISTORICAL REPLAY — {rep['symbol']} {rep['tf']} · session `{rep['session']}`", "",
         f"> {rep['notice']}", "",
         "## Data",
         f"- Requested: {d['requested'][0]} → {d['requested'][1]} (server time, {d['server_tz']})",
         f"- Actual: {d['actual'][0]} → {d['actual'][1]} · {d['candles']} candles · warm-up {d['warmup_candles']} candles",
         f"- Source: {d['source']} · dropped rows: {d['dropped_rows']}",
         f"- Gaps in range: {len(d['gaps'])} ({d['unexpected_gaps']} unexpected, "
         f"{d['missing_candles_unexpected']} candles missing in them)"]
    L += [f"- ⚠️ {p}" for p in d["problems"]]
    L += ["", "## Replay",
          f"- Candles processed: {rep['candles_processed']} of {rep['candles_in_range']}"
          f"{'' if rep['complete'] else ' (stopped early)'}",
          f"- Duration: {rep['replay_seconds']} s ({rep['candles_per_second']} candles/s)",
          f"- Evaluation: {rep['evaluation']}", "", "## Events by type", "", "| Event | Count |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in rep["events_by_type"].items()]
    dc = rep["discord"]
    L += ["", "## Discord (replay channel)",
          ("- disabled" if not dc.get("enabled") else
           f"- attempted {dc['attempted']} · delivered {dc['delivered']} · failed {dc['failed']} · "
           f"digests {dc['digests']} ({dc['events_in_digests']} events) · still waiting {dc['waiting']}")]
    p = rep["performance"]
    if p:
        L += ["", f"## Virtual trades — strategy `{p['strategy']['name']}` (simulated)",
              f"- Trades {p['trades']} · wins {p['wins']} · losses {p['losses']} · win rate {p['win_rate']}%",
              f"- Net {p['net']:+,.2f} · profit factor {p['profit_factor']} · expectancy {p['expectancy']}",
              f"- Balance {p['start_balance']:,.2f} → {p['end_balance']:,.2f} · max drawdown {p['max_drawdown']:,.2f} "
              f"({p['max_drawdown_pct']}%)",
              f"- Commission {p['commission']:,.2f} · ambiguous bars (stop assumed first) {p['ambiguous_bars']}",
              f"- Trade management {'ON' if p['trade_management'] else 'OFF'}: {p['sl_moves']} stop moves, "
              f"{p['blocked_sl_steps']} blocked steps", "",
              "| Month | Trades | Net | Win % | Return % |", "|---|---|---|---|---|"]
        L += [f"| {m['month']} | {m['trades']} | {m['net']:+,.2f} | {m['win_rate']} | {m['return_pct']} |"
              for m in p["monthly"]]
        L += ["", "| Signal | Trades | Net | Wins |", "|---|---|---|---|"]
        L += [f"| {k} | {v['trades']} | {v['net']:+,.2f} | {v['wins']} |" for k, v in p["by_signal"].items()]
    ex = rep["execution"]
    L += ["", "## Execution assumptions", f"- {ex['fills']}",
          f"- Costs: spread {ex['costs']['spread']}, commission {ex['costs']['commission_per_lot']}/lot round turn, "
          f"slippage {ex['costs']['slippage']}",
          f"- Symbol spec: {ex['spec']['source']} (contract {ex['spec']['contract_size']}, tick {ex['spec']['tick_size']}"
          f" = {ex['spec']['tick_value']} per lot)", ""]
    return "\n".join(L)
