import type { Metadata } from "next";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { SITE_NAME } from "@/lib/site";

export const metadata: Metadata = {
  title: "Private Use Terms",
  description: `Private-use terms for ${SITE_NAME}.`,
};

const EFFECTIVE_DATE = "August 23, 2026";

export default function TermsPage() {
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
          Private Use Terms
        </h1>
        <p className="mt-2 text-sm text-zinc-500">Effective {EFFECTIVE_DATE}</p>

        <div className="mt-8 space-y-8 text-sm leading-6 text-zinc-300">
          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">Current release</h2>
            <p>
              {SITE_NAME} is currently an owner-only paper-trading operations tool.
              It is not offered for public signup, paid subscriptions, signal resale,
              or live-money customer trading in this release.
            </p>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">Not financial advice</h2>
            <p>
              Market observations, scanner rankings, paper results, and performance
              data are operational information, not investment, financial, legal, or
              tax advice and not a recommendation to buy or sell a security.
            </p>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">Trading risk</h2>
            <p>
              Momentum day trading can produce rapid and substantial losses. Paper
              results, backtests, and historical performance do not guarantee future
              results. Live-money trading remains separately gated and is not
              authorized by access to this control room.
            </p>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">Access and credentials</h2>
            <p>
              Access is restricted to the configured owner account. Brokerage,
              market-data, dashboard, API, and deployment credentials must remain
              private and must not be shared, committed to source control, or exposed
              through client-side code.
            </p>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">Public product features</h2>
            <p>
              Repository code for signup, billing, API-key provisioning, and other
              SaaS functions may exist for future use, but those features are not part
              of the current private release and must remain disabled until their
              separate launch and legal gates are satisfied.
            </p>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">Future release</h2>
            <p>
              A future public or monetized release requires a fresh launch review,
              current legal and market-data-rights review, updated customer terms and
              privacy disclosures, and explicit activation of the relevant product
              surfaces. These private-use terms are not customer terms for such a
              release.
            </p>
          </section>
        </div>
      </div>
    </div>
  );
}
