"use client";

import { useCallback, useEffect, useState } from "react";
import { FileDown, RefreshCw } from "lucide-react";
import { Header } from "@/components/dashboard/header";
import { StatsCards } from "@/components/dashboard/stats-cards";
import { Scanner } from "@/components/dashboard/scanner";
import { Portfolio } from "@/components/dashboard/portfolio";
import { EquityChart } from "@/components/dashboard/equity-chart";
import { PerformanceScorecard } from "@/components/dashboard/performance-scorecard";
import { LiveReadinessCard } from "@/components/dashboard/live-readiness-card";
import { ModeToggle } from "@/components/dashboard/mode-toggle";
import { Button } from "@/components/ui/button";
import {
  fetchBotStatus,
  fetchEquityHistory,
  fetchLiveReadiness,
  fetchPerformance,
  fetchPositions,
  fetchScannerCandidates,
  type PerformanceScorecard as PerformanceScorecardData,
} from "@/lib/api";
import { exportDashboardPDF } from "@/lib/pdf-export";
import type {
  BotStatus,
  EquityPoint,
  Position,
  LiveReadiness,
  ScannerCandidateReport,
  TradingMode,
} from "@/types";

interface DashboardClientProps {
  userEmail?: string | null;
}

export function DashboardClient({ userEmail }: DashboardClientProps) {
  const [backendOnline, setBackendOnline] = useState(false);
  const [mode, setMode] = useState<TradingMode>("paper");
  const [status, setStatus] = useState<BotStatus | null>(null);
  const [report, setReport] = useState<ScannerCandidateReport | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [performance, setPerformance] = useState<PerformanceScorecardData | null>(null);
  const [liveReadiness, setLiveReadiness] = useState<LiveReadiness | null>(null);
  const [loading, setLoading] = useState(true);
  const [scannerError, setScannerError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const loadData = useCallback(async () => {
    const results = await Promise.allSettled([
      fetchBotStatus(),
      fetchScannerCandidates(),
      fetchPositions(),
      fetchEquityHistory(),
      fetchPerformance(),
      fetchLiveReadiness(),
    ]);

    if (results[0].status === "fulfilled") {
      setStatus(results[0].value);
      setBackendOnline(Boolean(results[0].value));
      if (results[0].value) setMode(results[0].value.run_mode);
    } else {
      setBackendOnline(false);
    }

    if (results[1].status === "fulfilled") {
      setReport(results[1].value);
      setMode(results[1].value.run_mode);
      setScannerError(null);
    } else {
      const err = results[1].reason;
      setScannerError(
        err instanceof Error ? err.message : "Failed to load scanner data"
      );
    }

    if (results[2].status === "fulfilled") setPositions(results[2].value);
    if (results[3].status === "fulfilled") setEquity(results[3].value);
    if (results[4].status === "fulfilled") setPerformance(results[4].value);
    if (results[5].status === "fulfilled") setLiveReadiness(results[5].value);
  }, []);

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

  return (
    <div className="min-h-screen bg-[#0f1211]">
      <Header
        mode={mode}
        backendOnline={backendOnline}
        userEmail={userEmail}
      />

      <main className="relative mx-auto max-w-7xl px-4 py-6 sm:px-6 sm:py-8">
        <div className="mb-6 flex flex-col gap-4 border-b border-white/10 pb-5 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <p className="mb-2 text-xs font-medium uppercase tracking-[0.16em] text-[#84968e]">
              Private operations
            </p>
            <h1 className="text-2xl font-semibold tracking-tight text-[#f2f0e8] sm:text-3xl">
              Trading command center
            </h1>
            <p className="mt-1 text-sm text-zinc-400">
              Paper-trading status, market observations, risk controls, and evidence
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
        </div>

        <div className="mb-6">
          <ModeToggle
            mode={mode}
            botRunning={status?.bot_running}
            regime={status?.regime}
            brokerProvider={status?.broker_provider}
          />
        </div>

        <div className="mb-6">
          <StatsCards
            status={status}
            signalCount={report?.count ?? 0}
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
          <PerformanceScorecard data={performance} loading={loading} />
        </div>

        <div className="mt-6 grid gap-6 lg:grid-cols-5">
          <div className="lg:col-span-3">
            <Portfolio positions={positions} loading={loading} />
          </div>
          <div className="lg:col-span-2">
            <LiveReadinessCard data={liveReadiness} loading={loading} />
          </div>
        </div>
      </main>
    </div>
  );
}
