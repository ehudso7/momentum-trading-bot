/**
 * Canonical site configuration shared by metadata, robots, sitemap,
 * and social-card generation. Override the origin per environment with
 * NEXT_PUBLIC_SITE_URL (e.g. a custom domain) without touching code.
 */
export const SITE_URL =
  process.env.NEXT_PUBLIC_SITE_URL ?? "https://momentumforge-enhanced.vercel.app";

export const SITE_NAME = "MomentumForge AI";

export const SITE_TITLE = "MomentumForge AI — Momentum Trading Dashboard";

export const SITE_DESCRIPTION =
  "Real-time momentum signals & AI-powered trading dashboard. Track portfolios, scan low-float gappers, and act on regime-aware entries with strict risk management.";

export const SITE_TAGLINE =
  "Real-time momentum signals & AI-powered trading dashboard";
