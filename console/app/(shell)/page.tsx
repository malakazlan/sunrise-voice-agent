import type { Metadata } from "next";

import { LiveCallsView } from "./live-calls";

export const metadata: Metadata = { title: "Live calls" };

export default function LiveCallsPage() {
  return <LiveCallsView />;
}
