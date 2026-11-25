import type { Metadata } from "next";

import { CallHistoryView } from "./call-history";

export const metadata: Metadata = { title: "Call history" };

export default function CallHistoryPage() {
  return <CallHistoryView />;
}
