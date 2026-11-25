import type { Metadata } from "next";

import { MetricsView } from "./metrics";

export const metadata: Metadata = { title: "Metrics" };

export default function MetricsPage() {
  return <MetricsView />;
}
