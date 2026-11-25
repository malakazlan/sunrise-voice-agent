// Reached only when proxy.ts found a valid ADMIN_API_ORIGIN; otherwise it answers 503 itself.
export const dynamic = "force-dynamic";

export function GET() {
  return Response.json({ status: "ok" }, { headers: { "Cache-Control": "no-store" } });
}
