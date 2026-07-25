import axios, { AxiosError, AxiosInstance } from "axios";
import {
  createClient as createSupabaseBrowserClient,
  isSupabaseConfigured,
} from "@/lib/supabase/client";
import type {
  BotStatus,
  EquityPoint,
  HealthResponse,
  Position,
  LiveReadiness,
  ScannerCandidateReport,
  SignalReport,
  Trade,
} from "@/types";

const RAILWAY_BACKEND =
  process.env.NEXT_PUBLIC_BACKEND_URL ||
  "https://momentum-trading-bot-production.up.railway.app";

const RAILWAY_DASHBOARD =
  process.env.NEXT_PUBLIC_DASHBOARD_URL || RAILWAY_BACKEND;

const BACKEND_URL =
  typeof window !== "undefined" ? "/api/backend" : RAILWAY_BACKEND;

const DASHBOARD_URL =
  typeof window !== "undefined" ? "/api/backend/dashboard" : RAILWAY_DASHBOARD;

export class ApiError extends Error {
  status: number;
  detail?: string;

  constructor(message: string, status: number, detail?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function createClient(baseURL: string): AxiosInstance {
  const client = axios.create({
    baseURL,
    timeout: 15000,
    headers: { "Content-Type": "application/json" },
  });

  client.interceptors.response.use(
    (response) => response,
    (error: AxiosError<{ detail?: string }>) => {
      const status = error.response?.status ?? 0;
      const detail =
        error.response?.data?.detail ??
        error.message ??
        "Unknown error";
      throw new ApiError(detail, status, detail);
    }
  );

  return client;
}

const backend = createClient(BACKEND_URL);
const dashboard = createClient(DASHBOARD_URL);

export function setApiKey(key: string | null) {
  if (typeof window !== "undefined") {
    if (key) {
      localStorage.setItem("mf_api_key", key);
    } else {
      localStorage.removeItem("mf_api_key");
    }
  }
}

export function getApiKey(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("mf_api_key");
}

export async function fetchHealth(): Promise<HealthResponse> {
  const { data } = await backend.get<HealthResponse>("/health");
  return data;
}

export async function fetchLatestSignals(): Promise<SignalReport> {
  const { data } = await backend.get<SignalReport>("/signals/latest");
  return data;
}

export async function fetchScannerCandidates(): Promise<ScannerCandidateReport> {
  const { data } = await dashboard.get<ScannerCandidateReport>("/api/candidates");
  return data;
}

export async function fetchLiveReadiness(): Promise<LiveReadiness> {
  const { data } = await dashboard.get<LiveReadiness>("/api/live-readiness");
  return data;
}

export async function fetchLatestReport(): Promise<Record<string, unknown>> {
  const { data } = await backend.get("/reports/latest");
  return data;
}

export async function fetchBotStatus(): Promise<BotStatus | null> {
  try {
    const { data } = await dashboard.get<BotStatus>("/api/status");
    return data;
  } catch (error) {
    if (error instanceof ApiError && (error.status === 404 || error.status === 0)) {
      return null;
    }
    throw error;
  }
}

export async function fetchPositions(): Promise<Position[]> {
  try {
    const { data } = await dashboard.get<Position[]>("/api/positions");
    return data;
  } catch (error) {
    if (error instanceof ApiError && (error.status === 404 || error.status === 0)) {
      return [];
    }
    throw error;
  }
}

export async function fetchTrades(): Promise<Trade[]> {
  try {
    const { data } = await dashboard.get<Trade[]>("/api/trades");
    return data;
  } catch (error) {
    if (error instanceof ApiError && (error.status === 404 || error.status === 0)) {
      return [];
    }
    throw error;
  }
}

export async function fetchEquityHistory(): Promise<EquityPoint[]> {
  try {
    const { data } = await dashboard.get<EquityPoint[]>("/api/equity-history");
    return data;
  } catch (error) {
    if (error instanceof ApiError && (error.status === 404 || error.status === 0)) {
      return [];
    }
    throw error;
  }
}

// ---------------------------------------------------------------------------
// Performance Scorecard — honest strategy edge metrics (GET /api/performance)
// ---------------------------------------------------------------------------

/** One row of the per-setup breakdown table. */
export interface PerformanceSetupBreakdown {
  setup: string;
  trades: number;
  win_rate: number;
  /** Ratio of gross wins to gross losses; null when undefined (no losses). */
  profit_factor: number | null;
  expectancy_per_trade: number;
}

/** One point of the rolling win-rate / expectancy / equity series. */
export interface PerformanceRollingPoint {
  index: number;
  win_rate: number;
  expectancy: number;
  equity: number;
}

/**
 * Honest performance summary returned by GET /api/performance.
 * Mirrors the bot dashboard service response exactly. When the strategy
 * has not traded (or the sample is thin) the metric fields are still
 * present but must not be read as a proven edge — consult
 * `is_statistically_significant` and `confidence_note` first.
 */
export interface PerformanceScorecard {
  trade_count: number;
  closed_trades: number;
  win_rate: number;
  wins: number;
  losses: number;
  avg_win: number;
  avg_loss: number;
  largest_win: number;
  largest_loss: number;
  /** Gross wins / gross losses; null when undefined (e.g. no losses yet). */
  profit_factor: number | null;
  expectancy_per_trade: number;
  expectancy_r: number;
  avg_rr: number;
  total_pnl: number;
  total_return_pct: number;
  max_drawdown_pct: number;
  sharpe_ratio: number;
  sortino_ratio: number;
  avg_hold_minutes: number;
  by_setup: PerformanceSetupBreakdown[];
  sample_size: number;
  min_sample_for_confidence: number;
  is_statistically_significant: boolean;
  confidence_note: string;
  rolling: PerformanceRollingPoint[];
}

/**
 * Raw wire shape from the bot dashboard: `by_setup` is an object keyed by
 * signal type, and `rolling` points use `trade_number`. We normalize both to
 * the array/`index` shape the UI consumes.
 */
interface RawPerformance
  extends Omit<PerformanceScorecard, "by_setup" | "rolling"> {
  by_setup: Record<string, Omit<PerformanceSetupBreakdown, "setup">> | PerformanceSetupBreakdown[];
  rolling?: Array<
    Partial<PerformanceRollingPoint> & { trade_number?: number }
  >;
}

function normalizePerformance(raw: RawPerformance): PerformanceScorecard {
  const by_setup: PerformanceSetupBreakdown[] = Array.isArray(raw.by_setup)
    ? raw.by_setup
    : Object.entries(raw.by_setup ?? {}).map(([setup, stats]) => ({
        setup,
        trades: stats.trades ?? 0,
        win_rate: stats.win_rate ?? 0,
        profit_factor: stats.profit_factor ?? null,
        expectancy_per_trade: stats.expectancy_per_trade ?? 0,
      }));

  const rolling: PerformanceRollingPoint[] = (raw.rolling ?? []).map((p, i) => ({
    index: p.index ?? p.trade_number ?? i + 1,
    win_rate: p.win_rate ?? 0,
    expectancy: p.expectancy ?? 0,
    equity: p.equity ?? 0,
  }));

  return { ...(raw as unknown as PerformanceScorecard), by_setup, rolling };
}

export async function fetchPerformance(): Promise<PerformanceScorecard | null> {
  try {
    const { data } = await dashboard.get<RawPerformance>("/api/performance");
    return normalizePerformance(data);
  } catch (error) {
    if (error instanceof ApiError && (error.status === 404 || error.status === 0)) {
      return null;
    }
    throw error;
  }
}

// ---------------------------------------------------------------------------
// Viral loop — public signal share cards (/s/<token>)
// ---------------------------------------------------------------------------

/** Sanitized public snapshot served by GET /share/signal/{token}. */
export interface SharedSignal {
  symbol: string;
  direction: "bullish" | "bearish" | "neutral";
  /** 0-100 momentum score (confidence × 100). */
  score: number;
  /** Momentum % vs the 50-day SMA; null when unavailable. */
  gap_pct: number | null;
  regime: string;
  date: string | null;
  referrer_label: string | null;
  token?: string;
  expires_at?: string;
}

/** Response of the authenticated POST /share/signal. */
export interface SignalShareLink {
  token: string;
  /** Frontend path serving the public card, e.g. "/s/<token>". */
  path: string;
  share_url: string | null;
  expires_at: string;
  signal: SharedSignal;
}

const SHARE_TOKEN_RE = /^[A-Za-z0-9_-]{8,64}$/;

/**
 * Mint a public share link for one signal in the latest report.
 * Requires the stored API key (the axios interceptor attaches it).
 */
export async function createSignalShareLink(
  symbol: string,
  label?: string
): Promise<SignalShareLink> {
  const body: { symbol: string; label?: string } = { symbol };
  if (label) body.label = label;
  const { data } = await backend.post<SignalShareLink>("/share/signal", body);
  return data;
}

/**
 * Server-safe fetch of a shared signal card — PUBLIC endpoint, no
 * API key. Used by the /s/[token] page and its OG image. Invalid,
 * unknown, and expired tokens all resolve to null (the caller
 * renders a tasteful fallback card, never an error page).
 */
export async function fetchSharedSignal(
  token: string
): Promise<SharedSignal | null> {
  if (!SHARE_TOKEN_RE.test(token)) return null;
  try {
    const res = await fetch(
      `${RAILWAY_BACKEND}/share/signal/${encodeURIComponent(token)}`,
      { next: { revalidate: 300 } }
    );
    if (!res.ok) return null;
    return (await res.json()) as SharedSignal;
  } catch {
    return null;
  }
}

export type CheckoutPlan = "pro" | "elite";

export interface CheckoutSession {
  checkout_url: string;
  checkout_session_id: string;
}

export type PlanTier = "free" | "pro" | "elite";

export interface BillingStatus {
  tier: PlanTier;
  premium: boolean;
  plan: PlanTier;
  plan_source: string;
}

export const PLAN_LABELS: Record<PlanTier, string> = {
  free: "Free",
  pro: "Pro",
  elite: "Elite",
};

async function getSupabaseUserId(): Promise<string | undefined> {
  if (typeof window === "undefined" || !isSupabaseConfigured()) {
    return undefined;
  }
  try {
    const supabase = createSupabaseBrowserClient();
    const {
      data: { session },
    } = await supabase.auth.getSession();
    return session?.user.id ?? undefined;
  } catch {
    // Logged out or Supabase unavailable — checkout still works keyed on
    // the API key alone; the webhook just can't tag the Supabase user.
    return undefined;
  }
}

export async function createCheckoutSession(
  plan: CheckoutPlan
): Promise<CheckoutSession> {
  const supabaseUserId = await getSupabaseUserId();
  const body: { plan: CheckoutPlan; supabase_user_id?: string } = { plan };
  if (supabaseUserId) {
    body.supabase_user_id = supabaseUserId;
  }
  const { data } = await backend.post<CheckoutSession>(
    "/billing/checkout",
    body
  );
  return data;
}

export async function fetchBillingStatus(): Promise<BillingStatus> {
  const { data } = await backend.get<BillingStatus>("/billing/status");
  return data;
}

export { BACKEND_URL, DASHBOARD_URL, RAILWAY_BACKEND, RAILWAY_DASHBOARD };
