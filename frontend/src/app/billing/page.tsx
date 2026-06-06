import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { PricingCards } from "@/components/billing/pricing-cards";

export default function BillingPage() {
  return (
    <div className="min-h-screen bg-[#0a0b14]">
      <div className="pointer-events-none fixed inset-0 overflow-hidden">
        <div className="absolute left-1/2 top-0 h-[400px] w-[600px] -translate-x-1/2 rounded-full bg-cyan-500/10 blur-[120px]" />
      </div>
      <div className="relative mx-auto max-w-5xl px-4 py-12 sm:px-6">
        <Link
          href="/"
          className="mb-8 inline-flex items-center gap-2 text-sm text-zinc-400 hover:text-white"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to dashboard
        </Link>
        <div className="mb-10 text-center">
          <h1 className="text-3xl font-bold text-white">Choose Your Plan</h1>
          <p className="mt-2 text-zinc-400">
            Unlock full momentum signals and premium features
          </p>
        </div>
        <PricingCards />
      </div>
    </div>
  );
}