"use client";

import { motion } from "framer-motion";
import { DollarSign, TrendingDown, TrendingUp, Wallet } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { formatCurrency, formatPercent } from "@/lib/utils";
import type { BotStatus } from "@/types";

interface StatsCardsProps {
  status: BotStatus | null;
  signalCount: number;
}

export function StatsCards({ status, signalCount }: StatsCardsProps) {
  const equity = status?.equity ?? 0;
  const dailyPnl = status?.daily_pnl ?? 0;
  const dailyReturn = status?.daily_return_pct ?? 0;
  const positions = status?.open_positions_count ?? 0;

  const stats = [
    {
      label: "Portfolio Value",
      value: formatCurrency(equity),
      icon: Wallet,
      color: "from-cyan-500 to-blue-600",
    },
    {
      label: "Daily P&L",
      value: formatCurrency(dailyPnl),
      sub: formatPercent(dailyReturn),
      icon: dailyPnl >= 0 ? TrendingUp : TrendingDown,
      color: dailyPnl >= 0 ? "from-emerald-500 to-green-600" : "from-red-500 to-rose-600",
    },
    {
      label: "Open Positions",
      value: positions.toString(),
      icon: DollarSign,
      color: "from-violet-500 to-purple-600",
    },
    {
      label: "Scanner Candidates",
      value: signalCount.toString(),
      icon: TrendingUp,
      color: "from-amber-500 to-orange-600",
    },
  ];

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      {stats.map((stat, i) => (
        <motion.div
          key={stat.label}
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: i * 0.1 }}
        >
          <Card className="overflow-hidden">
            <CardContent className="p-5">
              <div className="flex items-start justify-between">
                <div>
                  <p className="text-xs font-medium uppercase tracking-wider text-zinc-500">
                    {stat.label}
                  </p>
                  <p className="mt-2 text-2xl font-bold text-white">{stat.value}</p>
                  {stat.sub && (
                    <p
                      className={`mt-1 text-sm font-medium ${
                        dailyPnl >= 0 ? "text-emerald-400" : "text-red-400"
                      }`}
                    >
                      {stat.sub}
                    </p>
                  )}
                </div>
                <div
                  className={`flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br ${stat.color} shadow-lg`}
                >
                  <stat.icon className="h-5 w-5 text-white" />
                </div>
              </div>
            </CardContent>
          </Card>
        </motion.div>
      ))}
    </div>
  );
}
