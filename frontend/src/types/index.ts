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

export interface ScannerCandidate {
  symbol: string;
  price: number;
  gap_pct: number;
  relative_volume: number;
  float_shares: number | null;
  volume: number;
  prev_close: number;
  catalyst: string | null;
  scanner_score: number;
  observed_at: string;
}

export interface ScannerCandidateReport {
  observed_at: string | null;
  market_status: string;
  run_mode: TradingMode;
  broker_provider: string;
  count: number;
  candidates: ScannerCandidate[];
  disclaimer: string;
}

export interface LiveReadiness {
  ready: boolean;
  checks: Record<string, boolean>;
  reasons: string[];
  metrics: {
    closed_trades: number;
    trading_days: number;
    expectancy_per_trade: number;
    profit_factor: number | null;
    max_drawdown_pct: number;
  };
  criteria: {
    minimum_closed_trades: number;
    minimum_trading_days: number;
    minimum_profit_factor: number;
    maximum_drawdown_pct: number;
  };
}

export interface Position {
  symbol: string;
  signal_type: string;
  entry_price: number;
  current_price: number;
  shares: number;
  shares_remaining: number;
  stop_price: number;
  pnl_unrealized: number;
  pnl_realized: number;
  trailing_stop_active: boolean;
  trailing_stop_price: number | null;
  entry_time: string;
}

export interface Trade {
  date: string;
  symbol: string;
  side: string;
  signal_type: string;
  entry_price: number;
  exit_price: number;
  shares: number;
  pnl: number;
  rr_ratio: number;
  hold_time_minutes: number;
  entry_time: string;
  exit_time: string;
  exit_reason: string;
  notes: string;
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
  broker_provider: string;
  circuit_breaker: Record<string, unknown>;
  health: Record<string, unknown>;
  market_status: string;
  market_status_detail?: string;
  open_positions_count: number;
  total_trades_today: number;
  last_updated: string;
  bot_running: boolean;
  last_error?: string | null;
}

export interface SubscriptionTier {
  id: "free" | "pro" | "elite";
  name: string;
  price: number;
  features: string[];
  highlighted?: boolean;
}
