"use client";

import { Badge } from "@/components/ui/badge";
import type { TradingMode } from "@/types";

interface ModeToggleProps {
  mode: TradingMode;
  botRunning?: boolean;
  regime?: string;
}

export function ModeToggle({ mode, botRunning, regime }: ModeToggleProps) {
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-white/10 bg-white/[0.03] p-4">
      <div className="flex items-center gap-2">
        <span className="text-sm text-zinc-400">Trading Mode</span>
        <Badge variant={mode === "live" ? "warning" : "default"}>
          {mode.toUpperCase()}
        </Badge>
      </div>
      {botRunning != null && (
        <div className="flex items-center gap-2">
          <span className="text-sm text-zinc-400">Bot</span>
          <Badge variant={botRunning ? "success" : "neutral"}>
            {botRunning ? "Running" : "Stopped"}
          </Badge>
        </div>
      )}
      {regime && (
        <div className="flex items-center gap-2">
          <span className="text-sm text-zinc-400">Regime</span>
          <Badge variant="neutral" className="capitalize">
            {regime}
          </Badge>
        </div>
      )}
    </div>
  );
}