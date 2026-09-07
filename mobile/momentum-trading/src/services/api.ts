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

export interface Position {
  symbol: string;
  name?: string;
  shares: number;
  value: number;
  change: number;
  changePercent: number;
}

export interface PortfolioResponse {
  totalValue: number;
  dayChange: number;
  dayChangePercent: number;
  positions: Position[];
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

export interface Signal {
  id: string;
  symbol: string;
  type: string;
  action: string;
  confidence: number;
  price: number;
  timestamp: string;
  reasoning: string;
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

  async getPositions(): Promise<Position[]> {
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
  async getLatestSignals(): Promise<Signal[]> {
    return this.get('/signals/latest');
  }

  async getSignalHistory(): Promise<Signal[]> {
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