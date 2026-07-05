"use client";

import { useEffect, useRef } from "react";
import { getApiKey, setApiKey } from "@/lib/api";

export interface ProvisionResult {
  api_key: string;
  tier: "free" | "premium";
  rotated: boolean;
}

/**
 * Requests a backend API key for the signed-in user via
 * POST /api/keys/provision and stores it in localStorage (mf_api_key).
 *
 * Note: the backend may rotate — a newly generated key replaces any
 * previously issued key for this account.
 */
export async function provisionApiKey(): Promise<ProvisionResult> {
  const res = await fetch("/api/keys/provision", { method: "POST" });

  let payload: (Partial<ProvisionResult> & { error?: string }) | null = null;
  try {
    payload = (await res.json()) as Partial<ProvisionResult> & {
      error?: string;
    };
  } catch {
    // Non-JSON response — handled below
  }

  if (!res.ok || !payload?.api_key || !payload.tier) {
    throw new Error(
      payload?.error ?? `Key provisioning failed (${res.status}).`
    );
  }

  setApiKey(payload.api_key);
  return {
    api_key: payload.api_key,
    tier: payload.tier,
    rotated: Boolean(payload.rotated),
  };
}

/**
 * Fire-and-forget provisioning for use right after signup/login.
 * Never throws and never rotates an existing key — it only fills the
 * gap when no key is stored in this browser yet.
 */
export function provisionApiKeyIfMissing(): void {
  if (typeof window === "undefined" || getApiKey()) return;
  provisionApiKey().catch((err: unknown) => {
    console.warn(
      "[mf-api-key] auto-provision failed:",
      err instanceof Error ? err.message : err
    );
  });
}

/**
 * On authenticated app load: if no mf_api_key is stored, provision one
 * automatically. Pass `enabled=false` while the auth state is unknown.
 */
export function useAutoProvisionApiKey(enabled: boolean): void {
  const attempted = useRef(false);

  useEffect(() => {
    if (!enabled || attempted.current) return;
    if (getApiKey()) return;
    attempted.current = true;
    provisionApiKeyIfMissing();
  }, [enabled]);
}
