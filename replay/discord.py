"""Replay notifications: a DEDICATED Discord webhook, every card marked HISTORICAL REPLAY. Off by default.

    REPLAY_DISCORD_ENABLED=false     REPLAY_DISCORD_WEBHOOK=     REPLAY_DISCORD_RATE_LIMIT=5   (messages / second)

* The live alert webhooks are refused (config.replay_problems): a replay can never post into the live channels.
* Cards reuse notifier.format_event (the live card), with the historical close time in server time, UTC and IST,
  the session id, and no mention / no ping.
* One worker thread posts in historical order; the replay itself only appends to a bounded queue (never waits on
  the network). If the replay outruns Discord, the OLDEST waiting cards are folded into a clearly labelled digest
  card, still in order. Nothing is dropped from the local log (logs/replay/<session>/events.jsonl), which is
  written whether or not Discord is enabled.
* A card counts as delivered only when Discord answered 2xx (notifier._post); failures are retried (3 tries) and
  counted. A final summary card is sent at the end.
"""
from __future__ import annotations
import json, logging, os, threading, time
from collections import deque
import config as C
import notifier, notify

log = logging.getLogger("replay.discord")
MAX_LINES = 20


def _event_dict(ev: dict) -> dict:
    """A replay event in the shape notifier.format_event expects."""
    e = {"type": ev["type"], "dir": ev["dir"] or "UNCLEAR", "price": ev["price"] or 0.0}
    for k, v in (ev.get("details") or {}).items():
        if k in ("tag", "lesser_gap", "finished", "pair", "dist", "dist_atr", "gap", "zone_lo", "zone_hi", "bars",
                 "pre_extreme", "pullback_extreme", "episode"):
            e[k] = v
    return e


def card(ev: dict) -> dict:
    """The live alert card for a replay event, re-labelled HISTORICAL REPLAY with historical times."""
    import pandas as pd
    if ev["kind"] == "signal":
        try:
            msg = notifier.format_event(ev["tf"], pd.Timestamp(ev["bar_time"]), _event_dict(ev), symbol=ev["symbol"],
                                        status=ev.get("status", ""))
        except Exception:                                          # an event without the fields the formatter needs
            msg = notifier.info(f"{ev['type']} {ev['dir']}", symbol=ev["symbol"], kind=ev["type"],
                                fields=[("Price", f"{ev['price']}")])
    else:
        d = ev.get("details") or {}
        tr = d.get("trade") or {}
        fields = [(k, str(v)) for k, v in (("Trade", tr.get("id", d.get("trade_id"))), ("Side", ev["dir"]),
                                           ("Lots", tr.get("lots")), ("Entry", tr.get("entry_price")),
                                           ("Exit", tr.get("exit_price")), ("SL", tr.get("sl", d.get("new"))),
                                           ("TP", tr.get("tp")), ("Net", tr.get("net")),
                                           ("Reason", tr.get("exit_reason") or tr.get("reason") or d.get("why")))
                  if v not in (None, "")]
        msg = notifier.info(f"🧪 {ev['type'].replace('_', ' ')}", symbol=ev["symbol"], kind=ev["type"], fields=fields)
    msg = dict(msg)
    msg["title"] = f"🕰️ HISTORICAL REPLAY · {msg['title']}"
    msg["fields"] = [f for f in msg.get("fields", []) if f[0] != "Candle closed"] + [
        ("Historical candle close", f"{ev['close_time']} server · {ev['close_utc'][:16].replace('T', ' ')} UTC · "
                                    f"{ev['close_ist']}", False),
        ("Replay", f"session {ev['session']} · #{ev['seq']} · {ev['symbol']} {ev['tf']}", False)]
    msg["footer"] = f"HISTORICAL REPLAY — simulated, not live · {ev['severity'].upper()}"
    msg["ping"] = False                                           # a replay never buzzes a phone
    msg.pop("buttons", None)
    msg.pop("image", None)
    return notify.redact(msg)


def digest_card(evs: list[dict]) -> dict:
    lines = [f"`{e['close_time'][5:16].replace('T', ' ')}` {e['type']} {e['dir']} @ {e['price']}" for e in evs]
    shown = lines[:MAX_LINES]
    more = len(lines) - len(shown)
    return {"title": f"🕰️ HISTORICAL REPLAY · DIGEST · {len(evs)} events (replay faster than Discord)",
            "desc": "\n".join(shown) + (f"\n… and {more} more (all in the replay report)" if more else ""),
            "color": "INFO", "fields": [("Replay", f"session {evs[0]['session']} · #{evs[0]['seq']}–#{evs[-1]['seq']}",
                                         False)],
            "footer": "HISTORICAL REPLAY — simulated, not live", "ping": False, "channel": "alerts",
            "symbol": evs[0]["symbol"], "kind": "REPLAY_DIGEST"}


class EventLog:
    """Every replay event, one JSON line each, in order. Always on."""
    def __init__(self, folder: str):
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "events.jsonl")
        self._f = open(self.path, "a", encoding="utf-8")

    def __call__(self, res: dict):
        for ev in res["events"]:
            self._f.write(json.dumps(ev, default=str, ensure_ascii=False) + "\n")
        if res.get("finished"):
            self._f.flush()

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass


