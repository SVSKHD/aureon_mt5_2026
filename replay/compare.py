"""Strategy comparison: identical candles, identical evaluation, identical costs and fill policy.

The candles are loaded once and every candle is evaluated once (one ReplaySession); each strategy x sizing variant is
an Account trading on those same events in lockstep. Each row reports fixed-lot and dynamic-lot variants, monthly
P&L, equity curve, max drawdown, win rate, profit factor, average R, consecutive losses, entries/exits, costs,
strategy version and parameter hash, plus a fingerprint of the data, so the comparison can be reproduced exactly.

Only strategies implemented in replay/strategies.py are compared. C5 Freeze 001 and H1 Horizon One are NOT: their
definitions are not in this repository. The EMA 20/50 strategy is the experimental basic version and is flagged.
"""
from __future__ import annotations
import csv, hashlib, json, os, time
import config as C
from replay import data as D, strategies as S, report as R
from replay.broker import Costs, spec_for
from replay.engine import ReplaySession, ReplaySettings

DEFAULT_SET = ("ema_cross", "pullback", "ema_20_50_basic")
SIZINGS = {"fixed_1_lot": {"mode": "fixed", "lots": 1.0}, "risk_1pct": {"mode": "risk", "risk_pct": 1.0}}
NOT_AVAILABLE = {"C5 Freeze 001": "definition not in this repository: not implemented, not reproduced",
                 "H1 Horizon One": "definition not in this repository: not implemented, not reproduced"}


def data_fingerprint(ds: D.Dataset) -> str:
    cols = ds.df[["time", "open", "high", "low", "close"]].iloc[: ds.last_index + 1]
    return hashlib.sha1(cols.to_csv(index=False).encode()).hexdigest()[:16]


def run(ds: D.Dataset, names=DEFAULT_SET, sizings: dict | None = None, protective: dict | None = None,
        costs: Costs | None = None, balance: float = float(C.REPLAY_ACCOUNT_BALANCE), trade_management: bool = False,
        spec=None) -> dict:
    sizings = sizings or SIZINGS
    variants = [(n, sz_name, S.make(n, protective=protective, sizing=sz)) for n in names for sz_name, sz in sizings.items()]
    s = ReplaySession(ds, ReplaySettings(strategy=variants[0][2], extra_strategies=[v[2] for v in variants[1:]],
                                         trade_management=trade_management, balance=balance, costs=costs or Costs(),
                                         spec=spec or spec_for(ds.symbol)))
    t0 = time.monotonic()
    s.run()
    wall = time.monotonic() - t0
    rows = []
    for (name, sz_name, _), acc in zip(variants, s.accounts):
        perf = R.account_performance(acc.strategy, acc.broker)
        rows.append({"strategy": name, "version": acc.strategy.version, "param_hash": acc.strategy.param_hash,
                     "sizing": sz_name, "experimental": bool(acc.strategy.describe().get("experimental")),
                     **{k: perf[k] for k in ("trades", "win_rate", "net", "profit_factor", "avg_r", "expectancy",
                                             "max_drawdown", "max_drawdown_pct", "max_consecutive_losses",
                                             "start_balance", "end_balance", "ambiguous_bars", "sl_moves",
                                             "blocked_sl_steps")},
                     "costs": perf["costs"], "monthly": perf["monthly"], "exits": perf["exits"], "entries": perf["entries"],
                     "unverified_rules": acc.strategy.describe().get("unverified_rules")})
    b = s.broker
    return {"label": "HISTORICAL REPLAY — STRATEGY COMPARISON (simulated)", "session": s.id,
            "symbol": ds.symbol, "tf": ds.tf, "data": ds.coverage(), "data_fingerprint": data_fingerprint(ds),
            "candles": s.done, "seconds": round(wall, 2), "rows": rows, "not_compared": NOT_AVAILABLE,
            "assumptions": {"spread": b.costs.spread, "commission_per_lot_round_turn": b.costs.commission_per_lot,
                            "slippage": b.costs.slippage, "spec": vars(b.spec), "start_balance": balance,
                            "trade_management": trade_management, "protective": protective or vars(S.Protective()),
                            "fills": "decided at a candle's close, filled at the next open; bid candles, ask = bid + "
                                     "spread; one candle touching stop and target = stop first; gaps through the stop "
                                     "fill at the open"},
            "_accounts": list(zip(variants, s.accounts))}


