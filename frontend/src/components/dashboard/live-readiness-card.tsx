"use client";

import { CheckCircle2, LockKeyhole, XCircle } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { LoadingState } from "@/components/ui/loading";
import type { LiveReadiness } from "@/types";

interface LiveReadinessCardProps {
  data: LiveReadiness | null;
  loading: boolean;
}

export function LiveReadinessCard({ data, loading }: LiveReadinessCardProps) {
  const rows = data
    ? [
        ["Closed paper trades", data.metrics.closed_trades, data.criteria.minimum_closed_trades, data.checks.minimum_closed_trades],
        ["Distinct trading days", data.metrics.trading_days, data.criteria.minimum_trading_days, data.checks.minimum_trading_days],
        ["Positive expectancy", `$${data.metrics.expectancy_per_trade.toFixed(2)}`, "> $0", data.checks.positive_expectancy],
        ["Profit factor", data.metrics.profit_factor?.toFixed(2) ?? "—", data.criteria.minimum_profit_factor.toFixed(2), data.checks.minimum_profit_factor],
        ["Maximum drawdown", `${data.metrics.max_drawdown_pct.toFixed(2)}%`, `≤ ${data.criteria.maximum_drawdown_pct.toFixed(2)}%`, data.checks.maximum_drawdown],
      ] as const
    : [];

  return (
    <Card className="h-full">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <LockKeyhole className="h-5 w-5 text-amber-400" />
          Live-Money Evidence Gate
        </CardTitle>
      </CardHeader>
      <CardContent>
        {loading && !data ? (
          <LoadingState message="Checking paper evidence..." />
        ) : data ? (
          <div className="space-y-3">
            <div className={`rounded-xl border p-3 text-sm ${data.ready ? "border-emerald-500/30 bg-emerald-500/[0.06] text-emerald-200" : "border-amber-500/30 bg-amber-500/[0.06] text-amber-100"}`}>
              {data.ready
                ? "Evidence thresholds pass. Live activation still requires explicit operator configuration and risk acknowledgement."
                : "Live trading is locked. Continue paper validation; failed checks cannot be bypassed from this dashboard."}
            </div>
            <div className="space-y-2">
              {rows.map(([label, actual, target, passed]) => (
                <div key={label} className="grid grid-cols-[1fr_auto_auto] items-center gap-3 rounded-lg bg-white/[0.02] px-3 py-2 text-sm">
                  <span className="text-zinc-300">{label}</span>
                  <span className="font-mono text-white">{actual}</span>
                  <span className="flex min-w-20 items-center justify-end gap-1 text-xs text-zinc-500">
                    {passed ? <CheckCircle2 className="h-4 w-4 text-emerald-400" /> : <XCircle className="h-4 w-4 text-red-400" />}
                    {target}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <p className="text-sm text-red-300">Readiness evidence is unavailable.</p>
        )}
      </CardContent>
    </Card>
  );
}
