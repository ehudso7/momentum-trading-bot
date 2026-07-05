"use client";

import { useCallback, useEffect, useState } from "react";
import { FileDown, RefreshCw } from "lucide-react";
import { motion } from "framer-motion";
import { Header } from "@/components/dashboard/header";
import { StatsCards } from "@/components/dashboard/stats-cards";
import { Scanner } from "@/components/dashboard/scanner";
import { Portfolio } from "@/components/dashboard/portfolio";
import { EquityChart } from "@/components/dashboard/equity-chart";
import { GrowthSimulator } from "@/components/dashboard/growth-simulator";
import { ModeToggle } from "@/components/dashboard/mode-toggle";
import { Button } from "@/components/ui/button";
import {
  fetchBotStatus,
  fetchEquityHistory,
  fetchHealth,
  fetchLatestSignals,
  fetchPositions,
  fetchTrades,
} from "@/lib/api";
import { TrendingUp } from "lucide-react";
import { useAutoProvisionApiKey } from "@/lib/use-api-key";
import { exportDashboardPDF } from "@/lib/pdf-export";
import type {
  BotStatus,
  EquityPoint,
  Position,
  SignalReport,
  Trade,
  TradingMode,
} from "@/types";

interface DashboardClientProps {
  userEmail?: string | null;
}

export function DashboardClient({ userEmail }: DashboardClientProps) {
  // Signed-in users with no stored API key get one provisioned automatically
  useAutoProvisionApiKey(Boolean(userEmail));

  const [backendOnline, setBackendOnline] = useState(false);
  const [mode, setMode] = useState<TradingMode>("paper");
  const [status, setStatus] = useState<BotStatus | null>(null);
  const [report, setReport] = useState<SignalReport | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [scannerError, setScannerError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const loadData = useCallback(async () => {
    try {
      await fetchHealth();
      setBackendOnline(true);
    } catch {
      setBackendOnline(false);
    }

    const results = await Promise.allSettled([
      fetchLatestSignals(),
      fetchBotStatus(),
      fetchPositions(),
      fetchTrades(),
      fetchEquityHistory(),
    ]);

    if (results[0].status === "fulfilled") {
      setReport(results[0].value);
      setMode(results[0].value.mode);
      setScannerError(null);
    } else {
      const err = results[0].reason;
      setScannerError(
        err instanceof Error ? err.message : "Failed to load signals"
      );
    }

    if (results[1].status === "fulfilled" && results[1].value) {
      setStatus(results[1].value);
      setMode(results[1].value.run_mode);
    }
    if (results[2].status === "fulfilled") setPositions(results[2].value);
    if (results[3].status === "fulfilled") setTrades(results[3].value);
    if (results[4].status === "fulfilled") setEquity(results[4].value);
  }, []);

  const currentEquity = status?.equity ?? (equity.length > 0 ? equity[equity.length - 1].equity : 100000);

  useEffect(() => {
    let cancelled = false;
    const initialLoad = async () => {
      try {
        await loadData();
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void initialLoad();
    const interval = setInterval(() => void loadData(), 60000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [loadData]);

  const handleRefresh = async () => {
    setRefreshing(true);
    await loadData();
    setRefreshing(false);
  };

  const handleExportPDF = () => {
    exportDashboardPDF({ status, report, positions });
  };

  const syncTrades = async () => {
    if (!userEmail || trades.length === 0) return;
    try {
      await fetch("/api/trades/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trades }),
      });
    } catch {
      // Best-effort sync
    }
  };

  useEffect(() => {
    if (userEmail && trades.length > 0) {
      syncTrades();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userEmail, trades.length]);

  return (
    <div className="min-h-screen bg-[#0a0b14]">
      <div className="pointer-events-none fixed inset-0 overflow-hidden">
        <div className="absolute -left-1/4 top-0 h-[500px] w-[500px] rounded-full bg-cyan-500/10 blur-[120px]" />
        <div className="absolute -right-1/4 bottom-0 h-[500px] w-[500px] rounded-full bg-blue-600/10 blur-[120px]" />
      </div>

      <Header
        mode={mode}
        backendOnline={backendOnline}
        userEmail={userEmail}
      />

      <main className="relative mx-auto max-w-7xl px-4 py-6 sm:px-6 sm:py-8">
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between"
        >
          <div>
            <h1 className="text-2xl font-bold text-white sm:text-3xl">
              Trading Command Center
            </h1>
            <p className="mt-1 text-sm text-zinc-400">
              Real-time momentum signals powered by AI
            </p>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={handleRefresh} disabled={refreshing}>
              <RefreshCw className={`mr-1.5 h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
              Refresh
            </Button>
            <Button variant="outline" size="sm" onClick={handleExportPDF}>
              <FileDown className="mr-1.5 h-4 w-4" />
              Export PDF
            </Button>
          </div>
        </motion.div>

        <div className="mb-6">
          <ModeToggle
            mode={mode}
            botRunning={status?.bot_running}
            regime={status?.regime}
          />
        </div>

        <div className="mb-6">
          <StatsCards
            status={status}
            signalCount={report?.summary?.signal_count ?? 0}
          />
        </div>

        <div className="grid gap-6 lg:grid-cols-2">
          <Scanner
            report={report}
            loading={loading}
            error={scannerError}
            onRetry={handleRefresh}
          />
          <EquityChart data={equity} loading={loading} />
        </div>

        {/* === PEAK MOMENTUMFORGE AI: EXPONENTIAL GROWTH ENGINE === */}
        <div className="mt-2">
          <GrowthSimulator status={status} currentEquity={currentEquity} />
        </div>

        <div className="mt-6 grid gap-6 lg:grid-cols-5">
          <div className="lg:col-span-3">
            <Portfolio positions={positions} loading={loading} />
          </div>
          <div className="lg:col-span-2">
            {/* MomentumForge AI Intelligence Layer — the "AI doing the work" */}
            <div className="h-full rounded-xl border border-white/10 bg-[#0a0b14] p-5">
              <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-white">
                <TrendingUp className="h-4 w-4 text-amber-400" /> Forge Intelligence — AI Layer
              </div>
              <div className="space-y-3 text-sm">
                <div className="rounded-lg bg-white/[0.02] p-3">
                  <div className="text-xs text-zinc-400">Market Regime</div>
                  <div className="font-mono text-lg font-semibold text-white">{status?.regime || "unknown"}</div>
                  <div className="mt-1 text-xs text-emerald-400">Strategy parameters auto-adjusted for edge preservation + compounding.</div>
                </div>
                {report && report.signals?.length > 0 && (
                  <div className="rounded-lg bg-white/[0.02] p-3">
                    <div className="text-xs text-zinc-400 mb-1">Top Momentum Setups (from SaaS AI)</div>
                    <div className="flex flex-wrap gap-1.5">
                      {report.signals.slice(0, 4).map((s, i) => (
                        <span key={i} className="rounded bg-white/5 px-2 py-0.5 text-xs font-medium text-white/90">
                          {s.symbol} <span className="text-amber-400">{((s.confidence || 0) * 100).toFixed(0)}%</span>
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                <div className="text-[11px] text-zinc-500 pt-1">
                  MomentumForge AI turns scanner + regime + advisor output into intelligence and exponential projections. The core bot executes with iron risk rails (circuit breaker first, hard time exit, position sizing).
                </div>
              </div>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}