import { ImageResponse } from "next/og";

import { SITE_TAGLINE, SITE_URL } from "@/lib/site";

export const alt = "MomentumForge — Private paper-trading operations dashboard";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

/**
 * Restrained chart mark for private operations. The card intentionally avoids
 * AI-style gradients, glow effects, or claims about predictive intelligence.
 */
const MARK_SVG = `<svg width="512" height="512" viewBox="0 0 512 512" fill="none" xmlns="http://www.w3.org/2000/svg">
  <rect width="512" height="512" rx="92" fill="#151918"/>
  <path d="M96 386 L188 294 L254 344 L346 252 L416 182" stroke="#D9E3DD" stroke-width="34" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M416 182 L393 260 L337 204 Z" fill="#D9E3DD"/>
  <g fill="#6F837A">
    <rect x="122" y="280" width="22" height="126" rx="6"/>
    <rect x="236" y="224" width="22" height="160" rx="6"/>
    <rect x="350" y="160" width="22" height="166" rx="6"/>
  </g>
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
          backgroundColor: "#0F1211",
          color: "#F2F0E8",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 40 }}>
          <img
            src={MARK_DATA_URI}
            width={156}
            height={156}
            alt=""
            style={{ borderRadius: 28 }}
          />
          <div
            style={{
              fontSize: 88,
              fontWeight: 700,
              letterSpacing: -3,
              display: "flex",
              color: "#F2F0E8",
            }}
          >
            MomentumForge
          </div>
        </div>
        <div
          style={{
            marginTop: 42,
            fontSize: 34,
            color: "#A8B0AC",
            textAlign: "center",
            maxWidth: 960,
          }}
        >
          {SITE_TAGLINE}
        </div>
        <div
          style={{
            marginTop: 50,
            fontSize: 24,
            color: "#7F9389",
            letterSpacing: 1,
          }}
        >
          {host}
        </div>
      </div>
    ),
    size
  );
}