class ReplayDiscord:
    """The replay's Discord sink. Use as a session sink: session.sinks.append(rd); rd.finish(summary) at the end."""

    def __init__(self, webhook: str | None = None, enabled: bool | None = None, rate: float | None = None,
                 max_queue: int | None = None, post=None, clock=time.monotonic, sleep=time.sleep,
                 min_severity: str = "low"):
        self.enabled = C.REPLAY_DISCORD_ENABLED if enabled is None else enabled
        self.webhook = webhook if webhook is not None else C.REPLAY_DISCORD_WEBHOOK
        self.rate = float(rate if rate is not None else C.REPLAY_DISCORD_RATE_LIMIT)
        self.max_queue = int(max_queue if max_queue is not None else C.REPLAY_DISCORD_QUEUE)
        self.min_rank = notify.E.RANK.get(min_severity, 0)
        self._post = post or (lambda payload: notifier._post(self.webhook, payload, "Replay Discord", attempts=3))
        self.clock, self.sleep = clock, sleep
        self.q: deque = deque()                                    # ("card", event) | ("digest", [events]) | ("summary", card)
        self.cv = threading.Condition()
        self.counts = {"events": 0, "queued": 0, "attempted": 0, "delivered": 0, "failed": 0, "digests": 0,
                       "events_in_digests": 0, "skipped_severity": 0}
        self.order: list[int] = []                                 # first seq of every delivered card (ordering proof)
        self._closing = False
        self._thread = None
        self.problem = None
        if self.enabled:
            bad = [p for p in C.replay_problems() if "REPLAY_DISCORD" in p]
            if not self.webhook:
                bad.append("no REPLAY_DISCORD_WEBHOOK")
            if bad:
                self.problem, self.enabled = "; ".join(bad), False
                log.error("replay Discord disabled: %s", self.problem)

    # ------------------------------------------------------------------------------------- producer
    def __call__(self, res: dict):
        if not self.enabled:
            return
        with self.cv:
            for ev in res["events"]:
                self.counts["events"] += 1
                if notify.E.RANK.get(ev.get("severity", "high"), 2) < self.min_rank:
                    self.counts["skipped_severity"] += 1
                    continue
                self.q.append(("card", ev))
                self.counts["queued"] += 1
            self._bound()
            self._ensure()
            self.cv.notify_all()

    def _bound(self):
        """Over the bound: fold the OLDEST waiting items into one digest at the head of the queue (order kept)."""
        if len(self.q) <= self.max_queue:
            return
        fold = []
        while len(self.q) > self.max_queue - 1:
            kind, item = self.q.popleft()
            fold += [item] if kind == "card" else item
        self.q.appendleft(("digest", fold))

    def _ensure(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker, daemon=True, name="replay-discord")
            self._thread.start()

    # ------------------------------------------------------------------------------------- consumer
    def _worker(self):
        interval = 1.0 / self.rate if self.rate > 0 else 0.0
        last = -1e18
        while True:
            with self.cv:
                while not self.q and not self._closing:
                    self.cv.wait(0.5)
                if not self.q and self._closing:
                    return
                kind, item = self.q.popleft()
            wait = interval - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
            last = self.clock()
            if kind == "card":
                payload, first = notifier._discord(card(item)), item["seq"]
            elif kind == "digest":
                payload, first = notifier._discord(digest_card(item)), item[0]["seq"]
                with self.cv:
                    self.counts["digests"] += 1; self.counts["events_in_digests"] += len(item)
            else:
                payload, first = notifier._discord(item), None
            payload.pop("_file", None)
            with self.cv:
                self.counts["attempted"] += 1
            try:
                ok = self._post(payload) == "ok"
            except Exception:
                log.exception("replay Discord post failed")
                ok = False
            with self.cv:
                self.counts["delivered" if ok else "failed"] += 1
                if ok and first is not None:
                    self.order.append(first)
                self.cv.notify_all()

    def finish(self, summary: dict | None = None, timeout: float = 300.0) -> dict:
        """Queue the final summary card, then wait for the queue to drain (or the timeout)."""
        if self.enabled and summary is not None:
            with self.cv:
                self.q.append(("summary", notify.redact(summary)))
                self._ensure(); self.cv.notify_all()
        with self.cv:
            self._closing = True; self.cv.notify_all()
        if self._thread:
            self._thread.join(timeout)
        return self.status()

    def status(self) -> dict:
        with self.cv:
            return {**self.counts, "enabled": self.enabled, "waiting": len(self.q), "problem": self.problem,
                    "rate_per_second": self.rate}


def summary_card(rep: dict) -> dict:
    """The final replay summary as a card (from report.build)."""
    cov = rep["data"]
    lines = [f"Requested {cov['requested'][0]} → {cov['requested'][1]} · actual {cov['actual'][0]} → {cov['actual'][1]}",
             f"{rep['candles_processed']} candles · {sum(rep['events_by_type'].values())} events · "
             f"{cov['unexpected_gaps']} unexpected gap(s)"]
    top = sorted(rep["events_by_type"].items(), key=lambda kv: -kv[1])[:8]
    fields = [("Events", " · ".join(f"{k} {v}" for k, v in top) or "none", False)]
    if rep.get("performance"):
        p = rep["performance"]
        fields.append(("Virtual trades", f"{p['trades']} trades · net {p['net']:+,.2f} · max DD {p['max_drawdown']:,.2f} "
                                         f"({p['max_drawdown_pct']:.1f}%)", False))
    else:
        fields.append(("Strategy", "indicator replay only: no profit or loss is implied", False))
    return {"title": f"🕰️ HISTORICAL REPLAY · SUMMARY · {rep['symbol']} {rep['tf']}", "desc": "\n".join(lines),
            "color": "INFO", "fields": fields + [("Replay", f"session {rep['session']}", False)],
            "footer": "HISTORICAL REPLAY — simulated, not live", "ping": False, "channel": "alerts",
            "symbol": rep["symbol"], "kind": "REPLAY_SUMMARY"}
