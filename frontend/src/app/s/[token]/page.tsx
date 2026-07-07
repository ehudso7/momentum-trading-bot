import type { Metadata } from "next";
import Link from "next/link";
import {
  ArrowDownRight,
  ArrowUpRight,
  CalendarDays,
  Gauge,
  Minus,
  TrendingUp,
  Zap,
} from "lucide-react";
import { fetchSharedSignal, type SharedSignal } from "@/lib/api";
import { SITE_NAME } from "@/lib/site";

interface SharePageProps {
  params: Promise<{ token: string }>;
}

function directionLabel(direction: SharedSignal["direction"]): string {
  if (direction === "bullish") return "Bullish";
  if (direction === "bearish") return "Bearish";
  return "Neutral";
}

export async function generateMetadata({
  params,
}: SharePageProps): Promise<Metadata> {
  const { token } = await params;
  const signal = await fetchSharedSignal(token);

  if (!signal) {
    return {
      title: "Shared signal",
      description:
        "This shared signal link has expired. Get live momentum signals free on MomentumForge AI.",
      robots: { index: false, follow: true },
    };
  }

  const title = `${signal.symbol} ${directionLabel(signal.direction)} — momentum score ${signal.score}`;
  const description = [
    `${signal.symbol} flagged ${signal.direction} by the MomentumForge scanner`,
    signal.gap_pct != null ? `${signal.gap_pct > 0 ? "+" : ""}${signal.gap_pct}% vs 50-day trend` : null,
    `${signal.regime} regime`,
    signal.date ? `on ${signal.date}` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return {
    title,
    description,
    alternates: { canonical: `/s/${token}` },
    openGraph: {
      title: `${title} | ${SITE_NAME}`,
      description,
      url: `/s/${token}`,
      type: "website",
    },
    twitter: {
      card: "summary_large_image",
      title: `${title} | ${SITE_NAME}`,
      description,
    },
  };
}

function DirectionBadge({ direction }: { direction: SharedSignal["direction"] }) {
  if (direction === "bullish") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/15 px-4 py-1.5 text-sm font-semibold text-emerald-300">
        <ArrowUpRight className="h-4 w-4" /> Bullish
      </span>
    );
  }
  if (direction === "bearish") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-red-500/15 px-4 py-1.5 text-sm font-semibold text-red-300">
        <ArrowDownRight className="h-4 w-4" /> Bearish
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-zinc-500/15 px-4 py-1.5 text-sm font-semibold text-zinc-300">
      <Minus className="h-4 w-4" /> Neutral
    </span>
  );
}

function BrandMark() {
  return (
    <div className="flex items-center gap-2.5">
      <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-cyan-500 to-blue-600 shadow-lg shadow-cyan-500/30">
        <Zap className="h-5 w-5 text-white" />
      </div>
      <span className="text-lg font-bold tracking-tight text-white">
        MomentumForge <span className="text-violet-400">AI</span>
      </span>
    </div>
  );
}

function SignupCta({ token }: { token?: string }) {
  const href = token ? `/signup?ref=${encodeURIComponent(token)}` : "/signup";
  return (
    <Link
      href={href}
      className="inline-flex h-12 w-full items-center justify-center rounded-xl bg-gradient-to-r from-cyan-500 to-violet-600 px-6 text-base font-semibold text-white shadow-lg shadow-cyan-500/25 transition-all duration-200 hover:from-cyan-400 hover:to-violet-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/50"
    >
      Get live signals free →
    </Link>
  );
}

function ExpiredCard() {
  return (
    <div className="w-full max-w-md rounded-3xl border border-white/10 bg-white/[0.03] p-8 text-center shadow-2xl backdrop-blur-xl">
      <div className="flex justify-center">
        <BrandMark />
      </div>
      <div className="mx-auto mt-8 flex h-14 w-14 items-center justify-center rounded-2xl bg-zinc-500/10">
        <TrendingUp className="h-7 w-7 text-zinc-400" />
      </div>
      <h1 className="mt-6 text-xl font-bold text-white">
        This shared signal has expired
      </h1>
      <p className="mt-2 text-sm text-zinc-400">
        Signal links stay live for a limited time — but the scanner never
        sleeps. Fresh momentum signals are generated every trading day.
      </p>
      <div className="mt-8">
        <SignupCta />
      </div>
      <p className="mt-6 text-[11px] uppercase tracking-[0.3em] text-zinc-600">
        momentumforge
      </p>
    </div>
  );
}

