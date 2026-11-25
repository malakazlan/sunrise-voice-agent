import type { Metadata } from "next";

import { GuardrailsView } from "./guardrails";

export const metadata: Metadata = { title: "Guardrails" };

export default function GuardrailsPage() {
  return <GuardrailsView />;
}
