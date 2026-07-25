"use client";

import { motion } from "framer-motion";
import { Radar } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { LoadingState, ErrorState } from "@/components/ui/loading";
import type { ScannerCandidate, ScannerCandidateReport } from "@/types";

interface ScannerProps {
  report: ScannerCandidateReport | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}

export function Scanner({ report, loading, error, onRetry }: ScannerProps) {
  const candidates = report?.candidates ?? [];

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
              <Badge variant="neutral" className="capitalize">
                {report.market_status}
              </Badge>
              <Badge variant="default">REAL SCANNER</Badge>
            </div>
          )}
        </div>
        {report && (
          <p className="text-sm text-zinc-400">
            {report.count} current candidates · {report.disclaimer}
          </p>
        )}
      </CardHeader>
      <CardContent>
        {loading && <LoadingState message="Scanning markets..." />}
        {error && !loading && <ErrorState message={error} onRetry={onRetry} />}
        {!loading && !error && candidates.length === 0 && (
          <p className="py-8 text-center text-sm text-zinc-500">
            No candidates currently pass every scanner filter. This is a valid
            no-trade state.
          </p>
        )}
        {!loading && !error && candidates.length > 0 && (
          <div className="space-y-2">
            {candidates.map((candidate: ScannerCandidate, i: number) => (
              <motion.div
                key={`${candidate.symbol}-${candidate.observed_at}-${i}`}
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: i * 0.05 }}
                className="flex items-center justify-between rounded-xl border border-white/5 bg-white/[0.02] p-4 transition-colors hover:bg-white/[0.04]"
              >
                <div className="flex items-center gap-3">
                  <span className="w-6 text-center font-mono text-xs text-zinc-500">
                    {i + 1}
                  </span>
                  <div>
                    <p className="font-semibold text-white">{candidate.symbol}</p>
                    {candidate.catalyst && (
                      <p className="mt-0.5 max-w-xs truncate text-xs text-zinc-500 sm:max-w-md">
                        {candidate.catalyst}
                      </p>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-3 text-right">
                  <div className="hidden sm:block">
                    <p className="text-xs text-zinc-500">Price / Gap</p>
                    <p className="text-sm font-medium text-white">
                      ${candidate.price.toFixed(2)} · {candidate.gap_pct >= 0 ? "+" : ""}{candidate.gap_pct.toFixed(1)}%
                    </p>
                  </div>
                  <div className="hidden md:block">
                    <p className="text-xs text-zinc-500">Relative volume</p>
                    <p className="text-sm font-medium text-white">
                      {candidate.relative_volume.toFixed(1)}x
                    </p>
                  </div>
                  <Badge variant="neutral">
                    Rank {(candidate.scanner_score * 100).toFixed(0)}
                  </Badge>
                </div>
              </motion.div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
