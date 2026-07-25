"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Activity, LogOut, User, Zap } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { createClient } from "@/lib/supabase/client";
import type { TradingMode } from "@/types";

interface HeaderProps {
  mode: TradingMode;
  backendOnline: boolean;
  userEmail?: string | null;
}

export function Header({ mode, backendOnline, userEmail }: HeaderProps) {
  const router = useRouter();

  const handleLogout = async () => {
    if (
      process.env.NEXT_PUBLIC_SUPABASE_URL &&
      process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY
    ) {
      const supabase = createClient();
      await supabase.auth.signOut();
    }
    router.push("/login");
    router.refresh();
  };

  return (
    <header className="sticky top-0 z-50 border-b border-white/10 bg-[#0a0b14]/80 backdrop-blur-xl">
      <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-4 sm:px-6">
        <Link href="/" className="flex items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-cyan-500 to-blue-600 shadow-lg shadow-cyan-500/30">
            <Zap className="h-5 w-5 text-white" />
          </div>
          <div>
            <span className="text-lg font-bold tracking-tight text-white">
              MomentumForge
            </span>
            <span className="ml-1.5 text-xs font-medium text-cyan-400">PRIVATE</span>
          </div>
        </Link>

        <div className="flex items-center gap-3">
          <Badge variant={backendOnline ? "success" : "danger"}>
            <Activity className="mr-1 h-3 w-3" />
            {backendOnline ? "Connected" : "Offline"}
          </Badge>
          <Badge variant={mode === "live" ? "warning" : "default"}>
            {mode.toUpperCase()}
          </Badge>

          {userEmail ? (
            <div className="flex items-center gap-2">
              <Link href="/profile">
                <Button variant="ghost" size="sm">
                  <User className="mr-1.5 h-4 w-4" />
                  <span className="hidden sm:inline">{userEmail.split("@")[0]}</span>
                </Button>
              </Link>
              <Button variant="ghost" size="sm" onClick={handleLogout}>
                <LogOut className="h-4 w-4" />
              </Button>
            </div>
          ) : null}
        </div>
      </div>
    </header>
  );
}
