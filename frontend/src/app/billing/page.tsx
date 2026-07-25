import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { redirect } from "next/navigation";
import { PricingCards } from "@/components/billing/pricing-cards";
import { CheckoutConfirmation } from "@/components/billing/checkout-confirmation";
import { ManageSubscriptionButton } from "@/components/billing/manage-subscription-button";
import { isPrivateMode } from "@/lib/access-policy";

export default async function BillingPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  if (isPrivateMode()) redirect("/");
  const { checkout } = await searchParams;
  const isSuccess = checkout === "success";
  const isCancel = checkout === "cancel";

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

        {isSuccess && (
          <>
            <div className="mb-6 rounded-xl border border-emerald-500/20 bg-emerald-500/5 p-4 text-center text-sm text-emerald-400">
              Payment successful. Thank you for supporting MomentumForge.
            </div>
            <CheckoutConfirmation />
          </>
        )}
        {isCancel && (
          <div className="mb-6 rounded-xl border border-amber-500/20 bg-amber-500/5 p-4 text-center text-sm text-amber-400">
            Checkout canceled. You can try again anytime.
          </div>
        )}

        <PricingCards />

        <div className="mt-10">
          <ManageSubscriptionButton />
        </div>
      </div>
    </div>
  );
}
