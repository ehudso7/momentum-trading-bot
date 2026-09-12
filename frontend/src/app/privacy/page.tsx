import type { Metadata } from "next";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { SITE_NAME } from "@/lib/site";

export const metadata: Metadata = {
  title: "Privacy Notice",
  description: `Privacy notice for the private ${SITE_NAME} paper-trading control room.`,
};

const EFFECTIVE_DATE = "August 23, 2026";

export default function PrivacyPage() {
  return (
    <div className="min-h-screen bg-[#0f1211]">
      <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
        <Link
          href="/"
          className="mb-8 inline-flex items-center gap-2 text-sm text-zinc-400 hover:text-white"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to control room
        </Link>
        <h1 className="text-3xl font-semibold tracking-tight text-[#f2f0e8]">
          Privacy Notice
        </h1>
        <p className="mt-2 text-sm text-zinc-500">Effective {EFFECTIVE_DATE}</p>

        <div className="mt-8 space-y-8 text-sm leading-6 text-zinc-300">
          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">Current scope</h2>
            <p>
              {SITE_NAME} is currently an owner-only paper-trading operations tool.
              Public signup, paid subscriptions, and public signal distribution are
              not enabled in this release.
            </p>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">Data used</h2>
            <ul className="list-disc space-y-1 pl-5">
              <li>Owner account and authentication records handled by Supabase.</li>
              <li>Paper-trading journal, position, scanner, risk, and performance data used by the control room.</li>
              <li>Operational logs and request metadata needed for security, reliability, and troubleshooting.</li>
            </ul>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">Broker credentials</h2>
            <p>
              Brokerage and market-data credentials belong in protected server-side
              environment configuration. They are not collected through the web
              interface and must never be placed in browser storage or client-side code.
            </p>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">Service providers</h2>
            <p>
              The current private deployment uses Supabase for authentication and
              data services, Vercel for the owner web interface, Railway for backend
              services, and external market/broker providers configured by the owner.
              Stripe billing code exists in the repository but public billing is not
              enabled for this release.
            </p>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">Retention and access</h2>
            <p>
              Operational records are retained only as needed for paper-trading
              evaluation, troubleshooting, security, and evidence review. The current
              release is restricted to the configured owner account.
            </p>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">Future public release</h2>
            <p>
              If {SITE_NAME} later becomes a public product, this notice must be
              replaced or materially updated before public accounts, subscriptions,
              outreach, or customer data collection are enabled.
            </p>
          </section>
        </div>
      </div>
    </div>
  );
}
