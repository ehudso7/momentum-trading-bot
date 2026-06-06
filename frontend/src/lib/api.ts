import axios, { AxiosError, AxiosInstance } from "axios";
import type {
  BotStatus,
  EquityPoint,
  HealthResponse,
  Position,
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

  client.interceptors.request.use((config) => {
    if (typeof window !== "undefined") {
      const apiKey = localStorage.getItem("mf_api_key");
      if (apiKey) {
        config.headers.Authorization = `Bearer ${apiKey}`;
      }
    }
    return config;
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

export async function createCheckoutSession(): Promise<{
  checkout_url: string;
  checkout_session_id: string;
}> {
  const { data } = await backend.post("/billing/checkout");
  return data;
}

export { BACKEND_URL, DASHBOARD_URL, RAILWAY_BACKEND, RAILWAY_DASHBOARD };