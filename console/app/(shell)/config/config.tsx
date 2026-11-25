"use client";

import { CircleCheck, Info, PhoneOff } from "lucide-react";
import { useState } from "react";

import { useClinic } from "@/components/clinic";
import { ConfirmDialog, writeFailure } from "@/components/confirm-dialog";
import { FilterPills } from "@/components/filter-pills";
import { PageHeader, Section } from "@/components/page-header";
import { BlockSkeleton, ErrorState } from "@/components/states";
import { useChangeConfig, useConfig, type ConfigRequest } from "@/lib/api/hooks";
import type { CallsValue, Config, ConfigChange, OfficeValue } from "@/lib/api/schemas";
import { clinicDateTime } from "@/lib/format";

const CALLS_OPTIONS: { value: CallsValue; label: string }[] = [
  { value: "on", label: "On" },
  { value: "off", label: "Off" },
];

const OFFICE_OPTIONS: { value: OfficeValue; label: string }[] = [
  { value: "schedule", label: "Schedule" },
  { value: "open", label: "Open" },
  { value: "closed", label: "Closed" },
];

// What each change does, in the words the confirmation uses. Only "calls off" stops anything.
const CHANGE: Record<string, { title: string; description: string; action: string; destructive: boolean }> = {
  "calls:off": {
    title: "Switch the phone assistant off?",
    description:
      "New callers will be told the assistant is unavailable and to call the front desk. Calls in progress continue.",
    action: "Switch the assistant off",
    destructive: true,
  },
  "calls:on": {
    title: "Switch the phone assistant on?",
    description: "The assistant will answer new calls again.",
    action: "Switch the assistant on",
    destructive: false,
  },
  "office:open": {
    title: "Treat the office as open?",
    description:
      "Callers who ring after this are served as in office hours, whatever the time: the assistant can verify them, give them their own details, book appointments and pass them to staff.",
    action: "Treat as open",
    destructive: false,
  },
  "office:closed": {
    title: "Treat the office as closed?",
    description:
      "Callers who ring after this are told the office is closed, whatever the time: the assistant takes a message, and gives out no patient details, books nothing and passes no one to staff.",
    action: "Treat as closed",
    destructive: false,
  },
  "office:schedule": {
    title: "Follow the office hours again?",
    description:
      "Callers who ring after this are served by the office hours: open Monday to Friday, 9 AM to 5 PM clinic time; outside them the assistant only takes messages.",
    action: "Follow the office hours",
    destructive: false,
  },
};

const VALUE_LABEL: Record<string, string> = {
  "calls:on": "on",
  "calls:off": "off",
  "office:schedule": "following the office hours",
  "office:open": "open",
  "office:closed": "closed",
};

function ChangedBy({
  changedBy,
  changedAt,
  zone,
}: {
  changedBy: string | null;
  changedAt: string | null;
  zone: string;
}) {
  if (changedBy === null || changedAt === null) {
    return <p className="text-sm text-muted-foreground">Never changed: the default.</p>;
  }
  return (
    <p className="text-sm text-muted-foreground">
      Changed by <span className="font-medium text-foreground">{changedBy}</span> at{" "}
      {clinicDateTime(changedAt, zone)}
    </p>
  );
}

function Outcome({ result, zone }: { result: { created: boolean; answer: ConfigChange }; zone: string }) {
  const value = VALUE_LABEL[`${result.answer.key}:${result.answer.value}`];
  const subject = result.answer.key === "calls" ? "The phone assistant" : "The office";
  return (
    <div
      role="status"
      className="flex gap-2 rounded-lg border border-success/30 bg-success-soft/60 px-4 py-3 text-sm"
    >
      <CircleCheck className="mt-0.5 size-4 shrink-0 text-success" aria-hidden />
      <p>
        {result.created
          ? `${subject} is now ${value}. Applies within ${result.answer.applies_within_seconds} seconds.`
          : `Unchanged: ${subject.toLowerCase()} was already ${value}.`}
        {result.answer.changed_at !== null && result.answer.changed_by !== null
          ? ` Set by ${result.answer.changed_by} at ${clinicDateTime(result.answer.changed_at, zone)}.`
          : ""}
      </p>
    </div>
  );
}

