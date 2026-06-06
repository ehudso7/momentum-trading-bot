"use client";

import { useCallback, useEffect, useState } from "react";
import { FileDown, RefreshCw } from "lucide-react";
import { motion } from "framer-motion";
import { Header } from "@/components/dashboard/header";
import { StatsCards } from "@/components/dashboard/stats-cards";
import { Scanner } from "@/components/dashboard/scanner";
import { Portfolio } from "@/components/dashboard/portfolio";
import { EquityChart } from "@/components/dashboard/equity-chart";
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

  useEffect(() => {
    loadData().finally(() => setLoading(false));
    const interval = setInterval(loadData, 60000);
    return () => clearInterval(interval);
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

        <div className="mt-6">
          <Portfolio positions={positions} loading={loading} />
        </div>
      </main>
    </div>
  );
}