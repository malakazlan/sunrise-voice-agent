"use client";

import { ArrowUpRight, CalendarDays, Inbox, MessageSquare, Phone, Pill, type LucideIcon } from "lucide-react";
import { useId, useState } from "react";

import { CallLink } from "@/components/call-link";
import { useClinic } from "@/components/clinic";
import { FilterPills } from "@/components/filter-pills";
import { MaskedText } from "@/components/masked-text";
import { PageHeader, Section } from "@/components/page-header";
import { StatusPill } from "@/components/status-badge";
import { EmptyState, ErrorState, ShowMore, TableSkeleton } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { allRows, useQueue } from "@/lib/api/hooks";
import { REQUEST_TYPES, type QueueItem, type RequestType } from "@/lib/api/schemas";
import { clinicDateTime, plural } from "@/lib/format";
import { REQUEST_TYPE_LABEL } from "@/lib/labels";

type Filter = RequestType | "all";

const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All types" },
  ...REQUEST_TYPES.map((type) => ({ value: type, label: REQUEST_TYPE_LABEL[type] })),
];

const TYPE_ICON: Record<RequestType, LucideIcon> = {
  refill: Pill,
  booking: CalendarDays,
  callback: Phone,
  doctor_message: MessageSquare,
  transfer: ArrowUpRight,
};

function Summary({ request }: { request: QueueItem }) {
  const notes = [
    ...request.callbacks.map((number) => `Call back ${number}`),
    ...(request.after_hours ? ["left after hours"] : []),
    ...(request.type === "callback" || request.type === "doctor_message" ? ["caller's name hidden"] : []),
  ];
  return (
    <div className="max-w-md min-w-56 whitespace-normal">
      <p className="leading-snug">
        <MaskedText text={request.summary} />
      </p>
      {notes.length > 0 ? <p className="mt-0.5 text-xs text-muted-foreground">{notes.join(" · ")}</p> : null}
    </div>
  );
}

// What staff can do with a request, written once for the stacked rows and the table. Both writes
// come with the next release; until then they are shown, disabled, with the note that says so.
function RequestActions({
  request,
  describedBy,
  className,
}: {
  request: QueueItem;
  describedBy: string;
  className: string;
}) {
  return (
    <div className={className}>
      <Button variant="outline" size="sm" disabled aria-describedby={describedBy}>
        Mark handled
      </Button>
      {request.type === "callback" ? (
        <Button variant="ghost" size="sm" disabled aria-describedby={describedBy}>
          Show callback details
        </Button>
      ) : null}
    </div>
  );
}

export function RequestsView() {
  const { time_zone: zone } = useClinic();
  const [filter, setFilter] = useState<Filter>("all");
  const queue = useQueue(filter === "all" ? undefined : filter);
  const rows = allRows(queue.data);
  const laterId = useId();

  let body;
  if (queue.isPending) {
    body = <TableSkeleton label="Loading the requests" />;
  } else if (queue.isError) {
    body = <ErrorState error={queue.error} onRetry={() => void queue.refetch()} />;
  } else if (rows.length === 0) {
    body = (
      <EmptyState
        icon={Inbox}
        title={
          filter === "all" ? "No requests yet" : `No ${REQUEST_TYPE_LABEL[filter].toLowerCase()} requests`
        }
      >
        Refills, bookings, callbacks, messages for a doctor and transfers appear here as the agent takes them.
      </EmptyState>
    );
  } else {
    body = (
      <>
        <ul className="divide-y sm:hidden">
          {rows.map((request) => (
            <li key={request.id} className="space-y-2 px-5 py-4">
              <div className="flex items-center justify-between gap-3">
                <StatusPill tone="neutral" icon={TYPE_ICON[request.type]}>
                  {REQUEST_TYPE_LABEL[request.type]}
                </StatusPill>
                <span className="tabular text-sm text-muted-foreground">
                  {clinicDateTime(request.created_at, zone)}
                </span>
              </div>
              <Summary request={request} />
              <p className="flex flex-wrap items-center gap-x-2 text-sm">
                {request.patient_id === null ? (
                  <span className="text-muted-foreground">Not verified</span>
                ) : (
                  <span className="font-mono text-xs">{request.patient_id}</span>
                )}
                <span className="text-muted-foreground">· call</span>
                <CallLink callId={request.call_id} />
              </p>
              <RequestActions request={request} describedBy={laterId} className="flex flex-wrap gap-2" />
            </li>
          ))}
        </ul>
        <Table className="hidden sm:table">
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="pl-5">Received</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Patient</TableHead>
              <TableHead>Summary</TableHead>
              <TableHead>Call</TableHead>
              <TableHead className="pr-5">
                <span className="sr-only">Actions</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((request) => {
              const Icon = TYPE_ICON[request.type];
              return (
                <TableRow key={request.id} className="align-top">
                  <TableCell className="tabular pt-3 pl-5 align-top">
                    {clinicDateTime(request.created_at, zone)}
                  </TableCell>
                  <TableCell className="pt-3 align-top">
                    <StatusPill tone="neutral" icon={Icon}>
                      {REQUEST_TYPE_LABEL[request.type]}
                    </StatusPill>
                  </TableCell>
                  <TableCell className="pt-3 align-top">
                    {request.patient_id === null ? (
                      <span className="text-muted-foreground">Not verified</span>
                    ) : (
                      <span className="font-mono text-xs">{request.patient_id}</span>
                    )}
                  </TableCell>
                  <TableCell className="pt-3 align-top">
                    <Summary request={request} />
                  </TableCell>
                  <TableCell className="pt-3 align-top">
                    <CallLink callId={request.call_id} />
                  </TableCell>
                  <TableCell className="pr-5 align-top">
                    <RequestActions
                      request={request}
                      describedBy={laterId}
                      className="flex flex-col items-end gap-1.5"
                    />
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
        <ShowMore
          hasMore={queue.hasNextPage}
          loading={queue.isFetchingNextPage}
          onClick={() => void queue.fetchNextPage()}
          label="Show older requests"
          shown={`Showing ${plural(rows.length, "request")}`}
        />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Requests"
        description="Everything the agent took for staff to act on: refills, bookings, callbacks, messages for a doctor and transfers."
      />
      <Section
        title="Queue"
        description="Newest first. Patients appear as ids. A callback number shows its last four digits only, and no caller name is shown."
      >
        <div className="border-b px-5 py-4">
          <FilterPills label="Type of request" value={filter} options={FILTERS} onChange={setFilter} />
        </div>
        {body}
        <p id={laterId} className="border-t bg-muted/40 px-5 py-3 text-sm text-muted-foreground">
          Coming with the next release: “Mark handled” and “Show callback details”, each an audited action.
        </p>
      </Section>
    </>
  );
}