function SignalCard({ signal }: { signal: SharedSignal }) {
  const gap = signal.gap_pct;
  return (
    <div className="relative w-full max-w-md overflow-hidden rounded-3xl border border-white/10 bg-white/[0.03] p-8 shadow-2xl backdrop-blur-xl">
      {/* Accent glow */}
      <div
        aria-hidden
        className="pointer-events-none absolute -top-24 -right-24 h-56 w-56 rounded-full bg-violet-500/20 blur-3xl"
      />
      <div
        aria-hidden
        className="pointer-events-none absolute -bottom-24 -left-24 h-56 w-56 rounded-full bg-cyan-500/15 blur-3xl"
      />

      <div className="relative">
        <div className="flex items-center justify-between">
          <BrandMark />
          {signal.date && (
            <span className="inline-flex items-center gap-1.5 text-xs text-zinc-500">
              <CalendarDays className="h-3.5 w-3.5" />
              {signal.date}
            </span>
          )}
        </div>

        <div className="mt-10 flex items-end justify-between">
          <div>
            <p className="text-xs font-medium uppercase tracking-[0.2em] text-zinc-500">
              Momentum signal
            </p>
            <p className="mt-1 text-5xl font-bold tracking-tight text-white">
              {signal.symbol}
            </p>
          </div>
          <DirectionBadge direction={signal.direction} />
        </div>

        <div className="mt-8 grid grid-cols-3 gap-3">
          <div className="rounded-2xl border border-white/5 bg-white/[0.03] p-4">
            <p className="flex items-center gap-1 text-[11px] uppercase tracking-wider text-zinc-500">
              <Gauge className="h-3 w-3" /> Score
            </p>
            <p className="mt-1 text-2xl font-bold text-cyan-300">
              {signal.score}
              <span className="text-sm font-medium text-zinc-500">/100</span>
            </p>
          </div>
          <div className="rounded-2xl border border-white/5 bg-white/[0.03] p-4">
            <p className="text-[11px] uppercase tracking-wider text-zinc-500">
              Trend gap
            </p>
            <p
              className={`mt-1 text-2xl font-bold ${
                gap == null
                  ? "text-zinc-400"
                  : gap >= 0
                    ? "text-emerald-300"
                    : "text-red-300"
              }`}
            >
              {gap == null ? "—" : `${gap > 0 ? "+" : ""}${gap}%`}
            </p>
          </div>
          <div className="rounded-2xl border border-white/5 bg-white/[0.03] p-4">
            <p className="text-[11px] uppercase tracking-wider text-zinc-500">
              Regime
            </p>
            <p className="mt-1 text-lg font-bold capitalize text-violet-300">
              {signal.regime}
            </p>
          </div>
        </div>

        {signal.referrer_label && (
          <p className="mt-6 text-center text-sm text-zinc-400">
            Shared by{" "}
            <span className="font-medium text-zinc-200">
              {signal.referrer_label}
            </span>
          </p>
        )}

        <div className="mt-8">
          <SignupCta token={signal.token} />
        </div>
        <p className="mt-3 text-center text-xs text-zinc-500">
          Entry, stop &amp; target levels are reserved for members.
        </p>

        <p className="mt-6 text-center text-[11px] uppercase tracking-[0.3em] text-zinc-600">
          momentumforge
        </p>
        <p className="mt-4 text-center text-[10px] leading-relaxed text-zinc-600">
          Educational information, not investment advice. Trading involves
          substantial risk of loss.
        </p>
      </div>
    </div>
  );
}

export default async function SharedSignalPage({ params }: SharePageProps) {
  const { token } = await params;
  const signal = await fetchSharedSignal(token);

  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-16">
      {signal ? <SignalCard signal={signal} /> : <ExpiredCard />}
    </main>
  );
}
