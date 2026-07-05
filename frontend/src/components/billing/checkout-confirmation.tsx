"use client";

import { useEffect, useState } from "react";
import { CheckCircle, Clock } from "lucide-react";
import { LoadingSpinner } from "@/components/ui/loading";
import { fetchBillingStatus, getApiKey } from "@/lib/api";
import { provisionApiKey } from "@/lib/use-api-key";

const POLL_INTERVAL_MS = 2_000;
const POLL_TIMEOUT_MS = 30_000;

type ConfirmationState = "confirming" | "active" | "delayed";

/**
 * Shown after a Stripe checkout completes. Polls the backend billing
 * status (Bearer auth from the locally stored API key) every 2s for up
 * to 30s, confirming that the premium entitlement has landed.
 */
export function CheckoutConfirmation() {
  const [state, setState] = useState<ConfirmationState>("confirming");

  useEffect(() => {
    let cancelled = false;

    const sleep = (ms: number) =>
      new Promise<void>((resolve) => setTimeout(resolve, ms));

    async function confirm() {
      // The buyer normally already holds a key; if this browser lost it,
      // best-effort re-provision so the status poll can authenticate.
      if (!getApiKey()) {
        try {
          await provisionApiKey();
        } catch {
          // Fall through — polling below will time out into "delayed"
        }
      }

      const deadline = Date.now() + POLL_TIMEOUT_MS;
      while (!cancelled && Date.now() < deadline) {
        try {
          const status = await fetchBillingStatus();
          if (status.premium) {
            if (!cancelled) setState("active");
            return;
          }
        } catch {
          // Transient backend/auth error — keep polling until deadline
        }
        await sleep(POLL_INTERVAL_MS);
      }
      if (!cancelled) setState("delayed");
    }

    void confirm();
    return () => {
      cancelled = true;
    };
  }, []);

  if (state === "active") {
    return (
      <div className="mb-6 flex items-center justify-center gap-2 rounded-xl border border-emerald-500/20 bg-emerald-500/5 p-4 text-sm text-emerald-400">
        <CheckCircle className="h-4 w-4 shrink-0" />
        <span>
          Premium active <span aria-hidden>✓</span> — full signals and premium
          features are unlocked.
        </span>
      </div>
    );
  }

  if (state === "delayed") {
    return (
      <div className="mb-6 flex items-start justify-center gap-2 rounded-xl border border-amber-500/20 bg-amber-500/5 p-4 text-sm text-amber-400">
        <Clock className="mt-0.5 h-4 w-4 shrink-0" />
        <span>
          Confirmation is taking longer than expected — your payment is safe,
          and the entitlement usually lands within a minute. Refresh this page
          to check again.
        </span>
      </div>
    );
  }

  return (
    <div className="mb-6 flex items-center justify-center gap-3 rounded-xl border border-cyan-500/20 bg-cyan-500/5 p-4 text-sm text-cyan-300">
      <LoadingSpinner className="h-4 w-4" />
      <span>Confirming your upgrade…</span>
    </div>
  );
}
