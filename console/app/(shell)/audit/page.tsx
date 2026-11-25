import type { Metadata } from "next";

import { AuditView } from "./audit";

export const metadata: Metadata = { title: "Audit log" };

export default async function AuditPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { call_id: callId } = await searchParams;
  const fromLink = typeof callId === "string" ? callId : "";
  // A new call id in the address (a link from Live calls or a call's page while this page is open)
  // starts the filters afresh from it.
  return <AuditView key={fromLink} fromLink={fromLink} />;
}
