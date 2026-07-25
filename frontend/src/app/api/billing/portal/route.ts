import { NextResponse } from "next/server";
import Stripe from "stripe";
import { createClient } from "@/lib/supabase/server";
import { isPrivateMode } from "@/lib/access-policy";

const stripe = process.env.STRIPE_SECRET_KEY
  ? new Stripe(process.env.STRIPE_SECRET_KEY)
  : null;

/**
 * POST /api/billing/portal
 *
 * Creates a Stripe customer portal session for the authenticated user.
 * The Stripe customer is resolved exclusively from the user's
 * app_metadata (written by the Stripe webhook) — customer IDs are never
 * accepted from the request, so users can only ever open their own portal.
 */
export async function POST(request: Request) {
  if (isPrivateMode()) {
    return NextResponse.json(
      { error: "Billing is disabled during the private paper launch." },
      { status: 404 }
    );
  }
  if (!stripe) {
    return NextResponse.json({ error: "Stripe not configured" }, { status: 503 });
  }

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const customerId = user.app_metadata?.stripe_customer_id;
  if (typeof customerId !== "string" || customerId.length === 0) {
    return NextResponse.json(
      {
        error:
          "No billing profile found for this account. Complete a checkout first — the subscription portal becomes available after your first payment.",
      },
      { status: 404 }
    );
  }

  const origin = request.headers.get("origin") ?? "http://localhost:3000";

  try {
    const session = await stripe.billingPortal.sessions.create({
      customer: customerId,
      return_url: `${origin}/billing`,
    });
    return NextResponse.json({ url: session.url });
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Failed to create portal session";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
