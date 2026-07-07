"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Check, Share2 } from "lucide-react";
import { createSignalShareLink, getApiKey } from "@/lib/api";
import { cn } from "@/lib/utils";

type ShareState = "idle" | "busy" | "copied" | "error";

const RESET_DELAY_MS = 2000;

interface ShareButtonProps {
  symbol: string;
  className?: string;
}

// localStorage-backed "do we hold an API key?" store: false during SSR
// (stable markup for signed-out visitors), live across tabs after mount.
function subscribeToStorage(callback: () => void): () => void {
  window.addEventListener("storage", callback);
  return () => window.removeEventListener("storage", callback);
}

function hasApiKeySnapshot(): boolean {
  return Boolean(getApiKey());
}

function serverSnapshot(): boolean {
  return false;
}

/**
 * Per-signal share affordance. Mints a public /s/<token> link via the
 * authenticated backend and copies it to the clipboard, flashing a
 * brief "Link copied" state. Renders nothing until an API key is
 * present in this browser (share links require auth), and fails
 * gracefully — a console.warn, never a crash — if the key was revoked
 * or the clipboard is unavailable.
 */
export function ShareButton({ symbol, className }: ShareButtonProps) {
  const hasKey = useSyncExternalStore(
    subscribeToStorage,
    hasApiKeySnapshot,
    serverSnapshot
  );
  const [state, setState] = useState<ShareState>("idle");
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Cleanup only: never leave the "copied" flash timer running after
  // the row unmounts.
  useEffect(() => {
    return () => {
      if (resetTimer.current) clearTimeout(resetTimer.current);
    };
  }, []);

  if (!hasKey) return null;

  const flash = (next: ShareState) => {
    setState(next);
    if (resetTimer.current) clearTimeout(resetTimer.current);
    resetTimer.current = setTimeout(() => setState("idle"), RESET_DELAY_MS);
  };

  const handleShare = async () => {
    if (state === "busy") return;
    if (!getApiKey()) {
      console.warn("[share] no API key stored — sign in to share signals");
      return;
    }
    setState("busy");
    try {
      const link = await createSignalShareLink(symbol);
      const url = `${window.location.origin}${link.path}`;
      await navigator.clipboard.writeText(url);
      flash("copied");
    } catch (err) {
      console.warn(
        `[share] could not create share link for ${symbol}:`,
        err instanceof Error ? err.message : err
      );
      flash("error");
    }
  };

  const copied = state === "copied";

  return (
    <button
      type="button"
      onClick={handleShare}
      disabled={state === "busy"}
      aria-label={copied ? "Link copied" : `Share ${symbol} signal`}
      title={copied ? "Link copied" : `Share ${symbol} signal`}
      className={cn(
        "inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg px-2 text-xs font-medium transition-all duration-200",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/50",
        copied
          ? "bg-emerald-500/15 text-emerald-300"
          : state === "error"
            ? "bg-red-500/10 text-red-300"
            : "text-zinc-500 hover:bg-white/5 hover:text-cyan-300",
        state === "busy" && "cursor-wait opacity-60",
        className
      )}
    >
      {copied ? (
        <>
          <Check className="h-3.5 w-3.5" />
          <span>Link copied</span>
        </>
      ) : state === "error" ? (
        <>
          <Share2 className="h-3.5 w-3.5" />
          <span>Try again</span>
        </>
      ) : (
        <Share2 className="h-3.5 w-3.5" />
      )}
    </button>
  );
}
