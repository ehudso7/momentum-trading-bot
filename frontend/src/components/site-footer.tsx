import Link from "next/link";
import { SITE_NAME } from "@/lib/site";

export function SiteFooter() {
  return (
    <footer className="border-t border-white/5 bg-[#0a0b14] px-4 py-6">
      <div className="mx-auto flex max-w-5xl flex-col items-center gap-3 text-center text-xs text-zinc-500 sm:flex-row sm:justify-between sm:text-left">
        <p>
          © {new Date().getFullYear()} {SITE_NAME}. Not financial advice.
          Trading involves substantial risk of loss.
        </p>
        <nav className="flex items-center gap-4">
          <Link href="/terms" className="transition hover:text-zinc-300">
            Terms
          </Link>
          <Link href="/privacy" className="transition hover:text-zinc-300">
            Privacy
          </Link>
          <Link href="/billing" className="transition hover:text-zinc-300">
            Pricing
          </Link>
        </nav>
      </div>
    </footer>
  );
}
