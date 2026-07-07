"use client";

import { useEffect } from "react";
import * as Sentry from "@sentry/nextjs";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // No-op when NEXT_PUBLIC_SENTRY_DSN is unset (Sentry init is disabled).
    Sentry.captureException(error);
  }, [error]);

  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          background: "#0a0b14",
          color: "#fff",
          fontFamily:
            "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif",
          textAlign: "center",
          padding: "0 16px",
        }}
      >
        <h1 style={{ fontSize: 24, fontWeight: 700 }}>Something went wrong</h1>
        <p style={{ color: "#a1a1aa", fontSize: 14, maxWidth: 420 }}>
          A fatal error occurred{error.digest ? ` (ref: ${error.digest})` : ""}.
          Reloading usually fixes it.
        </p>
        <button
          onClick={reset}
          style={{
            marginTop: 24,
            padding: "10px 20px",
            borderRadius: 12,
            border: "none",
            cursor: "pointer",
            fontWeight: 600,
            color: "#fff",
            background: "linear-gradient(90deg,#06b6d4,#8b5cf6)",
          }}
        >
          Reload
        </button>
      </body>
    </html>
  );
}
