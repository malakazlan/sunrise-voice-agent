import { NextResponse, type NextRequest } from "next/server";

import { adminApiOrigin } from "@/lib/config";

// Every response, pages, static files and /health alike, carries the security headers. Pages get
// a fresh nonce on each request; Next.js reads it from the request's CSP header and puts it on
// the scripts and styles it renders. With no valid ADMIN_API_ORIGIN nothing is served but a 503.

const FIXED_HEADERS: Record<string, string> = {
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
};

function contentSecurityPolicy(nonce: string, apiOrigin: string | null): string {
  const development = process.env.NODE_ENV === "development";
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${development ? " 'unsafe-eval'" : ""}`,
    `style-src 'self' 'nonce-${nonce}'`,
    `connect-src 'self'${apiOrigin === null ? "" : ` ${apiOrigin}`}`,
    "img-src 'self' data:",
    "font-src 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
  ].join("; ");
}

export function proxy(request: NextRequest) {
  const nonce = Buffer.from(crypto.getRandomValues(new Uint8Array(16))).toString("base64");
  const apiOrigin = adminApiOrigin();
  const policy = contentSecurityPolicy(nonce, apiOrigin);

  let response: NextResponse;
  if (apiOrigin === null) {
    response = new NextResponse("The console is not configured.", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
    });
  } else {
    const requestHeaders = new Headers(request.headers);
    requestHeaders.set("Content-Security-Policy", policy);
    response = NextResponse.next({ request: { headers: requestHeaders } });
  }
  response.headers.set("Content-Security-Policy", policy);
  for (const [name, value] of Object.entries(FIXED_HEADERS)) {
    response.headers.set(name, value);
  }
  return response;
}
