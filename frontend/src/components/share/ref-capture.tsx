"use client";

import { useEffect } from "react";
import { useSearchParams } from "next/navigation";

/** localStorage key holding the first-touch referral token. */
export const REF_STORAGE_KEY = "mf_ref";

/** Same shape as backend share tokens (share_links.TOKEN_RE). */
const REF_TOKEN_RE = /^[A-Za-z0-9_-]{8,64}$/;

/**
 * Read the stored referral token (set when the visitor arrived via a
 * shared signal card's "/signup?ref=<token>" CTA), if any.
 */
export function getStoredRef(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(REF_STORAGE_KEY);
    return raw && REF_TOKEN_RE.test(raw) ? raw : null;
  } catch {
    return null;
  }
}

/**
 * Invisible client component: persists a well-formed `?ref=<token>`
 * query param to localStorage (first touch wins — an existing ref is
 * never overwritten, so the original sharer keeps the attribution).
 * Mount inside a <Suspense> boundary (useSearchParams requirement).
 */
export function RefCapture() {
  const searchParams = useSearchParams();

  useEffect(() => {
    const ref = searchParams.get("ref");
    if (!ref || !REF_TOKEN_RE.test(ref)) return;
    try {
      if (!localStorage.getItem(REF_STORAGE_KEY)) {
        localStorage.setItem(REF_STORAGE_KEY, ref);
      }
    } catch {
      // Storage unavailable (private mode / quota) — attribution is
      // best-effort by design; signup must never break because of it.
    }
  }, [searchParams]);

  return null;
}
