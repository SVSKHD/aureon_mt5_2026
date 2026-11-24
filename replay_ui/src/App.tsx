import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { Bridge } from "./bridge";
import { ReplayChart, THEME } from "./chart";
import { store } from "./store";
import type { Bar, InMsg, ReplayEvent, Trade } from "./types";

const SPEEDS: [string, number | null][] = [["0.5×", 0.5], ["1×", 1], ["2×", 2], ["5×", 5], ["10×", 10], ["30×", 30], ["60×", 60], ["Max", null]];
const fmt = (x: number | null | undefined, d = 2) => (x === null || x === undefined ? "–" : x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));

function useStore() {
  return useSyncExternalStore(store.subscribe, store.get);
}

export default function App({ bridge }: { bridge: Bridge }) {
  const s = useStore();
  const chartEl = useRef<HTMLDivElement>(null);
  const chart = useRef<ReplayChart | null>(null);
  const readout = useRef<HTMLDivElement>(null);
  const [following, setFollowing] = useState(true);
  const [panels, setPanels] = useState({ details: true, timeline: true, trades: true, stats: true });
  const [sideOpen, setSideOpen] = useState(true);
  const [speed, setSpeed] = useState<number | null>(2);
  const [spacing, setSpacing] = useState(8);
  const [signals, setSignals] = useState(true);
  const digits = s.session?.digits ?? 2;
  const [form, setForm] = useState({
    symbol: "XAUUSD", tf: "M30", start: "2026-10-01", end: "2026-10-31", source: "mt5", strategy: "indicators",
    trade_management: false, discord: false, animate: false, balance: 50000,
  });

  // ------------------------------------------------------------------- chart + bridge wiring (once)
  useEffect(() => {
    const c = new ReplayChart(chartEl.current!);
    chart.current = c;
    c.onFollow = setFollowing;
    c.onClickEvent = (seq) => {
      const e = store.s.events.find((x) => x.seq === seq);
      if (e) store.update({ selected: e }, true);
    };
    c.onCrosshair = ({ bar }) => paintReadout(readout.current, bar ?? lastBar.current, digits);
    (window as unknown as { __chart: ReplayChart }).__chart = c;
    const lastBar = { current: null as Bar | null };
    bridge.onMessage((m: InMsg) => {
      switch (m.type) {
        case "config":
          store.update({ config: m }, true);
          setForm((f) => ({ ...f, ...(m.defaults as object) }));
          break;
        case "session":
          c.reset(m.warmup);
          lastBar.current = m.warmup[m.warmup.length - 1] ?? null;
          store.reset({ session: m, state: "running", balance: m.balance, equity: m.balance });
          paintReadout(readout.current, lastBar.current, m.digits);
          break;
        case "batch":
          c.apply(m.bars, m.events, m.forming);
          if (m.bars.length) lastBar.current = m.bars[m.bars.length - 1];
          if (m.forming) lastBar.current = m.forming;
          store.addEvents(m.events);
          store.update({ progress: m.progress ?? store.s.progress, openTrades: m.open_trades ?? store.s.openTrades,
            equity: m.equity ?? store.s.equity, balance: m.balance ?? store.s.balance, status: m.status ?? store.s.status });
          break;
        case "state":
          store.update({ state: m.state, error: m.error ?? null }, true);
          break;
        case "discord":
          store.update({ discord: m.status });
          break;
        case "report":
          store.update({ report: { summary: m.summary, files: m.files } }, true);
          break;
        case "csv_picked":
          store.update({ csvPath: m.path }, true);
          setForm((f) => ({ ...f, source: "csv" }));
          break;
        case "error":
          store.update({ error: m.text, state: store.s.state === "loading" ? "idle" : store.s.state }, true);
          break;
      }
    });
    return () => c.destroy();
  }, [bridge]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------------------------------------------------------------------------------- commands
  const send = useCallback((cmd: string, extra: Record<string, unknown> = {}) => bridge.send({ cmd, ...extra }), [bridge]);
  const start = () => {
    store.update({ state: "loading", error: null, report: null }, true);
    send("start", { request: { ...form, csv_path: form.source === "csv" ? store.s.csvPath : null, speed } });
  };
  const running = s.state === "running";
  const active = ["running", "paused"].includes(s.state);
  const toggle = () => send(running ? "pause" : "resume");
  const changeSpeed = (v: number | null) => { setSpeed(v); send("speed", { value: v }); };
  const jump = () => chart.current?.jumpToLatest();
  const fullscreen = () => {
    if (bridge.kind === "qt") send("fullscreen");
    else if (!document.fullscreenElement) document.documentElement.requestFullscreen?.();
    else document.exitFullscreen?.();
  };

  // ------------------------------------------------------------------------------- shortcuts
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).tagName === "INPUT" || (e.target as HTMLElement).tagName === "SELECT") return;
      const i = SPEEDS.findIndex(([, v]) => v === speed);
      if (e.code === "Space") { e.preventDefault(); if (active) toggle(); }
      else if (e.key === "ArrowRight") { e.preventDefault(); if (active) send("step"); }
      else if (e.key === "+" || e.key === "=") changeSpeed(SPEEDS[Math.min(SPEEDS.length - 1, i + 1)][1]);
      else if (e.key === "-") changeSpeed(SPEEDS[Math.max(0, i - 1)][1]);
      else if (e.key === "End") jump();
      else if (e.key.toLowerCase() === "f") fullscreen();
      else if (e.key.toLowerCase() === "p") setSideOpen((v) => !v);
      else if (e.key.toLowerCase() === "m") { setSignals((v) => { chart.current?.setSignalsVisible(!v); return !v; }); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const pct = s.progress && s.progress.total ? (s.progress.done / s.progress.total) * 100 : 0;
  const cfg = s.config;

  return (
    <div className="app">
      <header className="toolbar">
        <span className="brand">AUREON <b>REPLAY LAB</b></span>
        <select value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value })} disabled={active}>
          {(cfg?.symbols ?? ["XAUUSD"]).map((x) => <option key={x}>{x}</option>)}
        </select>
        <select value={form.tf} onChange={(e) => setForm({ ...form, tf: e.target.value })} disabled={active}>
          {(cfg?.tfs ?? ["M15", "M30", "H1", "H4"]).map((x) => <option key={x}>{x}</option>)}
        </select>
        <label>From <input type="date" value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} disabled={active} /></label>
        <label>To <input type="date" value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} disabled={active} /></label>
        <select value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })} disabled={active} title="Data source">
          {(cfg?.sources ?? ["mt5", "csv", "cache"]).map((x) => <option key={x} value={x}>{x.toUpperCase()}</option>)}
        </select>
        {form.source === "csv" && <button onClick={() => send("pick_csv")} disabled={active} title={s.csvPath}>{s.csvPath ? s.csvPath.split(/[\\/]/).pop() : "Choose CSV…"}</button>}
        <select value={form.strategy} onChange={(e) => setForm({ ...form, strategy: e.target.value })} disabled={active} title="Strategy">
          {(cfg?.strategies ?? ["indicators"]).map((x) => <option key={x} value={x}>{x === "indicators" ? "Indicators only" : x}</option>)}
        </select>
        <label className="chk" title="Simulate the stepped trailing stop on virtual trades (replay only)">
          <input type="checkbox" checked={form.trade_management} onChange={(e) => setForm({ ...form, trade_management: e.target.checked })} disabled={active} /> Trade mgmt (sim)
        </label>
        <label className="chk" title={cfg?.discord_configured ? "Send HISTORICAL REPLAY cards to the replay channel" : (cfg?.discord_problem ?? "REPLAY_DISCORD_WEBHOOK not set")}>
          <input type="checkbox" checked={form.discord} disabled={active || !cfg?.discord_configured} onChange={(e) => setForm({ ...form, discord: e.target.checked })} /> Discord
        </label>
        <label className="chk" title="Build the active candle from genuine M1 history when available">
          <input type="checkbox" checked={form.animate} onChange={(e) => setForm({ ...form, animate: e.target.checked })} disabled={active} /> Intrabar
        </label>
        <button className="primary" onClick={start} disabled={active || s.state === "loading"}>{s.state === "loading" ? "Loading…" : "Load & start"}</button>
      </header>

      <div className="playback">
        <button onClick={toggle} disabled={!active} title="Space">{running ? "❚❚ Pause" : "▶ Play"}</button>
        <button onClick={() => send("step")} disabled={!active} title="→">Step ▸|</button>
        <button onClick={() => send("restart")} disabled={!s.session} title="Restart from the first candle (new session)">↺ Restart</button>
        <button onClick={() => send("stop")} disabled={!active}>■ Stop</button>
        <span className="sep" />
        {SPEEDS.map(([l, v]) => <button key={l} className={speed === v ? "on" : ""} onClick={() => changeSpeed(v)} title="+ / −">{l}</button>)}
        <span className="sep" />
        <label title="Candle spacing">↔ <input type="range" min={2} max={30} value={spacing} onChange={(e) => { setSpacing(+e.target.value); chart.current?.setSpacing(+e.target.value); }} /></label>
        <button onClick={jump} className={following ? "" : "attn"} title="End">{following ? "● Live edge" : "⇥ Jump to latest"}</button>
        <button onClick={() => { setSignals(!signals); chart.current?.setSignalsVisible(!signals); }} title="M">{signals ? "Markers ✓" : "Markers ✕"}</button>
        <button onClick={() => setSideOpen(!sideOpen)} title="P">{sideOpen ? "Panels ⟩" : "⟨ Panels"}</button>
        <button onClick={fullscreen} title="F">⛶</button>
        <div className="progress" title={`${s.progress?.done ?? 0} / ${s.progress?.total ?? 0}`}><div style={{ width: `${pct}%` }} /></div>
        <span className="mono">{s.progress ? `${s.progress.done}/${s.progress.total}` : ""}</span>
      </div>

      <main className="main">
        <div className="chartwrap">
          <div ref={chartEl} className="chart" />
          <div ref={readout} className="readout mono" />
          <div className="legend"><i style={{ background: THEME.ema8 }} />EMA 8 <i style={{ background: THEME.ema32 }} />EMA 32 <i style={{ background: THEME.ema200 }} />EMA 200
            <span className="hist">HISTORICAL REPLAY · simulated</span></div>
          {!s.session && <div className="empty">Choose a symbol, timeframe and date range, then <b>Load &amp; start</b>.<br />Candles are revealed one closed candle at a time; nothing from the future is ever drawn.</div>}
        </div>
        {sideOpen && <aside className="side">
          <Panel title="Signal details" open={panels.details} toggle={() => setPanels({ ...panels, details: !panels.details })}>
            <Details e={s.selected} />
          </Panel>
          <Panel title={`Notifications (${s.events.length})`} open={panels.timeline} toggle={() => setPanels({ ...panels, timeline: !panels.timeline })} grow>
            <Timeline events={s.events} version={s.version} onPick={(e) => { store.update({ selected: e }, true); chart.current?.jumpTo(e.bar_time); }} />
          </Panel>
          <Panel title={`Trades (${s.openTrades.length} open · ${s.closedTrades.length} closed)`} open={panels.trades} toggle={() => setPanels({ ...panels, trades: !panels.trades })}>
            <Trades open={s.openTrades} closed={s.closedTrades} digits={digits} />
          </Panel>
          <Panel title="Statistics" open={panels.stats} toggle={() => setPanels({ ...panels, stats: !panels.stats })}>
            <Stats />
          </Panel>
        </aside>}
      </main>

      <footer className="status mono">
        <span className={`state ${s.state}`}>{s.state.toUpperCase()}</span>
        <span title="historical candle close (broker server time)">Hist: {s.progress?.hist_time?.replace("T", " ") ?? "–"} srv · {s.progress?.close_ist ?? ""}</span>
        <span title="when this replay step ran (your clock)">Replay clock: {s.progress?.wall ?? "–"}</span>
        <span>{s.progress?.rate ? `${s.progress.rate.toFixed(1)} candles/s` : ""}</span>
        {s.session && <span>{s.session.symbol} {s.session.tf} · {String((s.session.strategy as { name?: string }).name)} · TM {s.session.trade_management ? "ON" : "OFF"} · {s.session.animation}</span>}
        {s.error && <span className="err">⚠ {s.error}</span>}
        <span className="attr">Charts: <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">TradingView Lightweight Charts™</a></span>
      </footer>
      {s.report && <ReportBox summary={s.report.summary} files={s.report.files} close={() => store.update({ report: null }, true)} open={(p) => send("open_path", { path: p })} />}
    </div>
  );
}

