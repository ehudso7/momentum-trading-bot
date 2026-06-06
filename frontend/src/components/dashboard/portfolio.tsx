"use client";

import { motion } from "framer-motion";
import { Briefcase } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { LoadingState } from "@/components/ui/loading";
import { formatCurrency } from "@/lib/utils";
import type { Position } from "@/types";

interface PortfolioProps {
  positions: Position[];
  loading: boolean;
}

export function Portfolio({ positions, loading }: PortfolioProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Briefcase className="h-5 w-5 text-violet-400" />
          Open Positions
        </CardTitle>
      </CardHeader>
      <CardContent>
        {loading && <LoadingState message="Loading positions..." />}
        {!loading && positions.length === 0 && (
          <p className="py-8 text-center text-sm text-zinc-500">
            No open positions
          </p>
        )}
        {!loading && positions.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-white/10 text-left text-xs uppercase tracking-wider text-zinc-500">
                  <th className="pb-3 pr-4">Symbol</th>
                  <th className="pb-3 pr-4">Side</th>
                  <th className="pb-3 pr-4">Qty</th>
                  <th className="pb-3 pr-4">Entry</th>
                  <th className="pb-3">P&L</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((pos, i) => (
                  <motion.tr
                    key={`${pos.symbol}-${i}`}
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ delay: i * 0.05 }}
                    className="border-b border-white/5"
                  >
                    <td className="py-3 pr-4 font-semibold text-white">
                      {pos.symbol}
                    </td>
                    <td className="py-3 pr-4">
                      <Badge
                        variant={
                          pos.side?.toLowerCase() === "long"
                            ? "success"
                            : "danger"
                        }
                      >
                        {pos.side}
                      </Badge>
                    </td>
                    <td className="py-3 pr-4 text-zinc-300">{pos.qty}</td>
                    <td className="py-3 pr-4 text-zinc-300">
                      {formatCurrency(pos.entry_price)}
                    </td>
                    <td
                      className={`py-3 font-medium ${
                        (pos.unrealized_pnl ?? 0) >= 0
                          ? "text-emerald-400"
                          : "text-red-400"
                      }`}
                    >
                      {pos.unrealized_pnl != null
                        ? formatCurrency(pos.unrealized_pnl)
                        : "—"}
                    </td>
                  </motion.tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}