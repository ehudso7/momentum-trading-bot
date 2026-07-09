"use client";

import { useMemo } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { AlertTriangle, CheckCircle2, Gauge } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { LoadingState } from "@/components/ui/loading";
import { formatCurrency } from "@/lib/utils";
import type { PerformanceScorecard as PerformanceScorecardData } from "@/lib/api";

interface PerformanceScorecardProps {
  data: PerformanceScorecardData | null;
  loading: boolean;
}

const DASH = "—";

/** A finite number, or null/undefined/NaN which we render as an em-dash. */
function isFiniteNumber(v: number | null | undefined): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

function fmtNumber(v: number | null | undefined, digits = 2): string {
  return isFiniteNumber(v) ? v.toFixed(digits) : DASH;
}

function fmtPercent(v: number | null | undefined, digits = 1): string {
  return isFiniteNumber(v) ? `${v.toFixed(digits)}%` : DASH;
}

function fmtSignedPercent(v: number | null | undefined, digits = 2): string {
  if (!isFiniteNumber(v)) return DASH;
  const sign = v >= 0 ? "+" : "";
  return `${sign}${v.toFixed(digits)}%`;
}

function fmtMoney(v: number | null | undefined): string {
  return isFiniteNumber(v) ? formatCurrency(v) : DASH;
}

function fmtRatio(v: number | null | undefined): string {
  return isFiniteNumber(v) ? `${v.toFixed(2)}x` : DASH;
}

function fmtHold(minutes: number | null | undefined): string {
  if (!isFiniteNumber(minutes) || minutes <= 0) return DASH;
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

interface StatCellProps {
  label: string;
  value: string;
  tone?: "neutral" | "positive" | "negative";
  hint?: string;
}

function StatCell({ label, value, tone = "neutral", hint }: StatCellProps) {
  const valueColor =
    tone === "positive"
      ? "text-emerald-400"
      : tone === "negative"
        ? "text-red-400"
        : "text-white";
  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-3">
      <p className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">
        {label}
      </p>
      <p className={`mt-1.5 font-mono text-lg font-semibold ${valueColor}`}>
        {value}
      </p>
      {hint && <p className="mt-0.5 text-[11px] text-zinc-500">{hint}</p>}
    </div>
  );
}

/** Tone of a signed value, only meaningful when we actually have trades. */
function signedTone(
  v: number | null | undefined,
  active: boolean
): "neutral" | "positive" | "negative" {
  if (!active || !isFiniteNumber(v) || v === 0) return "neutral";
  return v > 0 ? "positive" : "negative";
}

