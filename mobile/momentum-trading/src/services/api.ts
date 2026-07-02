import axios, { AxiosInstance } from 'axios';
import Constants from 'expo-constants';

const API_URL = Constants.expoConfig?.extra?.apiUrl || 'https://momentum-trading-bot-production.up.railway.app';

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

  // Auth endpoints
  async login(email: string, password: string) {
    return this.client.post('/auth/login', { email, password });
  }

  async signup(email: string, password: string, name: string) {
    return this.client.post('/auth/signup', { email, password, name });
  }

  async logout() {
    return this.client.post('/auth/logout');
  }

  // Portfolio endpoints
  async getPortfolio() {
    return this.client.get('/portfolio');
  }

  async getPositions() {
    return this.client.get('/positions');
  }

  async getPerformance(period: string = '1d') {
    return this.client.get(`/performance?period=${period}`);
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
    return this.client.get('/orders/history');
  }

  // Signals endpoints
  async getLatestSignals() {
    return this.client.get('/signals/latest');
  }

  async getSignalHistory() {
    return this.client.get('/signals/history');
  }

  async subscribeToSignal(signalId: string) {
    return this.client.post(`/signals/${signalId}/subscribe`);
  }

  // Market data endpoints
  async getMarketData(symbol: string) {
    return this.client.get(`/market/${symbol}`);
  }

  async getWatchlist() {
    return this.client.get('/watchlist');
  }

  async addToWatchlist(symbol: string) {
    return this.client.post('/watchlist', { symbol });
  }

  async removeFromWatchlist(symbol: string) {
    return this.client.delete(`/watchlist/${symbol}`);
  }

  // Settings endpoints
  async getSettings() {
    return this.client.get('/settings');
  }

  async updateSettings(settings: any) {
    return this.client.put('/settings', settings);
  }

  // Billing endpoints
  async getSubscription() {
    return this.client.get('/billing/subscription');
  }

  async createCheckoutSession(priceId: string) {
    return this.client.post('/billing/checkout', { priceId });
  }

  async cancelSubscription() {
    return this.client.post('/billing/cancel');
  }
}

export const api = new ApiService();