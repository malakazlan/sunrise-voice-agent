import type { Metadata } from "next";

import { ConfigView } from "./config";

export const metadata: Metadata = { title: "Config" };

export default function ConfigPage() {
  return <ConfigView />;
}
