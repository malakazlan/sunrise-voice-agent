import { SlidersHorizontal } from "lucide-react";
import type { Metadata } from "next";

import { PageHeader, Section } from "@/components/page-header";
import { EmptyState } from "@/components/states";

export const metadata: Metadata = { title: "Config" };

export default function ConfigPage() {
  return (
    <>
      <PageHeader
        title="Config"
        description="The agent's settings: office hours, the call cap, the model and providers in use, and the switch that stops new calls."
      />
      <Section title="Coming with the next release">
        <EmptyState icon={SlidersHorizontal} title="Nothing to change here yet">
          Each change will need a confirmation and will be written to the audit log. Until then the settings
          are read from the server&apos;s configuration.
        </EmptyState>
      </Section>
    </>
  );
}