export function PerformanceScorecard({ data, loading }: PerformanceScorecardProps) {
  const bySetup = data?.by_setup ?? [];

  // "Active" = the strategy has actually closed at least one trade. Until then
  // every metric is a hollow zero and we must not dress it up.
  const closed = data?.closed_trades ?? 0;
  const active = closed > 0;
  const significant = Boolean(data?.is_statistically_significant);

  const sampleSize = data?.sample_size ?? 0;
  const minSample = data?.min_sample_for_confidence ?? 0;
  const progress = useMemo(() => {
    if (!minSample || minSample <= 0) return 0;
    return Math.min(100, Math.max(0, (sampleSize / minSample) * 100));
  }, [sampleSize, minSample]);

  const confidenceNote =
    data?.confidence_note?.trim() ||
    (active
      ? "Sample size is thin — treat these metrics as indicative only."
      : "No closed trades yet — the strategy has not entered a position.");

  const chartData = useMemo(
    () =>
      (data?.rolling ?? []).map((p) => ({
        index: p.index,
        winRate: isFiniteNumber(p.win_rate) ? p.win_rate : null,
        expectancy: isFiniteNumber(p.expectancy) ? p.expectancy : null,
      })),
    [data?.rolling]
  );

  // Banner styling: honest by construction. Only celebrate (emerald) when the
  // sample is genuinely significant. Thin/zero samples get an amber warning.
  const bannerClass = significant
    ? "border-emerald-500/30 bg-emerald-500/[0.07]"
    : "border-amber-500/30 bg-amber-500/[0.06]";
  const bannerIconColor = significant ? "text-emerald-400" : "text-amber-400";
  const BannerIcon = significant ? CheckCircle2 : AlertTriangle;
  const bannerHeadline = significant
    ? "Statistically significant sample"
    : active
      ? "Indicative only — sample too small to confirm an edge"
      : "No verified edge yet";

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Gauge className="h-5 w-5 text-violet-400" />
          Performance Scorecard
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-5">
        {loading && !data ? (
          <LoadingState message="Loading performance..." />
        ) : (
          <>
            {/* Honesty banner — carries the truth about the sample. */}
            <div className={`rounded-xl border p-4 ${bannerClass}`}>
              <div className="flex items-start gap-3">
                <BannerIcon
                  className={`mt-0.5 h-5 w-5 flex-shrink-0 ${bannerIconColor}`}
                />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-semibold text-white">
                    {bannerHeadline}
                  </p>
                  <p className="mt-1 text-sm text-zinc-300">{confidenceNote}</p>

                  {minSample > 0 && (
                    <div className="mt-3">
                      <div className="mb-1 flex items-center justify-between text-[11px] font-medium text-zinc-400">
                        <span>Sample toward confidence</span>
                        <span className="font-mono">
                          {sampleSize}/{minSample} trades
                        </span>
                      </div>
                      <div className="h-1.5 w-full overflow-hidden rounded-full bg-white/10">
                        <div
                          className={`h-full rounded-full transition-all ${
                            significant ? "bg-emerald-400" : "bg-amber-400"
                          }`}
                          style={{ width: `${progress}%` }}
                        />
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </div>

            {/* Stat grid. When inactive, every cell is an em-dash — no fake zeros. */}
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
              <StatCell
                label="Win Rate"
                value={active ? fmtPercent(data?.win_rate) : DASH}
                hint={
                  active
                    ? `${data?.wins ?? 0}W / ${data?.losses ?? 0}L`
                    : undefined
                }
              />
              <StatCell
                label="Profit Factor"
                value={active ? fmtNumber(data?.profit_factor) : DASH}
              />
              <StatCell
                label="Expectancy / Trade"
                value={active ? fmtMoney(data?.expectancy_per_trade) : DASH}
                tone={signedTone(data?.expectancy_per_trade, active)}
                hint={active ? `${fmtNumber(data?.expectancy_r)}R` : undefined}
              />
              <StatCell
                label="Total P&L"
                value={active ? fmtMoney(data?.total_pnl) : DASH}
                tone={signedTone(data?.total_pnl, active)}
              />
              <StatCell
                label="Total Return"
                value={active ? fmtSignedPercent(data?.total_return_pct) : DASH}
                tone={signedTone(data?.total_return_pct, active)}
              />
              <StatCell
                label="Max Drawdown"
                value={active ? fmtPercent(data?.max_drawdown_pct) : DASH}
                tone={
                  active && isFiniteNumber(data?.max_drawdown_pct) && data!.max_drawdown_pct > 0
                    ? "negative"
                    : "neutral"
                }
              />
              <StatCell
                label="Sharpe"
                value={active ? fmtNumber(data?.sharpe_ratio) : DASH}
              />
              <StatCell
                label="Sortino"
                value={active ? fmtNumber(data?.sortino_ratio) : DASH}
              />
              <StatCell
                label="Avg R:R"
                value={active ? fmtRatio(data?.avg_rr) : DASH}
              />
              <StatCell
                label="Avg Hold"
                value={active ? fmtHold(data?.avg_hold_minutes) : DASH}
              />
              <StatCell
                label="Trades"
                value={
                  isFiniteNumber(data?.trade_count)
                    ? String(data?.trade_count)
                    : "0"
                }
                hint={
                  isFiniteNumber(data?.closed_trades)
                    ? `${data?.closed_trades} closed`
                    : undefined
                }
              />
            </div>

            {/* Per-setup breakdown — hidden when empty. */}
            {bySetup.length > 0 && (
              <div>
                <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
                  By Setup
                </p>
                <div className="overflow-x-auto rounded-xl border border-white/5">
                  <table className="w-full min-w-[440px] text-sm">
                    <thead>
                      <tr className="border-b border-white/5 text-left text-[11px] uppercase tracking-wider text-zinc-500">
                        <th className="px-3 py-2 font-medium">Setup</th>
                        <th className="px-3 py-2 text-right font-medium">Trades</th>
                        <th className="px-3 py-2 text-right font-medium">Win %</th>
                        <th className="px-3 py-2 text-right font-medium">PF</th>
                        <th className="px-3 py-2 text-right font-medium">Exp / Trade</th>
                      </tr>
                    </thead>
                    <tbody>
                      {bySetup.map((row) => (
                        <tr
                          key={row.setup}
                          className="border-b border-white/5 last:border-0"
                        >
                          <td className="px-3 py-2 font-medium text-white">
                            {row.setup}
                          </td>
                          <td className="px-3 py-2 text-right font-mono text-zinc-300">
                            {isFiniteNumber(row.trades) ? row.trades : DASH}
                          </td>
                          <td className="px-3 py-2 text-right font-mono text-zinc-300">
                            {fmtPercent(row.win_rate)}
                          </td>
                          <td className="px-3 py-2 text-right font-mono text-zinc-300">
                            {fmtNumber(row.profit_factor)}
                          </td>
                          <td
                            className={`px-3 py-2 text-right font-mono ${
                              signedTone(row.expectancy_per_trade, true) === "positive"
                                ? "text-emerald-400"
                                : signedTone(row.expectancy_per_trade, true) === "negative"
                                  ? "text-red-400"
                                  : "text-zinc-300"
                            }`}
                          >
                            {fmtMoney(row.expectancy_per_trade)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* Rolling sparkline — hidden until we have at least two points. */}
            {chartData.length >= 2 && (
              <div>
                <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
                  Rolling Win Rate &amp; Expectancy
                </p>
                <div className="h-40 w-full min-h-[160px]" style={{ minHeight: "160px" }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={chartData}>
                      <CartesianGrid
                        strokeDasharray="3 3"
                        stroke="rgba(255,255,255,0.05)"
                      />
                      <XAxis
                        dataKey="index"
                        stroke="#71717a"
                        fontSize={11}
                        tickLine={false}
                      />
                      <YAxis
                        yAxisId="left"
                        stroke="#06b6d4"
                        fontSize={11}
                        tickLine={false}
                        width={38}
                        tickFormatter={(v) => `${Number(v).toFixed(0)}%`}
                      />
                      <YAxis
                        yAxisId="right"
                        orientation="right"
                        stroke="#a78bfa"
                        fontSize={11}
                        tickLine={false}
                        width={44}
                        tickFormatter={(v) => `$${Number(v).toFixed(0)}`}
                      />
                      <Tooltip
                        contentStyle={{
                          background: "#18181b",
                          border: "1px solid rgba(255,255,255,0.1)",
                          borderRadius: "8px",
                          color: "#fff",
                          fontSize: "12px",
                        }}
                        formatter={(value, name) => {
                          if (name === "Win Rate")
                            return [`${Number(value).toFixed(1)}%`, name];
                          return [formatCurrency(Number(value)), name];
                        }}
                        labelFormatter={(label) => `Trade #${label}`}
                      />
                      <Line
                        yAxisId="left"
                        type="monotone"
                        dataKey="winRate"
                        name="Win Rate"
                        stroke="#06b6d4"
                        strokeWidth={2}
                        dot={false}
                        connectNulls
                      />
                      <Line
                        yAxisId="right"
                        type="monotone"
                        dataKey="expectancy"
                        name="Expectancy"
                        stroke="#a78bfa"
                        strokeWidth={2}
                        dot={false}
                        connectNulls
                      />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
