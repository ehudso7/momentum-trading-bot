import axios, { AxiosInstance } from 'axios';
import Constants from 'expo-constants';

const API_URL = Constants.expoConfig?.extra?.apiUrl || 'https://momentum-trading-bot-production.up.railway.app';

// Response shapes, mirroring the Pydantic models the backend declares in
// trading_bot/api/mobile_routes.py. They exist so field access on an API
// result is type-checked; they are NOT a claim that the endpoints are
// reachable. See PRIVATE_BETA_STATUS.md — the mobile router is gated off,
// and the paths below omit its `/api/mobile` prefix. Both are open blockers.
export interface AuthUser {
  id: string;
  name: string;
  email: string;
  tier: 'free' | 'premium' | 'enterprise';
}

export interface AuthResponse {
  token: string;
  user: AuthUser;
  expires_at: string;
}

// The two position endpoints return DIFFERENT shapes, so they get different
// types. `/portfolio` embeds valuation fields; `/positions` returns the open
// lot. An earlier revision of this file declared a single `Position` with
// `shares`/`value`/`change` — those keys exist in neither payload; they were
// carried over from the screens' old mock data. Typing the client against
// the fixtures instead of the server is what let PortfolioScreen call
// `position.value.toLocaleString()` on a field the API never sends.
//
// Caveat: the backend declares `PortfolioResponse.positions` as
// `List[Dict]`, so these fields are read off the handler's literal, not off
// an enforced schema. Every field is optional here for that reason, and the
// screens format through src/utils/format.ts so a missing one renders as an
// em dash rather than crashing.

/** An entry in `/portfolio` -> `positions`. */
export interface PortfolioPosition {
  symbol: string;
  name?: string;
  quantity?: number;
  avgPrice?: number;
  currentPrice?: number;
  marketValue?: number;
  dayChange?: number;
  dayChangePercent?: number;
  unrealizedGain?: number;
  unrealizedGainPercent?: number;
}

/** An entry returned by `/positions`. */
export interface AccountPosition {
  symbol: string;
  quantity?: number;
  side?: string;
  entryPrice?: number;
  currentPrice?: number;
  unrealizedPnL?: number;
  entryDate?: string;
}

export interface PortfolioResponse {
  totalValue: number;
  dayChange: number;
  dayChangePercent: number;
  positions: PortfolioPosition[];
}

export interface Quote {
  symbol: string;
  price: number;
  change: number;
  changePercent: number;
  volume: number;
  open: number;
  high: number;
  low: number;
  previousClose: number;
  marketCap?: number;
  pe?: number;
  timestamp: string;
}

// As with positions, the two signal endpoints return DIFFERENT shapes and an
// earlier revision typed both with one `Signal` that declared `price` and
// `reasoning` as required. History rows carry neither, so TypeScript would
// have let a call site read `signal.price` on a history row and get
// undefined. Note also that `/signals/latest` has no `response_model`, so the
// `SignalResponse` model declared in mobile_routes.py is not applied to it —
// these fields come from the handler's literal, hence all optional.

interface SignalBase {
  id: string;
  symbol: string;
  type?: string;
  action?: string;
  /** 0..1. Values outside that range are treated as unavailable on render. */
  confidence?: number;
  timestamp?: string;
}

/** An entry from `/signals/latest`. */
export interface LatestSignal extends SignalBase {
  price?: number;
  stopLoss?: number;
  takeProfit?: number[];
  reasoning?: string;
}

/** An entry from `/signals/history`, which reports a closed result. */
export interface HistoricalSignal extends SignalBase {
  entryPrice?: number;
  exitPrice?: number;
  profit?: number;
  profitPercent?: number;
  result?: string;
}

export type Signal = LatestSignal | HistoricalSignal;

/** Narrows a signal to the history shape. */
export function isHistoricalSignal(
  signal: Signal,
): signal is HistoricalSignal {
  return 'entryPrice' in signal || 'result' in signal;
}

class ApiService {
  private client: AxiosInstance;
  private authToken: string | null = null;