function Facts({ facts }: { facts: Config["facts"] }) {
  const rows: [string, string][] = [
    ["Environment", facts.environment],
    ["Language model", facts.model],
    ["Speech to text", facts.speech_to_text],
    ["Text to speech", facts.text_to_speech],
    ["Calls at once, at most", String(facts.max_concurrent_calls)],
    ["Longest call", `${facts.max_call_seconds} seconds`],
    ["Test clock", facts.test_clock ? "Set: the agent's clock is not the real time" : "Off"],
  ];
  return (
    <dl className="divide-y">
      {rows.map(([label, value]) => (
        <div key={label} className="grid gap-1 px-5 py-3 sm:grid-cols-[14rem_1fr]">
          <dt className="text-sm text-muted-foreground">{label}</dt>
          <dd className="font-mono text-sm break-all">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function ConfigView() {
  const { time_zone: zone } = useClinic();
  const config = useConfig();
  const change = useChangeConfig();
  const [pending, setPending] = useState<ConfigRequest | null>(null);
  const described = pending === null ? null : CHANGE[`${pending.key}:${pending.value}`];

  const confirm = () => {
    if (pending === null) return;
    const asked = pending;
    setPending(null);
    change.mutate(asked);
  };

  let content;
  if (config.isPending) {
    content = <BlockSkeleton className="h-72 w-full" label="Loading the settings" />;
  } else if (config.isError) {
    content = (
      <Section title="Settings">
        <ErrorState error={config.error} onRetry={() => void config.refetch()} />
      </Section>
    );
  } else {
    const { calls, office } = config.data.config;
    content = (
      <div className="space-y-6">
        {change.isSuccess ? <Outcome result={change.data} zone={zone} /> : null}
        {change.isError ? (
          <p
            role="alert"
            className="rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive"
          >
            {writeFailure(change.error, "The change could not be made. Nothing changed.")}
          </p>
        ) : null}
        <div className="grid gap-6 lg:grid-cols-2">
          <Section
            title="Phone assistant"
            description="Whether the assistant answers new calls. Off: callers are told it is unavailable and to call the front desk."
            flush={false}
          >
            <div className="space-y-3">
              {calls.value === "off" ? (
                <p className="inline-flex items-center gap-2 text-sm font-medium text-destructive">
                  <PhoneOff className="size-4" aria-hidden />
                  Off: new calls are refused.
                </p>
              ) : null}
              <FilterPills
                label="Phone assistant"
                value={calls.value}
                options={CALLS_OPTIONS}
                onChange={(value) => setPending({ key: "calls", value })}
              />
              <ChangedBy changedBy={calls.changed_by} changedAt={calls.changed_at} zone={zone} />
            </div>
          </Section>
          <Section
            title="Office hours"
            description="Whether callers are served as in office hours: by the schedule, or open or closed whatever the time."
            flush={false}
          >
            <div className="space-y-3">
              <FilterPills
                label="Office hours"
                value={office.value}
                options={OFFICE_OPTIONS}
                onChange={(value) => setPending({ key: "office", value })}
              />
              <ChangedBy changedBy={office.changed_by} changedAt={office.changed_at} zone={zone} />
            </div>
          </Section>
        </div>
        <p className="flex items-start gap-2 text-sm text-muted-foreground">
          <Info className="mt-0.5 size-4 shrink-0" aria-hidden />A change applies to new calls within{" "}
          {config.data.applies_within_seconds} seconds, and is recorded in the audit log with your name.
        </p>
        <Section title="How the agent runs" description="Set on the server; shown here, not changed here.">
          <Facts facts={config.data.facts} />
        </Section>
      </div>
    );
  }

  return (
    <>
      <PageHeader
        title="Config"
        description="Switch the phone assistant on or off, and say whether the office is open. Every change asks first."
      />
      {content}
      {described ? (
        <ConfirmDialog
          open={pending !== null}
          onOpenChange={(open) => {
            if (!open) setPending(null);
          }}
          title={described.title}
          description={described.description}
          action={described.action}
          destructive={described.destructive}
          onConfirm={confirm}
        />
      ) : null}
    </>
  );
}
