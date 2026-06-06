export type TradingMode = "paper" | "live" | "demo";

export interface HealthResponse {
  status: string;
  service: string;
  timestamp: string;
}

export interface SignalSummary {
  signal_count: number;
  bullish_count: number;
  bearish_count: number;
  neutral_count: number;
  average_confidence: number;
  risk_level: string;
}

export interface Signal {
  symbol: string;
  direction: "bullish" | "bearish" | "neutral";
  confidence: number;
  entry?: number;
  stop?: number;
  target?: number;
  rationale?: string;
  indicators?: Record<string, number | string>;
}

export interface SignalReport {
  schema_version?: string;
  generated_at: string;
  report_date: string;
  mode: TradingMode;
  universe?: string[];
  summary: SignalSummary;
  signals: Signal[];
  preview?: boolean;
  upgrade?: {
    message: string;
    checkout_url?: string;
  };
}

export interface Position {
  symbol: string;
  qty: number;
  side: string;
  entry_price: number;
  current_price?: number;
  unrealized_pnl?: number;
  unrealized_pnl_pct?: number;
}

export interface Trade {
  symbol: string;
  side: string;
  qty: number;
  price: number;
  pnl?: number;
  timestamp: string;
}

export interface EquityPoint {
  timestamp: string;
  equity: number;
}

export interface BotStatus {
  equity: number;
  starting_equity: number;
  daily_pnl: number;
  daily_return_pct: number;
  buying_power: number;
  regime: string;
  run_mode: TradingMode;
  circuit_breaker: Record<string, unknown>;
  health: Record<string, unknown>;
  market_status: string;
  open_positions_count: number;
  total_trades_today: number;
  last_updated: string;
  bot_running: boolean;
}

export interface SubscriptionTier {
  id: "free" | "pro" | "elite";
  name: string;
  price: number;
  features: string[];
  highlighted?: boolean;
}