import type { Metadata, Viewport } from "next";
import { ServiceWorkerRegister } from "@/components/pwa/service-worker-register";
import { SiteFooter } from "@/components/site-footer";
import {
  SITE_DESCRIPTION,
  SITE_NAME,
  SITE_TITLE,
  SITE_URL,
} from "@/lib/site";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: SITE_TITLE,
    template: `%s | ${SITE_NAME}`,
  },
  description: SITE_DESCRIPTION,
  applicationName: SITE_NAME,
  keywords: [
    "momentum trading",
    "paper trading",
    "stock scanner",
    "day trading dashboard",
    "low-float gappers",
  ],
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "MomentumForge Private",
  },
  openGraph: {
    type: "website",
    url: SITE_URL,
    siteName: SITE_NAME,
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    locale: "en_US",
    // The 1200×630 card itself is emitted by app/opengraph-image.tsx
    // (file-convention metadata takes priority over config images).
  },
  twitter: {
    card: "summary_large_image",
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    // Card image is emitted by app/twitter-image.tsx.
  },
  // Icons (favicon.ico, icon.svg, apple-icon.png) are emitted by the
  // app/ file conventions, which take priority over config-based icons.
  robots: {
    index: false,
    follow: false,
    googleBot: {
      index: false,
      follow: false,
    },
  },
};

export const viewport: Viewport = {
  themeColor: "#0a0b14",
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col">
        <div className="flex-1">{children}</div>
        <SiteFooter />
        <ServiceWorkerRegister />
      </body>
    </html>
  );
}