def write(rep: dict, folder: str) -> dict:
    os.makedirs(folder, exist_ok=True)
    accounts = rep.pop("_accounts", [])
    out = {"json": os.path.join(folder, "comparison.json"), "csv": os.path.join(folder, "comparison.csv"),
           "summary": os.path.join(folder, "comparison.md")}
    with open(out["json"], "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1, default=str)
    flat = [{k: v for k, v in r.items() if k not in ("monthly", "exits", "entries", "costs", "unverified_rules")} |
            {"commission": r["costs"]["commission"]} for r in rep["rows"]]
    with open(out["csv"], "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(flat[0]))
        w.writeheader(); w.writerows(flat)
    for (name, sz, _), acc in accounts:
        R._csv(os.path.join(folder, f"trades_{name}_{sz}.csv"), [t.to_dict() for t in acc.broker.closed])
        R._csv(os.path.join(folder, f"equity_{name}_{sz}.csv"), acc.broker.equity_curve)
    with open(out["summary"], "w", encoding="utf-8") as f:
        f.write(summary_md(rep))
    return out


def summary_md(rep: dict) -> str:
    d, a = rep["data"], rep["assumptions"]
    L = [f"# {rep['label']}", "", f"{rep['symbol']} {rep['tf']} · {d['actual'][0]} → {d['actual'][1]} (server time) · "
         f"{rep['candles']} candles · data fingerprint `{rep['data_fingerprint']}` · {rep['seconds']} s", "",
         "> Simulated on historical candles. Not live results, not a forecast. Same candles, same events, same costs "
         "for every row.", "",
         "| Strategy | Version | Params | Sizing | Trades | Win % | Net | PF | Avg R | Max DD | Max DD % | Max losses in a row |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        L.append(f"| {r['strategy']}{' ⚠️ experimental' if r['experimental'] else ''} | {r['version']} | `{r['param_hash']}` | "
                 f"{r['sizing']} | {r['trades']} | {r['win_rate']} | {r['net']:+,.2f} | {r['profit_factor']} | {r['avg_r']} | "
                 f"{r['max_drawdown']:,.2f} | {r['max_drawdown_pct']} | {r['max_consecutive_losses']} |")
    L += ["", "## Monthly net", "", "| Strategy | Sizing | " + " | ".join(m["month"] for m in rep["rows"][0]["monthly"]) + " |",
          "|---|---|" + "---|" * len(rep["rows"][0]["monthly"])]
    for r in rep["rows"]:
        L.append(f"| {r['strategy']} | {r['sizing']} | " + " | ".join(f"{m['net']:+,.0f}" for m in r["monthly"]) + " |")
    L += ["", "## Exits", ""] + [f"- {r['strategy']} / {r['sizing']}: " + " · ".join(f"{k} {v}" for k, v in r["exits"].items())
                                 for r in rep["rows"]]
    L += ["", "## Not compared", ""] + [f"- {k}: {v}" for k, v in rep["not_compared"].items()]
    exp = [r for r in rep["rows"] if r["experimental"]]
    if exp:
        L += ["", "## Unverified rules (experimental EMA 20/50)", ""] + [f"- {x}" for x in exp[0]["unverified_rules"]]
    L += ["", "## Assumptions", f"- Spread {a['spread']} · commission {a['commission_per_lot_round_turn']}/lot round turn · "
          f"slippage {a['slippage']} · start balance {a['start_balance']:,.0f} · trade management "
          f"{'ON (simulated)' if a['trade_management'] else 'OFF'}",
          f"- Protective: {a['protective']}", f"- Fills: {a['fills']}",
          f"- Symbol spec: {a['spec']['source']}", ""]
    return "\n".join(L)
