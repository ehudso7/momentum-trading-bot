import Link from "next/link";
import { TrendingUp } from "lucide-react";

export default function NotFound() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-[#0a0b14] px-4 text-center">
      <TrendingUp className="mb-4 h-10 w-10 text-cyan-400" />
      <h1 className="text-4xl font-bold text-white">404</h1>
      <p className="mt-2 max-w-md text-zinc-400">
        This page gapped down and got delisted. Let&apos;s get you back to the
        action.
      </p>
      <Link
        href="/"
        className="mt-6 rounded-xl bg-gradient-to-r from-cyan-500 to-violet-500 px-5 py-2.5 text-sm font-semibold text-white transition hover:opacity-90"
      >
        Back to dashboard
      </Link>
    </div>
  );
}
