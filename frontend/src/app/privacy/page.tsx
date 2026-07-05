import type { Metadata } from "next";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { SITE_NAME } from "@/lib/site";

export const metadata: Metadata = {
  title: "Privacy Policy",
  description: `Privacy Policy for ${SITE_NAME}.`,
};

const EFFECTIVE_DATE = "July 5, 2026";

export default function PrivacyPage() {
  return (
    <div className="min-h-screen bg-[#0a0b14]">
      <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
        <Link
          href="/"
          className="mb-8 inline-flex items-center gap-2 text-sm text-zinc-400 hover:text-white"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to dashboard
        </Link>
        <h1 className="text-3xl font-bold text-white">Privacy Policy</h1>
        <p className="mt-2 text-sm text-zinc-500">Effective {EFFECTIVE_DATE}</p>

        <div className="mt-8 space-y-8 text-sm leading-6 text-zinc-300">
          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">
              1. What we collect
            </h2>
            <ul className="list-disc space-y-1 pl-5">
              <li>
                <strong className="text-white">Account data</strong> — email
                address and authentication records, stored with our auth
                provider (Supabase).
              </li>
              <li>
                <strong className="text-white">Billing data</strong> — handled
                by Stripe. We never see or store full card numbers; we keep your
                Stripe customer ID and subscription status.
              </li>
              <li>
                <strong className="text-white">Usage data</strong> — API request
                counts, feature usage, and logs (including IP address and user
                agent) used for rate limiting, abuse prevention, and reliability.
              </li>
              <li>
                <strong className="text-white">Trading journal data</strong> —
                if you sync trades, symbols/quantities/prices/PnL you submit are
                stored against your account so the dashboard can display them.
              </li>
            </ul>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">
              2. What we do NOT collect
            </h2>
            <p>
              We never ask for or store your brokerage credentials. Your
              exchange/broker API keys stay on your own infrastructure. Your
              {" " + SITE_NAME} API key is stored in your browser
              (localStorage) and sent only to our API.
            </p>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">
              3. How we use data
            </h2>
            <p>
              To operate the service: authenticate you, deliver signals and
              dashboards, enforce plan limits, process subscriptions, prevent
              abuse, and improve reliability. We do not sell personal data and we
              do not use your data for third-party advertising.
            </p>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">
              4. Processors we rely on
            </h2>
            <p>
              Supabase (authentication and database), Stripe (payments), Vercel
              (web hosting), and Railway (API hosting). Each receives only what
              it needs to perform its function.
            </p>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">
              5. Retention and deletion
            </h2>
            <p>
              Account and journal data are kept while your account is active.
              You can request deletion of your account and associated personal
              data at any time via the support contact on your billing receipt;
              we delete within 30 days except records we must keep for tax,
              billing-dispute, or legal reasons.
            </p>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">6. Cookies</h2>
            <p>
              We use only the cookies required for authentication sessions
              (Supabase auth). No third-party advertising or cross-site tracking
              cookies.
            </p>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">
              7. Your rights
            </h2>
            <p>
              Depending on your jurisdiction (e.g. GDPR, CCPA) you may have the
              right to access, correct, export, or delete your personal data,
              and to object to processing. Contact us via the support channel to
              exercise these rights.
            </p>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-white">8. Changes</h2>
            <p>
              We will announce material changes to this policy in the app or by
              email before they take effect.
            </p>
          </section>
        </div>
      </div>
    </div>
  );
}
