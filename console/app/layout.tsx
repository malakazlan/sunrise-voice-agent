import type { Metadata } from "next";
import localFont from "next/font/local";
import { headers } from "next/headers";
import { connection } from "next/server";

import { Providers } from "@/app/providers";
import { adminApiOrigin } from "@/lib/config";

import "./globals.css";

const plexSans = localFont({
  src: "./fonts/ibm-plex-sans-latin-wght.woff2",
  weight: "100 700",
  variable: "--font-plex-sans",
  display: "swap",
});

const plexMono = localFont({
  src: [
    { path: "./fonts/ibm-plex-mono-latin-400.woff2", weight: "400" },
    { path: "./fonts/ibm-plex-mono-latin-500.woff2", weight: "500" },
  ],
  variable: "--font-plex-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: { default: "Sunrise console", template: "%s · Sunrise console" },
  description: "The Sunrise Family Clinic voice agent's admin console.",
  robots: { index: false, follow: false },
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Every page is rendered per request: the CSP nonce is new each time, and the admin API's
  // origin is read at run time, never built in.
  await connection();
  const apiOrigin = adminApiOrigin();
  if (apiOrigin === null) {
    throw new Error("ADMIN_API_ORIGIN is not a valid origin");
  }
  // This request's nonce, from the policy proxy.ts put on it, for the styles the UI kit adds
  // while it runs.
  const nonce = /'nonce-([^']+)'/.exec((await headers()).get("content-security-policy") ?? "")?.[1];
  if (nonce === undefined) {
    throw new Error("the request carries no CSP nonce");
  }
  return (
    <html lang="en" className={`${plexSans.variable} ${plexMono.variable}`}>
      <body className="min-h-dvh">
        <Providers apiOrigin={apiOrigin} nonce={nonce}>
          {children}
        </Providers>
      </body>
    </html>
  );
}
