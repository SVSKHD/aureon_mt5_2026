// Messages between the Python replay host and this UI. Python computes everything; the UI only displays.

export interface Bar {
  i: number;
  time: string; // candle OPEN, broker server time (ISO, no zone)
  utc: string; // the same instant in UTC (ISO with +00:00)
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
  ema8?: number;
  ema32?: number;
  ema200?: number;
  forming?: boolean;
  as_of?: string;
}

export interface ReplayEvent {
  seq: number;
  label: string;
  session: string;
  symbol: string;
  tf: string;
  kind: "signal" | "virtual";
  type: string;
  dir: string;
  price: number | null;
  key: string;
  bar_time: string;
  close_time: string;
  close_utc: string;
  close_ist: string;
  status: string;
  severity: string;
  details: Record<string, unknown>;
}

export interface Trade {
  id: number;
  side: number;
  lots: number;
  entry_time: string | null;
  entry_price: number | null;
  sl: number | null;
  tp: number | null;
  exit_time?: string | null;
  exit_price?: number | null;
  exit_reason?: string | null;
  net?: number;
  reason: string;
}

export interface Progress {
  done: number;
  total: number;
  hist_time: string;
  close_ist: string;
  wall: string;
  rate: number | null;
}

export interface Batch {
  type: "batch";
  bars: Bar[];
  events: ReplayEvent[];
  forming?: Bar | null;
  progress?: Progress;
  open_trades?: Trade[];
  equity?: number;
  balance?: number;
  status?: string;
}

export interface SessionMsg {
  type: "session";
  id: string;
  symbol: string;
  tf: string;
  digits: number;
  warmup: Bar[];
  coverage: Record<string, unknown>;
  animation: string;
  strategy: Record<string, unknown>;
  trade_management: boolean;
  balance: number;
}

export interface ConfigMsg {
  type: "config";
  symbols: string[];
  tfs: string[];
  strategies: string[];
  sources: string[];
  defaults: Record<string, unknown>;
  discord_configured: boolean;
  discord_problem: string | null;
}

export type InMsg =
  | ConfigMsg
  | SessionMsg
  | Batch
  | { type: "state"; state: string; error?: string | null }
  | { type: "discord"; status: Record<string, number | boolean | string | null> }
  | { type: "report"; summary: string; files: Record<string, string>; report: Record<string, unknown> }
  | { type: "csv_picked"; path: string }
  | { type: "error"; text: string };

export type Command = { cmd: string; [k: string]: unknown };
