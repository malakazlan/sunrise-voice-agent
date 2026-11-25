import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { isCallId } from "@/lib/api/schemas";

import { CallDetailView } from "./call-detail";

export const metadata: Metadata = { title: "Call" };

export default async function CallDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  // An id without the shape of a call id is not asked of the API.
  if (!isCallId(id)) {
    notFound();
  }
  return <CallDetailView callId={id} />;
}
