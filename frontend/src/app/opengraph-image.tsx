import { ImageResponse } from "next/og";

import { SITE_TAGLINE, SITE_URL } from "@/lib/site";

export const alt = "MomentumForge AI — Real-time momentum signals & AI-powered trading dashboard";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

/**
 * Brand mark embedded as a data URI so the card renders identically to
 * src/app/icon.svg without any filesystem access at request time.
 */
const MARK_SVG = `<svg width="512" height="512" viewBox="0 0 512 512" fill="none" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="mf-arrow" x1="96" y1="416" x2="424" y2="180" gradientUnits="userSpaceOnUse">
      <stop stop-color="#06b6d4"/>
      <stop offset="1" stop-color="#8b5cf6"/>
    </linearGradient>
    <linearGradient id="mf-bars" x1="118" y1="440" x2="394" y2="116" gradientUnits="userSpaceOnUse">
      <stop stop-color="#0891b2"/>
      <stop offset="1" stop-color="#7c3aed"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="120" fill="#11131f"/>
  <g opacity="0.45" fill="url(#mf-bars)">
    <rect x="139" y="268" width="12" height="172" rx="6"/>
    <rect x="118" y="296" width="54" height="112" rx="14"/>
    <rect x="250" y="196" width="12" height="192" rx="6"/>
    <rect x="229" y="224" width="54" height="136" rx="14"/>
    <rect x="361" y="116" width="12" height="216" rx="6"/>
    <rect x="340" y="144" width="54" height="156" rx="14"/>
  </g>
  <path d="M100 400 L200 300 L256 356 L346 266" stroke="url(#mf-arrow)" stroke-width="40" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M424 180 L395 293 L311 209 Z" fill="url(#mf-arrow)"/>
</svg>`;

const MARK_DATA_URI = `data:image/svg+xml;base64,${Buffer.from(MARK_SVG).toString("base64")}`;

export default function OpengraphImage() {
  const host = new URL(SITE_URL).host;

  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          backgroundColor: "#0a0b14",
          backgroundImage:
            "radial-gradient(circle at 78% 18%, rgba(139, 92, 246, 0.28) 0%, rgba(139, 92, 246, 0) 46%), radial-gradient(circle at 18% 84%, rgba(6, 182, 212, 0.22) 0%, rgba(6, 182, 212, 0) 44%)",
          color: "#f8fafc",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 40 }}>
          <img
            src={MARK_DATA_URI}
            width={168}
            height={168}
            alt=""
            style={{ borderRadius: 40 }}
          />
          <div
            style={{
              fontSize: 92,
              fontWeight: 700,
              letterSpacing: -3,
              display: "flex",
            }}
          >
            <span style={{ color: "#f8fafc" }}>MomentumForge</span>
            <span style={{ color: "#8b5cf6", marginLeft: 24 }}>AI</span>
          </div>
        </div>
        <div
          style={{
            marginTop: 44,
            fontSize: 36,
            color: "#94a3b8",
            textAlign: "center",
            maxWidth: 960,
          }}
        >
          {SITE_TAGLINE}
        </div>
        <div
          style={{
            marginTop: 52,
            fontSize: 26,
            color: "#22d3ee",
            letterSpacing: 2,
          }}
        >
          {host}
        </div>
      </div>
    ),
    size
  );
}
