"use client";

import { ArrowRight, History } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { CallLink } from "@/components/call-link";
import { useClinic } from "@/components/clinic";
import { FilterPills } from "@/components/filter-pills";
import { PageHeader, Section } from "@/components/page-header";
import { CallerBadge, EndReasonBadge } from "@/components/status-badge";
import { EmptyState, ErrorState, ShowMore, TableSkeleton } from "@/components/states";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { allRows, useCalls } from "@/lib/api/hooks";
import { END_REASONS, type EndReason } from "@/lib/api/schemas";
import { clinicDateTime, duration, plural, secondsBetween, shortId } from "@/lib/format";
import { END_REASON_LABEL } from "@/lib/labels";

type Filter = EndReason | "all";

const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All" },
  ...END_REASONS.map((reason) => ({ value: reason, label: END_REASON_LABEL[reason] })),
];

export function CallHistoryView() {
  const { time_zone: zone } = useClinic();
  const [filter, setFilter] = useState<Filter>("all");
  const calls = useCalls(filter === "all" ? undefined : filter);
  const rows = allRows(calls.data);
  const showOlder = (
    <ShowMore
      hasMore={calls.hasNextPage}
      loading={calls.isFetchingNextPage}
      onClick={() => void calls.fetchNextPage()}
      label="Show older calls"
      shown={`Showing ${plural(rows.length, "call")}`}
    />
  );

  let body;
  if (calls.isPending) {
    body = <TableSkeleton label="Loading the calls" />;
  } else if (calls.isError) {
    body = <ErrorState error={calls.error} onRetry={() => void calls.refetch()} />;
  } else if (rows.length === 0) {
    body = (
      <EmptyState icon={History} title={filter === "all" ? "No calls yet" : "No calls ended this way"}>
        {filter === "all"
          ? "A call is listed here once it has ended."
          : `No call has ended as “${END_REASON_LABEL[filter]}”. Choose another filter to see the rest.`}
      </EmptyState>
    );
  } else {
    body = (
      <>
        <ul className="divide-y sm:hidden">
          {rows.map((call) => (
            <li key={call.call_id}>
              <Link href={`/calls/${call.call_id}`} className="block space-y-2 px-5 py-4 hover:bg-muted/50">
                <span className="flex items-center justify-between gap-3">
                  <span className="font-mono text-xs text-primary underline underline-offset-3">
                    {shortId(call.call_id)}…
                  </span>
                  <EndReasonBadge reason={call.end_reason} />
                </span>
                <span className="tabular block text-sm">
                  {clinicDateTime(call.started_at, zone)} ·{" "}
                  {duration(secondsBetween(call.started_at, call.ended_at))} ·{" "}
                  {plural(call.turn_count, "turn")}
                </span>
                <CallerBadge
                  verified={call.verified}
                  patientId={call.patient_id}
                  officeOpen={call.office_open}
                />
              </Link>
            </li>
          ))}
        </ul>
        <Table className="hidden sm:table">
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="pl-5">Call</TableHead>
              <TableHead>Started</TableHead>
              <TableHead>Length</TableHead>
              <TableHead>Caller</TableHead>
              <TableHead className="text-right">Turns</TableHead>
              <TableHead>How it ended</TableHead>
              <TableHead className="pr-5">
                <span className="sr-only">Open</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((call) => (
              <TableRow key={call.call_id}>
                <TableCell className="pl-5">
                  <CallLink callId={call.call_id} />
                </TableCell>
                <TableCell className="tabular">{clinicDateTime(call.started_at, zone)}</TableCell>
                <TableCell className="tabular">
                  {duration(secondsBetween(call.started_at, call.ended_at))}
                </TableCell>
                <TableCell>
                  <CallerBadge
                    verified={call.verified}
                    patientId={call.patient_id}
                    officeOpen={call.office_open}
                  />
                </TableCell>
                <TableCell className="tabular text-right">{call.turn_count}</TableCell>
                <TableCell>
                  <EndReasonBadge reason={call.end_reason} />
                </TableCell>
                <TableCell className="pr-5 text-right">
                  <Link
                    href={`/calls/${call.call_id}`}
                    className="inline-flex min-h-9 items-center gap-1 rounded-md px-2 text-sm font-medium text-primary hover:bg-secondary"
                  >
                    Open
                    <ArrowRight className="size-4" aria-hidden />
                    <span className="sr-only">call {call.call_id}</span>
                  </Link>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {showOlder}
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Call history"
        description="Every call that has ended, newest first. Open a call to read its masked transcript and see what the agent did."
      />
      <Section title="Ended calls" description="Filter by how the call ended.">
        <div className="border-b px-5 py-4">
          <FilterPills label="How the call ended" value={filter} options={FILTERS} onChange={setFilter} />
        </div>
        {body}
      </Section>
    </>
  );
}
