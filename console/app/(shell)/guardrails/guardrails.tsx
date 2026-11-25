"use client";

import { Ban, EyeOff, Lock, ShieldAlert, TriangleAlert } from "lucide-react";
import { useState } from "react";

import { useClinic } from "@/components/clinic";
import { FilterPills } from "@/components/filter-pills";
import { PageHeader, Section, StatCard } from "@/components/page-header";
import { BlockSkeleton, ErrorState, TableSkeleton } from "@/components/states";
import {
  Table,
  TableBody,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useGuardrails } from "@/lib/api/hooks";
import type { Guardrails } from "@/lib/api/schemas";
import { addDays, clinicYmd, ymdLabel } from "@/lib/format";
import { EMERGENCY_TRIGGER_LABEL, GUARD_RULE_LABEL, PERIODS, type Period } from "@/lib/labels";

const COLUMNS = [
  ["guard_block", "Replies stopped"],
  ["emergency_detected", "Emergencies"],
  ["tool_refused", "Tool calls refused"],
  ["identity_refused", "Identity refused"],
  ["identity_locked", "Callers locked out"],
] as const;

function total(counts: Guardrails["counts"], action: string, reason?: string | null): number {
  return counts
    .filter((row) => row.action === action && (reason === undefined || row.reason === reason))
    .reduce((sum, row) => sum + row.count, 0);
}

function windowDays(since: string, days: number, zone: string): string[] {
  const first = clinicYmd(since, zone);
  return Array.from({ length: days }, (_, index) => addDays(first, index));
}

function ByReason({
  counts,
  action,
  labels,
  noun,
}: {
  counts: Guardrails["counts"];
  action: string;
  labels: Record<string, string>;
  noun: string;
}) {
  const reasons = [
    ...new Set([
      ...Object.keys(labels),
      ...counts.filter((row) => row.action === action).map((row) => row.reason ?? ""),
    ]),
  ];
  const all = total(counts, action);
  return (
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead className="pl-5">{noun}</TableHead>
          <TableHead className="pr-5 text-right">Count</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {reasons.map((reason) => (
          <TableRow key={reason}>
            <TableHead scope="row" className="pl-5 font-normal">
              <span className="block">{labels[reason] ?? (reason === "" ? "Not recorded" : reason)}</span>
              <span className="block font-mono text-xs text-muted-foreground">
                {reason === "" ? "–" : reason}
              </span>
            </TableHead>
            <TableCell className="tabular pr-5 text-right">
              {total(counts, action, reason === "" ? null : reason)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
      <TableFooter>
        <TableRow>
          <TableHead scope="row" className="pl-5">
            All
          </TableHead>
          <TableCell className="tabular pr-5 text-right">{all}</TableCell>
        </TableRow>
      </TableFooter>
    </Table>
  );
}

export function GuardrailsView() {
  const { time_zone: zone } = useClinic();
  const [period, setPeriod] = useState<Period>("7");
  const days = Number(period);
  const summary = useGuardrails(days);
  const data = summary.data;

  const heading = data
    ? `${ymdLabel(clinicYmd(data.since, zone), { month: "long", day: "numeric" })} to today`
    : null;

  let content;
  if (summary.isPending) {
    content = (
      <>
        <div className="mb-6 grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
          {[0, 1, 2, 3].map((index) => (
            <BlockSkeleton key={index} className="h-28 w-full" label="Loading" />
          ))}
        </div>
        <TableSkeleton label="Loading the guardrail counts" />
      </>
    );
  } else if (summary.isError) {
    content = (
      <Section title="Guardrails">
        <ErrorState error={summary.error} onRetry={() => void summary.refetch()} />
      </Section>
    );
  } else {
    const counts = summary.data.counts;
    const dayList = windowDays(summary.data.since, summary.data.days, zone).reverse();
    content = (
      <div className={summary.isPlaceholderData ? "opacity-60 transition-opacity" : undefined}>
        <div className="mb-6 grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
          <StatCard
            label="Replies stopped by a rule"
            value={total(counts, "guard_block")}
            icon={<ShieldAlert className="size-4" aria-hidden />}
          />
          <StatCard
            label="Emergencies"
            value={total(counts, "emergency_detected")}
            tone={total(counts, "emergency_detected") > 0 ? "emergency" : "default"}
            icon={<TriangleAlert className="size-4" aria-hidden />}
          />
          <StatCard
            label="Tool calls refused"
            value={total(counts, "tool_refused")}
            icon={<Ban className="size-4" aria-hidden />}
          />
          <StatCard
            label="Callers locked out"
            value={total(counts, "identity_locked")}
            note={`${total(counts, "identity_refused")} identity checks refused`}
            icon={<Lock className="size-4" aria-hidden />}
          />
        </div>
        <div className="space-y-6">
          <div className="grid gap-6 lg:grid-cols-2">
            <Section
              title="Replies stopped, by rule"
              description="Each time a rule stops something the agent was about to say, the agent speaks a fixed line instead."
            >
              <ByReason counts={counts} action="guard_block" labels={GUARD_RULE_LABEL} noun="Rule" />
            </Section>
            <Section
              title="Emergencies, by trigger"
              description="What noticed the emergency. Either way the agent plays the 911 line and ends the call."
            >
              <ByReason
                counts={counts}
                action="emergency_detected"
                labels={EMERGENCY_TRIGGER_LABEL}
                noun="Trigger"
              />
            </Section>
          </div>
          <Section title="Per day" description="Newest day first. A day with nothing to count shows zeros.">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead className="pl-5">Day</TableHead>
                  {COLUMNS.map(([action, label], index) => (
                    <TableHead
                      key={action}
                      className={`text-right ${index === COLUMNS.length - 1 ? "pr-5" : ""}`}
                    >
                      {label}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {dayList.map((day) => {
                  const ofDay = counts.filter((row) => clinicYmd(row.day, zone) === day);
                  return (
                    <TableRow key={day}>
                      <TableHead scope="row" className="pl-5 font-normal tabular">
                        {ymdLabel(day, { weekday: "short", month: "short", day: "numeric" })}
                      </TableHead>
                      {COLUMNS.map(([action], index) => {
                        const value = total(ofDay, action);
                        return (
                          <TableCell
                            key={action}
                            className={`tabular text-right ${value === 0 ? "text-muted-foreground" : "font-medium"} ${index === COLUMNS.length - 1 ? "pr-5" : ""}`}
                          >
                            {value}
                          </TableCell>
                        );
                      })}
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </Section>
        </div>
      </div>
    );
  }

  return (
    <>
      <PageHeader
        title="Guardrails"
        description="How often the safety rules stepped in. This page shows rule names and counts only."
        aside={heading}
      />
      <div className="mb-6 flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div className="md:w-auto">
          <FilterPills label="Period" value={period} options={PERIODS} onChange={setPeriod} />
        </div>
        <p className="flex items-start gap-2 text-sm text-muted-foreground md:max-w-md">
          <EyeOff className="mt-0.5 size-4 shrink-0" aria-hidden />
          No blocked text is stored anywhere, so none can be shown here: only the rule that stepped in, and
          when.
        </p>
      </div>
      {content}
    </>
  );
}
