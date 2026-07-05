"use client";

import { useState } from "react";
import { Check, Crown, Sparkles, Zap } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError, createCheckoutSession } from "@/lib/api";
import type { SubscriptionTier } from "@/types";

const TIERS: SubscriptionTier[] = [
  {
    id: "free",
    name: "Free",
    price: 0,
    features: [
      "3 signals per day (preview)",
      "Basic scanner access",
      "Paper trading dashboard",
      "50 API requests/day",
    ],
  },
  {
    id: "pro",
    name: "Pro",
    price: 29,
    highlighted: true,
    features: [
      "Full signal details (entry/stop/target)",
      "Signal history access",
      "Unlimited scanner",
      "Priority API access",
      "PDF report exports",
    ],
  },
  {
    id: "elite",
    name: "Elite",
    price: 99,
    features: [
      "Everything in Pro",
      "Live trading signals",
      "Custom alerts",
      "API key management",
      "Dedicated support",
    ],
  },
];

export function PricingCards() {
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleUpgrade = async (tierId: SubscriptionTier["id"]) => {
    if (tierId === "free") return;
    setLoading(tierId);
    setError(null);
    try {
      const { checkout_url } = await createCheckoutSession(tierId);
      window.location.assign(checkout_url);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // Backend can't fulfill checkout for this key (e.g. already premium)
        setError(err.detail ?? err.message);
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

  const icons = { free: Zap, pro: Crown, elite: Sparkles };

  return (
    <div>
      {error && (
        <div className="mb-6 rounded-xl border border-red-500/20 bg-red-500/5 p-4 text-center text-sm text-red-300">
          {error}
        </div>
      )}
      <div className="grid gap-6 md:grid-cols-3">
        {TIERS.map((tier) => {
          const Icon = icons[tier.id];
          return (
            <Card
              key={tier.id}
              className={
                tier.highlighted
                  ? "relative border-cyan-500/30 shadow-cyan-500/10"
                  : ""
              }
            >
              {tier.highlighted && (
                <div className="absolute -top-3 left-1/2 -translate-x-1/2 rounded-full bg-gradient-to-r from-cyan-500 to-blue-600 px-3 py-1 text-xs font-medium text-white">
                  Most Popular
                </div>
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
                  variant={tier.highlighted ? "default" : "outline"}
                  className="w-full"
                  disabled={tier.id === "free" || loading === tier.id}
                  onClick={() => handleUpgrade(tier.id)}
                >
                  {tier.id === "free"
                    ? "Current Plan"
                    : loading === tier.id
                      ? "Redirecting..."
                      : `Upgrade to ${tier.name}`}
                </Button>
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}