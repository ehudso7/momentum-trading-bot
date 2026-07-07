import { ImageResponse } from "next/og";
import { fetchSharedSignal, type SharedSignal } from "@/lib/api";

export const alt = "MomentumForge AI — shared momentum signal";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

/**
 * Brand mark embedded as a data URI — identical treatment to
 * src/app/opengraph-image.tsx so shared cards stay on-brand without
 * any filesystem access at request time.
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

const BACKGROUND = {
  width: "100%",
  height: "100%",
  display: "flex" as const,
  flexDirection: "column" as const,
  backgroundColor: "#0a0b14",
  backgroundImage:
    "radial-gradient(circle at 82% 12%, rgba(139, 92, 246, 0.30) 0%, rgba(139, 92, 246, 0) 46%), radial-gradient(circle at 12% 88%, rgba(6, 182, 212, 0.24) 0%, rgba(6, 182, 212, 0) 44%)",
  color: "#f8fafc",
  padding: 72,
};

function directionColor(direction: SharedSignal["direction"]): string {
  if (direction === "bullish") return "#34d399";
  if (direction === "bearish") return "#f87171";
  return "#a1a1aa";
}

function DirectionArrow({ direction }: { direction: SharedSignal["direction"] }) {
  const color = directionColor(direction);
  if (direction === "neutral") {
    return (
      <svg width="96" height="96" viewBox="0 0 24 24" fill="none">
        <path
          d="M4 12h16"
          stroke={color}
          strokeWidth={2.5}
          strokeLinecap="round"
        />
      </svg>
    );
  }
  const d =
    direction === "bullish"
      ? "M6 18L18 6M9 6h9v9"
      : "M6 6l12 12M9 18h9V9";
  return (
    <svg width="96" height="96" viewBox="0 0 24 24" fill="none">
      <path
        d={d}
        stroke={color}
        strokeWidth={2.5}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function Brand() {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 24 }}>
      <img
        src={MARK_DATA_URI}
        width={72}
        height={72}
        alt=""
        style={{ borderRadius: 18 }}
      />
      <div
        style={{
          display: "flex",
          fontSize: 40,
          fontWeight: 700,
          letterSpacing: -1,
        }}
      >
        <span style={{ color: "#f8fafc" }}>MomentumForge</span>
        <span style={{ color: "#8b5cf6", marginLeft: 12 }}>AI</span>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color: string;
}) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        padding: "24px 36px",
        borderRadius: 24,
        border: "1px solid rgba(255, 255, 255, 0.08)",
        backgroundColor: "rgba(255, 255, 255, 0.03)",
      }}
    >
      <span
        style={{
          fontSize: 22,
          color: "#71717a",
          textTransform: "uppercase",
          letterSpacing: 3,
        }}
      >
        {label}
      </span>
      <span style={{ fontSize: 48, fontWeight: 700, color, marginTop: 6 }}>
        {value}
      </span>
    </div>
  );
}

export default async function OgImage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = await params;
  const signal = await fetchSharedSignal(token);

  if (!signal) {
    return new ImageResponse(
      (
        <div
          style={{
            ...BACKGROUND,
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <Brand />
          <div style={{ marginTop: 56, fontSize: 44, color: "#94a3b8" }}>
            This shared signal has expired
          </div>
          <div
            style={{
              marginTop: 28,
              fontSize: 30,
              color: "#22d3ee",
            }}
          >
            Get live signals free → momentumforge
          </div>
        </div>
      ),
      size
    );
  }

  const gap = signal.gap_pct;
  const gapValue = gap == null ? "—" : `${gap > 0 ? "+" : ""}${gap}%`;
  const gapColor =
    gap == null ? "#a1a1aa" : gap >= 0 ? "#34d399" : "#f87171";
  const dirColor = directionColor(signal.direction);
  const dirLabel =
    signal.direction.charAt(0).toUpperCase() + signal.direction.slice(1);

  return new ImageResponse(
    (
      <div style={BACKGROUND}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <Brand />
          {signal.date ? (
            <span style={{ fontSize: 28, color: "#71717a" }}>
              {signal.date}
            </span>
          ) : null}
        </div>

        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            flexGrow: 1,
            marginTop: 24,
          }}
        >
          <div style={{ display: "flex", flexDirection: "column" }}>
            <span
              style={{
                fontSize: 26,
                color: "#71717a",
                textTransform: "uppercase",
                letterSpacing: 6,
              }}
            >
              Momentum signal
            </span>
            <span
              style={{
                fontSize: 148,
                fontWeight: 700,
                letterSpacing: -4,
                color: "#f8fafc",
                lineHeight: 1.05,
              }}
            >
              {signal.symbol}
            </span>
          </div>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: 8,
            }}
          >
            <DirectionArrow direction={signal.direction} />
            <span style={{ fontSize: 40, fontWeight: 700, color: dirColor }}>
              {dirLabel}
            </span>
          </div>
        </div>

        <div style={{ display: "flex", gap: 24 }}>
          <Stat
            label="Score"
            value={`${signal.score}/100`}
            color="#22d3ee"
          />
          <Stat label="Trend gap" value={gapValue} color={gapColor} />
          <Stat label="Regime" value={signal.regime} color="#a78bfa" />
        </div>

        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginTop: 40,
          }}
        >
          <span style={{ fontSize: 30, color: "#22d3ee" }}>
            Get live signals free →
          </span>
          <span
            style={{
              fontSize: 24,
              color: "#52525b",
              textTransform: "uppercase",
              letterSpacing: 8,
            }}
          >
            momentumforge
          </span>
        </div>
      </div>
    ),
    size
  );
}
