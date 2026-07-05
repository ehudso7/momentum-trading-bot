"use client";

import { useState } from "react";
import { Settings2 } from "lucide-react";
import { Button } from "@/components/ui/button";

type PortalState = "idle" | "loading" | "unavailable";

/**
 * Opens the Stripe customer portal via POST /api/billing/portal.
 * The route resolves the customer from the authenticated session's
 * app_metadata — nothing is passed from the client. Hidden gracefully
 * when the user has no billing profile yet (404).
 */
export function ManageSubscriptionButton() {
  const [state, setState] = useState<PortalState>("idle");
  const [error, setError] = useState<string | null>(null);

  const handleClick = async () => {
    setState("loading");
    setError(null);
    try {
      const res = await fetch("/api/billing/portal", { method: "POST" });
      const payload = (await res.json().catch(() => null)) as {
        url?: string;
        error?: string;
      } | null;

      if (res.status === 404) {
        setState("unavailable");
        return;
      }
      if (!res.ok || !payload?.url) {
        throw new Error(
          payload?.error ?? `Could not open the billing portal (${res.status}).`
        );
      }
      window.location.assign(payload.url);
    } catch (err) {
      setState("idle");
      setError(
        err instanceof Error ? err.message : "Could not open the billing portal."
      );
    }
  };

  if (state === "unavailable") {
    return (
      <p className="text-center text-xs text-zinc-500">
        No active subscription found for this account yet — the billing portal
        unlocks after your first payment.
      </p>
    );
  }

  return (
    <div className="flex flex-col items-center gap-2">
      <Button
        variant="outline"
        size="sm"
        onClick={handleClick}
        disabled={state === "loading"}
      >
        <Settings2 className="mr-1.5 h-4 w-4" />
        {state === "loading" ? "Opening portal..." : "Manage subscription"}
      </Button>
      {error && (
        <p className="max-w-md text-center text-xs text-red-300">{error}</p>
      )}
    </div>
  );
}
