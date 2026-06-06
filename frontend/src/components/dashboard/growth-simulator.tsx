"use client";

import { useMemo, useState } from "react";
import { motion } from "framer-motion";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { TrendingUp, Rocket, Target, Calendar } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { formatCurrency, formatPercent } from "@/lib/utils";
import type { BotStatus } from "@/types";

interface GrowthSimulatorProps {
  status: BotStatus | null;
  currentEquity: number;
}

interface Projection {
  period: string;
  days: number;
  conservative: number;
  base: number;
  optimistic: number;
  multiplier: number;
}

export function GrowthSimulator({ status, currentEquity }: GrowthSimulatorProps) {
  const startingEquity = status?.starting_equity ?? currentEquity ?? 100000;

  // Tunable parameters for "AI" projections (realistic for momentum edge + compound)
  // These are illustrative/educational only. Real results vary wildly.
  const [winRate, setWinRate] = useState(0.58); // strong but achievable with good filter
  const [avgRR, setAvgRR] = useState(1.85); // after scale-outs + winner management
  const [tradesPerMonth, setTradesPerMonth] = useState(12); // conservative for quality setups
  const [showAdvanced, setShowAdvanced] = useState(false);

  const expectancy = useMemo(() => {
    const loss = 1.0; // normalized R
    return winRate * avgRR - (1 - winRate) * loss;
  }, [winRate, avgRR]);

  const monthlyExpectancy = useMemo(() => {
    return expectancy * tradesPerMonth;
  }, [expectancy, tradesPerMonth]);

  const projections: Projection[] = useMemo(() => {
    const periods = [
      { period: "30 days", days: 30 },
      { period: "90 days", days: 90 },
      { period: "6 months", days: 180 },
      { period: "1 year", days: 365 },
    ];

    const baseMonthlyMult = 1 + monthlyExpectancy / 100;

    return periods.map((p) => {
      const months = p.days / 30;
      // Compound monthly
      const baseMult = Math.pow(baseMonthlyMult, months);
      const baseEquity = currentEquity * baseMult;

      // Conservative: 70% of edge
      const consMult = Math.pow(1 + (monthlyExpectancy * 0.7) / 100, months);
      const consEquity = currentEquity * consMult;

      // Optimistic: 130% edge + slightly higher frequency (still within risk rules)
      const optMult = Math.pow(1 + (monthlyExpectancy * 1.3) / 100, months);
      const optEquity = currentEquity * optMult;

      return {
        period: p.period,
        days: p.days,
        conservative: Math.round(consEquity),
        base: Math.round(baseEquity),
        optimistic: Math.round(optEquity),
        multiplier: parseFloat(baseMult.toFixed(2)),
      };
    });
  }, [currentEquity, monthlyExpectancy]);

  // Mini projection curve for the chart (base case over 12 months)
  const curveData = useMemo(() => {
    const points = [];
    const baseMonthlyMult = 1 + monthlyExpectancy / 100;
    let eq = currentEquity;

    for (let m = 0; m <= 12; m++) {
      points.push({
        month: m,
        equity: Math.round(eq),
        label: m === 0 ? "Now" : `M${m}`,
      });
      eq = eq * baseMonthlyMult;
    }
    return points;
  }, [currentEquity, monthlyExpectancy]);

  const timeToDouble = useMemo(() => {
    if (monthlyExpectancy <= 0) return "N/A (no edge)";
    // months to 2x
    const months = Math.log(2) / Math.log(1 + monthlyExpectancy / 100);
    const years = months / 12;
    if (years < 1) return `${months.toFixed(1)} months`;
    return `${years.toFixed(1)} years`;
  }, [monthlyExpectancy]);

  const currentMultiplier = (currentEquity / startingEquity).toFixed(2);

  const resetToDefaults = () => {
    setWinRate(0.58);
    setAvgRR(1.85);
    setTradesPerMonth(12);
  };

  return (
    <Card className="overflow-hidden border-white/10 bg-[#0a0b14]">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2 text-xl">
            <Rocket className="h-6 w-6 text-violet-400" />
            MomentumForge Growth Simulator
            <span className="ml-2 rounded-full bg-violet-500/10 px-2.5 py-0.5 text-xs font-medium text-violet-400">
              AI PROJECTIONS
            </span>
          </CardTitle>
          <Button variant="ghost" size="sm" onClick={() => setShowAdvanced(!showAdvanced)}>
            {showAdvanced ? "Simple" : "Tune Edge"}
          </Button>
        </div>
        <p className="text-sm text-zinc-400">
          Educational projections based on current equity, realistic momentum edge, and compound mode.
          <span className="font-medium text-amber-400"> Not financial advice. Markets are uncertain.</span>
        </p>
      </CardHeader>

      <CardContent className="space-y-6">
        {/* Current Snapshot */}
        <div className="grid grid-cols-2 gap-4 rounded-xl bg-white/[0.02] p-4 sm:grid-cols-4">
          <div>
            <div className="text-xs uppercase tracking-widest text-zinc-500">Current Equity</div>
            <div className="text-2xl font-semibold text-white">{formatCurrency(currentEquity)}</div>
          </div>
          <div>
            <div className="text-xs uppercase tracking-widest text-zinc-500">Starting Equity</div>
            <div className="text-2xl font-semibold text-white">{formatCurrency(startingEquity)}</div>
          </div>
          <div>
            <div className="text-xs uppercase tracking-widest text-zinc-500">Current Compounding</div>
            <div className="text-2xl font-semibold text-emerald-400">{currentMultiplier}x</div>
          </div>
          <div>
            <div className="text-xs uppercase tracking-widest text-zinc-500">Time to 2× (at pace)</div>
            <div className="text-2xl font-semibold text-violet-400">{timeToDouble}</div>
          </div>
        </div>

        {/* Tuners */}
        {showAdvanced && (
          <div className="space-y-4 rounded-xl border border-white/10 bg-black/30 p-4">
            <div className="flex items-center justify-between text-sm">
              <span className="font-medium text-white">Tune your edge parameters (for simulation only)</span>
              <Button size="sm" variant="outline" onClick={resetToDefaults}>
                Reset to strong momentum defaults
              </Button>
            </div>

            <div className="grid gap-4 sm:grid-cols-3">
              <div>
                <label className="mb-1 block text-xs text-zinc-400">Win Rate: {(winRate * 100).toFixed(0)}%</label>
                <input
                  type="range"
                  min="0.42"
                  max="0.72"
                  step="0.01"
                  value={winRate}
                  onChange={(e) => setWinRate(parseFloat(e.target.value))}
                  className="w-full accent-violet-500"
                />
                <div className="mt-0.5 text-[10px] text-zinc-500">Realistic filtered momentum: 52-65%</div>
              </div>
              <div>
                <label className="mb-1 block text-xs text-zinc-400">Avg R:R after scale-outs: {avgRR.toFixed(2)}R</label>
                <input
                  type="range"
                  min="1.2"
                  max="2.6"
                  step="0.05"
                  value={avgRR}
                  onChange={(e) => setAvgRR(parseFloat(e.target.value))}
                  className="w-full accent-cyan-500"
                />
                <div className="mt-0.5 text-[10px] text-zinc-500">Strategy target + winner management</div>
              </div>
              <div>
                <label className="mb-1 block text-xs text-zinc-400">High-quality trades / mo: {tradesPerMonth}</label>
                <input
                  type="range"
                  min="4"
                  max="22"
                  step="1"
                  value={tradesPerMonth}
                  onChange={(e) => setTradesPerMonth(parseInt(e.target.value))}
                  className="w-full accent-emerald-500"
                />
                <div className="mt-0.5 text-[10px] text-zinc-500">Quality over quantity (regime adaptive)</div>
              </div>
            </div>

            <div className="rounded bg-white/[0.03] p-3 text-xs text-zinc-400">
              Implied monthly expectancy: <span className="font-mono text-emerald-400">+{monthlyExpectancy.toFixed(1)}%</span> (winrate × R:R minus losers).
              This drives the projections below. Higher quality filters → better real expectancy.
            </div>
          </div>
        )}

        {/* Projection Table */}
        <div>
          <div className="mb-2 flex items-center gap-2 text-sm font-medium text-white">
            <Target className="h-4 w-4 text-cyan-400" /> Projected Equity (Compound Mode Active)
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-white/10 text-left text-xs uppercase tracking-widest text-zinc-500">
                  <th className="pb-2 pr-4">Horizon</th>
                  <th className="pb-2 pr-4 text-right">Conservative</th>
                  <th className="pb-2 pr-4 text-right text-emerald-400">Base Case</th>
                  <th className="pb-2 text-right text-violet-400">Optimistic</th>
                  <th className="pb-2 pl-4 text-right">× from now</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5">
                {projections.map((p, idx) => (
                  <tr key={idx} className="text-white">
                    <td className="py-2.5 pr-4 font-medium">{p.period}</td>
                    <td className="py-2.5 pr-4 text-right font-mono text-zinc-400">{formatCurrency(p.conservative)}</td>
                    <td className="py-2.5 pr-4 text-right font-mono font-semibold text-emerald-400">{formatCurrency(p.base)}</td>
                    <td className="py-2.5 text-right font-mono text-violet-400">{formatCurrency(p.optimistic)}</td>
                    <td className="py-2.5 pl-4 text-right font-semibold text-white/80">{p.multiplier}×</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-1 text-[10px] text-zinc-500">
            Base uses your current parameters + natural equity compounding (see risk/compound.py).
          </div>
        </div>

        {/* Beautiful Projection Curve */}
        <div className="h-64 w-full rounded-xl bg-black/40 p-3">
          <div className="mb-1 flex items-center gap-2 text-xs uppercase tracking-widest text-zinc-400">
            <Calendar className="h-3.5 w-3.5" /> 12-Month Base Case Trajectory
          </div>
          <ResponsiveContainer width="100%" height="90%">
            <AreaChart data={curveData}>
              <defs>
                <linearGradient id="projGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#8b5cf6" stopOpacity={0.45} />
                  <stop offset="100%" stopColor="#8b5cf6" stopOpacity={0.05} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="2 2" stroke="rgba(255,255,255,0.06)" />
              <XAxis dataKey="label" stroke="#3f3f46" fontSize={10} />
              <YAxis
                stroke="#3f3f46"
                fontSize={10}
                tickFormatter={(v) => `$${(v / 1000).toFixed(0)}k`}
              />
              <Tooltip
                contentStyle={{ background: "#111113", border: "1px solid #27272a", borderRadius: 6 }}
                formatter={(v) => [formatCurrency(Number(v)), "Projected Equity"]}
              />
              <Area
                type="natural"
                dataKey="equity"
                stroke="#a78bfa"
                strokeWidth={2.5}
                fill="url(#projGradient)"
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-white/10 bg-white/[0.015] p-3 text-xs">
          <div className="text-zinc-400">
            With compound mode enabled and disciplined execution, small edges become life-changing over time.
            The real "AI" is the combination of the strategy + risk engine + your consistency.
          </div>
          <Button size="sm" variant="secondary" onClick={resetToDefaults} className="shrink-0">
            Reset to Peak Defaults
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