function paintReadout(el: HTMLDivElement | null, b: Bar | null, digits: number) {
  if (!el) return;
  if (!b) { el.textContent = ""; return; }
  const f = (x?: number) => (x === undefined ? "–" : x.toFixed(digits));
  const ist = new Date(b.utc).toLocaleString("en-GB", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  const up = b.close >= b.open;
  el.innerHTML = `<span>${b.time.replace("T", " ").slice(0, 16)} srv · ${b.utc.slice(0, 16).replace("T", " ")} UTC · ${ist} IST${b.forming ? " · <b>forming</b> as of " + (b.as_of ?? "").slice(11, 16) : ""}</span>` +
    `<span class="${up ? "up" : "dn"}">O ${f(b.open)} H ${f(b.high)} L ${f(b.low)} C ${f(b.close)}</span>` +
    `<span><i style="color:${THEME.ema8}">EMA8 ${f(b.ema8)}</i> <i style="color:${THEME.ema32}">EMA32 ${f(b.ema32)}</i> <i style="color:${THEME.ema200}">EMA200 ${f(b.ema200)}</i></span>`;
}

function Panel({ title, open, toggle, children, grow }: { title: string; open: boolean; toggle: () => void; children: React.ReactNode; grow?: boolean }) {
  return (
    <section className={`panel ${open ? "open" : ""} ${grow && open ? "grow" : ""}`}>
      <h3 onClick={toggle}>{open ? "▾" : "▸"} {title}</h3>
      {open && <div className="body">{children}</div>}
    </section>
  );
}

function Details({ e }: { e: ReplayEvent | null }) {
  if (!e) return <p className="dim">Click a marker or a timeline entry.</p>;
  return (
    <div className="details">
      <div className="hdr"><b>{e.type}</b> {e.dir} <span className={`sev ${e.severity}`}>{e.severity}</span></div>
      <table><tbody>
        <tr><td>Price</td><td>{e.price ?? "–"}</td></tr>
        <tr><td>Candle</td><td>{e.bar_time.replace("T", " ")} → {e.close_time.slice(11, 16)} srv</td></tr>
        <tr><td>Closed</td><td>{e.close_utc.slice(0, 16).replace("T", " ")} UTC · {e.close_ist}</td></tr>
        <tr><td>Market</td><td>{e.status}</td></tr>
        <tr><td>Key</td><td className="dim">{e.key}</td></tr>
        <tr><td>Session</td><td className="dim">{e.session} · #{e.seq}</td></tr>
      </tbody></table>
      <pre>{JSON.stringify(e.details, null, 1)}</pre>
    </div>
  );
}

function Timeline({ events, onPick, version }: { events: ReplayEvent[]; onPick: (e: ReplayEvent) => void; version: number }) {
  const shown = useMemo(() => events.slice(-400).reverse(), [events, version]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <ul className="timeline">
      {shown.map((e) => (
        <li key={e.seq} onClick={() => onPick(e)} className={`${e.kind} ${e.severity}`}>
          <span className="t mono">{e.close_time.slice(5, 16).replace("T", " ")}</span>
          <span className={`d ${["BULL", "LONG"].includes(e.dir) ? "up" : ["BEAR", "SHORT"].includes(e.dir) ? "dn" : ""}`}>{e.type}</span>
          <span className="p mono">{e.price ?? ""}</span>
        </li>
      ))}
      {events.length > 400 && <li className="dim">… {events.length - 400} earlier (all in the report)</li>}
    </ul>
  );
}

function Trades({ open, closed, digits }: { open: Trade[]; closed: Trade[]; digits: number }) {
  if (!open.length && !closed.length) return <p className="dim">No virtual trades (indicator replay, or no entry yet).</p>;
  return (
    <table className="trades mono"><thead><tr><th>#</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th><th>Exit</th><th>Net</th></tr></thead>
      <tbody>
        {open.map((t) => <tr key={`o${t.id}`} className="open"><td>{t.id}</td><td>{t.side > 0 ? "BUY" : "SELL"} {t.lots}</td><td>{t.entry_price?.toFixed(digits)}</td><td>{t.sl?.toFixed(digits) ?? "–"}</td><td>{t.tp?.toFixed(digits) ?? "–"}</td><td>open</td><td>–</td></tr>)}
        {closed.slice(-50).reverse().map((t) => <tr key={`c${t.id}`}><td>{t.id}</td><td>{t.side > 0 ? "BUY" : "SELL"} {t.lots}</td><td>{t.entry_price?.toFixed(digits)}</td><td>{t.sl?.toFixed(digits) ?? "–"}</td><td>{t.tp?.toFixed(digits) ?? "–"}</td><td title={t.exit_reason ?? ""}>{t.exit_price?.toFixed(digits)}</td><td className={(t.net ?? 0) >= 0 ? "up" : "dn"}>{fmt(t.net)}</td></tr>)}
      </tbody></table>
  );
}

function Stats() {
  const s = useStore();
  const top = Object.entries(s.counts).sort((a, b) => b[1] - a[1]);
  const dc = s.discord;
  const pnl = s.balance !== null && s.session ? s.balance - s.session.balance : null;
  const trades = (s.session?.strategy as { trades?: boolean } | undefined)?.trades;
  return (
    <div className="stats">
      {trades ? <div className="kpis mono">
        <div><small>Balance</small>{fmt(s.balance)}</div><div><small>Equity</small>{fmt(s.equity)}</div>
        <div><small>Closed P/L</small><span className={(pnl ?? 0) >= 0 ? "up" : "dn"}>{fmt(pnl)}</span></div>
        <div><small>Trades</small>{s.closedTrades.length}</div>
      </div> : <p className="dim">Indicator replay: events only, no profit or loss is implied.</p>}
      <table className="mono"><tbody>{top.map(([k, v]) => <tr key={k}><td>{k}</td><td>{v}</td></tr>)}</tbody></table>
      {dc && <p className="dim mono">Discord: {dc.enabled ? `attempted ${dc.attempted} · delivered ${dc.delivered} · failed ${dc.failed} · waiting ${dc.waiting} · digests ${dc.digests}` : "off"}</p>}
    </div>
  );
}

function ReportBox({ summary, files, close, open }: { summary: string; files: Record<string, string>; close: () => void; open: (p: string) => void }) {
  return (
    <div className="modal" onClick={close}>
      <div className="box" onClick={(e) => e.stopPropagation()}>
        <h2>Replay report</h2>
        <pre>{summary}</pre>
        <div className="files">{Object.entries(files).map(([k, p]) => <button key={k} onClick={() => open(p)}>{k}</button>)}</div>
        <button className="primary" onClick={close}>Close</button>
      </div>
    </div>
  );
}
