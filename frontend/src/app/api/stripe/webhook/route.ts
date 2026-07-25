import { NextRequest, NextResponse } from "next/server";
import Stripe from "stripe";
import { createClient } from "@supabase/supabase-js";
import { isPrivateMode } from "@/lib/access-policy";

const stripe = process.env.STRIPE_SECRET_KEY
  ? new Stripe(process.env.STRIPE_SECRET_KEY)
  : null;

function getSupabaseAdmin() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) return null;
  return createClient(url, key);
}

export async function POST(request: NextRequest) {
  if (isPrivateMode()) {
    return NextResponse.json(
      { error: "Billing is disabled during the private paper launch." },
      { status: 404 }
    );
  }
  if (!stripe || !process.env.STRIPE_WEBHOOK_SECRET) {
    return NextResponse.json(
      { error: "Stripe not configured" },
      { status: 503 }
    );
  }

  const body = await request.text();
  const signature = request.headers.get("stripe-signature");

  if (!signature) {
    return NextResponse.json(
      { error: "Missing stripe-signature header" },
      { status: 400 }
    );
  }

  let event: Stripe.Event;
  try {
    event = stripe.webhooks.constructEvent(
      body,
      signature,
      process.env.STRIPE_WEBHOOK_SECRET
    );
  } catch (err) {
    const message = err instanceof Error ? err.message : "Invalid signature";
    return NextResponse.json({ error: message }, { status: 400 });
  }

  const supabase = getSupabaseAdmin();

  switch (event.type) {
    case "checkout.session.completed": {
      const session = event.data.object as Stripe.Checkout.Session;
      const userId = session.metadata?.supabase_user_id;
      const plan = session.metadata?.plan || "pro";
      if (supabase && userId && session.customer) {
        await supabase.auth.admin.updateUserById(userId, {
          app_metadata: {
            stripe_customer_id: session.customer,
            subscription_status: "active",
            plan,
          },
        });
      }
      break;
    }
    case "customer.subscription.updated":
    case "customer.subscription.deleted": {
      const subscription = event.data.object as Stripe.Subscription;
      const customerId = subscription.customer as string;
      if (supabase && customerId) {
        const { data: users } = await supabase.auth.admin.listUsers();
        const user = users.users.find(
          (u) => u.app_metadata?.stripe_customer_id === customerId
        );
        if (user) {
          const existingPlan =
            typeof user.app_metadata?.plan === "string" &&
            user.app_metadata.plan !== "free"
              ? user.app_metadata.plan
              : "pro";
          await supabase.auth.admin.updateUserById(user.id, {
            app_metadata: {
              ...user.app_metadata,
              subscription_status: subscription.status,
              plan: subscription.status === "active" ? existingPlan : "free",
            },
          });
        }
      }
      break;
    }
    default:
      break;
  }

  return NextResponse.json({ received: true });
}
