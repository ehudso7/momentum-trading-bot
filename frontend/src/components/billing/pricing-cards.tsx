"use client";

import { useEffect, useState } from "react";
import { Check, Crown, Sparkles, Zap } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ApiError,
  createCheckoutSession,
  fetchBillingStatus,
  getApiKey,
} from "@/lib/api";
import type { PlanTier } from "@/lib/api";
import { ManageSubscriptionButton } from "@/components/billing/manage-subscription-button";
import type { SubscriptionTier } from "@/types";

// Feature lists mirror the entitlements enforced by the backend
// (rate limits, report history windows, experiment caps, signal detail).
const TIERS: SubscriptionTier[] = [
  {
    id: "free",
    name: "Free",
    price: 0,
    features: [
      "Truncated signal previews",
      "3-day report history",
      "3 experiments",
      "60 API requests/min",
      "Paper trading dashboard",
    ],
  },
  {
    id: "pro",
    name: "Pro",
    price: 29,
    highlighted: true,
    features: [
      "Full signal details (entry/stop/target)",
      "30-day report history",
      "25 experiments",
      "120 API requests/min",
      "PDF report exports",
    ],
  },
  {
    id: "elite",
    name: "Elite",
    price: 99,
    features: [
      "Everything in Pro",
      "Elite insights on every report",
      "Unlimited report history",
      "Unlimited experiments",
      "300 API requests/min (priority)",
      "Live trading signals",
      "Custom alerts & dedicated support",
    ],
  },
];

export function PricingCards() {
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showPortalPath, setShowPortalPath] = useState(false);
  const [currentPlan, setCurrentPlan] = useState<PlanTier | null>(null);

  useEffect(() => {
    // Plan-aware rendering only when this browser holds an API key —
    // anonymous visitors just see the plain pricing grid.
    if (!getApiKey()) return;
    let cancelled = false;
    void fetchBillingStatus()
      .then((status) => {
        if (!cancelled) setCurrentPlan(status.plan);
      })
      .catch(() => {
        // Invalid key or backend unreachable — fall back to anonymous view.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const subscribed = currentPlan === "pro" || currentPlan === "elite";

  const handleUpgrade = async (tierId: SubscriptionTier["id"]) => {
    if (tierId === "free") return;
    setLoading(tierId);
    setError(null);
    setShowPortalPath(false);
    try {
      const { checkout_url } = await createCheckoutSession(tierId);
      window.location.assign(checkout_url);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // Already subscribed — the backend routes plan switches through
        // the Stripe billing portal, so surface that path directly.
        setError(err.detail ?? err.message);
        setShowPortalPath(true);
      } else {
        setError(
          err instanceof Error
            ? err.message
            : "Checkout failed. Ensure your API key is configured."
        );
      }
    } finally {
      setLoading(null);
    }
  };

  const buttonLabel = (tier: SubscriptionTier): string => {
    if (loading === tier.id) return "Redirecting...";
    if (currentPlan === tier.id || (tier.id === "free" && !subscribed)) {
      return "Current Plan";
    }
    if (tier.id === "free") return "Included";
    if (subscribed) return "Switch plan";
    return `Upgrade to ${tier.name}`;
  };

  const icons = { free: Zap, pro: Crown, elite: Sparkles };

  return (
    <div>
      {error && (
        <div className="mb-6 rounded-xl border border-red-500/20 bg-red-500/5 p-4 text-center text-sm text-red-300">
          <p>{error}</p>
          {showPortalPath && (
            <div className="mt-3 flex flex-col items-center gap-2">
              <p className="text-xs text-zinc-400">
                Plan changes for an active subscription go through the billing
                portal:
              </p>
              <ManageSubscriptionButton />
            </div>
          )}
        </div>
      )}
      <div className="grid gap-6 md:grid-cols-3">
        {TIERS.map((tier) => {
          const Icon = icons[tier.id];
          const isCurrent = currentPlan === tier.id;
          return (
            <Card
              key={tier.id}
              className={
                isCurrent
                  ? "relative border-emerald-500/30 shadow-emerald-500/10"
                  : tier.highlighted
                    ? "relative border-cyan-500/30 shadow-cyan-500/10"
                    : ""
              }
            >
              {isCurrent ? (
                <div className="absolute -top-3 left-1/2 -translate-x-1/2 rounded-full bg-gradient-to-r from-emerald-500 to-teal-600 px-3 py-1 text-xs font-medium text-white">
                  Current plan
                </div>
              ) : (
                tier.highlighted && (
                  <div className="absolute -top-3 left-1/2 -translate-x-1/2 rounded-full bg-gradient-to-r from-cyan-500 to-blue-600 px-3 py-1 text-xs font-medium text-white">
                    Most Popular
                  </div>
                )
              )}
              <CardHeader className="text-center">
                <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-white/5">
                  <Icon className="h-6 w-6 text-cyan-400" />
                </div>
                <CardTitle>{tier.name}</CardTitle>
                <p className="text-3xl font-bold text-white">
                  ${tier.price}
                  <span className="text-sm font-normal text-zinc-500">/mo</span>
                </p>
              </CardHeader>
              <CardContent>
                <ul className="mb-6 space-y-3">
                  {tier.features.map((feature) => (
                    <li
                      key={feature}
                      className="flex items-start gap-2 text-sm text-zinc-300"
                    >
                      <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
                      {feature}
                    </li>
                  ))}
                </ul>
                <Button
                  variant={
                    tier.highlighted && !isCurrent ? "default" : "outline"
                  }
                  className="w-full"
                  disabled={
                    tier.id === "free" || isCurrent || loading === tier.id
                  }
                  onClick={() => handleUpgrade(tier.id)}
                >
                  {buttonLabel(tier)}
                </Button>
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
