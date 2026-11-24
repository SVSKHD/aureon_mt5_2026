"""Headless replay / backtest:

    python -m replay --symbol XAUUSD --tf M30 --from 2026-10-01 --to 2026-10-31 --source csv \
        --csv tests/data/XAUUSD_M30_sample_20260901_20261030.csv [--strategy ema_cross] [--trade-management]

    python -m replay app          # the desktop replay window (PySide6)
"""
import argparse, json, logging, sys, time


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["app"]:
        from replay.app.main import main as app_main
        return app_main(argv[1:])
    import config as C
    from replay import api
    ap = argparse.ArgumentParser(prog="python -m replay", description="Aureon historical replay (simulation only)")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M30", choices=["M15", "M30", "H1", "H4"])
    ap.add_argument("--from", dest="start", required=True, help="server time, e.g. 2026-10-01 or '2026-10-01 06:00'")
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--source", default="mt5", choices=["mt5", "csv", "cache"])
    ap.add_argument("--csv")
    ap.add_argument("--csv-tz", choices=["utc", "server"], help="time column zone (a time_utc column is always UTC)")
    ap.add_argument("--strategy", default="indicators", choices=["indicators", "ema_cross", "pullback", "manual"])
    ap.add_argument("--params", default="{}", help='JSON, e.g. {"align_200": true} or {"trades": [...]}')
    ap.add_argument("--protective", default="{}", help='JSON, e.g. {"sl_mode": "atr", "sl": 2, "tp_mode": "r", "tp": 2}')
    ap.add_argument("--sizing", default="{}", help='JSON, e.g. {"mode": "risk", "risk_pct": 1}')
    ap.add_argument("--trade-management", action="store_true", default=C.REPLAY_TRADE_MANAGEMENT_ENABLED,
                    help="simulate the stepped trailing stop on virtual trades (default: REPLAY_TRADE_MANAGEMENT_ENABLED)")
    ap.add_argument("--balance", type=float, default=float(C.REPLAY_ACCOUNT_BALANCE))
    ap.add_argument("--spread", type=float, default=float(C.REPLAY_SPREAD))
    ap.add_argument("--commission", type=float, default=float(C.REPLAY_COMMISSION_PER_LOT))
    ap.add_argument("--slippage", type=float, default=float(C.REPLAY_SLIPPAGE))
    ap.add_argument("--discord", action="store_true", default=C.REPLAY_DISCORD_ENABLED,
                    help="send HISTORICAL REPLAY cards to REPLAY_DISCORD_WEBHOOK")
    ap.add_argument("--out", help="report folder (default logs/replay/<session>)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    req = api.Request(a.symbol, a.tf, a.start, a.end, a.source, a.csv, a.csv_tz, a.strategy, json.loads(a.params),
                      json.loads(a.protective), json.loads(a.sizing), a.trade_management, a.balance, a.spread,
                      a.commission, a.slippage, a.discord, False, a.out)
    p = api.prepare(req)
    t0 = time.monotonic()
    p.session.run()
    rep, files = api.finish(p, time.monotonic() - t0)
    print(open(files["summary"], encoding="utf-8").read())
    print("Files:", json.dumps(files, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
