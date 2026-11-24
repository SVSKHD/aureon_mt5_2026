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
  ema20?: number; // strategy overlays (EMA 20/50 cloud), when the strategy draws them
  ema50?: number;
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

export interface Condition {
  name: string;
  passed: boolean;
  value?: unknown;
}

export interface SlMove {
  time: string;
  step_time: string;
  old: number | null;
  new: number;
  pre_extreme?: number;
  pullback_extreme?: number;
}

export interface Trade {
  id: number;
  side: number;
  lots: number;
  strategy?: string;
  strategy_version?: string;
  param_hash?: string;
  entry_signal?: string;
  conditions?: Condition[];
  decided_time?: string;
  decided_index?: number | null;
  open_index?: number | null;
  exit_index?: number | null;
  entry_time: string | null;
  entry_price: number | null;
  sl: number | null;
  tp: number | null;
  initial_sl?: number | null;
  exit_time?: string | null;
  exit_price?: number | null;
  exit_reason?: string | null;
  gross?: number;
  commission?: number;
  net?: number;
  r_multiple?: number | null;
  mfe?: number;
  mae?: number;
  mfe_money?: number | null;
  mae_money?: number | null;
  risk_money?: number | null;
  sl_moves?: SlMove[];
  blocked_steps?: SlMove[];
  ambiguous?: boolean;
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
  | { type: "jump_view"; time: string }
  | { type: "focus_trade"; trade_id: number; decided_time: string; jump_time: string }
  | { type: "error"; text: string };

export type Command = { cmd: string; [k: string]: unknown };