  constructor() {
    this.client = axios.create({
      baseURL: API_URL,
      timeout: 10000,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // Request interceptor
    this.client.interceptors.request.use((config) => {
      if (this.authToken) {
        config.headers.Authorization = `Bearer ${this.authToken}`;
      }
      return config;
    });

    // Response interceptor
    this.client.interceptors.response.use(
      (response) => response.data,
      (error) => {
        if (error.response?.status === 401) {
          // Handle unauthorized
          this.authToken = null;
        }
        throw error;
      }
    );
  }

  setAuthToken(token: string | null) {
    this.authToken = token;
  }

  // The response interceptor above returns `response.data`, so every call
  // resolves to the payload rather than an AxiosResponse. Axios' own types
  // cannot express that, so the cast is confined to these two helpers
  // instead of being spread across every call site as `any`.
  private async get<T>(url: string): Promise<T> {
    return this.client.get(url) as unknown as Promise<T>;
  }

  private async send<T>(
    method: 'post' | 'put',
    url: string,
    body?: unknown,
  ): Promise<T> {
    return this.client[method](url, body) as unknown as Promise<T>;
  }

  // Separate from `send`: axios types delete's second argument as a request
  // config, not a body, so it cannot share the signature above.
  private async remove<T>(url: string): Promise<T> {
    return this.client.delete(url) as unknown as Promise<T>;
  }

  // Auth endpoints
  async login(email: string, password: string): Promise<AuthResponse> {
    return this.send('post', '/auth/login', { email, password });
  }

  async signup(email: string, password: string, name: string): Promise<AuthResponse> {
    return this.send('post', '/auth/signup', { email, password, name });
  }

  async logout() {
    return this.send('post', '/auth/logout');
  }

  // Portfolio endpoints
  async getPortfolio(): Promise<PortfolioResponse> {
    return this.get('/portfolio');
  }

  async getPositions(): Promise<AccountPosition[]> {
    return this.get('/positions');
  }

  async getPerformance(period: string = '1d') {
    return this.get(`/performance?period=${period}`);
  }

  // Trading endpoints — REMOVED.
  //
  // Momentum's iOS app is positioned as an educational market-intelligence
  // product (see AUDIT sheet Momentum_Trading, App Store Guideline 3.2.1
  // pivot). The mobile client does NOT place, cancel, or route orders.
  // Users execute in their own regulated brokerage account via broker
  // deep-links from TradingScreen. If you find yourself wanting to re-add
  // placeOrder / getOrders / cancelOrder to this SDK, stop and re-read the
  // audit — the codebase is on the wrong side of App Review 3.2.1 without
  // a registered broker-dealer entity.

  async getOrderHistory() {
    // Read-only order history from the user's linked broker (via Plaid /
    // SnapTrade pass-through), NOT Momentum's own order routing. Safe to
    // expose because it is a fetch, not an execute.
    return this.get('/orders/history');
  }

  // Signals endpoints
  async getLatestSignals(): Promise<LatestSignal[]> {
    return this.get('/signals/latest');
  }

  async getSignalHistory(): Promise<HistoricalSignal[]> {
    return this.get('/signals/history');
  }

  async subscribeToSignal(signalId: string) {
    return this.send('post', `/signals/${signalId}/subscribe`);
  }

  // Market data endpoints
  async getMarketData(symbol: string): Promise<Quote> {
    return this.get(`/market/${symbol}`);
  }

  async getWatchlist() {
    return this.get('/watchlist');
  }

  async addToWatchlist(symbol: string) {
    return this.send('post', '/watchlist', { symbol });
  }

  async removeFromWatchlist(symbol: string) {
    return this.remove(`/watchlist/${symbol}`);
  }

  // Settings endpoints
  async getSettings() {
    return this.get('/settings');
  }

  async updateSettings(settings: any) {
    return this.send('put', '/settings', settings);
  }

  // Billing endpoints
  async getSubscription() {
    return this.get('/billing/subscription');
  }

  async createCheckoutSession(priceId: string) {
    return this.send('post', '/billing/checkout', { priceId });
  }

  async cancelSubscription() {
    return this.send('post', '/billing/cancel');
  }
}

export const api = new ApiService();