/**
 * Canonical site configuration shared by metadata, robots, sitemap,
 * and social-card generation. Override the origin per environment with
 * NEXT_PUBLIC_SITE_URL (e.g. a custom domain) without touching code.
 */
export const SITE_URL =
  process.env.NEXT_PUBLIC_SITE_URL ?? "https://momentumforge-enhanced.vercel.app";

export const SITE_NAME = "MomentumForge";

export const SITE_TITLE = "MomentumForge — Private Paper Trading";

export const SITE_DESCRIPTION =
  "Owner-only paper-trading operations dashboard with real market scanner observations, risk controls, and evidence-based performance tracking.";

export const SITE_TAGLINE =
  "Private paper-trading operations and evidence tracking";
