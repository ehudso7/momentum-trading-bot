import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";
import { isPrivateMode } from "@/lib/access-policy";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL ||
  "https://momentum-trading-bot-production.up.railway.app";

interface ProvisionResponse {
  api_key: string;
  tier: "free" | "premium";
  rotated: boolean;
}

/**
 * POST /api/keys/provision
 *
 * Issues (or rotates) the caller's backend API key. Requires an
 * authenticated Supabase session. The raw key is returned only to the
 * client — it is never persisted in Supabase; we only flag that a key
 * has been issued via user_metadata.mf_api_key_issued.
 */
export async function POST() {
  if (isPrivateMode()) {
    return NextResponse.json(
      { error: "Browser API-key provisioning is disabled in private mode." },
      { status: 404 }
    );
  }
  if (
    !process.env.NEXT_PUBLIC_SUPABASE_URL ||
    !process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY
  ) {
    return NextResponse.json(
      { error: "Supabase is not configured on this deployment." },
      { status: 503 }
    );
  }

  const provisionSecret = process.env.TRADING_PROVISION_SECRET;
  if (!provisionSecret) {
    return NextResponse.json(
      {
        error:
          "Key provisioning is not configured. Set TRADING_PROVISION_SECRET on the frontend deployment (it must match the backend's provisioning secret).",
      },
      { status: 503 }
    );
  }

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND_URL}/keys/provision`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Provision-Secret": provisionSecret,
      },
      body: JSON.stringify({
        user_ref: user.id,
        label: user.email ?? undefined,
      }),
      cache: "no-store",
    });
  } catch {
    return NextResponse.json(
      { error: "Trading backend is unreachable. Try again shortly." },
      { status: 502 }
    );
  }

  if (!upstream.ok) {
    let detail = `Key provisioning failed (${upstream.status}).`;
    try {
      const payload = (await upstream.json()) as { detail?: string };
      if (payload.detail) detail = payload.detail;
    } catch {
      // Non-JSON upstream error — keep the generic message
    }
    return NextResponse.json(
      { error: detail },
      { status: upstream.status === 401 || upstream.status === 403 ? 502 : upstream.status }
    );
  }

  let data: ProvisionResponse;
  try {
    data = (await upstream.json()) as ProvisionResponse;
  } catch {
    return NextResponse.json(
      { error: "Trading backend returned an invalid provisioning response." },
      { status: 502 }
    );
  }

  if (!data.api_key) {
    return NextResponse.json(
      { error: "Trading backend did not return an API key." },
      { status: 502 }
    );
  }

  // Record that a key has been issued — never the key itself.
  const { error: metadataError } = await supabase.auth.updateUser({
    data: { mf_api_key_issued: true },
  });
  if (metadataError) {
    // Non-fatal: the key was issued; the flag is best-effort bookkeeping.
    console.warn(
      "[keys/provision] failed to flag mf_api_key_issued:",
      metadataError.message
    );
  }

  return NextResponse.json({
    api_key: data.api_key,
    tier: data.tier,
    rotated: data.rotated,
  });
}
