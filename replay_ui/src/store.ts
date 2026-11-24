// Panel state outside React: written on every batch, published to React at most ~8 times a second so the panels
// never compete with the chart for frames. The chart itself is driven imperatively (chart.ts), not through React.
import type { ConfigMsg, Progress, ReplayEvent, SessionMsg, Trade } from "./types";

export interface PanelState {
  config: ConfigMsg | null;
  session: SessionMsg | null;
  state: string;
  error: string | null;
  events: ReplayEvent[];
  counts: Record<string, number>;
  openTrades: Trade[];
  closedTrades: Trade[];
  progress: Progress | null;
  equity: number | null;
  balance: number | null;
  status: string;
  discord: Record<string, number | boolean | string | null> | null;
  report: { summary: string; files: Record<string, string> } | null;
  selected: ReplayEvent | null;
  selectedTrade: Trade | null;
  focusTradeId: number | null;
  csvPath: string;
  version: number;
}

const initial: PanelState = {
  config: null, session: null, state: "idle", error: null, events: [], counts: {}, openTrades: [], closedTrades: [],
  progress: null, equity: null, balance: null, status: "", discord: null, report: null, selected: null,
  selectedTrade: null, focusTradeId: null, csvPath: "",
  version: 0,
};

class Store {
  s: PanelState = { ...initial };
  private subs = new Set<() => void>();
  private timer: number | null = null;

  subscribe = (f: () => void) => {
    this.subs.add(f);
    return () => this.subs.delete(f);
  };
  get = () => this.s;

  update(patch: Partial<PanelState>, urgent = false) {
    this.s = { ...this.s, ...patch, version: this.s.version + 1 };
    if (urgent) return this.flush();
    if (this.timer === null) this.timer = window.setTimeout(() => this.flush(), 125);
  }
  // events arrive in large batches: mutate in place, publish later
  addEvents(evs: ReplayEvent[]) {
    if (!evs.length) return;
    const counts = { ...this.s.counts };
    const closed = [...this.s.closedTrades];
    for (const e of evs) {
      counts[e.type] = (counts[e.type] ?? 0) + 1;
      if (e.type === "VIRTUAL_CLOSE") closed.push((e.details as { trade: Trade }).trade);
    }
    this.s.events.push(...evs);
    this.update({ counts, closedTrades: closed });
  }
  reset(patch: Partial<PanelState>) {
    this.s = { ...initial, config: this.s.config, csvPath: this.s.csvPath, ...patch, events: [], version: this.s.version + 1 };
    this.flush();
  }
  private flush() {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    this.subs.forEach((f) => f());
  }
}

export const store = new Store();
