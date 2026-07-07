"use client";

import { motion } from "framer-motion";
import { ArrowDownRight, ArrowUpRight, Minus, Radar } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { LoadingState, ErrorState } from "@/components/ui/loading";
import { ShareButton } from "@/components/share/share-button";
import type { Signal, SignalReport } from "@/types";

interface ScannerProps {
  report: SignalReport | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}

function DirectionIcon({ direction }: { direction: string }) {
  if (direction === "bullish")
    return <ArrowUpRight className="h-4 w-4 text-emerald-400" />;
  if (direction === "bearish")
    return <ArrowDownRight className="h-4 w-4 text-red-400" />;
  return <Minus className="h-4 w-4 text-zinc-400" />;
}

function directionVariant(direction: string) {
  if (direction === "bullish") return "success" as const;
  if (direction === "bearish") return "danger" as const;
  return "neutral" as const;
}

export function Scanner({ report, loading, error, onRetry }: ScannerProps) {
  const signals = report?.signals ?? [];

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <Radar className="h-5 w-5 text-cyan-400" />
            Momentum Scanner
          </CardTitle>
          {report && (
            <div className="flex items-center gap-2">
              <Badge variant="neutral">{report.report_date}</Badge>
              {report.preview && <Badge variant="warning">Preview</Badge>}
            </div>
          )}
        </div>
        {report?.summary && (
          <p className="text-sm text-zinc-400">
            {report.summary.signal_count} signals ·{" "}
            <span className="text-emerald-400">{report.summary.bullish_count} bullish</span>
            {" · "}
            <span className="text-red-400">{report.summary.bearish_count} bearish</span>
            {" · Risk: "}
            <span className="capitalize">{report.summary.risk_level}</span>
          </p>
        )}
      </CardHeader>
      <CardContent>
        {loading && <LoadingState message="Scanning markets..." />}
        {error && !loading && <ErrorState message={error} onRetry={onRetry} />}
        {!loading && !error && signals.length === 0 && (
          <p className="py-8 text-center text-sm text-zinc-500">
            No signals available. Generate a report on the backend.
          </p>
        )}
        {!loading && !error && signals.length > 0 && (
          <div className="space-y-2">
            {signals.map((signal: Signal, i: number) => (
              <motion.div
                key={`${signal.symbol}-${i}`}
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: i * 0.05 }}
                className="flex items-center justify-between rounded-xl border border-white/5 bg-white/[0.02] p-4 transition-colors hover:bg-white/[0.04]"
              >
                <div className="flex items-center gap-3">
                  <DirectionIcon direction={signal.direction} />
                  <div>
                    <p className="font-semibold text-white">{signal.symbol}</p>
                    {signal.rationale && (
                      <p className="mt-0.5 max-w-xs truncate text-xs text-zinc-500 sm:max-w-md">
                        {signal.rationale}
                      </p>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-3 text-right">
                  {signal.entry != null && (
                    <div className="hidden sm:block">
                      <p className="text-xs text-zinc-500">Entry</p>
                      <p className="text-sm font-medium text-white">
                        ${signal.entry.toFixed(2)}
                      </p>
                    </div>
                  )}
                  <Badge variant={directionVariant(signal.direction)}>
                    {(signal.confidence * 100).toFixed(0)}%
                  </Badge>
                  <ShareButton symbol={signal.symbol} />
                </div>
              </motion.div>
            ))}
          </div>
        )}
        {report?.upgrade && (
          <div className="mt-4 rounded-xl border border-amber-500/20 bg-amber-500/5 p-4 text-center">
            <p className="text-sm text-amber-200">{report.upgrade.message}</p>
            <a
              href="/billing"
              className="mt-2 inline-block text-sm font-medium text-cyan-400 hover:text-cyan-300"
            >
              Upgrade to Pro →
            </a>
          </div>
        )}
      </CardContent>
    </Card>
  );
}